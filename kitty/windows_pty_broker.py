# License: GPL v3
"""Opt-in native ConPTY ownership outside the graphical terminal process.

The renderer starts one normal-user helper per persistent session before making
its per-pane job. Only the byte relay belongs to that job. The helper owns the
existing engine ConPTY implementation, its child job and a bounded replay journal.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time

from . import windows_pty_protocol as wire

ROOT_KEY = 'KILIX_NATIVE_SESSION_ROOT'
ID_KEY = 'KILIX_NATIVE_SESSION_ID'
TOKEN_KEY = 'KILIX_NATIVE_SESSION_TOKEN'
CONTROL_KEYS = ('KITTY_LISTEN_ON', 'KITTY_RC_PASSWORD', 'KITTY_PUBLIC_KEY', 'KITTY_WINDOW_ID', 'KITTY_PID')
REQUEST_KEYS = ('KILIX_NATIVE_PERSIST', 'KILIX_NATIVE_SESSION_NAME', 'KILIX_NATIVE_ATTACH')
_servers: dict[str, subprocess.Popen] = {}


def session_name(value: str) -> str:
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,31}', value):
        raise ValueError('Session names need 1-32 letters/digits/_/-, starting with a letter or digit')
    return value


def runtime(env=None) -> Path:
    from .windows_pty_ipc import private_directory
    env = os.environ if env is None else env
    default = Path(env['LOCALAPPDATA']) / 'Kilix' / 'native-sessions'
    return private_directory(Path(env.get(ROOT_KEY, str(default))))


def read_record(path: Path) -> dict:
    from .windows_pty_ipc import GetFileAttributes
    try:
        if GetFileAttributes(str(path)) & 0x400:
            return {}
        with path.open('rb') as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            return {}
        result = json.loads(data)
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def write_record(path: Path, data: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def helper_command(*args: str) -> list[str]:
    from .constants import kitten_host_exe
    return [kitten_host_exe(), '+runpy',
            'from kitty.windows_pty_broker import main; raise SystemExit(main())', *args]


def request(root: Path, name: str, kind: int) -> bytes:
    from .windows_pty_ipc import close_connection, connect
    connection = connect(root, session_name(name))
    try:
        wire.send(connection, kind)
        if not connection.poll(3):
            raise TimeoutError('Session broker did not respond')
        response, payload = wire.receive(connection)
        if response == wire.ERROR:
            raise ValueError(payload.decode('utf-8', 'replace'))
        expected = wire.STATUS_REPLY if kind == wire.STATUS else wire.ACK
        if response != expected:
            raise ValueError('Unexpected session broker reply')
        return payload
    finally:
        close_connection(connection)


def status(root: Path, name: str) -> dict:
    return wire.status_dict(request(root, name, wire.STATUS))


def sessions(root: Path) -> list[dict]:
    answer = []
    for path in sorted(root.glob('*.session.json')):
        name = path.name[:-len('.session.json')]
        try:
            item = status(root, name)
            if item['id'] == name:
                answer.append(item)
        except (OSError, ValueError, EOFError):
            continue
    return answer


def refresh_environment() -> None:
    """The native command runner calls this before using inherited control env."""
    name = os.environ.get(ID_KEY)
    if not name:
        return
    context = {}
    try:
        from .windows_pty_ipc import Process
        root = runtime()
        record = read_record(root / (session_name(name) + '.context.json'))
        token = os.environ.get(TOKEN_KEY, '')
        if re.fullmatch('[0-9a-f]{32}', token) and record.get('token') == token:
            process = Process(record['pid'])
            try:
                if process.started == record['started'] and process.poll() is None:
                    context = record.get('env', {})
            finally:
                process.close()
    except (OSError, ValueError, KeyError, TypeError):
        pass
    for key in CONTROL_KEYS:
        os.environ.pop(key, None)
        if isinstance(context, dict) and isinstance(context.get(key), str):
            os.environ[key] = context[key]


class ConPTY:
    def __init__(self, spec: dict):
        from . import fast_data_types as native
        from .windows_pty_ipc import Process
        self.native = native
        self.lock = threading.Lock()
        self.closed = False
        self.read_fd, self.write_fd, self.pty = native.open_pty(spec['columns'], spec['rows'])
        try:
            self.pid = native.spawn(self.pty, spec['exe'], spec['cwd'], tuple(spec['argv']),
                                    tuple(f'{k}={v}' for k, v in spec['env'].items()))
            self.process = Process(self.pid)
        except BaseException:
            native.close_pty(self.pty)
            raise

    def read(self):
        import ctypes as C
        from ctypes import wintypes as W
        import msvcrt
        from .windows_pty_ipc import api, kernel
        peek = api(kernel, 'PeekNamedPipe', W.BOOL, W.HANDLE, W.LPVOID, W.DWORD,
                   W.LPVOID, C.POINTER(W.DWORD), W.LPVOID)
        available = W.DWORD()
        if not peek(msvcrt.get_osfhandle(self.read_fd), None, 0, None, C.byref(available), None):
            return b''
        return os.read(self.read_fd, min(32768, available.value)) if available.value else None

    def input(self, kind: int, payload: bytes):
        import ctypes as C
        from ctypes import wintypes as W
        import msvcrt
        from .windows_pty_ipc import CloseHandle, GetCurrentProcess, api, checked, kernel
        if kind == wire.RESIZE:
            rows, cols, _, _ = wire.SIZE.unpack(payload)
            if not 0 < rows <= 32767 or not 0 < cols <= 32767:
                raise ValueError('Invalid terminal dimensions')
            with self.lock:
                if not self.closed:
                    self.native.resize_pty(self.pty, cols, rows)
        else:
            duplicate = api(kernel, 'DuplicateHandle', W.BOOL, W.HANDLE, W.HANDLE,
                            W.HANDLE, C.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD)
            write = api(kernel, 'WriteFile', W.BOOL, W.HANDLE, W.LPVOID, W.DWORD,
                        C.POINTER(W.DWORD), W.LPVOID)
            handle = W.HANDLE()
            with self.lock:
                if self.closed:
                    return
                checked(duplicate(GetCurrentProcess(), msvcrt.get_osfhandle(self.write_fd),
                                  GetCurrentProcess(), C.byref(handle), 0, False, 2))
            try:
                # Avoid the CRT fd lock: a blocked os.write would prevent
                # close_pty's _close before it can shut down ConPTY. The worker
                # owns this duplicate until WriteFile completes/cancels; closing
                # ConPTY breaks the pipe without reusing an in-flight handle.
                while payload:
                    count = W.DWORD()
                    buffer = C.create_string_buffer(payload)
                    checked(write(handle, buffer, len(payload), C.byref(count), None))
                    if not count.value:
                        raise OSError('ConPTY input closed')
                    payload = payload[count.value:]
            finally:
                CloseHandle(handle)

    def poll(self):
        return self.process.poll()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        self.native.close_pty(self.pty)
        self.process.close()


class Attachment:
    def __init__(self, connection, native_exit=False):
        self.connection = connection
        self.native_exit = native_exit
        self.output: queue.Queue[tuple[int, bytes]] = queue.Queue(maxsize=64)
        self.closed = threading.Event()

    def close(self):
        if not self.closed.is_set():
            from .windows_pty_ipc import close_connection
            self.closed.set()
            close_connection(self.connection)


class Session:
    def __init__(self, spec: dict, process):
        self.spec, self.process = spec, process
        self.lock = threading.RLock()
        self.journal = wire.Journal()
        self.attached: Attachment | None = None
        self.stopping = threading.Event()
        self.input: queue.Queue[tuple[int, bytes]] = queue.Queue(maxsize=32)
        self.started = int(time.time() * 1000)
        self.rows, self.columns = spec['rows'], spec['columns']

    def information(self) -> dict:
        with self.lock:
            return dict(id=self.spec['id'], broker_pid=os.getpid(), child_pid=self.process.pid,
                        started_millis=self.started, journal_bytes=self.journal.size,
                        journal_epoch=self.journal.epoch, attached=self.attached is not None,
                        replay_complete=self.journal.complete, rows=self.rows, columns=self.columns,
                        cwd=self.spec['cwd'], command=subprocess.list2cmdline(self.spec['argv']))

    def detach(self, attachment):
        with self.lock:
            if self.attached is attachment:
                self.attached = None
            attachment.close()

    def broadcast(self, kind: int, payload: bytes):
        with self.lock:
            if kind == wire.OUTPUT:
                self.journal.append(payload)
            attachment = self.attached
            if attachment:
                try:
                    attachment.output.put_nowait((kind, payload))
                except queue.Full:
                    # A stalled renderer must not stop detached work or the control plane.
                    self.detach(attachment)

    def sender(self, attachment, replay):
        try:
            wire.send(attachment.connection, wire.RESET, b'\x1bc')
            for chunk in replay:
                wire.send(attachment.connection, wire.OUTPUT, chunk)
            wire.send(attachment.connection, wire.REPLAY_DONE)
            while not attachment.closed.is_set():
                try:
                    kind, payload = attachment.output.get(timeout=.1)
                except queue.Empty:
                    continue
                if kind == wire.NATIVE_EXIT and not attachment.native_exit:
                    kind = wire.EXIT
                    code = wire.EXIT_STATUS.unpack(payload)[0]
                    payload = wire.EXIT_STATUS.pack((code & 255) << 8)
                wire.send(attachment.connection, kind, payload)
                if kind in (wire.EXIT, wire.NATIVE_EXIT):
                    return
        except (OSError, EOFError, ValueError, TypeError):
            pass
        finally:
            self.detach(attachment)

    def input_worker(self):
        while not self.stopping.is_set():
            try:
                kind, payload = self.input.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                self.process.input(kind, payload)
                if kind == wire.RESIZE:
                    self.rows, self.columns, _, _ = wire.SIZE.unpack(payload)
            except (OSError, ValueError):
                return

    def serve_client(self, connection):
        from .windows_pty_ipc import check_peer, close_connection
        attachment = None
        try:
            check_peer(connection, server=False)
            if not connection.poll(3):
                return
            kind, payload = wire.receive(connection)
            if kind == wire.STATUS and not payload:
                wire.send(connection, wire.STATUS_REPLY, wire.status_bytes(self.information()))
                return
            if kind == wire.TERMINATE and not payload:
                wire.send(connection, wire.ACK)
                self.stopping.set()
                return
            native_exit = kind == wire.NATIVE_ATTACH
            if native_exit:
                if len(payload) != 12 or wire.NATIVE_VERSION.unpack(payload[8:])[0] != 1:
                    raise ValueError('Unsupported native attach version')
                payload = payload[:8]
            if (kind not in (wire.ATTACH, wire.NATIVE_ATTACH) or len(payload) != wire.SIZE.size):
                raise ValueError('Expected a protocol-v1 attach or control request')
            rows, cols, _, _ = wire.SIZE.unpack(payload)
            if not 0 < rows <= 32767 or not 0 < cols <= 32767:
                raise ValueError('Invalid terminal dimensions')
            with self.lock:
                if self.attached is not None:
                    raise ValueError('Session is already attached')
                attachment = Attachment(connection, native_exit=native_exit)
                self.attached = attachment
                replay = tuple(self.journal.chunks)
                self.input.put_nowait((wire.RESIZE, payload))
            threading.Thread(target=self.sender, args=(attachment, replay), daemon=True).start()
            while not attachment.closed.is_set() and not self.stopping.is_set():
                if not connection.poll(.1):
                    continue
                kind, payload = wire.receive(connection)
                if kind == wire.DETACH and not payload:
                    break
                if kind == wire.INPUT and len(payload) <= 32768:
                    self.input.put_nowait((kind, payload))
                elif kind == wire.RESIZE and len(payload) == wire.SIZE.size:
                    rows, cols, _, _ = wire.SIZE.unpack(payload)
                    if not 0 < rows <= 32767 or not 0 < cols <= 32767:
                        break
                    self.input.put_nowait((kind, payload))
                else:
                    break
        except ValueError as error:
            if attachment is None:
                try:
                    wire.send(connection, wire.ERROR, str(error).encode())
                except (OSError, EOFError):
                    pass
        except (OSError, EOFError, queue.Full, TypeError):
            pass
        finally:
            if attachment:
                self.detach(attachment)
            else:
                close_connection(connection)

    def run(self):
        threading.Thread(target=self.input_worker, daemon=True).start()
        exited_at = None
        code = 0
        try:
            while not self.stopping.is_set():
                data = self.process.read()
                if data:
                    self.broadcast(wire.OUTPUT, data)
                status_code = self.process.poll()
                if status_code is not None:
                    code = status_code
                    exited_at = exited_at or time.monotonic()
                    if time.monotonic() - exited_at >= .2 and not data:
                        break
                if not data:
                    time.sleep(.005)
            self.broadcast(wire.NATIVE_EXIT, wire.EXIT_STATUS.pack(code & 0xffffffff))
            deadline = time.monotonic() + 2
            while self.attached and time.monotonic() < deadline:
                time.sleep(.01)
        finally:
            self.stopping.set()
            with self.lock:
                if self.attached:
                    self.detach(self.attached)
            self.process.close()


def serve() -> int:
    from .windows_pty_ipc import listener
    # Only the spawning renderer writes this inherited pipe; no launch env is
    # placed on a command line or in the registry directory.
    spec = json.loads(sys.stdin.buffer.read(1024 * 1024 + 1))
    name = session_name(spec['id'])
    root = runtime()
    server = listener(root, name)  # First pipe instance wins before any child is spawned.
    process = None
    record_path = root / (name + '.session.json')
    try:
        process = ConPTY(spec)
        session = Session(spec, process)
        write_record(record_path, {'id': name, 'pid': os.getpid(), 'token': spec['token'], 'argv': spec['argv']})
        slots = threading.BoundedSemaphore(16)

        def accept():
            from .windows_pty_ipc import close_connection
            while not session.stopping.is_set():
                try:
                    connection = server.accept()
                except (OSError, ValueError):
                    return
                if not slots.acquire(blocking=False):
                    close_connection(connection)
                    continue
                def handle(connection=connection):
                    try:
                        session.serve_client(connection)
                    finally:
                        slots.release()
                threading.Thread(target=handle, daemon=True).start()

        threading.Thread(target=accept, daemon=True).start()
        print(json.dumps({'ready': True}), flush=True)
        sys.stdout.close()
        session.run()
        process = None
        return 0
    finally:
        if process is not None:
            process.close()
        record_path.unlink(missing_ok=True)
        server.close()
        server.security.close()


def start_session(name: str, argv: list[str], exe: str, cwd: str, env: dict,
                  rows: int = 24, columns: int = 80) -> dict:
    name = session_name(name)
    root = runtime(env)
    token = secrets.token_hex(16)
    child_env = {k: v for k, v in env.items() if k not in REQUEST_KEYS}
    child_env.update({ID_KEY: name, TOKEN_KEY: token, ROOT_KEY: str(root)})
    spec = dict(id=name, token=token, argv=argv, exe=exe, cwd=cwd, env=child_env, rows=rows, columns=columns)
    raw = json.dumps(spec).encode()
    if len(raw) > 1024 * 1024:
        raise ValueError('Native session startup environment is too large')
    server_env = dict(os.environ, **{ROOT_KEY: str(root)})
    process = subprocess.Popen(helper_command('_serve'), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=server_env, close_fds=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    ready: queue.Queue = queue.Queue(maxsize=1)

    def bootstrap():
        try:
            process.stdin.write(raw)
            process.stdin.close()
            ready.put(json.loads(process.stdout.readline(65536)))
        except (OSError, ValueError) as error:
            ready.put(error)
        finally:
            process.stdout.close()

    threading.Thread(target=bootstrap, daemon=True).start()
    try:
        answer = ready.get(timeout=15)
        if not isinstance(answer, dict) or not answer.get('ready'):
            raise ValueError('Cannot start the native session broker (name may already be in use)')
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    for old_name, old in tuple(_servers.items()):
        if old.poll() is not None:
            del _servers[old_name]
    _servers[name] = process
    return {'id': name, 'token': token, 'root': str(root), 'argv': argv}


def prepare_child(argv: list[str], exe: str, cwd: str, env: dict,
                  rows: int, columns: int, attach: str = '', name: str = '') -> tuple[list[str], dict, str, list[str]]:
    root = runtime(env)
    if attach:
        name = session_name(attach)
        existing = status(root, name)
        if existing['attached']:
            raise ValueError('Native session is already attached; detach it first')
        record = read_record(root / (name + '.session.json'))
        token = record.get('token', '')
        if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token):
            raise ValueError('Native session has no valid recovery identity')
    else:
        name = name or 'native-' + secrets.token_hex(6)
        record = start_session(name, argv, exe, cwd, env, rows, columns)
        token = record['token']
    relay_env = {k: v for k, v in env.items() if k not in REQUEST_KEYS}
    relay_env.update({ROOT_KEY: str(root), ID_KEY: name, TOKEN_KEY: token})
    original = record.get('argv', [])
    if not isinstance(original, list) or not all(isinstance(arg, str) for arg in original):
        original = []
    return helper_command('_relay', name, str(rows), str(columns)), relay_env, name, original


def relay(name: str, rows: int, columns: int) -> int:
    import msvcrt
    from .windows_pty_ipc import Process, attachment_lock, close_connection, connect
    root = runtime()
    name = session_name(name)
    msvcrt.setmode(0, os.O_BINARY)
    msvcrt.setmode(1, os.O_BINARY)
    with attachment_lock(root, name):
        if status(root, name)['attached']:
            raise ValueError('Native session is already attached')
        connection = connect(root, name)
        process = Process(os.getpid())
        context_path = root / (name + '.context.json')
        identity = {'pid': os.getpid(), 'started': process.started,
                    'token': os.environ[TOKEN_KEY],
                    'env': {key: os.environ[key] for key in CONTROL_KEYS if key in os.environ}}
        process.close()
        stopped = threading.Event()
        try:
            write_record(context_path, identity)
            wire.send(connection, wire.NATIVE_ATTACH,
                      wire.SIZE.pack(rows, columns, 0, 0) + wire.NATIVE_VERSION.pack(1))

            def input_worker():
                import ctypes as C
                from ctypes import wintypes as W
                from .windows_pty_ipc import api, kernel
                peek = api(kernel, 'PeekNamedPipe', W.BOOL, W.HANDLE, W.LPVOID, W.DWORD,
                           W.LPVOID, C.POINTER(W.DWORD), W.LPVOID)
                decoder = wire.ResizeDecoder()
                try:
                    while not stopped.is_set():
                        available = W.DWORD()
                        if not peek(msvcrt.get_osfhandle(0), None, 0, None, C.byref(available), None):
                            return
                        if not available.value:
                            time.sleep(.02)
                            if decoder.pending:
                                wire.send(connection, wire.INPUT, decoder.flush())
                            continue
                        data = os.read(0, min(available.value, 32768))
                        if not data:
                            return
                        for kind, payload in decoder.feed(data):
                            wire.send(connection, kind, payload)
                except (OSError, EOFError, ValueError, TypeError):
                    pass
                finally:
                    stopped.set()

            threading.Thread(target=input_worker, daemon=True).start()
            while not stopped.is_set():
                if not connection.poll(.1):
                    continue
                kind, payload = wire.receive(connection)
                if kind in (wire.OUTPUT, wire.RESET):
                    while payload:
                        payload = payload[os.write(1, payload):]
                elif kind == wire.NATIVE_EXIT:
                    return wire.EXIT_STATUS.unpack(payload)[0]
                elif kind == wire.ERROR:
                    raise ValueError(payload.decode('utf-8', 'replace'))
                elif kind != wire.REPLAY_DONE:
                    raise ValueError('Unexpected native broker event')
            return 0
        finally:
            stopped.set()
            close_connection(connection)
            context_path.unlink(missing_ok=True)


def detach(root: Path, name: str):
    from .windows_pty_ipc import Process, TerminateProcess, checked
    record = read_record(root / (session_name(name) + '.context.json'))
    try:
        process = Process(record['pid'], terminate=True)
    except (KeyError, OSError):
        if not status(root, name)['attached']:
            return
        raise ValueError('No managed relay for this attachment') from None
    try:
        if process.started != record.get('started'):
            raise ValueError('The attachment has ended; refresh the session list')
        if process.poll() is None:
            checked(TerminateProcess(process.handle, 0))
    finally:
        process.close()


def main(args=None, remote=None) -> int:
    args = sys.argv[1:] if args is None else args
    if args[:1] == ['_serve']:
        return serve()
    if args[:1] == ['_relay']:
        code = relay(session_name(args[1]), int(args[2]), int(args[3]))
        # CPython's SystemExit integer conversion is signed on Windows. Preserve
        # NTSTATUS values as well as ordinary exit codes such as MSI's 3010.
        from .windows_pty_ipc import ExitProcess
        ExitProcess(code)
        return code
    parser = argparse.ArgumentParser(prog='kilix session', description=(
        'Live native Windows sessions. New/attach/restore open tabs in the current Kilix instance. '
        'Closing a persistent pane detaches it; kill deliberately terminates its processes.'))
    commands = parser.add_subparsers(dest='action')
    new = commands.add_parser('new')
    new.add_argument('name', type=session_name)
    new.add_argument('command', nargs=argparse.REMAINDER)
    listing = commands.add_parser('list')
    listing.add_argument('--json', action='store_true')
    for action in ('attach', 'kill'):
        commands.add_parser(action).add_argument('name', type=session_name)
    commands.add_parser('detach').add_argument('name', nargs='?', type=session_name)
    commands.add_parser('restore')
    options = parser.parse_args(['--help'] if args == ['help'] else args or ['list'])
    refresh_environment()
    root = runtime()
    if options.action == 'list':
        data = sessions(root)
        if options.json:
            print(json.dumps(data))
        else:
            for item in data:
                print(f"{item['id']}\t{'attached' if item['attached'] else 'detached'}\tpid={item['child_pid']}")
        return 0
    if options.action == 'kill':
        request(root, options.name, wire.TERMINATE)
        return 0
    if options.action == 'detach':
        name = options.name or os.environ.get(ID_KEY)
        if not name:
            raise ValueError('Use session detach NAME outside a persistent shell')
        detach(root, name)
        return 0
    if remote is None:
        raise ValueError('Open a native Kilix pane and use kilix session to create or attach sessions')
    if options.action == 'new':
        command = options.command
        if command[:1] == ['--']:
            command = command[1:]
        return remote(['launch', '--type=tab', '--tab-title=' + options.name,
                       '--env=KILIX_NATIVE_PERSIST=1', '--env=KILIX_NATIVE_SESSION_NAME=' + options.name,
                       '--', *(command or ['powershell.exe', '-NoLogo'])])
    names = ([options.name] if options.action == 'attach' else
             [item['id'] for item in sessions(root) if not item['attached']])
    for name in names:
        item = status(root, name)
        if item['attached']:
            raise ValueError(f'Session {name} is already attached')
        if not item['replay_complete']:
            print(f'kilix: {name} replay was truncated; the live process is unchanged.', file=sys.stderr)
        result = remote(['launch', '--type=tab', '--tab-title=' + name,
                         '--env=KILIX_NATIVE_ATTACH=' + name, '--env=KILIX_NATIVE_PERSIST=0',
                         '--', 'powershell.exe', '-NoLogo'])
        if result:
            return result
    return 0

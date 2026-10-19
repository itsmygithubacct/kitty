# License: GPL v3
"""Native owned-process recovery; kitty-console.exe +launch test.py --module windows_recovery."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from . import BaseTest, is_windows


def eventually(function, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = function()
        if result:
            return result
        time.sleep(.03)
    raise AssertionError('Timed out waiting for native recovery condition')


@unittest.skipUnless(is_windows, 'Requires the bundled Windows ConPTY runtime')
class WindowsRecoveryTest(BaseTest):
    def setUp(self):
        from kitty import windows_pty_broker as broker
        self.broker = broker
        self.temporary = tempfile.TemporaryDirectory(prefix='kilix-native-recovery-')
        self.root = Path(self.temporary.name) / 'sessions'
        self.patch = patch.dict(os.environ, {broker.ROOT_KEY: str(self.root)})
        self.patch.start()
        self.clients = []
        self.input_locks = {}
        self.addCleanup(self.cleanup)
        self.cmd = os.path.join(os.environ['SystemRoot'], 'System32', 'cmd.exe')
        self.powershell = os.path.join(os.environ['SystemRoot'], 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')

    def cleanup(self):
        for process, output in self.clients:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
        if self.root.exists():
            for item in self.broker.sessions(self.root):
                self.broker.request(self.root, item['id'], self.broker.wire.TERMINATE)
            eventually(lambda: not self.broker.sessions(self.root))
        self.patch.stop()
        self.temporary.cleanup()

    def start(self, name, argv, ignore_ctrl_c=False):
        # The creating process exits before any attachment: the broker must be
        # independent of both the creator and every subsequent renderer/relay.
        from kitty.constants import kitten_host_exe
        code = ('import json,os,sys; from kitty.windows_pty_broker import start_session; '
                'from ctypes import wintypes as W; from kitty.windows_pty_ipc import api,checked,kernel; '
                'reset=api(kernel,"SetConsoleCtrlHandler",W.BOOL,W.LPVOID,W.BOOL); '
                'checked(reset(None,True)) if sys.argv[3]=="ignore" else None; '
                'a=json.loads(sys.argv[2]); print(json.dumps(start_session('
                'sys.argv[1], a, a[0], os.getcwd(), os.environ.copy())))')
        result = subprocess.run([kitten_host_exe(), '+runpy', code, name, json.dumps(argv),
                                 'ignore' if ignore_ctrl_c else 'normal'],
                                capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def client(self, record, pane='101'):
        env = dict(os.environ, **{self.broker.ID_KEY: record['id'], self.broker.TOKEN_KEY: record['token'],
                                  'KITTY_WINDOW_ID': pane, 'KITTY_RC_PASSWORD': 'test-context-' + pane,
                                  'KITTY_LISTEN_ON': 'tcp:127.0.0.1:12345', 'KITTY_PUBLIC_KEY': 'test-key'})
        process = subprocess.Popen(self.broker.helper_command('_relay', record['id'], '24', '80'), env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        output = bytearray()
        self.input_locks[process.pid] = threading.Lock()
        self.clients.append((process, output))
        def reader():
            pending = b''
            try:
                while data := process.stdout.read1(65536):
                    output.extend(data)
                    # ConPTY/PSReadLine can wait for a cursor-position report
                    # after resize. A raw pipe reader must act like a terminal
                    # here, including when the query spans separate reads.
                    pending += data
                    for _ in range(pending.count(b'\x1b[6n')):
                        self.send((process, output), b'\x1b[1;1R')
                    pending = pending[-3:]
            except (OSError, ValueError):
                pass
        threading.Thread(target=reader, daemon=True).start()
        eventually(lambda: self.broker.status(self.root, record['id'])['attached'])
        return process, output

    def send(self, client, data):
        with self.input_locks[client[0].pid]:
            client[0].stdin.write(data)
            client[0].stdin.flush()

    def test_native_powershell_survives_creator_and_relay_loss_with_same_state(self):
        from kitty.windows_pty_ipc import Process
        record = self.start('powershell', [self.powershell, '-NoLogo', '-NoProfile', '-NoExit',
                                          '-Command', "function prompt { 'KILIX_TEST_PROMPT> ' }"], ignore_ctrl_c=True)
        original = self.broker.status(self.root, 'powershell')['child_pid']
        handle = Process(original)
        self.addCleanup(handle.close)
        first = self.client(record)
        eventually(lambda: b'KILIX_TEST_PROMPT>' in first[1])
        self.send(first, b"$state='retained'; $n=0; while ($true) { $n++; Write-Output ('TICK:'+$n); Start-Sleep -Milliseconds 100 }\r")
        eventually(lambda: b'TICK:3' in first[1])
        first[0].kill()
        first[0].wait(timeout=5)
        eventually(lambda: not self.broker.status(self.root, 'powershell')['attached'])
        before = self.broker.status(self.root, 'powershell')['journal_bytes']
        eventually(lambda: self.broker.status(self.root, 'powershell')['journal_bytes'] > before + 20)
        self.assertIsNone(handle.poll())
        second = self.client(record, '202')
        eventually(lambda: b'TICK:3' in second[1])
        self.send(second, b'\x1b[8;37;111t')
        eventually(lambda: self.broker.status(self.root, 'powershell')['columns'] == 111)
        self.assertEqual(self.broker.status(self.root, 'powershell')['child_pid'], original)
        before_interrupt = len(second[1])
        self.send(second, b'\x03')
        eventually(lambda: b'KILIX_TEST_PROMPT>' in second[1][before_interrupt:])
        self.send(second, b"Write-Output ('MEMENTO:'+$state+':'+$n+':'+$PID)\r")
        eventually(lambda: re.search(rb'MEMENTO:retained:[0-9]+:' + str(original).encode(), second[1]))
        with patch.dict(os.environ, {self.broker.ID_KEY: 'powershell', self.broker.TOKEN_KEY: record['token'],
                                     'KITTY_WINDOW_ID': 'stale', 'KITTY_RC_PASSWORD': 'stale'}):
            self.broker.refresh_environment()
            self.assertEqual(os.environ['KITTY_WINDOW_ID'], '202')
            self.assertEqual(os.environ['KITTY_RC_PASSWORD'], 'test-context-202')
        self.broker.detach(self.root, 'powershell')
        second[0].wait(timeout=5)
        eventually(lambda: not self.broker.status(self.root, 'powershell')['attached'])
        self.assertIsNone(handle.poll())
        self.broker.request(self.root, 'powershell', self.broker.wire.TERMINATE)
        eventually(lambda: handle.poll() is not None)

    def test_native_cmd_reconnect_preserves_environment_and_exact_exit_status(self):
        record = self.start('command', [self.cmd, '/d', '/q'])
        first = self.client(record)
        original = self.broker.status(self.root, 'command')['child_pid']
        self.send(first, b'set KILIX_MEMORY=kept\r\necho INITIAL_%KILIX_MEMORY%\r\n')
        eventually(lambda: b'INITIAL_kept' in first[1])
        self.broker.detach(self.root, 'command')
        first[0].wait(timeout=5)
        eventually(lambda: not self.broker.status(self.root, 'command')['attached'])
        second = self.client(record, '203')
        self.send(second, b'echo RESTORED_%KILIX_MEMORY%\r\n')
        eventually(lambda: b'RESTORED_kept' in second[1])
        self.assertEqual(self.broker.status(self.root, 'command')['child_pid'], original)
        self.send(second, b'exit 17\r\n')
        self.assertEqual(second[0].wait(timeout=15), 17)
        eventually(lambda: not self.broker.sessions(self.root))

    def test_bad_frames_and_duplicate_names_do_not_terminate_live_session(self):
        from kitty.windows_pty_ipc import close_connection, connect
        record = self.start('valid', [self.cmd, '/d', '/q'])
        original = self.broker.status(self.root, 'valid')['child_pid']
        connection = connect(self.root, 'valid')
        try:
            connection.send_bytes(b'not a broker frame')
            self.assertTrue(connection.poll(3))
            kind, payload = self.broker.wire.receive(connection)
            self.assertEqual(kind, self.broker.wire.ERROR)
        finally:
            close_connection(connection)
        with self.assertRaises(ValueError):
            self.broker.start_session('valid', [self.cmd], self.cmd, os.getcwd(), os.environ.copy())
        self.assertEqual(self.broker.status(self.root, 'valid')['child_pid'], original)
        connection = connect(self.root, 'valid')
        try:
            self.broker.wire.send(connection, self.broker.wire.ATTACH, self.broker.wire.SIZE.pack(24, 80, 0, 0))
            eventually(lambda: self.broker.status(self.root, 'valid')['attached'])
            self.broker.wire.send(connection, self.broker.wire.RESIZE, self.broker.wire.SIZE.pack(0, 80, 0, 0))
            eventually(lambda: not self.broker.status(self.root, 'valid')['attached'])
        finally:
            close_connection(connection)
        client = self.client(record)
        self.send(client, b'echo STILL_RUNNING\r\n')
        eventually(lambda: b'STILL_RUNNING' in client[1])

    def test_native_exit_preserves_full_dword(self):
        for code in (3010, 0xc000013a):
            with self.subTest(code=code):
                record = self.start('exit-' + str(code), [self.cmd, '/d', '/q'])
                client = self.client(record)
                signed = code if code < 0x80000000 else code - 0x100000000
                self.send(client, f'exit {signed}\r\n'.encode())
                self.assertEqual(client[0].wait(timeout=15), code)

    def test_terminate_stays_responsive_with_unread_input_and_output(self):
        from kitty.windows_pty_ipc import Process, close_connection, connect
        record = self.start('pressure', [self.powershell, '-NoLogo', '-NoProfile', '-Command', 'Start-Sleep -Seconds 120'])
        child = Process(self.broker.status(self.root, 'pressure')['child_pid'])
        self.addCleanup(child.close)
        connection = connect(self.root, record['id'])
        self.addCleanup(lambda: close_connection(connection))
        self.broker.wire.send(connection, self.broker.wire.ATTACH, self.broker.wire.SIZE.pack(24, 80, 0, 0))
        def flood():
            try:
                for _ in range(256):
                    self.broker.wire.send(connection, self.broker.wire.INPUT, b'x' * 32768)
            except (OSError, EOFError, ValueError, TypeError):
                pass
        sender = threading.Thread(target=flood, daemon=True)
        sender.start()
        time.sleep(.5)
        self.broker.request(self.root, 'pressure', self.broker.wire.TERMINATE)
        eventually(lambda: child.poll() is not None, seconds=8)
        close_connection(connection)
        sender.join(timeout=3)
        self.assertFalse(sender.is_alive())

    def test_case_variants_share_one_attachment_lock(self):
        from kitty.windows_pty_ipc import attachment_lock
        result = []
        def contender():
            try:
                with attachment_lock(self.root, 'case'):
                    result.append('acquired')
            except ValueError:
                result.append('blocked')
        with attachment_lock(self.root, 'Case'):
            thread = threading.Thread(target=contender, daemon=True)
            thread.start()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, ['blocked'])

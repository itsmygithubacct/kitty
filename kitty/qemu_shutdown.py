"""Request ACPI shutdown from the QEMU monitor owned by an app pane."""
import json
import os
import socket
import stat
import struct
import time


def request_shutdown(path: str) -> None:
    parent = os.stat(os.path.dirname(path), follow_symlinks=False)
    endpoint = os.stat(path, follow_symlinks=False)
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077 or not stat.S_ISSOCK(endpoint.st_mode)
            or endpoint.st_uid != os.getuid()):
        raise ValueError('VM monitor is not in a private directory owned by this user')
    deadline = time.monotonic() + 3
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1)
        connection.connect(path)
        pid, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid() or not os.path.basename(os.readlink(f'/proc/{pid}/exe')).startswith('qemu-system-'):
            raise ValueError('VM monitor peer is not QEMU owned by this user')
        with connection.makefile('rb') as stream:
            def receive():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('VM monitor did not acknowledge shutdown')
                connection.settimeout(remaining)
                line = stream.readline(65537)
                if not line or len(line) > 65536:
                    raise ValueError('Invalid VM monitor response')
                return json.loads(line)

            if 'QMP' not in receive():
                raise ValueError('VM monitor did not send a QMP greeting')
            for number, command in enumerate(('qmp_capabilities', 'system_powerdown')):
                connection.sendall(json.dumps({'execute': command, 'id': number}).encode() + b'\n')
                while True:
                    response = receive()
                    if response.get('id') == number:
                        if 'error' in response or 'return' not in response:
                            raise RuntimeError('VM monitor refused ' + command)
                        break

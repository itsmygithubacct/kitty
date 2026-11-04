#!/usr/bin/env python
# License: GPL v3
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class X11WindowLifetime(unittest.TestCase):
    @unittest.skipUnless(shutil.which('Xvfb') and shutil.which('cc'), 'requires Xvfb and a C compiler')
    def test_disappearing_window_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = os.path.join(tmp, 'window-lifetime')
            source = str(Path(__file__).with_suffix('.c'))
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', source, '-lX11', '-lX11-xcb', '-lxcb', '-o', binary], check=True, timeout=60)
            read_fd, write_fd = os.pipe()
            with tempfile.TemporaryFile() as log:
                server = subprocess.Popen(['Xvfb', '-displayfd', str(write_fd), '-screen', '0', '320x240x24', '-nolisten', 'tcp'],
                                          pass_fds=(write_fd,), stdout=log, stderr=log)
                os.close(write_fd)
                try:
                    import select
                    self.assertTrue(select.select([read_fd], [], [], 10)[0], 'Xvfb startup timed out')
                    display = os.read(read_fd, 64).decode().strip()
                    self.assertTrue(display.isdecimal(), 'Xvfb did not allocate a display')
                    subprocess.run([binary], env={'DISPLAY': ':' + display}, check=True, timeout=30)
                finally:
                    os.close(read_fd)
                    server.terminate()
                    server.wait(timeout=10)

#!/usr/bin/env python
# License: GPL v3
"""Native engine ownership regressions; run through kitty-console +launch test.py.

The packaged ConPTY runtime is resolved beside the engine launcher, so these
tests intentionally exercise the same executable and DLL loader as the GUI.
"""
import ctypes
import os
import time
import unittest

from . import BaseTest, is_windows


@unittest.skipUnless(is_windows, 'Native Windows ConPTY ownership')
class WindowsPtyTest(BaseTest):
    def setUp(self):
        from kitty import fast_data_types
        self.native = fast_data_types
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.GetCurrentProcess.restype = ctypes.c_void_p
        self.kernel.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        self.kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        self.kernel.OpenProcess.restype = ctypes.c_void_p
        self.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.PeekNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
        self.cmd = os.path.join(os.environ['SystemRoot'], 'System32', 'cmd.exe').replace('/', '\\')
        self.env = tuple(f'{k}={v}' for k, v in sorted(os.environ.items()))

    def handles(self):
        count = ctypes.c_ulong()
        self.assertTrue(self.kernel.GetProcessHandleCount(self.kernel.GetCurrentProcess(), ctypes.byref(count)))
        return count.value

    def output_session(self):
        import msvcrt
        read_fd, _, pty = self.native.open_pty(80, 24)
        process = None
        output = bytearray()
        try:
            pid = self.native.spawn(pty, self.cmd, os.getcwd(),
                                    (self.cmd, '/d', '/c', 'echo KILIX_ENGINE_PTY'), self.env)
            process = self.kernel.OpenProcess(0x100000, False, pid)
            self.assertTrue(process)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                available = ctypes.c_ulong()
                if self.kernel.PeekNamedPipe(msvcrt.get_osfhandle(read_fd), None, 0, None,
                                             ctypes.byref(available), None) and available.value:
                    output.extend(os.read(read_fd, available.value))
                if b'KILIX_ENGINE_PTY' in output and self.kernel.WaitForSingleObject(process, 0) == 0:
                    break
                time.sleep(0.01)
            self.assertIn(b'KILIX_ENGINE_PTY', output)
            self.ae(self.kernel.WaitForSingleObject(process, 1000), 0)
        finally:
            self.native.close_pty(pty)
            if process:
                self.kernel.CloseHandle(process)

    def test_windows_pty_repeated_launch_and_failed_spawn_release_handles(self):
        for _ in range(3):
            self.output_session()
        before = self.handles()
        for _ in range(12):
            self.output_session()
            _, _, pty = self.native.open_pty(80, 24)
            try:
                with self.assertRaises(OSError):
                    self.native.spawn(pty, 'Z:\\kilix-missing.exe', os.getcwd(),
                                      ('Z:\\kilix-missing.exe',), self.env)
            finally:
                self.native.close_pty(pty)
        self.ae(self.handles(), before)

    def test_windows_pty_close_terminates_owned_child(self):
        _, _, pty = self.native.open_pty(80, 24)
        process = None
        try:
            pid = self.native.spawn(pty, self.cmd, os.getcwd(),
                                    (self.cmd, '/d', '/c', 'ping -n 60 127.0.0.1 >nul'), self.env)
            process = self.kernel.OpenProcess(0x100000, False, pid)
            self.assertTrue(process)
            self.native.close_pty(pty)
            self.ae(self.kernel.WaitForSingleObject(process, 5000), 0)
        finally:
            self.native.close_pty(pty)
            if process:
                self.kernel.CloseHandle(process)

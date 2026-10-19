# License: GPL v3
"""Owner-only local Windows IPC and pinned process identities for recovery."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from contextlib import contextmanager
from functools import lru_cache
import hashlib
from pathlib import Path
import os

kernel = C.WinDLL('kernel32', use_last_error=True)
advapi = C.WinDLL('advapi32', use_last_error=True)


def api(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, args
    return fn


CloseHandle = api(kernel, 'CloseHandle', W.BOOL, W.HANDLE)
LocalFree = api(kernel, 'LocalFree', W.HANDLE, W.HANDLE)
OpenProcess = api(kernel, 'OpenProcess', W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
GetCurrentProcess = api(kernel, 'GetCurrentProcess', W.HANDLE)
WaitForSingleObject = api(kernel, 'WaitForSingleObject', W.DWORD, W.HANDLE, W.DWORD)
TerminateProcess = api(kernel, 'TerminateProcess', W.BOOL, W.HANDLE, W.UINT)
ExitProcess = api(kernel, 'ExitProcess', None, W.UINT)
GetExitCodeProcess = api(kernel, 'GetExitCodeProcess', W.BOOL, W.HANDLE, C.POINTER(W.DWORD))
GetProcessTimes = api(kernel, 'GetProcessTimes', W.BOOL, W.HANDLE,
                     *([C.POINTER(W.FILETIME)] * 4))
OpenProcessToken = api(advapi, 'OpenProcessToken', W.BOOL, W.HANDLE, W.DWORD, C.POINTER(W.HANDLE))
GetTokenInformation = api(advapi, 'GetTokenInformation', W.BOOL, W.HANDLE, C.c_int,
                          W.LPVOID, W.DWORD, C.POINTER(W.DWORD))
ConvertSidToStringSid = api(advapi, 'ConvertSidToStringSidW', W.BOOL, W.LPVOID, C.POINTER(W.LPWSTR))
ConvertSDDL = api(advapi, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', W.BOOL,
                  W.LPCWSTR, W.DWORD, C.POINTER(W.LPVOID), C.POINTER(W.DWORD))
GetNamedSecurityInfo = api(advapi, 'GetNamedSecurityInfoW', W.DWORD, W.LPCWSTR, W.DWORD, W.DWORD,
                           C.POINTER(W.LPVOID), W.LPVOID, W.LPVOID, W.LPVOID, C.POINTER(W.LPVOID))
SetFileSecurity = api(advapi, 'SetFileSecurityW', W.BOOL, W.LPCWSTR, W.DWORD, W.LPVOID)
CancelIoEx = api(kernel, 'CancelIoEx', W.BOOL, W.HANDLE, W.LPVOID)
GetPipeServerPid = api(kernel, 'GetNamedPipeServerProcessId', W.BOOL, W.HANDLE, C.POINTER(W.DWORD))
GetPipeClientPid = api(kernel, 'GetNamedPipeClientProcessId', W.BOOL, W.HANDLE, C.POINTER(W.DWORD))


def checked(value):
    if not value:
        raise C.WinError(C.get_last_error())
    return value


def sid_string(sid) -> str:
    value = W.LPWSTR()
    checked(ConvertSidToStringSid(sid, C.byref(value)))
    try:
        return value.value
    finally:
        LocalFree(C.cast(value, W.LPVOID))


def process_sid(handle) -> str:
    token = W.HANDLE()
    checked(OpenProcessToken(handle, 8, C.byref(token)))
    try:
        size = W.DWORD()
        GetTokenInformation(token, 1, None, 0, C.byref(size))
        buffer = C.create_string_buffer(size.value)
        checked(GetTokenInformation(token, 1, buffer, size, C.byref(size)))
        return sid_string(C.cast(buffer, C.POINTER(W.LPVOID))[0])
    finally:
        CloseHandle(token)


@lru_cache(maxsize=1)
def current_sid() -> str:
    return process_sid(GetCurrentProcess())


class Security(C.Structure):
    _fields_ = [('nLength', W.DWORD), ('lpSecurityDescriptor', W.LPVOID), ('bInheritHandle', W.BOOL)]

    def __init__(self, inherit=False):
        super().__init__()
        self.nLength = C.sizeof(self)
        flags = 'OICI' if inherit else ''
        descriptor = W.LPVOID()
        sddl = f'O:{current_sid()}D:P(A;{flags};GA;;;SY)(A;{flags};GA;;;{current_sid()})'
        checked(ConvertSDDL(sddl, 1, C.byref(descriptor), None))
        self.lpSecurityDescriptor = descriptor

    def close(self):
        if self.lpSecurityDescriptor:
            LocalFree(self.lpSecurityDescriptor)
            self.lpSecurityDescriptor = None


CreateDirectory = api(kernel, 'CreateDirectoryW', W.BOOL, W.LPCWSTR, C.POINTER(Security))
GetFileAttributes = api(kernel, 'GetFileAttributesW', W.DWORD, W.LPCWSTR)
CreateNamedPipe = api(kernel, 'CreateNamedPipeW', W.HANDLE, W.LPCWSTR,
                      W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, C.POINTER(Security))
CreateMutex = api(kernel, 'CreateMutexW', W.HANDLE, C.POINTER(Security), W.BOOL, W.LPCWSTR)
ReleaseMutex = api(kernel, 'ReleaseMutex', W.BOOL, W.HANDLE)


def private_directory(path: Path) -> Path:
    if not path.is_absolute() or str(path).startswith('\\\\'):
        raise ValueError('Native session storage must be an absolute local directory')
    path.parent.mkdir(parents=True, exist_ok=True)
    security = Security(inherit=True)
    try:
        if not CreateDirectory(str(path), C.byref(security)) and C.get_last_error() != 183:
            checked(False)
        attributes = GetFileAttributes(str(path))
        if attributes == 0xffffffff or attributes & 0x400 or not attributes & 0x10:
            raise ValueError('Native session storage must be a real directory, not a reparse point')
        owner, descriptor = W.LPVOID(), W.LPVOID()
        error = GetNamedSecurityInfo(str(path), 1, 1, C.byref(owner), None, None, None, C.byref(descriptor))
        if error:
            raise C.WinError(error)
        try:
            if sid_string(owner) != current_sid():
                raise ValueError('Native session storage belongs to another Windows user')
        finally:
            LocalFree(descriptor)
        checked(SetFileSecurity(str(path), 0x80000004, security.lpSecurityDescriptor))
    finally:
        security.close()
    return path


def namespace(root: Path) -> str:
    value = current_sid() + '\0' + os.path.normcase(str(root.resolve()))
    return 'kilix-pty-' + hashlib.sha256(value.encode()).hexdigest()[:24]


def pipe_address(root: Path, name: str) -> str:
    return '\\\\.\\pipe\\' + namespace(root) + '-' + name


class Process:
    def __init__(self, pid: int, terminate=False):
        self.handle = checked(OpenProcess(0x1000 | 0x100000 | (1 if terminate else 0), False, pid))
        try:
            if process_sid(self.handle) != current_sid():
                raise ValueError('Process belongs to another Windows user')
            times = [W.FILETIME() for _ in range(4)]
            checked(GetProcessTimes(self.handle, *(C.byref(item) for item in times)))
            self.started = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        except BaseException:
            self.close()
            raise

    def poll(self):
        if WaitForSingleObject(self.handle, 0) == 258:
            return None
        status = W.DWORD()
        checked(GetExitCodeProcess(self.handle, C.byref(status)))
        return status.value

    def close(self):
        if self.handle:
            CloseHandle(self.handle)
            self.handle = None


def check_peer(connection, server: bool):
    pid = W.DWORD()
    checked((GetPipeServerPid if server else GetPipeClientPid)(connection.fileno(), C.byref(pid)))
    process = Process(pid.value)
    process.close()


def close_connection(connection):
    try:
        CancelIoEx(connection.fileno(), None)
    except (ValueError, OSError):
        pass
    connection.close()


def listener(root, name):
    from multiprocessing.connection import PipeListener

    class PrivateListener(PipeListener):
        def __init__(self, address):
            self.security = Security()
            try:
                super().__init__(address)
            except BaseException:
                self.security.close()
                raise

        def _new_handle(self, first=False):
            # Duplex, overlapped, first-instance ownership; local clients only.
            handle = CreateNamedPipe(self._address, 3 | 0x40000000 | (0x80000 if first else 0),
                                     4 | 2 | 8, 255, 65536, 65536, 0, C.byref(self.security))
            if handle == C.c_void_p(-1).value:
                raise C.WinError(C.get_last_error())
            return handle

    return PrivateListener(pipe_address(root, name))


def connect(root, name):
    import _winapi
    from multiprocessing.connection import PipeConnection
    # No unbounded retry against a stopped or saturated broker.
    address = pipe_address(root, name)
    _winapi.WaitNamedPipe(address, 500)
    handle = _winapi.CreateFile(address, 0xc0000000, 0, 0, 3, 0x40000000, 0)
    connection = PipeConnection(handle)
    try:
        _winapi.SetNamedPipeHandleState(handle, 2, None, None)
        check_peer(connection, server=True)
        return connection
    except BaseException:
        connection.close()
        raise


@contextmanager
def attachment_lock(root, name):
    security = Security()
    try:
        # Pipe names and metadata filenames are case-insensitive on Windows;
        # kernel mutex names are case-sensitive. Share one attachment lock for
        # every spelling of the same session before publishing its context.
        handle = checked(CreateMutex(C.byref(security), False, 'Global\\' + namespace(root) + '-' + name.lower()))
    finally:
        security.close()
    acquired = False
    try:
        acquired = WaitForSingleObject(handle, 0) in (0, 128)
        if not acquired:
            raise ValueError('Session already has an attachment; detach it first')
        yield
    finally:
        if acquired:
            ReleaseMutex(handle)
        CloseHandle(handle)

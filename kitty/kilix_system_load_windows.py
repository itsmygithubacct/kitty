"""Read native Windows system counters without a helper process."""

from __future__ import annotations

import ctypes as C
from functools import lru_cache
from typing import Any


class _FileTime(C.Structure):
    _fields_ = [('low', C.c_uint32), ('high', C.c_uint32)]

    def ticks(self) -> int:
        return (self.high << 32) | self.low


class _MemoryStatus(C.Structure):
    _fields_ = [
        ('length', C.c_uint32), ('load', C.c_uint32),
        ('total_physical', C.c_uint64), ('available_physical', C.c_uint64),
        ('total_pagefile', C.c_uint64), ('available_pagefile', C.c_uint64),
        ('total_virtual', C.c_uint64), ('available_virtual', C.c_uint64),
        ('available_extended_virtual', C.c_uint64),
    ]


@lru_cache(maxsize=1)
def _kernel32() -> Any:
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.GetSystemTimes.argtypes = [C.POINTER(_FileTime)] * 3
    kernel.GetSystemTimes.restype = C.c_int32
    kernel.GetActiveProcessorGroupCount.argtypes = []
    kernel.GetActiveProcessorGroupCount.restype = C.c_uint16
    kernel.GetActiveProcessorCount.argtypes = [C.c_uint16]
    kernel.GetActiveProcessorCount.restype = C.c_uint32
    kernel.GlobalMemoryStatusEx.argtypes = [C.POINTER(_MemoryStatus)]
    kernel.GlobalMemoryStatusEx.restype = C.c_int32
    return kernel


def read_cpu_times() -> tuple[int, int, int] | None:
    """Return total/idle 100-ns ticks and the matching logical CPU count."""
    try:
        kernel = _kernel32()
        # GetSystemTimes covers only the calling thread's primary group on
        # multi-group machines. Do not label that subset as whole-system load.
        # https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-getsystemtimes
        if kernel.GetActiveProcessorGroupCount() != 1:
            return None
        cpus = int(kernel.GetActiveProcessorCount(0))
        if cpus < 1:
            return None
        idle, system, user = _FileTime(), _FileTime(), _FileTime()
        if not kernel.GetSystemTimes(C.byref(idle), C.byref(system), C.byref(user)):
            return None
        # Kernel time already includes idle time.
        total = system.ticks() + user.ticks()
        idle_ticks = idle.ticks()
        return (total, idle_ticks, cpus) if idle_ticks <= total else None
    except (AttributeError, OSError):
        return None


def read_memory_used() -> int | None:
    """Return physical bytes in use; unavailable counters remain unknown."""
    try:
        status = _MemoryStatus()
        status.length = C.sizeof(status)
        if not _kernel32().GlobalMemoryStatusEx(C.byref(status)):
            return None
        total, available = status.total_physical, status.available_physical
        # Available physical memory includes reusable standby pages, matching
        # the Linux MemTotal - MemAvailable readout rather than commit charge.
        if total == 0 or available > total:
            return None
        return total - available
    except (AttributeError, OSError):
        return None

import ctypes as C
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kitty import kilix_system_load_windows as windows

from . import BaseTest


class TestWindowsSystemCounters(BaseTest):

    def setUp(self) -> None:
        super().setUp()
        self.idle, self.kernel, self.user = 400, 500, 300
        self.total_memory, self.available_memory = 16 << 30, 4 << 30
        self.memory_size = 0
        self.api = SimpleNamespace(
            GetSystemTimes=MagicMock(side_effect=self.cpu_times),
            GetActiveProcessorGroupCount=MagicMock(return_value=1),
            GetActiveProcessorCount=MagicMock(return_value=4),
            GlobalMemoryStatusEx=MagicMock(side_effect=self.memory_status),
        )
        windows._kernel32.cache_clear()
        self.dll = patch.object(C, 'WinDLL', return_value=self.api, create=True)
        self.loader = self.dll.start()

    def tearDown(self) -> None:
        self.dll.stop()
        windows._kernel32.cache_clear()
        super().tearDown()

    def cpu_times(self, idle, kernel, user):
        for pointer, value in zip((idle, kernel, user), (self.idle, self.kernel, self.user)):
            target = C.cast(pointer, C.POINTER(windows._FileTime)).contents
            target.low, target.high = value & 0xffffffff, value >> 32
        return 1

    def memory_status(self, pointer):
        target = C.cast(pointer, C.POINTER(windows._MemoryStatus)).contents
        self.memory_size = target.length
        target.total_physical, target.available_physical = self.total_memory, self.available_memory
        # These fields are deliberately different from physical memory.
        target.total_pagefile, target.available_pagefile = 64 << 30, 32 << 30
        return 1

    def test_kernel_time_already_contains_idle(self) -> None:
        self.assertEqual(windows.read_cpu_times(), (800, 400, 4))
        self.api.GetActiveProcessorCount.assert_called_once_with(0)

    def test_filetime_keeps_high_bits(self) -> None:
        self.idle, self.kernel, self.user = (1 << 40) + 3, (1 << 40) + 9, (1 << 35) + 5
        self.assertEqual(windows.read_cpu_times(), (self.kernel + self.user, self.idle, 4))

    def test_cpu_failure_is_unknown(self) -> None:
        self.api.GetSystemTimes.side_effect = None
        self.api.GetSystemTimes.return_value = 0
        self.assertIsNone(windows.read_cpu_times())
        self.assertEqual(windows.read_memory_used(), 12 << 30)

    def test_invalid_cpu_count_does_not_query_times(self) -> None:
        self.api.GetActiveProcessorCount.return_value = 0
        self.assertIsNone(windows.read_cpu_times())
        self.api.GetSystemTimes.assert_not_called()

    def test_group_subset_is_not_reported_as_the_whole_system(self) -> None:
        for groups in (0, 2, 4):
            with self.subTest(groups=groups):
                self.api.GetActiveProcessorGroupCount.return_value = groups
                self.assertIsNone(windows.read_cpu_times())
        self.api.GetSystemTimes.assert_not_called()
        self.assertEqual(windows.read_memory_used(), 12 << 30)

    def test_impossible_idle_counter_is_unknown(self) -> None:
        self.idle = self.kernel + self.user + 1
        self.assertIsNone(windows.read_cpu_times())

    def test_physical_memory_uses_available_not_commit_or_percent(self) -> None:
        self.assertEqual(windows.read_memory_used(), 12 << 30)
        self.assertEqual(self.memory_size, 64)

    def test_fully_available_memory_is_zero(self) -> None:
        self.available_memory = self.total_memory
        self.assertEqual(windows.read_memory_used(), 0)

    def test_memory_failure_is_unknown(self) -> None:
        self.api.GlobalMemoryStatusEx.side_effect = None
        self.api.GlobalMemoryStatusEx.return_value = 0
        self.assertIsNone(windows.read_memory_used())
        self.assertEqual(windows.read_cpu_times(), (800, 400, 4))

    def test_invalid_memory_counts_are_unknown(self) -> None:
        for total, available in ((0, 0), (1 << 30, 2 << 30)):
            with self.subTest(total=total, available=available):
                self.total_memory, self.available_memory = total, available
                self.assertIsNone(windows.read_memory_used())

    def test_loader_error_does_not_break_sampling(self) -> None:
        self.loader.side_effect = OSError('Unavailable native API')
        self.assertIsNone(windows.read_cpu_times())
        self.assertIsNone(windows.read_memory_used())

    def test_native_error_does_not_break_sampling(self) -> None:
        self.api.GetSystemTimes.side_effect = OSError('Counter unavailable')
        self.api.GlobalMemoryStatusEx.side_effect = OSError('Counter unavailable')
        self.assertIsNone(windows.read_cpu_times())
        self.assertIsNone(windows.read_memory_used())

    def test_windows_abi_is_fixed_width_and_bindings_are_cached(self) -> None:
        self.assertEqual(C.sizeof(windows._FileTime), 8)
        self.assertEqual(C.sizeof(windows._MemoryStatus), 64)
        self.assertEqual(windows._MemoryStatus.total_physical.offset, 8)
        windows.read_cpu_times()
        windows.read_memory_used()
        windows.read_cpu_times()
        self.loader.assert_called_once_with('kernel32', use_last_error=True)
        self.assertEqual(self.api.GetSystemTimes.argtypes, [C.POINTER(windows._FileTime)] * 3)
        self.assertIs(self.api.GetSystemTimes.restype, C.c_int32)
        self.assertEqual(self.api.GetActiveProcessorCount.argtypes, [C.c_uint16])
        self.assertIs(self.api.GetActiveProcessorCount.restype, C.c_uint32)
        self.assertIs(self.api.GetActiveProcessorGroupCount.restype, C.c_uint16)
        self.assertEqual(self.api.GlobalMemoryStatusEx.argtypes, [C.POINTER(windows._MemoryStatus)])

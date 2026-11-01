#!/usr/bin/env python
"""Whole-system and per-pane GPU use for the Kilix top bar and pane chips.

Data comes from three places, all read without new dependencies:

* ``/sys/class/drm/card*`` lists the GPUs, their PCI address, driver and
  runtime power state. A device whose ``power/runtime_status`` is
  ``suspended`` or ``suspending`` is reported asleep and nothing else is read
  from it: reading a vendor counter (NVML, amdgpu's sysfs files, some
  drivers' fdinfo) resumes a sleeping GPU.
* NVIDIA devices are read through NVML (``libnvidia-ml.so.1``) with ctypes:
  utilisation, memory, and the per-process memory lists. ``nvidia-smi`` is
  never spawned. ``nvmlInit`` attaches every NVIDIA GPU, so NVML is not
  opened at all while any NVIDIA GPU is asleep.
* AMD devices are read from ``gpu_busy_percent`` / ``mem_info_vram_*`` where
  they exist; AMD and Intel per-process memory comes from DRM fdinfo, read
  only for a pane's own process tree and only for fds that point at
  ``/dev/dri``.

Runtime power management: every one of those reads (other than the PM-core
attributes under ``power/``) reaches the driver and can restart its idle
timer. On a GPU with ``power/control=auto`` a read that finds the GPU idle
(0% busy, and no process's GPU memory changed since the previous read; a
display server's resident allocation does not count as activity) starts a
backoff of at least 30 s, renewed for as long as the GPU stays idle. During
the backoff the GPU's last reading is shown and nothing reaches its driver,
so the readouts are up to that long out of date: a job started meanwhile
appears when the backoff ends, or at once if the GPU was asleep and the job
woke it.

Attribution is by process: memory held by a service outside every pane (for
example a model server started by systemd) is in the top-bar total but in no
pane chip, and a pane running only a client of that service gets no chip.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, NamedTuple

from .kilix_chrome.settings import chrome_value
from .kilix_memory import GIB, KIB, MIB, format_pane_memory
from .utils import log_error

SYSTEM_GPU_MODE_KEY = 'KILIX_CHROME_SYSTEM_GPU_MODE'
SYSTEM_GPU_MODE_DEFAULT = 'always'
SYSTEM_GPU_MODES = ('auto', 'always', 'off')
PANE_GPU_MODE_KEY = 'KILIX_CHROME_PANE_GPU_MODE'
PANE_GPU_MODE_DEFAULT = 'auto'
PANE_GPU_MODES = ('auto', 'always', 'off')
# A pane chip appears in auto mode once its process tree holds more than this.
# Fixed, like the 1 GiB RAM and 1.0-core CPU thresholds, which have no setting.
PANE_GPU_THRESHOLD = 512 * MIB
# In auto mode the top bar shows utilisation at or above this percentage and
# VRAM above the pane threshold.
SYSTEM_GPU_BUSY_THRESHOLD = 10
GPU_WIDGET_ACTION = 'kilix_show_gpu_widget'
# Nerd Fonts Material Design "expansion card": a PCIe graphics card. The
# alternates are kept for the owner to compare (rendered to evidence PNGs).
GPU_GLYPH = chr(0xf08ae)
GPU_GLYPH_ALTERNATES = (chr(0xf0fb2), chr(0xf2db))  # expansion_card_variant, fa-microchip
# A pale sky blue, beside the RAM chip's Tango green. Contrast (WCAG 2):
# 4.58:1 on Kilix's active title bar #3465a4, 9.78:1 on the inactive #2e3436
# and 13.8:1 on the top bar #16181a.
GPU_CHIP_RGB = 0xc9e6ff
ASLEEP_TEXT = 'sleep'
_CACHE_SECONDS = 1.5  # the same cadence as the CPU/RAM readouts
_DEVICE_LIST_SECONDS = 30.0  # GPU hot-plug is rare; power state is re-read every sample
# A runtime-PM GPU (power/control=auto) that reads idle is left alone for at
# least this long (twice its autosuspend delay when that is longer, up to the
# maximum), and again after every read that still finds it idle. The NVIDIA
# driver needs a use-free 5 s window before it starts to power the GPU down,
# and amdgpu's autosuspend delay is 5 s, so a 4 s poll would hold either awake.
_IDLE_BACKOFF_SECONDS = 30.0
_IDLE_BACKOFF_MAX_SECONDS = 120.0
# A process whose GPU memory moved by less than this between two reads, like
# a display server's or compositor's resident buffers, is not activity.
_ACTIVITY_TOLERANCE = MIB
# After nvmlInit fails (for example a driver/library version mismatch after an
# upgrade), it is tried again after this long, doubling up to the maximum.
_NVML_RETRY_SECONDS = 30.0
_NVML_RETRY_MAX_SECONDS = 600.0
_PCI_ADDRESS = re.compile(r'^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]$')
_CARD_NAME = re.compile(r'^card\d+$')
_NODE_NAME = re.compile(r'^(card|renderD)\d+$')
_VENDORS = {0x10de: 'NVIDIA', 0x1002: 'AMD', 0x8086: 'Intel'}
NVIDIA_VENDOR = 0x10de


class DrmDevice(NamedTuple):
    """The static facts about one GPU, from sysfs."""
    pci: str
    sysfs: str
    vendor_id: int
    device_id: int
    driver: str
    nodes: tuple[str, ...]  # card0, renderD128, ...
    boot_vga: bool = False  # the firmware's boot display device

    @property
    def is_nvidia(self) -> bool:
        return self.vendor_id == NVIDIA_VENDOR and self.driver == 'nvidia'

    @property
    def discrete(self) -> bool:
        # NVIDIA GPUs are always discrete. Otherwise an integrated GPU sits on
        # the root bus (Intel's is always 0000:00:02.0), or is the boot VGA
        # device behind an internal bridge (an AMD APU, e.g. 0000:05:00.0); a
        # card is behind a bridge.
        if self.vendor_id == NVIDIA_VENDOR:
            return True
        if self.boot_vga:
            return False
        try:
            return int(self.pci.split(':')[1], 16) != 0
        except (IndexError, ValueError):
            return False


class GpuDevice(NamedTuple):
    """One sampled GPU."""
    pci: str
    vendor_id: int
    device_id: int
    driver: str
    name: str
    discrete: bool
    asleep: bool
    utilisation: int | None  # percent busy
    memory_used: int | None  # bytes
    memory_total: int | None  # bytes
    # 'nvml', 'sysfs', 'asleep', 'none', or 'held' (an awake NVIDIA GPU left
    # unread because another NVIDIA GPU is asleep)
    source: str

    @property
    def readable(self) -> bool:
        return self.utilisation is not None or self.memory_used is not None


class GpuSnapshot(NamedTuple):
    devices: tuple[GpuDevice, ...]
    # Per-process GPU memory from NVML, summed over devices.
    process_memory: Mapping[int, int]
    # DRM node name -> PCI address, for awake non-NVIDIA devices whose
    # clients are counted through fdinfo.
    fdinfo_nodes: Mapping[str, str]
    # PCI address -> how many times its counters have been read. A pane
    # process's fdinfo for a device is read once per counter read, so a
    # device in its idle backoff is not reached through fdinfo either.
    read_epochs: Mapping[str, int] = {}


class DrmClient(NamedTuple):
    driver: str
    pdev: str
    client_id: str
    memory_bytes: int


EMPTY_SNAPSHOT = GpuSnapshot((), {}, {}, {})


def _mode(key: str, default: str, modes: tuple[str, ...]) -> str:
    mode = chrome_value(key, default).strip().lower()
    return mode if mode in modes else default


def system_gpu_mode() -> str:
    return _mode(SYSTEM_GPU_MODE_KEY, SYSTEM_GPU_MODE_DEFAULT, SYSTEM_GPU_MODES)


def pane_gpu_mode() -> str:
    return _mode(PANE_GPU_MODE_KEY, PANE_GPU_MODE_DEFAULT, PANE_GPU_MODES)


def _sys_root() -> str:
    value = os.environ.get('KILIX_GPU_SYSFS_ROOT') or '/sys'
    return os.path.abspath(os.path.expanduser(value))


def _proc_root() -> str:
    from .kilix_memory import _proc_root as memory_proc_root
    return memory_proc_root()


def _read(path: str, limit: int = 4096) -> str | None:
    try:
        with open(path, encoding='ascii', errors='replace') as stream:
            return stream.read(limit).strip()
    except OSError:
        return None


def _read_int(path: str, base: int = 10) -> int | None:
    text = _read(path, 64)
    if not text:
        return None
    try:
        return int(text, base)
    except ValueError:
        return None


# Discovery {{{

_DEVICE_LIST: tuple[DrmDevice, ...] = ()
_DEVICE_LIST_ROOT = ''
_DEVICE_LIST_UNTIL = 0.0


def _discover(sys_root: str) -> tuple[DrmDevice, ...]:
    drm = os.path.join(sys_root, 'class', 'drm')
    try:
        names = sorted(os.listdir(drm))
    except OSError:
        return ()
    by_device: dict[str, list[str]] = {}
    cards: dict[str, str] = {}
    for name in names:
        if not _NODE_NAME.match(name):
            continue
        device = os.path.realpath(os.path.join(drm, name, 'device'))
        pci = os.path.basename(device)
        if not _PCI_ADDRESS.match(pci):
            continue  # a platform or virtual GPU without a PCI function
        by_device.setdefault(device, []).append(name)
        if _CARD_NAME.match(name):
            cards.setdefault(device, name)
    devices: list[DrmDevice] = []
    for device, nodes in by_device.items():
        if device not in cards:
            continue
        vendor = _read_int(os.path.join(device, 'vendor'), 16)
        device_id = _read_int(os.path.join(device, 'device'), 16)
        try:
            driver = os.path.basename(os.readlink(os.path.join(device, 'driver')))
        except OSError:
            driver = ''
        devices.append(DrmDevice(
            os.path.basename(device), device, vendor or 0, device_id or 0,
            driver, tuple(nodes), _read(os.path.join(device, 'boot_vga'), 8) == '1'))
    devices.sort(key=lambda d: d.pci)
    return tuple(devices)


def drm_devices(force: bool = False) -> tuple[DrmDevice, ...]:
    global _DEVICE_LIST, _DEVICE_LIST_ROOT, _DEVICE_LIST_UNTIL
    root = _sys_root()
    now = time.monotonic()
    if force or root != _DEVICE_LIST_ROOT or now >= _DEVICE_LIST_UNTIL:
        _DEVICE_LIST = _discover(root)
        _DEVICE_LIST_ROOT = root
        _DEVICE_LIST_UNTIL = now + _DEVICE_LIST_SECONDS
    return _DEVICE_LIST


def runtime_status(device: DrmDevice) -> str:
    return (_read(os.path.join(device.sysfs, 'power', 'runtime_status'), 64) or 'unknown').lower()


def runtime_control(device: DrmDevice) -> str:
    return (_read(os.path.join(device.sysfs, 'power', 'control'), 64) or 'on').lower()


def is_asleep(device: DrmDevice) -> bool:
    # 'suspending' is on its way down; touching it now would resume it.
    return runtime_status(device) in ('suspended', 'suspending')


def backoff_seconds(device: DrmDevice) -> float:
    """How long an idle runtime-PM GPU is left alone. ``autosuspend_delay_ms``
    is a PM-core attribute (reading it does not touch the device); drivers
    that do not use autosuspend, like NVIDIA's, return an error for it."""
    seconds = _IDLE_BACKOFF_SECONDS
    delay = _read_int(os.path.join(device.sysfs, 'power', 'autosuspend_delay_ms'))
    if delay is not None and delay > 0:
        seconds = max(seconds, 2 * delay / 1000)
    return min(seconds, _IDLE_BACKOFF_MAX_SECONDS)

# }}}


# NVML {{{

NVML_SUCCESS = 0
NVML_ERROR_INSUFFICIENT_SIZE = 7
NVML_ERROR_FUNCTION_NOT_FOUND = 13
NVML_VALUE_NOT_AVAILABLE = (1 << 64) - 1


class _NvmlUtilization(ctypes.Structure):
    _fields_ = [('gpu', ctypes.c_uint), ('memory', ctypes.c_uint)]


class _NvmlMemory(ctypes.Structure):
    _fields_ = [('total', ctypes.c_ulonglong), ('free', ctypes.c_ulonglong),
                ('used', ctypes.c_ulonglong)]


class _NvmlProcessInfoV1(ctypes.Structure):
    _fields_ = [('pid', ctypes.c_uint), ('usedGpuMemory', ctypes.c_ulonglong)]


class _NvmlProcessInfoV2(ctypes.Structure):
    # nvmlProcessInfo_v2_t; the _v3 entry points use the same layout.
    _fields_ = [('pid', ctypes.c_uint), ('usedGpuMemory', ctypes.c_ulonglong),
                ('gpuInstanceId', ctypes.c_uint), ('computeInstanceId', ctypes.c_uint)]


# Newest first. Each entry is (suffix, struct).
_PROCESS_ABIS: tuple[tuple[str, type[ctypes.Structure]], ...] = (
    ('_v3', _NvmlProcessInfoV2), ('_v2', _NvmlProcessInfoV2), ('', _NvmlProcessInfoV1))
_PROCESS_LISTS = ('nvmlDeviceGetComputeRunningProcesses', 'nvmlDeviceGetGraphicsRunningProcesses')


def _default_nvml_loader() -> Any:
    return ctypes.CDLL('libnvidia-ml.so.1')


class Nvml:
    """A minimal NVML client. ``loader`` returns the library (or a test double)."""

    def __init__(self, loader: Callable[[], Any] = _default_nvml_loader) -> None:
        self.loader = loader
        self.lib: Any = None
        self.unavailable = False  # no library, or no init entry point
        self.initialised = False
        self.failures = 0  # nvmlInit failures in a row
        self.retry_at = 0.0
        self.names: dict[str, str] = {}
        # Which ABI suffix each process-list function answered to.
        self.process_abi: dict[str, int] = {}

    def open(self, now: float | None = None) -> bool:
        if self.initialised:
            return True
        if self.unavailable:
            return False
        now = time.monotonic() if now is None else now
        if now < self.retry_at:
            return False
        if self.lib is None:
            try:
                self.lib = self.loader()
            except (OSError, AttributeError):
                # No libnvidia-ml.so.1: not looked for again in this process.
                self.unavailable = True
                return False
        init = getattr(self.lib, 'nvmlInit_v2', None) or getattr(self.lib, 'nvmlInit', None)
        if init is None:
            self.unavailable = True
            return False
        if init() != NVML_SUCCESS:
            # Often transient (a driver/library mismatch until reboot, a GPU
            # still coming up): try again later, less often each time.
            self.failures += 1
            self.retry_at = now + min(
                _NVML_RETRY_MAX_SECONDS, _NVML_RETRY_SECONDS * 2 ** (self.failures - 1))
            return False
        self.failures, self.retry_at = 0, 0.0
        self.initialised = True
        return True

    def close(self) -> None:
        if self.initialised:
            self.initialised = False
            try:
                self.lib.nvmlShutdown()
            except Exception:
                pass

    def handle(self, pci: str) -> ctypes.c_void_p | None:
        handle = ctypes.c_void_p()
        bus_id = pci.encode('ascii')
        for name in ('nvmlDeviceGetHandleByPciBusId_v2', 'nvmlDeviceGetHandleByPciBusId'):
            fn = getattr(self.lib, name, None)
            if fn is not None and fn(bus_id, ctypes.byref(handle)) == NVML_SUCCESS:
                return handle
        return None

    def name(self, pci: str, handle: ctypes.c_void_p) -> str:
        if pci not in self.names:
            buffer = ctypes.create_string_buffer(96)
            get_name = getattr(self.lib, 'nvmlDeviceGetName', None)
            ok = get_name is not None and get_name(handle, buffer, ctypes.c_uint(96)) == NVML_SUCCESS
            self.names[pci] = buffer.value.decode('utf-8', 'replace') if ok and buffer.value else ''
        return self.names[pci]

    def utilisation(self, handle: ctypes.c_void_p) -> int | None:
        rates = _NvmlUtilization()
        if self.lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(rates)) != NVML_SUCCESS:
            return None
        return max(0, min(100, int(rates.gpu)))

    def memory(self, handle: ctypes.c_void_p) -> tuple[int, int] | None:
        info = _NvmlMemory()
        if self.lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(info)) != NVML_SUCCESS:
            return None
        return int(info.used), int(info.total)

    def _process_list(self, base: str, handle: ctypes.c_void_p) -> dict[int, int] | None:
        start = self.process_abi.get(base, 0)
        for index in range(start, len(_PROCESS_ABIS)):
            suffix, struct = _PROCESS_ABIS[index]
            fn = getattr(self.lib, base + suffix, None)
            if fn is None:
                continue
            capacity = 32
            for _attempt in range(3):
                count = ctypes.c_uint(capacity)
                array = (struct * capacity)()
                result = fn(handle, ctypes.byref(count), array)
                if result == NVML_ERROR_INSUFFICIENT_SIZE:
                    capacity = max(capacity * 2, int(count.value) + 8)
                    continue
                break
            if result == NVML_ERROR_FUNCTION_NOT_FOUND:
                continue
            self.process_abi[base] = index
            if result != NVML_SUCCESS:
                return None
            ans: dict[int, int] = {}
            for entry in array[:min(int(count.value), capacity)]:
                used = int(entry.usedGpuMemory)
                if entry.pid > 0 and used != NVML_VALUE_NOT_AVAILABLE:
                    ans[int(entry.pid)] = max(ans.get(int(entry.pid), 0), used)
            return ans
        return None

    def processes(self, handle: ctypes.c_void_p) -> dict[int, int]:
        """PID -> bytes on this device. A process in both the compute and the
        graphics list reports the same allocation in each, so take the larger."""
        ans: dict[int, int] = {}
        for base in _PROCESS_LISTS:
            for pid, used in (self._process_list(base, handle) or {}).items():
                ans[pid] = max(ans.get(pid, 0), used)
        return ans


_NVML = Nvml()


def set_nvml_loader(loader: Callable[[], Any] | None) -> Nvml:
    """Replace the NVML client (tests use a fake library)."""
    global _NVML
    _NVML.close()
    _NVML = Nvml(loader or _default_nvml_loader)
    return _NVML

# }}}


# Sampling {{{

class _Backoff(NamedTuple):
    until: float
    device: GpuDevice  # the reading shown while the GPU is left alone
    processes: Mapping[int, int]  # its per-process memory at that reading


_BACKOFF: dict[str, _Backoff] = {}
# What the previous read of each GPU saw: its per-process memory (NVML) or its
# VRAM in use (sysfs). Kept while a GPU sleeps, so that a job which wakes it
# is seen as activity on the first read after.
_LAST_ACTIVITY: dict[str, Any] = {}
_UNSEEN = object()
_READ_EPOCH: dict[str, int] = {}
_CACHE: GpuSnapshot = EMPTY_SNAPSHOT
_CACHE_UNTIL = 0.0
_GENERATION = 0  # bumped by every sample, for the per-refresh pane cache
_LAST_ERROR = ''
SAMPLE_COUNT = 0  # samples taken, for cost measurement


def _log_once(message: str) -> None:
    """Log an error once, not on every 1.5 s sample while it persists."""
    global _LAST_ERROR
    if message != _LAST_ERROR:
        _LAST_ERROR = message
        log_error(message)


def _vendor_name(device: DrmDevice) -> str:
    return _VENDORS.get(device.vendor_id, f'{device.vendor_id:04x}')


def _basic_name(device: DrmDevice) -> str:
    return f'{_vendor_name(device)} GPU {device.vendor_id:04x}:{device.device_id:04x}'


def _asleep_device(device: DrmDevice, name: str = '') -> GpuDevice:
    return GpuDevice(device.pci, device.vendor_id, device.device_id, device.driver,
                     name or _basic_name(device), device.discrete, True, None, None, None, 'asleep')


def _sysfs_device(device: DrmDevice) -> GpuDevice:
    busy = _read_int(os.path.join(device.sysfs, 'gpu_busy_percent'))
    used = _read_int(os.path.join(device.sysfs, 'mem_info_vram_used'))
    total = _read_int(os.path.join(device.sysfs, 'mem_info_vram_total'))
    if busy is not None:
        busy = max(0, min(100, busy))
    source = 'sysfs' if busy is not None or used is not None else 'none'
    return GpuDevice(device.pci, device.vendor_id, device.device_id, device.driver,
                     _basic_name(device), device.discrete, False, busy, used, total, source)


def _no_counter_device(device: DrmDevice, source: str = 'none') -> GpuDevice:
    return GpuDevice(device.pci, device.vendor_id, device.device_id, device.driver,
                     _NVML.names.get(device.pci, '') or _basic_name(device),
                     device.discrete, False, None, None, None, source)


def _nvml_device(device: DrmDevice) -> tuple[GpuDevice, dict[int, int]]:
    """Read one awake NVIDIA GPU and its per-process memory."""
    handle = _NVML.handle(device.pci)
    if handle is None:
        return _no_counter_device(device), {}
    name = _NVML.name(device.pci, handle) or _basic_name(device)
    busy = _NVML.utilisation(handle)
    memory = _NVML.memory(handle)
    processes = _NVML.processes(handle)
    used, total = memory if memory is not None else (None, None)
    return GpuDevice(device.pci, device.vendor_id, device.device_id, device.driver, name,
                     True, False, busy, used, total, 'nvml'), processes


def _same_activity(previous: Any, current: Any) -> bool:
    if isinstance(current, Mapping):
        if not isinstance(previous, Mapping) or previous.keys() != current.keys():
            return False  # a process started or stopped using the GPU
        return all(abs(previous[pid] - used) < _ACTIVITY_TOLERANCE for pid, used in current.items())
    if previous is None or current is None:
        return previous is current
    return abs(previous - current) < _ACTIVITY_TOLERANCE


def _after_read(device: DrmDevice, control: str, reading: GpuDevice, activity: Any,
                processes: Mapping[int, int], now: float) -> None:
    """Start (or renew) the backoff of a runtime-PM GPU that reads idle."""
    previous = _LAST_ACTIVITY.get(device.pci, _UNSEEN)
    _LAST_ACTIVITY[device.pci] = activity
    if control == 'on':
        _BACKOFF.pop(device.pci, None)
        return
    # Idle: nothing busy, and no process's GPU memory moved. Being listed is
    # not activity: a display server keeps a few MiB resident at 0%.
    idle = not reading.utilisation and (previous is _UNSEEN or _same_activity(previous, activity))
    if idle:
        _BACKOFF[device.pci] = _Backoff(now + backoff_seconds(device), reading, dict(processes))
    else:
        _BACKOFF.pop(device.pci, None)


def _add_processes(total: dict[int, int], processes: Mapping[int, int]) -> None:
    for pid, used in processes.items():
        total[pid] = total.get(pid, 0) + used


def _sample() -> GpuSnapshot:
    global SAMPLE_COUNT
    SAMPLE_COUNT += 1
    devices = drm_devices()
    if not devices:
        _NVML.close()
        return EMPTY_SNAPSHOT
    now = time.monotonic()
    slots: list[GpuDevice | None] = []
    process_memory: dict[int, int] = {}
    fdinfo_nodes: dict[str, str] = {}
    controls: dict[str, str] = {}
    nvml_targets: list[tuple[int, DrmDevice, str]] = []
    nvidia_asleep = False
    for device in devices:
        if is_asleep(device):
            # Nothing else is read from a sleeping GPU: not NVML, not its
            # counters, not its clients' fdinfo.
            _BACKOFF.pop(device.pci, None)
            nvidia_asleep = nvidia_asleep or device.is_nvidia
            slots.append(_asleep_device(device, _NVML.names.get(device.pci, '')))
            continue
        control = controls[device.pci] = runtime_control(device)
        held = _BACKOFF.get(device.pci)
        if held is not None and now < held.until:
            # Idle and runtime-PM: show the last reading, read nothing.
            slots.append(held.device)
            _add_processes(process_memory, held.processes)
            if not device.is_nvidia:
                for node in device.nodes:
                    fdinfo_nodes[node] = device.pci
        elif device.is_nvidia:
            nvml_targets.append((len(slots), device, control))
            slots.append(None)
        else:
            reading = _sysfs_device(device)
            _READ_EPOCH[device.pci] = _READ_EPOCH.get(device.pci, 0) + 1
            slots.append(reading)
            for node in device.nodes:
                fdinfo_nodes[node] = device.pci
            _after_read(device, control, reading, reading.memory_used, {}, now)
    if nvml_targets and nvidia_asleep:
        # nvmlInit attaches every NVIDIA GPU, opening each /dev/nvidiaN, and
        # that resumes a sleeping one. Leave NVML closed until all are awake.
        _NVML.close()
        for position, device, _control in nvml_targets:
            slots[position] = _no_counter_device(device, 'held')
    elif nvml_targets:
        available = _NVML.open(now)
        try:
            for position, device, control in nvml_targets:
                if not available:
                    slots[position] = _no_counter_device(device)
                    continue
                try:
                    reading, processes = _nvml_device(device)
                except Exception as error:
                    _log_once(f'Failed to read kilix GPU {device.pci}: {error}')
                    slots[position] = reading = _no_counter_device(device)
                    if control != 'on':
                        # A broken read is not retried at every refresh either.
                        _BACKOFF[device.pci] = _Backoff(now + backoff_seconds(device), reading, {})
                    continue
                slots[position] = reading
                _add_processes(process_memory, processes)
                _after_read(device, control, reading, processes, processes, now)
        finally:
            # An open NVML session holds every NVIDIA GPU awake, so it is kept
            # between samples only while every NVIDIA GPU is pinned on.
            if not all(controls.get(d.pci) == 'on' for d in devices if d.is_nvidia):
                _NVML.close()
    elif not all(controls.get(d.pci) == 'on' for d in devices if d.is_nvidia):
        _NVML.close()
    epochs = {pci: _READ_EPOCH.get(pci, 0) for pci in set(fdinfo_nodes.values())}
    return GpuSnapshot(tuple(d for d in slots if d is not None), process_memory, fdinfo_nodes, epochs)


def gpu_snapshot(force: bool = False) -> GpuSnapshot:
    """Return the cached sample, refreshing it at most every 1.5 s."""
    global _CACHE, _CACHE_UNTIL, _GENERATION
    now = time.monotonic()
    if force or now >= _CACHE_UNTIL:
        try:
            _CACHE = _sample()
        except Exception as error:
            _log_once(f'Failed to sample kilix GPU use: {error}')
            _CACHE = EMPTY_SNAPSHOT
        _GENERATION += 1
        _CACHE_UNTIL = now + _CACHE_SECONDS
    return _CACHE


def reset_state() -> None:
    """Forget every cached reading (tests and a settings change)."""
    global _CACHE, _CACHE_UNTIL, _DEVICE_LIST, _DEVICE_LIST_ROOT, _DEVICE_LIST_UNTIL, _LAST_ERROR
    _CACHE, _CACHE_UNTIL = EMPTY_SNAPSHOT, 0.0
    _DEVICE_LIST, _DEVICE_LIST_ROOT, _DEVICE_LIST_UNTIL = (), '', 0.0
    _BACKOFF.clear()
    _LAST_ACTIVITY.clear()
    _READ_EPOCH.clear()
    _FDINFO_CACHE.clear()
    _PANE_BYTES.clear()
    _LAST_ERROR = ''


def primary_device(devices: Iterable[GpuDevice]) -> GpuDevice | None:
    """The GPU the bar shows: a discrete GPU first, then the larger VRAM."""
    best: GpuDevice | None = None
    best_key: tuple[int, int, int] = (-1, -1, -1)
    for device in devices:
        if not (device.readable or device.asleep):
            continue
        key = (int(device.discrete), int(device.readable), device.memory_total or 0)
        if key > best_key:
            best, best_key = device, key
    return best

# }}}


# DRM fdinfo {{{

_SIZE_UNITS = {'': 1, 'B': 1, 'KiB': KIB, 'MiB': MIB, 'GiB': GIB, 'kB': KIB}
_MEMORY_PREFIXES = ('drm-resident-', 'drm-total-', 'drm-memory-')


def _size(text: str) -> int | None:
    parts = text.split()
    if not parts:
        return None
    try:
        value = int(parts[0])
    except ValueError:
        return None
    unit = parts[1] if len(parts) > 1 else ''
    multiplier = _SIZE_UNITS.get(unit)
    return None if multiplier is None else max(0, value) * multiplier


def parse_drm_fdinfo(text: str) -> DrmClient | None:
    """Parse one /proc/<pid>/fdinfo/<fd> of a DRM file.

    Per memory region the resident size is preferred, then the total, then
    the legacy amdgpu ``drm-memory-``. When a client reports device-local
    memory (``vram*`` or ``local*``) only that counts; an integrated GPU has
    none, so its system-memory regions are what it holds."""
    driver = pdev = client_id = ''
    regions: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(':')
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == 'drm-driver':
            driver = value
        elif key == 'drm-pdev':
            pdev = value
        elif key == 'drm-client-id':
            client_id = value
        else:
            for prefix in _MEMORY_PREFIXES:
                if key.startswith(prefix):
                    region = key[len(prefix):]
                    # drm-total-cycles-<engine> is time, not memory.
                    if region and not region.startswith('cycles-'):
                        size = _size(value)
                        if size is not None:
                            regions.setdefault(region, {})[prefix] = size
                    break
    if not driver:
        return None
    local = {name: values for name, values in regions.items()
             if name.startswith(('vram', 'local'))}
    counted = local or regions
    total = 0
    for values in counted.values():
        for prefix in _MEMORY_PREFIXES:
            if prefix in values:
                total += values[prefix]
                break
    return DrmClient(driver, pdev, client_id, total)


# (pid, PCI address) -> (process start time, device read epoch, ((client
# key, bytes), ...)): the DRM clients a process held on a device when its
# fdinfo was last read.
_FDINFO_CACHE: dict[tuple[int, str], tuple[int, int, tuple[tuple[tuple[str, str], int], ...]]] = {}


def fdinfo_bytes(pids: Iterable[int], nodes: Mapping[str, str],
                 epochs: Mapping[str, int] | None = None, samples: Mapping[int, Any] | None = None) -> int:
    """GPU memory the given processes hold through DRM fds on ``nodes``.

    With ``epochs``, a process's fdinfo for a device is read again only when
    the device's counters were read since (see GpuSnapshot.read_epochs); in
    between, the values from that last read are used."""
    if not nodes:
        return 0
    proc = _proc_root()
    by_pci = {device.pci: device for device in drm_devices()}
    awake: dict[str, bool] = {}
    clients: dict[tuple[str, str], int] = {}
    for pid in pids:
        fd_dir = os.path.join(proc, str(pid), 'fd')
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        start = getattr((samples or {}).get(pid), 'start_ticks', 0)
        by_device: dict[str, list[str]] = {}
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if not target.startswith('/dev/dri/'):
                continue
            node = target[len('/dev/dri/'):]
            pci = nodes.get(node)
            if pci is None:
                continue  # an NVIDIA (NVML-counted), sleeping or unknown device
            by_device.setdefault(pci, []).append(fd)
        for pci, device_fds in by_device.items():
            epoch = -1 if epochs is None else epochs.get(pci, 0)
            cached = _FDINFO_CACHE.get((pid, pci))
            if cached is not None and epoch >= 0 and cached[:2] == (start, epoch):
                clients.update(cached[2])
                continue
            if pci not in awake:
                # The node map can be 1.5 s old: check again just before reading.
                device = by_pci.get(pci)
                awake[pci] = device is not None and not is_asleep(device)
            if not awake[pci]:
                continue
            entries: list[tuple[tuple[str, str], int]] = []
            for fd in device_fds:
                text = _read(os.path.join(proc, str(pid), 'fdinfo', fd), 16384)
                client = parse_drm_fdinfo(text or '')
                if client is None:
                    continue
                # Duplicated and inherited fds share one DRM client.
                key = (client.pdev or pci, client.client_id or f'{pid}/{fd}')
                entries.append((key, client.memory_bytes))
            _FDINFO_CACHE[(pid, pci)] = (start, epoch, tuple(entries))
            clients.update(entries)
    return sum(clients.values())

# }}}


# Formatting {{{

def _normalise(mode: str, modes: tuple[str, ...], default: str) -> str:
    mode = mode.strip().lower()
    return mode if mode in modes else default


def format_pane_gpu(gpu_bytes: int | None, mode: str = PANE_GPU_MODE_DEFAULT) -> str:
    """The pane chip label, or '' when the policy hides it."""
    mode = _normalise(mode, PANE_GPU_MODES, PANE_GPU_MODE_DEFAULT)
    if mode == 'off' or gpu_bytes is None:
        return ''
    value = max(0, int(gpu_bytes))
    if mode == 'auto' and value <= PANE_GPU_THRESHOLD:
        return ''
    return format_pane_memory(value, 'always')


def pane_gpu_text(label: str) -> str:
    return f' {GPU_GLYPH} {label} ' if label else ''


def format_system_gpu(device: GpuDevice | None, mode: str = SYSTEM_GPU_MODE_DEFAULT) -> str:
    """Utilisation left of the GPU glyph and VRAM in use right of it, in the
    same grammar as the CPU/RAM readout."""
    mode = _normalise(mode, SYSTEM_GPU_MODES, SYSTEM_GPU_MODE_DEFAULT)
    if mode == 'off' or device is None:
        return ''
    if device.asleep:
        return f' {GPU_GLYPH} {ASLEEP_TEXT} ' if mode == 'always' else ''
    busy = ''
    if device.utilisation is not None and (
            mode == 'always' or device.utilisation >= SYSTEM_GPU_BUSY_THRESHOLD):
        busy = f'{device.utilisation}%'
    memory = ''
    if device.memory_used is not None and (
            mode == 'always' or device.memory_used > PANE_GPU_THRESHOLD):
        memory = format_pane_memory(device.memory_used, 'always')
    if memory:
        return f' {busy} {GPU_GLYPH} {memory} ' if busy else f' {GPU_GLYPH} {memory} '
    return f' {busy} {GPU_GLYPH} ' if busy else ''


def _gpu_chip_color() -> int:
    from .rgb import color_from_int
    from .utils import color_as_int
    return (color_as_int(color_from_int(GPU_CHIP_RGB)) << 8) | 2

# }}}


# Chrome entry points {{{

def system_gpu_text(force: bool = False) -> str:
    mode = system_gpu_mode()
    if mode == 'off':
        return ''
    return format_system_gpu(primary_device(gpu_snapshot(force).devices), mode)


def system_gpu_segment(text: str | None = None) -> tuple[str, str | None, int] | None:
    if text is None:
        text = system_gpu_text()
    if not text:
        return None
    action = None if sys.platform == 'win32' else GPU_WIDGET_ACTION
    return text, action, _gpu_chip_color()


# Pane GPU bytes by pane root PID, for one GPU sample and one process scan:
# title-bar redraws between two refreshes reuse them instead of re-listing
# every fd of the pane tree.
_PANE_BYTES: dict[int, int | None] = {}
_PANE_KEY: tuple[int, float] = (0, 0.0)


def pane_gpu_bytes(root_pid: int) -> int | None:
    """GPU memory held by a pane's process tree, or None with no GPU."""
    global _PANE_KEY
    try:
        root_pid = int(root_pid)
    except (TypeError, ValueError):
        return None
    if root_pid <= 0:
        return None
    snapshot = gpu_snapshot()
    if not snapshot.process_memory and not snapshot.fdinfo_nodes:
        return 0 if snapshot.devices else None
    from . import kilix_memory
    samples = kilix_memory._refresh_process_cache()
    key = (_GENERATION, kilix_memory._PROCESS_CACHE_UNTIL)
    if key != _PANE_KEY:
        _PANE_KEY = key
        _PANE_BYTES.clear()
        for stale in [k for k in _FDINFO_CACHE if k[0] not in samples]:
            del _FDINFO_CACHE[stale]
    if root_pid in _PANE_BYTES:
        return _PANE_BYTES[root_pid]
    pids = kilix_memory._pane_processes(root_pid, samples)
    total = sum(snapshot.process_memory.get(pid, 0) for pid in pids)
    if snapshot.fdinfo_nodes:
        total += fdinfo_bytes(pids, snapshot.fdinfo_nodes, snapshot.read_epochs, samples)
    _PANE_BYTES[root_pid] = total
    return total


def pane_gpu_label(root_pid: int) -> str:
    mode = pane_gpu_mode()
    if mode == 'off':
        return ''
    return format_pane_gpu(pane_gpu_bytes(root_pid), mode)


def pane_gpu_segment(root_pid: int) -> tuple[str, str] | None:
    text = pane_gpu_text(pane_gpu_label(root_pid))
    return (text, GPU_WIDGET_ACTION) if text else None

# }}}


# The click card {{{

def _pci_ids_name(vendor_id: int, device_id: int) -> str:
    """Look a device up in the system pci.ids (only for the card)."""
    for path in ('/usr/share/misc/pci.ids', '/usr/share/hwdata/pci.ids'):
        try:
            stream = open(path, encoding='utf-8', errors='replace')
        except OSError:
            continue
        vendor_tag, device_tag = f'{vendor_id:04x}  ', f'\t{device_id:04x}  '
        in_vendor = False
        with stream:
            for line in stream:
                if line.startswith('#') or not line.strip():
                    continue
                if not line.startswith('\t'):
                    if in_vendor:
                        break
                    in_vendor = line.startswith(vendor_tag)
                elif in_vendor and line.startswith(device_tag):
                    return line[len(device_tag):].strip()
    return ''


def _memory_text(value: int | None) -> str:
    if value is None:
        return '?'
    return f'{value / GIB:.1f} GiB' if value >= GIB else f'{value / MIB:.0f} MiB'


def card_lines(snapshot: GpuSnapshot | None = None) -> tuple[str, ...]:
    snapshot = snapshot if snapshot is not None else gpu_snapshot(force=True)
    if not snapshot.devices:
        return ('GPU', '', 'No readable GPU', '', 'Right-click for GPU settings · Esc close')
    shown = primary_device(snapshot.devices)
    lines = ['GPU', '']
    for device in snapshot.devices:
        name = device.name
        if device.source != 'nvml':
            name = _pci_ids_name(device.vendor_id, device.device_id) or name
        lines.append(('▶ ' if device is shown else '  ') + name)
        if device.asleep:
            detail = 'asleep (runtime suspended; not read, so not woken)'
        elif device.source == 'held':
            detail = 'not read: another NVIDIA GPU is asleep, and NVML would wake it'
        elif not device.readable:
            detail = 'no utilisation or VRAM counter'
        else:
            busy = '?' if device.utilisation is None else f'{device.utilisation}%'
            memory = _memory_text(device.memory_used)
            if device.memory_total:
                memory += f' / {_memory_text(device.memory_total)}'
            detail = f'busy {busy} · VRAM {memory}'
        lines.append(detail)
        lines.append(f'{device.driver or "no driver"} · {device.pci}')
        lines.append('')
    lines.append('▶ is shown in the top bar · Right-click for settings · Esc close')
    return tuple(lines)

# }}}

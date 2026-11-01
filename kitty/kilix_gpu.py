#!/usr/bin/env python
"""Whole-system and per-pane GPU use for the Kilix top bar and pane chips.

Data comes from three places, all read without new dependencies:

* ``/sys/class/drm/card*`` lists the GPUs, their PCI address, driver and
  runtime power state. A device whose ``power/runtime_status`` is
  ``suspended`` is reported asleep and nothing else is read from it: reading
  a vendor counter (NVML, or amdgpu's sysfs files) resumes a sleeping GPU.
* NVIDIA devices are read through NVML (``libnvidia-ml.so.1``) with ctypes:
  utilisation, memory, and the per-process memory lists. ``nvidia-smi`` is
  never spawned.
* AMD devices are read from ``gpu_busy_percent`` / ``mem_info_vram_*`` where
  they exist; AMD and Intel per-process memory comes from DRM fdinfo, read
  only for a pane's own process tree and only for fds that point at
  ``/dev/dri``.

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
GPU_CHIP_RGB = 0x729fcf  # Tango sky blue, beside the RAM chip's Tango green
ASLEEP_TEXT = 'sleep'
_CACHE_SECONDS = 1.5  # the same cadence as the CPU/RAM readouts
_DEVICE_LIST_SECONDS = 30.0  # GPU hot-plug is rare; power state is re-read every sample
# A runtime-PM GPU (power/control=auto) that reads idle is left alone for this
# long, so a 4 s poll never holds it awake past its autosuspend delay.
_IDLE_BACKOFF_SECONDS = 30.0
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

    @property
    def is_nvidia(self) -> bool:
        return self.vendor_id == NVIDIA_VENDOR and self.driver == 'nvidia'

    @property
    def discrete(self) -> bool:
        # NVIDIA GPUs are always discrete. Otherwise an integrated GPU sits on
        # the root bus (Intel's is always 0000:00:02.0); a card is behind a bridge.
        if self.vendor_id == NVIDIA_VENDOR:
            return True
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
    source: str  # 'nvml', 'sysfs', 'asleep' or 'none'

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


class DrmClient(NamedTuple):
    driver: str
    pdev: str
    client_id: str
    memory_bytes: int


EMPTY_SNAPSHOT = GpuSnapshot((), {}, {})


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
            driver, tuple(nodes)))
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
        self.unavailable = False
        self.initialised = False
        self.names: dict[str, str] = {}
        # Which ABI suffix each process-list function answered to.
        self.process_abi: dict[str, int] = {}

    def open(self) -> bool:
        if self.initialised:
            return True
        if self.unavailable:
            return False
        if self.lib is None:
            try:
                self.lib = self.loader()
            except (OSError, AttributeError):
                self.unavailable = True
                return False
        init = getattr(self.lib, 'nvmlInit_v2', None) or getattr(self.lib, 'nvmlInit', None)
        if init is None or init() != NVML_SUCCESS:
            self.unavailable = True
            return False
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
            ok = self.lib.nvmlDeviceGetName(handle, buffer, ctypes.c_uint(96)) == NVML_SUCCESS
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

class _IdleReading(NamedTuple):
    until: float
    device: GpuDevice


_IDLE: dict[str, _IdleReading] = {}
_CACHE: GpuSnapshot = EMPTY_SNAPSHOT
_CACHE_UNTIL = 0.0
SAMPLE_COUNT = 0  # samples taken, for cost measurement


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


def _no_counter_device(device: DrmDevice) -> GpuDevice:
    return GpuDevice(device.pci, device.vendor_id, device.device_id, device.driver,
                     _basic_name(device), device.discrete, False, None, None, None, 'none')


def _nvml_device(device: DrmDevice, process_memory: dict[int, int]) -> tuple[GpuDevice, bool]:
    """Read one awake NVIDIA GPU; also return whether any process holds it."""
    handle = _NVML.handle(device.pci)
    if handle is None:
        return _no_counter_device(device), False
    name = _NVML.name(device.pci, handle) or _basic_name(device)
    busy = _NVML.utilisation(handle)
    memory = _NVML.memory(handle)
    processes = _NVML.processes(handle)
    for pid, used in processes.items():
        process_memory[pid] = process_memory.get(pid, 0) + used
    used, total = memory if memory is not None else (None, None)
    return GpuDevice(device.pci, device.vendor_id, device.device_id, device.driver, name,
                     True, False, busy, used, total, 'nvml'), bool(processes)


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
    nvml_targets: list[tuple[int, DrmDevice, str]] = []
    for device in devices:
        if is_asleep(device):
            # Nothing else is read from a sleeping GPU: not NVML, not its
            # counters, not its clients' fdinfo.
            _IDLE.pop(device.pci, None)
            slots.append(_asleep_device(device, _NVML.names.get(device.pci, '')))
        elif device.is_nvidia:
            idle = _IDLE.get(device.pci)
            if idle is not None and now < idle.until:
                slots.append(idle.device)
            else:
                nvml_targets.append((len(slots), device, runtime_control(device)))
                slots.append(None)
        else:
            slots.append(_sysfs_device(device))
            for node in device.nodes:
                fdinfo_nodes[node] = device.pci
    if nvml_targets:
        available = _NVML.open()
        for position, device, control in nvml_targets:
            if not available:
                slots[position] = _no_counter_device(device)
                continue
            reading, busy_processes = _nvml_device(device, process_memory)
            slots[position] = reading
            if control == 'auto' and reading.utilisation == 0 and not busy_processes:
                _IDLE[device.pci] = _IdleReading(now + _IDLE_BACKOFF_SECONDS, reading)
            else:
                _IDLE.pop(device.pci, None)
        # An open NVML session keeps a runtime-PM GPU from suspending, so it
        # is kept between samples only when every GPU read is pinned on.
        if any(control != 'on' for _, _, control in nvml_targets):
            _NVML.close()
    return GpuSnapshot(tuple(d for d in slots if d is not None), process_memory, fdinfo_nodes)


def gpu_snapshot(force: bool = False) -> GpuSnapshot:
    """Return the cached sample, refreshing it at most every 1.5 s."""
    global _CACHE, _CACHE_UNTIL
    now = time.monotonic()
    if force or now >= _CACHE_UNTIL:
        try:
            _CACHE = _sample()
        except Exception as error:
            log_error(f'Failed to sample kilix GPU use: {error}')
            _CACHE = EMPTY_SNAPSHOT
        _CACHE_UNTIL = now + _CACHE_SECONDS
    return _CACHE


def reset_state() -> None:
    """Forget every cached reading (tests and a settings change)."""
    global _CACHE, _CACHE_UNTIL, _DEVICE_LIST, _DEVICE_LIST_ROOT, _DEVICE_LIST_UNTIL
    _CACHE, _CACHE_UNTIL = EMPTY_SNAPSHOT, 0.0
    _DEVICE_LIST, _DEVICE_LIST_ROOT, _DEVICE_LIST_UNTIL = (), '', 0.0
    _IDLE.clear()


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


def fdinfo_bytes(pids: Iterable[int], nodes: Mapping[str, str]) -> int:
    """GPU memory the given processes hold through DRM fds on ``nodes``."""
    if not nodes:
        return 0
    proc = _proc_root()
    seen: set[tuple[str, str]] = set()
    total = 0
    for pid in pids:
        fd_dir = os.path.join(proc, str(pid), 'fd')
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
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
            text = _read(os.path.join(proc, str(pid), 'fdinfo', fd), 16384)
            client = parse_drm_fdinfo(text or '')
            if client is None:
                continue
            # Duplicated and inherited fds share one DRM client.
            key = (client.pdev or pci, client.client_id or f'{pid}/{fd}')
            if key in seen:
                continue
            seen.add(key)
            total += client.memory_bytes
    return total

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


def pane_gpu_bytes(root_pid: int) -> int | None:
    """GPU memory held by a pane's process tree, or None with no GPU."""
    try:
        root_pid = int(root_pid)
    except (TypeError, ValueError):
        return None
    if root_pid <= 0:
        return None
    snapshot = gpu_snapshot()
    if not snapshot.process_memory and not snapshot.fdinfo_nodes:
        return 0 if snapshot.devices else None
    from .kilix_memory import _pane_processes, _refresh_process_cache
    samples = _refresh_process_cache()
    pids = _pane_processes(root_pid, samples)
    total = sum(snapshot.process_memory.get(pid, 0) for pid in pids)
    if snapshot.fdinfo_nodes:
        total += fdinfo_bytes(pids, snapshot.fdinfo_nodes)
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
            detail = 'asleep (runtime suspended; not woken)'
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

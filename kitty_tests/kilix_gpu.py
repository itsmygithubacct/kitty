import ctypes
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from kitty import kilix_gpu as gpu
from kitty import kilix_memory
from kitty import kilix_system_load as system
from kitty.kilix_memory import GIB, MEMORY_GLYPH, MEMORY_WIDGET_ACTION, MIB
from kitty.types import WindowGeometry
from kitty.window_title_bar import WindowTitleBarScreen, WindowTitleData, pane_resource_text

from . import BaseTest

G = gpu.GPU_GLYPH
INTEL = ('0000:00:02.0', 0x8086, 0x1002, 'i915', ('card0', 'renderD128'))
NVIDIA = ('0000:01:00.0', 0x10de, 0x1001, 'nvidia', ('card1', 'renderD129'))
AMD = ('0000:03:00.0', 0x1002, 0x73bf, 'amdgpu', ('card2', 'renderD130'))


def make_gpu(root: Path, pci: str, vendor: int, device: int, driver: str,
             nodes: tuple[str, ...], status: str = 'active', control: str = 'on',
             files: dict[str, str] | None = None) -> Path:
    """A sysfs PCI function and its DRM nodes, linked the way the kernel links them."""
    dev = root / 'devices' / 'pci0000:00' / pci
    (dev / 'power').mkdir(parents=True)
    (dev / 'vendor').write_text(f'0x{vendor:04x}\n')
    (dev / 'device').write_text(f'0x{device:04x}\n')
    (dev / 'power' / 'runtime_status').write_text(status + '\n')
    (dev / 'power' / 'control').write_text(control + '\n')
    drivers = root / 'bus' / 'pci' / 'drivers' / driver
    drivers.mkdir(parents=True, exist_ok=True)
    os.symlink(drivers, dev / 'driver')
    for name, value in (files or {}).items():
        (dev / name).write_text(value + '\n')
    drm = root / 'class' / 'drm'
    drm.mkdir(parents=True, exist_ok=True)
    for node in nodes:
        (drm / node).mkdir()
        os.symlink(dev, drm / node / 'device')
    # A connector shares the device but is not a GPU of its own.
    (drm / f'{nodes[0]}-DP-1').mkdir()
    os.symlink(dev, drm / f'{nodes[0]}-DP-1' / 'device')
    return dev


def set_status(dev: Path, status: str) -> None:
    (dev / 'power' / 'runtime_status').write_text(status + '\n')


class FakeNvml:
    """Enough of libnvidia-ml for kilix_gpu, keyed by PCI bus id."""

    STRUCT_SIZES = {'_v3': 24, '_v2': 24, '': 16}

    def __init__(self, devices: dict[str, dict[str, Any]], versions: tuple[str, ...] = ('_v3', '_v2', ''),
                 not_found: tuple[str, ...] = ()) -> None:
        self.devices = devices
        self.order = list(devices)
        self.versions = versions
        self.not_found = not_found
        self.calls: list[str] = []
        self.init_results: list[int] = []  # nvmlInit return codes, then success
        self.fail: str = ''  # a method that raises, as a broken library would

    def __getattr__(self, name: str) -> Any:
        for base in gpu._PROCESS_LISTS:
            if name.startswith(base):
                suffix = name[len(base):]
                if suffix in self.versions:
                    return lambda handle, count, array: self._processes(name, base, suffix, handle, count, array)
        raise AttributeError(name)

    def _device(self, handle: ctypes.c_void_p) -> dict[str, Any]:
        return self.devices[self.order[int(handle.value or 0) - 1]]

    def nvmlInit_v2(self) -> int:
        self.calls.append('init')
        return self.init_results.pop(0) if self.init_results else 0

    def nvmlShutdown(self) -> int:
        self.calls.append('shutdown')
        return 0

    def nvmlDeviceGetHandleByPciBusId_v2(self, bus_id: bytes, handle: Any) -> int:
        self.calls.append(f'handle {bus_id.decode()}')
        if bus_id.decode() not in self.devices:
            return 13
        handle._obj.value = self.order.index(bus_id.decode()) + 1
        return 0

    def nvmlDeviceGetName(self, handle: ctypes.c_void_p, buffer: Any, length: Any) -> int:
        buffer.value = self._device(handle).get('name', 'Fake GPU').encode()
        return 0

    def nvmlDeviceGetUtilizationRates(self, handle: ctypes.c_void_p, rates: Any) -> int:
        self.calls.append('utilisation')
        if self.fail == 'utilisation':
            raise OSError('nvmlDeviceGetUtilizationRates: broken library')
        if self._device(handle).get('util', 0) is None:
            return 3  # NVML_ERROR_NOT_SUPPORTED
        rates._obj.gpu = self._device(handle).get('util', 0)
        return 0

    def nvmlDeviceGetMemoryInfo(self, handle: ctypes.c_void_p, info: Any) -> int:
        device = self._device(handle)
        info._obj.used, info._obj.total = device.get('used', 0), device.get('total', 6 * GIB)
        return 0

    def _processes(self, name: str, base: str, suffix: str, handle: ctypes.c_void_p, count: Any, array: Any) -> int:
        self.calls.append(name)
        if name in self.not_found:
            return gpu.NVML_ERROR_FUNCTION_NOT_FOUND
        # The caller must use the struct layout this ABI writes. (Not an
        # assert: the test launcher may run Python with asserts stripped.)
        if ctypes.sizeof(array._type_) != self.STRUCT_SIZES[suffix]:
            raise ValueError(f'{name} given a {ctypes.sizeof(array._type_)}-byte struct')
        key = 'compute' if 'Compute' in base else 'graphics'
        entries = self._device(handle).get(key, ())
        if count._obj.value < len(entries):
            count._obj.value = len(entries)
            return gpu.NVML_ERROR_INSUFFICIENT_SIZE
        for i, (pid, used) in enumerate(entries):
            array[i].pid, array[i].usedGpuMemory = pid, used
        count._obj.value = len(entries)
        return 0


def write_process(proc: Path, pid: int, ppid: int) -> Path:
    directory = proc / str(pid)
    (directory / 'fd').mkdir(parents=True)
    (directory / 'fdinfo').mkdir()
    fields = ['S', str(ppid), str(pid), str(pid), *(['0'] * 17), '100']
    (directory / 'stat').write_text(f"{pid} (pane worker) {' '.join(fields)}\n")
    return directory


def add_fd(process: Path, fd: int, target: str, fdinfo: str = '') -> None:
    os.symlink(target, process / 'fd' / str(fd))
    (process / 'fdinfo' / str(fd)).write_text(fdinfo or 'pos:\t0\nflags:\t02\n')


I915_FDINFO = '''pos:\t0
flags:\t02100002
drm-driver:\ti915
drm-client-id:\t{client}
drm-pdev:\t0000:00:02.0
drm-engine-render:\t9288864723 ns
drm-total-system0:\t{total} KiB
drm-shared-system0:\t0
drm-resident-system0:\t{resident} KiB
drm-total-stolen-system0:\t0
drm-resident-stolen-system0:\t0
'''


class GpuTestCase(BaseTest):

    def setUp(self) -> None:
        super().setUp()
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sys = self.root / 'sys'
        self.sys.mkdir()
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.env = patch.dict(os.environ, {
            'KILIX_GPU_SYSFS_ROOT': str(self.sys),
            'KILIX_MEMORY_PROC_ROOT': str(self.proc),
            'GPU_TERMINAL_SETTINGS_FILE': str(self.root / 'missing.conf'),
        })
        self.env.start()
        # The real call posts to glfw, which tests never initialise.
        wake = patch('kitty.fast_data_types.wakeup_main_loop')
        self.wake = wake.start()
        self.addCleanup(wake.stop)
        for key in (gpu.SYSTEM_GPU_MODE_KEY, gpu.PANE_GPU_MODE_KEY):
            os.environ.pop(key, None)
        gpu.reset_state()
        self.nvml: FakeNvml | None = None
        self.loads = 0
        gpu.set_nvml_loader(self._load)
        kilix_memory._PROCESS_CACHE_UNTIL = 0
        kilix_memory._PROCESS_CACHE_ROOT = ''

    def tearDown(self) -> None:
        gpu.set_nvml_loader(None)
        gpu.reset_state()
        self.env.stop()
        self.tmp.cleanup()
        kilix_memory._PROCESS_CACHE_UNTIL = 0
        kilix_memory._PROCESS_CACHE_ROOT = ''
        system._DRAWN_TEXT.clear()
        system._DRAWN_GPU_TEXT.clear()
        super().tearDown()

    def _load(self) -> Any:
        self.loads += 1
        if self.nvml is None:
            raise OSError('libnvidia-ml.so.1: cannot open shared object file')
        return self.nvml

    def device(self, spec: tuple[Any, ...], **kw: Any) -> Path:
        return make_gpu(self.sys, *spec, **kw)

    def snapshot(self) -> gpu.GpuSnapshot:
        return gpu.gpu_snapshot(force=True)


class TestGpuFormatting(GpuTestCase):

    def test_pane_threshold_is_more_than_512_mib_in_auto(self) -> None:
        self.ae(gpu.format_pane_gpu(511 * MIB, 'auto'), '')
        self.ae(gpu.format_pane_gpu(512 * MIB, 'auto'), '')
        self.ae(gpu.format_pane_gpu(513 * MIB, 'auto'), '513M')
        self.ae(gpu.format_pane_gpu(int(1.5 * GIB), 'auto'), '1.5')
        self.ae(gpu.format_pane_gpu(64 * MIB, 'always'), '64.0M')
        self.ae(gpu.format_pane_gpu(0, 'always'), '0K')
        self.ae(gpu.format_pane_gpu(8 * GIB, 'off'), '')
        self.ae(gpu.format_pane_gpu(None, 'always'), '')
        self.ae(gpu.format_pane_gpu(513 * MIB, 'bogus'), '513M')
        self.ae(gpu.format_pane_gpu(511 * MIB, 'bogus'), '')
        self.ae(gpu.pane_gpu_text('600M'), f' {G} 600M ')
        self.ae(gpu.pane_gpu_text(''), '')

    def test_system_readout_matches_the_cpu_ram_grammar(self) -> None:
        def dev(**kw: Any) -> gpu.GpuDevice:
            values = dict(pci='0000:01:00.0', vendor_id=0x10de, device_id=1, driver='nvidia', name='n',
                          discrete=True, asleep=False, utilisation=37, memory_used=int(2.25 * GIB),
                          memory_total=6 * GIB, source='nvml')
            values.update(kw)
            return gpu.GpuDevice(**values)
        busy = dev()
        self.ae(gpu.format_system_gpu(busy, 'always'), f' 37% {G} 2.2 ')
        self.ae(gpu.format_system_gpu(busy, 'auto'), f' 37% {G} 2.2 ')
        self.ae(gpu.format_system_gpu(busy, 'off'), '')
        quiet = dev(utilisation=3, memory_used=400 * MIB)
        self.ae(gpu.format_system_gpu(quiet, 'always'), f' 3% {G} 400M ')
        self.ae(gpu.format_system_gpu(quiet, 'auto'), '')
        self.ae(gpu.format_system_gpu(dev(utilisation=10, memory_used=400 * MIB), 'auto'), f' 10% {G} ')
        self.ae(gpu.format_system_gpu(dev(utilisation=0, memory_used=513 * MIB), 'auto'), f' {G} 513M ')
        self.ae(gpu.format_system_gpu(dev(utilisation=None, memory_used=None), 'always'), '')
        self.ae(gpu.format_system_gpu(dev(utilisation=12, memory_used=None), 'always'), f' 12% {G} ')
        asleep = dev(asleep=True, utilisation=None, memory_used=None, source='asleep')
        self.ae(gpu.format_system_gpu(asleep, 'always'), f' {G} {gpu.ASLEEP_TEXT} ')
        self.ae(gpu.format_system_gpu(asleep, 'auto'), '')
        self.ae(gpu.format_system_gpu(None, 'always'), '')
        self.ae(gpu.format_system_gpu(busy, 'nonsense'), f' 37% {G} 2.2 ')

    def test_modes_default_and_validate(self) -> None:
        self.ae(gpu.system_gpu_mode(), 'always')
        self.ae(gpu.pane_gpu_mode(), 'auto')
        for value, system_expected, pane_expected in (
                ('off', 'off', 'off'), ('ALWAYS', 'always', 'always'), (' Auto ', 'auto', 'auto'),
                ('sometimes', 'always', 'auto')):
            with patch.dict(os.environ, {gpu.SYSTEM_GPU_MODE_KEY: value, gpu.PANE_GPU_MODE_KEY: value}):
                self.ae(gpu.system_gpu_mode(), system_expected)
                self.ae(gpu.pane_gpu_mode(), pane_expected)
        # The shared settings file wins over the environment, as for every chrome key.
        (self.root / 'settings.conf').write_text(f'{gpu.PANE_GPU_MODE_KEY}=always\n{gpu.SYSTEM_GPU_MODE_KEY}=off\n')
        with patch.dict(os.environ, {'GPU_TERMINAL_SETTINGS_FILE': str(self.root / 'settings.conf'),
                                     gpu.PANE_GPU_MODE_KEY: 'off'}):
            self.ae(gpu.pane_gpu_mode(), 'always')
            self.ae(gpu.system_gpu_mode(), 'off')

    def test_glyphs_are_single_cell_and_distinct_from_the_ram_chip(self) -> None:
        from kitty.fast_data_types import wcswidth
        for glyph in (G, *gpu.GPU_GLYPH_ALTERNATES):
            self.ae(wcswidth(glyph), 1)
            self.assertNotEqual(glyph, MEMORY_GLYPH)
        self.ae(G, chr(0xf08ae))
        self.assertNotEqual(gpu.GPU_CHIP_RGB, 0x8ae234)


class TestGpuLayout(GpuTestCase):

    def render(self, data: WindowTitleData) -> WindowTitleBarScreen:
        self.set_options()
        title_bar = WindowTitleBarScreen(1, 10, 20)
        title_bar.layout(WindowGeometry(0, 0, 1200, 20, 120, 1))
        with patch('kitty.window_title_bar.chrome_enabled', return_value=True):
            title_bar.render(data, '')
        return title_bar

    def columns(self, title_bar: WindowTitleBarScreen, action: str) -> list[int]:
        return sorted(c for c, a in title_bar.button_cols.items() if a == action)

    def test_pane_gpu_is_right_of_the_ram_measurement(self) -> None:
        memory_text = f' {MEMORY_GLYPH} 3.2 '
        gpu_text = gpu.pane_gpu_text('600M')
        title_bar = self.render(WindowTitleData(
            'pane', True, 1, 1, pane_memory_text=memory_text, pane_cpu_text='2.4', pane_gpu_text=gpu_text))
        resource = pane_resource_text('2.4', memory_text)
        ram = self.columns(title_bar, MEMORY_WIDGET_ACTION)
        chip = self.columns(title_bar, gpu.GPU_WIDGET_ACTION)
        # Each hit area is exactly as wide as its text, with no gap or overlap.
        self.ae(len(ram), len(resource))
        self.ae(len(chip), len(gpu_text))
        self.ae(ram, list(range(ram[0], ram[0] + len(ram))))
        self.ae(chip, list(range(ram[-1] + 1, ram[-1] + 1 + len(chip))))
        line = str(title_bar.screen.line(0))
        self.ae(line[ram[0]:chip[-1] + 1], resource + gpu_text)
        # The log button follows the GPU chip directly.
        self.ae(title_bar.button_cols.get(chip[-1] + 1), 'kilix_show_pane_log')

    def test_no_gpu_text_leaves_the_chip_row_as_before(self) -> None:
        memory_text = f' {MEMORY_GLYPH} 3.2 '
        with_gpu = self.render(WindowTitleData('pane', True, 1, 1, pane_memory_text=memory_text))
        self.ae(self.columns(with_gpu, gpu.GPU_WIDGET_ACTION), [])
        self.ae(len(self.columns(with_gpu, MEMORY_WIDGET_ACTION)), len(memory_text))
        # A GPU chip without the RAM side still stands alone and is clickable.
        alone = self.render(WindowTitleData('pane', True, 1, 1, pane_gpu_text=gpu.pane_gpu_text('1.5')))
        self.ae(len(self.columns(alone, gpu.GPU_WIDGET_ACTION)), len(f' {G} 1.5 '))
        self.ae(self.columns(alone, MEMORY_WIDGET_ACTION), [])

    def test_overlay_panes_show_no_gpu_chip(self) -> None:
        title_bar = self.render(WindowTitleData('app', True, 1, 1, is_overlay=True,
                                                pane_gpu_text=gpu.pane_gpu_text('2.0')))
        self.ae(self.columns(title_bar, gpu.GPU_WIDGET_ACTION), [])


class TestGpuSources(GpuTestCase):

    def test_nvml_reads_utilisation_memory_and_both_process_lists(self) -> None:
        self.device(INTEL)
        self.device(NVIDIA)
        self.nvml = FakeNvml({NVIDIA[0]: dict(
            name='Example NVIDIA GPU', util=41, used=2 * GIB, total=6 * GIB,
            compute=[(500, 600 * MIB), (501, 10 * MIB)], graphics=[(500, 600 * MIB), (77, 4 * MIB)])})
        snapshot = self.snapshot()
        self.ae([d.driver for d in snapshot.devices], ['i915', 'nvidia'])
        shown = gpu.primary_device(snapshot.devices)
        self.ae((shown.name, shown.utilisation, shown.memory_used, shown.source),
                ('Example NVIDIA GPU', 41, 2 * GIB, 'nvml'))
        # A process on both lists is one allocation, not two.
        self.ae(dict(snapshot.process_memory), {500: 600 * MIB, 501: 10 * MIB, 77: 4 * MIB})
        self.ae(dict(snapshot.fdinfo_nodes), {'card0': INTEL[0], 'renderD128': INTEL[0]})
        self.assertIn('nvmlDeviceGetComputeRunningProcesses_v3', self.nvml.calls)
        self.assertIn('nvmlDeviceGetGraphicsRunningProcesses_v3', self.nvml.calls)

    def test_process_lists_fall_back_from_v3_to_v2_to_v1(self) -> None:
        self.device(NVIDIA)
        lists = dict(compute=[(500, 700 * MIB)], graphics=[(77, 5 * MIB)])
        # An older driver: no _v3 symbols at all.
        self.nvml = FakeNvml({NVIDIA[0]: dict(lists)}, versions=('_v2', ''))
        self.ae(dict(self.snapshot().process_memory), {500: 700 * MIB, 77: 5 * MIB})
        self.assertIn('nvmlDeviceGetComputeRunningProcesses_v2', self.nvml.calls)
        # The symbol exists but the driver says the function is not found.
        gpu.reset_state()
        self.nvml = FakeNvml({NVIDIA[0]: dict(lists)}, not_found=(
            'nvmlDeviceGetComputeRunningProcesses_v3', 'nvmlDeviceGetGraphicsRunningProcesses_v3'))
        gpu.set_nvml_loader(self._load)
        self.ae(dict(self.snapshot().process_memory), {500: 700 * MIB, 77: 5 * MIB})
        self.assertIn('nvmlDeviceGetGraphicsRunningProcesses_v2', self.nvml.calls)
        # Once found, the working version is used directly on the next sample.
        self.nvml.calls.clear()
        self.snapshot()
        self.assertNotIn('nvmlDeviceGetComputeRunningProcesses_v3', self.nvml.calls)
        # Only the original, 16-byte ABI.
        gpu.reset_state()
        self.nvml = FakeNvml({NVIDIA[0]: dict(lists)}, versions=('',))
        gpu.set_nvml_loader(self._load)
        self.ae(dict(self.snapshot().process_memory), {500: 700 * MIB, 77: 5 * MIB})

    def test_process_list_grows_when_nvml_reports_insufficient_size(self) -> None:
        self.device(NVIDIA)
        many = [(1000 + i, (i + 1) * MIB) for i in range(80)]
        self.nvml = FakeNvml({NVIDIA[0]: dict(compute=many)})
        self.ae(len(self.snapshot().process_memory), 80)

    def test_unavailable_memory_values_are_skipped(self) -> None:
        self.device(NVIDIA)
        self.nvml = FakeNvml({NVIDIA[0]: dict(compute=[(500, gpu.NVML_VALUE_NOT_AVAILABLE), (501, 9 * MIB)])})
        self.ae(dict(self.snapshot().process_memory), {501: 9 * MIB})

    def test_amd_sysfs_counters_where_they_exist(self) -> None:
        self.device(AMD, files={'gpu_busy_percent': '64', 'mem_info_vram_used': str(3 * GIB),
                                'mem_info_vram_total': str(16 * GIB)})
        snapshot = self.snapshot()
        (device,) = snapshot.devices
        self.ae((device.utilisation, device.memory_used, device.memory_total, device.source),
                (64, 3 * GIB, 16 * GIB, 'sysfs'))
        self.ae(gpu.format_system_gpu(device), f' 64% {G} 3.0 ')
        self.ae(self.loads, 0)  # NVML is never loaded without an NVIDIA GPU

    def test_nvml_missing_degrades_to_an_unreadable_device(self) -> None:
        self.device(NVIDIA)
        self.nvml = None  # libnvidia-ml.so.1 cannot be loaded
        snapshot = self.snapshot()
        self.ae((snapshot.devices[0].readable, snapshot.devices[0].source), (False, 'none'))
        self.ae(gpu.system_gpu_text(), '')
        self.snapshot()
        self.ae(self.loads, 1)  # not retried every refresh


class TestGpuNoWake(GpuTestCase):

    def test_suspended_nvidia_gpu_is_reported_asleep_without_nvml(self) -> None:
        self.device(INTEL)
        dev = self.device(NVIDIA, status='suspended', control='auto')
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=50, used=GIB)})
        snapshot = self.snapshot()
        nvidia = snapshot.devices[1]
        self.ae((nvidia.asleep, nvidia.source, nvidia.utilisation, nvidia.memory_used), (True, 'asleep', None, None))
        # NVML was not even loaded, let alone initialised or asked.
        self.ae(self.loads, 0)
        self.ae(self.nvml.calls, [])
        self.assertIs(gpu.primary_device(snapshot.devices), nvidia)
        self.ae(gpu.system_gpu_text(), f' {G} {gpu.ASLEEP_TEXT} ')
        with patch.dict(os.environ, {gpu.SYSTEM_GPU_MODE_KEY: 'auto'}):
            self.ae(gpu.system_gpu_text(), '')
        # 'suspending' is on its way down and is left alone too.
        set_status(dev, 'suspending')
        self.snapshot()
        self.ae(self.nvml.calls, [])
        # Awake again: now it is read.
        set_status(dev, 'active')
        self.ae(gpu.primary_device(self.snapshot().devices).utilisation, 50)
        self.assertIn('utilisation', self.nvml.calls)

    def test_suspended_amd_gpu_counters_and_clients_are_not_read(self) -> None:
        self.device(AMD, status='suspended', files={'gpu_busy_percent': '64', 'mem_info_vram_used': '1'})
        read = gpu._read
        paths: list[str] = []

        def recording_read(path: str, limit: int = 4096) -> str | None:
            paths.append(path)
            return read(path, limit)
        with patch.object(gpu, '_read', recording_read):
            snapshot = self.snapshot()
        self.assertTrue(snapshot.devices[0].asleep)
        self.ae([p for p in paths if 'gpu_busy' in p or 'mem_info' in p], [])
        # Its DRM nodes are not offered for fdinfo either.
        self.ae(dict(snapshot.fdinfo_nodes), {})

    def test_runtime_pm_gpu_is_not_held_awake(self) -> None:
        self.device(NVIDIA, control='auto')
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=0, used=300 * MIB)})
        self.snapshot()
        # NVML is closed after each sample of a runtime-PM device...
        self.ae(self.nvml.calls.count('init'), 1)
        self.ae(self.nvml.calls.count('shutdown'), 1)
        # ...and an idle one is not polled again until the backoff ends.
        self.nvml.calls.clear()
        self.ae(gpu.primary_device(self.snapshot().devices).memory_used, 300 * MIB)
        self.ae(self.nvml.calls, [])
        gpu._BACKOFF[NVIDIA[0]] = gpu._BACKOFF[NVIDIA[0]]._replace(until=0.0)
        self.snapshot()
        self.assertIn('utilisation', self.nvml.calls)

    def test_pinned_on_gpu_keeps_one_nvml_session(self) -> None:
        self.device(NVIDIA, control='on')
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=0)})
        for _ in range(3):
            self.snapshot()
        self.ae((self.nvml.calls.count('init'), self.nvml.calls.count('shutdown')), (1, 0))
        self.ae(self.nvml.calls.count('utilisation'), 3)


class TestGpuChoice(GpuTestCase):

    def test_discrete_gpu_is_shown_and_the_card_lists_each_gpu(self) -> None:
        self.device(INTEL)
        self.device(NVIDIA)
        self.nvml = FakeNvml({NVIDIA[0]: dict(name='Example NVIDIA GPU', util=5, used=400 * MIB)})
        snapshot = self.snapshot()
        shown = gpu.primary_device(snapshot.devices)
        self.ae(shown.pci, NVIDIA[0])
        with patch.object(gpu, '_pci_ids_name', return_value='Example Intel iGPU'):
            lines = gpu.card_lines(snapshot)
        text = '\n'.join(lines)
        self.assertIn('▶ Example NVIDIA GPU', text)
        self.assertIn('  Example Intel iGPU', text)
        self.assertIn('busy 5% · VRAM 400 MiB / 6.0 GiB', text)
        self.assertIn('no utilisation or VRAM counter', text)
        self.assertIn(INTEL[0], text)

    def test_integrated_gpu_alone_without_counters_is_hidden(self) -> None:
        self.device(INTEL)
        snapshot = self.snapshot()
        self.assertIsNone(gpu.primary_device(snapshot.devices))
        self.ae(gpu.system_gpu_text(), '')
        self.assertIsNone(gpu.system_gpu_segment())
        self.ae(self.loads, 0)

    def test_larger_vram_wins_between_two_cards(self) -> None:
        apu = ('0000:00:08.1', 0x1002, 0x1638, 'amdgpu', ('card3', 'renderD131'))
        self.device(apu, files={'gpu_busy_percent': '3', 'mem_info_vram_used': str(100 * MIB),
                                'mem_info_vram_total': str(512 * MIB)})
        self.device(AMD, files={'gpu_busy_percent': '1', 'mem_info_vram_used': str(GIB),
                                'mem_info_vram_total': str(16 * GIB)})
        self.ae(gpu.primary_device(self.snapshot().devices).pci, AMD[0])

    def test_sleeping_discrete_gpu_is_still_the_one_shown(self) -> None:
        self.device(AMD, status='suspended')
        self.device(('0000:00:08.1', 0x1002, 0x1638, 'amdgpu', ('card3',)),
                    files={'gpu_busy_percent': '3', 'mem_info_vram_used': str(100 * MIB)})
        shown = gpu.primary_device(self.snapshot().devices)
        self.ae((shown.pci, shown.asleep), (AMD[0], True))


class TestGpuHidden(GpuTestCase):

    def test_no_gpu_is_hidden_without_loading_nvml(self) -> None:
        # A WSL or headless machine: NVML may be installed, but sysfs lists no GPU.
        self.nvml = FakeNvml({})
        self.ae(self.snapshot(), gpu.EMPTY_SNAPSHOT)
        self.ae(gpu.system_gpu_text(), '')
        self.assertIsNone(gpu.system_gpu_segment())
        self.assertIsNone(gpu.pane_gpu_bytes(os.getpid()))
        self.ae(gpu.pane_gpu_label(os.getpid()), '')
        self.ae(self.loads, 0)
        self.ae(gpu.card_lines(), ('GPU', '', 'No readable GPU', '', 'Right-click for GPU settings · Esc close'))

    def test_missing_sysfs_is_hidden(self) -> None:
        with patch.dict(os.environ, {'KILIX_GPU_SYSFS_ROOT': str(self.root / 'absent')}):
            gpu.reset_state()
            self.ae(self.snapshot(), gpu.EMPTY_SNAPSHOT)

    def test_native_windows_turns_both_readouts_off(self) -> None:
        self.device(NVIDIA)
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=90, used=4 * GIB)})
        with patch('sys.platform', 'win32'), patch.dict(os.environ, {
                gpu.SYSTEM_GPU_MODE_KEY: 'always', gpu.PANE_GPU_MODE_KEY: 'always'}):
            self.ae(gpu.system_gpu_mode(), 'off')
            self.ae(gpu.pane_gpu_mode(), 'off')
            self.ae(gpu.system_gpu_text(), '')
            self.assertIsNone(system.system_gpu_bar_segment(3))
            self.ae(gpu.pane_gpu_label(123), '')
        self.ae(self.loads, 0)

    def test_sampling_errors_are_logged_and_hide_the_widget(self) -> None:
        with patch.object(gpu, '_sample', side_effect=RuntimeError('boom')), \
                patch.object(gpu, 'log_error') as log:
            self.ae(gpu.gpu_snapshot(force=True), gpu.EMPTY_SNAPSHOT)
        log.assert_called_once()


class TestFdinfo(GpuTestCase):

    def test_i915_counts_resident_system_memory(self) -> None:
        client = gpu.parse_drm_fdinfo(I915_FDINFO.format(client=7, total=8192, resident=4096))
        self.ae(client, gpu.DrmClient('i915', '0000:00:02.0', '7', 4096 * 1024))

    def test_amdgpu_counts_only_vram_and_accepts_the_legacy_keys(self) -> None:
        legacy = 'drm-driver:\tamdgpu\ndrm-pdev:\t0000:03:00.0\ndrm-client-id:\t3\n' \
                 'drm-memory-vram:\t614400 KiB\ndrm-memory-gtt:\t2048 KiB\ndrm-memory-cpu:\t0 KiB\n'
        self.ae(gpu.parse_drm_fdinfo(legacy).memory_bytes, 600 * MIB)
        modern = legacy + 'drm-total-vram:\t700 MiB\ndrm-resident-vram:\t650 MiB\ndrm-total-gtt:\t1 GiB\n'
        self.ae(gpu.parse_drm_fdinfo(modern).memory_bytes, 650 * MIB)

    def test_xe_cycles_are_not_memory_and_units_are_honoured(self) -> None:
        text = ('drm-driver:\txe\ndrm-client-id:\t9\ndrm-total-cycles-rcs:\t28257900\n'
                'drm-cycles-rcs:\t123\ndrm-total-vram0:\t1 GiB\ndrm-resident-vram0:\t524289 KiB\n'
                'drm-total-gtt:\t5 MiB\ndrm-resident-gtt:\t5 MiB\n')
        self.ae(gpu.parse_drm_fdinfo(text).memory_bytes, 524289 * 1024)
        # An integrated GPU has no device-local region, so cycles must be
        # excluded by name, not by the local-memory rule.
        integrated = ('drm-driver:\txe\ndrm-total-cycles-rcs:\t28257900\n'
                      'drm-total-system:\t8 KiB\ndrm-resident-system:\t4 KiB\n')
        self.ae(gpu.parse_drm_fdinfo(integrated).memory_bytes, 4096)
        self.ae(gpu.parse_drm_fdinfo('drm-driver: x\ndrm-total-system: 4096\n').memory_bytes, 4096)
        self.ae(gpu.parse_drm_fdinfo('drm-driver: x\ndrm-total-system: 4 PiB\n').memory_bytes, 0)
        self.assertIsNone(gpu.parse_drm_fdinfo('pos: 0\nflags: 02\n'))
        self.assertIsNone(gpu.parse_drm_fdinfo(''))

    def test_pane_tree_fds_on_dri_nodes_only_and_clients_once(self) -> None:
        self.device(INTEL)
        self.device(NVIDIA)
        self.nvml = FakeNvml({NVIDIA[0]: dict(compute=[(101, 300 * MIB)], graphics=[(999, 4 * GIB)])})
        shell = write_process(self.proc, 100, 1)
        worker = write_process(self.proc, 101, 100)
        outsider = write_process(self.proc, 999, 1)
        add_fd(shell, 0, '/dev/pts/3')
        add_fd(worker, 3, '/dev/dri/renderD128', I915_FDINFO.format(client=11, total=300000, resident=262144))
        # A dup of the same DRM client and an inherited copy count once.
        add_fd(worker, 4, '/dev/dri/renderD128', I915_FDINFO.format(client=11, total=300000, resident=262144))
        add_fd(shell, 5, '/dev/dri/renderD128', I915_FDINFO.format(client=11, total=300000, resident=262144))
        # The NVIDIA node is NVML's: its fdinfo is never read.
        add_fd(worker, 6, '/dev/dri/renderD129', 'drm-driver:\tnvidia-drm\ndrm-total-vram:\t9 GiB\n')
        add_fd(outsider, 3, '/dev/dri/renderD128', I915_FDINFO.format(client=12, total=9, resident=9 * 1024 * 1024))
        self.snapshot()
        with patch.dict(os.environ, {gpu.PANE_GPU_MODE_KEY: 'auto'}):
            # 300 MiB on the NVIDIA GPU + 256 MiB through i915 = 556 MiB: shown.
            self.ae(gpu.pane_gpu_bytes(100), 556 * MIB)
            self.ae(gpu.pane_gpu_segment(100), (f' {G} 556M ', gpu.GPU_WIDGET_ACTION))
            # The worker's own tree is a subset of the pane's.
            self.ae(gpu.pane_gpu_bytes(101), 556 * MIB)
            # The outsider is a service outside the pane: no chip for the pane,
            # though the top bar total includes its memory.
            self.ae(gpu.pane_gpu_bytes(999), 4 * GIB + 9 * GIB)

    def test_only_the_pane_tree_is_opened(self) -> None:
        self.device(INTEL)
        write_process(self.proc, 100, 1)
        other = write_process(self.proc, 555, 1)
        add_fd(other, 3, '/dev/dri/renderD128', I915_FDINFO.format(client=1, total=1, resident=1))
        self.snapshot()
        opened: list[str] = []
        listdir = os.listdir

        def recording_listdir(path: Any) -> list[str]:
            opened.append(str(path))
            return listdir(path)
        with patch('kitty.kilix_gpu.os.listdir', recording_listdir):
            gpu.pane_gpu_bytes(100)
        self.ae([p for p in opened if p.endswith('/fd')], [str(self.proc / '100' / 'fd')])

    def test_client_of_a_service_gets_no_chip(self) -> None:
        # A pane running only a client (curl to a model server) holds no GPU memory.
        self.device(NVIDIA)
        self.nvml = FakeNvml({NVIDIA[0]: dict(compute=[(4242, 5 * GIB)])})
        write_process(self.proc, 100, 1)
        write_process(self.proc, 101, 100)
        self.snapshot()
        self.ae(gpu.pane_gpu_bytes(100), 0)
        self.ae(gpu.pane_gpu_label(100), '')
        self.ae(gpu.system_gpu_text(), f' 0% {G} 0K ')


class TestGpuRefresh(GpuTestCase):

    def test_bar_timer_redraws_when_only_the_gpu_text_changes(self) -> None:
        self.device(NVIDIA)
        reading = dict(util=5, used=400 * MIB)
        self.nvml = FakeNvml({NVIDIA[0]: reading})
        manager = MagicMock(os_window_id=7)
        boss = MagicMock(all_tab_managers=[manager])
        segment = system.system_gpu_bar_segment(7)
        self.ae(segment[0], f' 5% {G} 400M ')
        self.ae(segment[1], gpu.GPU_WIDGET_ACTION)
        with patch('kitty.fast_data_types.get_boss', return_value=boss), \
                patch('kitty.fast_data_types.mark_os_window_dirty') as dirty:
            gpu._CACHE_UNTIL = 0.0
            system._system_load_timer()
            dirty.assert_not_called()
            self.wake.assert_not_called()
            reading['util'] = 60
            gpu._CACHE_UNTIL = 0.0
            system._system_load_timer()
            dirty.assert_called_once_with(7)
            manager.mark_tab_bar_dirty.assert_called_once_with()
            # The timer fires inside the event wait; without a wakeup an idle
            # window keeps showing the old text.
            self.wake.assert_called_once_with()

    def test_bar_timer_does_not_poll_a_gpu_no_bar_shows(self) -> None:
        self.device(NVIDIA)
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=5)})
        system._DRAWN_TEXT[7] = 'cpu'
        with patch('kitty.fast_data_types.get_boss', return_value=MagicMock(all_tab_managers=[])), \
                patch.object(system, 'system_load', return_value=system.SystemLoad(None, None)):
            system._system_load_timer()
        self.ae(self.loads, 0)

    def test_pane_timer_relabels_when_only_gpu_changes(self) -> None:
        window = SimpleNamespace(id=1, child=SimpleNamespace(process_tree_root_pid=100))
        tab = MagicMock()
        tab.__iter__.side_effect = lambda: iter([window])
        manager = MagicMock(os_window_id=3)
        manager.__iter__.side_effect = lambda: iter([tab])
        boss = MagicMock(all_tab_managers=[manager])
        labels = iter(['', '600M'])
        with patch('kitty.fast_data_types.get_boss', return_value=boss), \
                patch('kitty.fast_data_types.mark_os_window_dirty') as dirty, \
                patch('kitty.kilix_memory.pane_memory_mode', return_value='off'), \
                patch('kitty.kilix_cpu.pane_cpu_mode', return_value='off'), \
                patch('kitty.kilix_gpu.pane_gpu_label', side_effect=lambda pid: next(labels)), \
                patch('kitty.kilix_telemetry.refresh_panes') as refresh:
            kilix_memory._LAST_LABELS = {1: ('', '', '')}
            kilix_memory._memory_timer()
            dirty.assert_not_called()
            self.wake.assert_not_called()
            kilix_memory._memory_timer()
            dirty.assert_called_once_with(3)
            tab.update_window_title_bars.assert_called_once_with()
            # A changed chip is rendered now, not at the next unrelated event.
            self.wake.assert_called_once_with()
        # GPU alone needs no RAM/CPU telemetry refresh.
        refresh.assert_not_called()
        kilix_memory._LAST_LABELS = {}

    def test_cache_is_shared_between_refreshes(self) -> None:
        self.device(NVIDIA)
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=5)})
        before = gpu.SAMPLE_COUNT
        gpu.gpu_snapshot(force=True)
        for _ in range(5):
            gpu.gpu_snapshot()
            gpu.system_gpu_text()
        self.ae(gpu.SAMPLE_COUNT - before, 1)


class TestGpuActions(GpuTestCase):

    def dispatch(self, gesture: str) -> tuple[MagicMock, MagicMock]:
        from kitty.kilix_chrome import registry
        target = MagicMock(kilix_popup=None)
        tab = MagicMock()
        target.tabref.return_value = tab
        manager = MagicMock()
        manager.active_tab.active_window = target
        boss = MagicMock()
        with patch('kitty.fast_data_types.get_boss', return_value=boss), \
                patch('kitty.kilix_chrome.popup.popup_target', side_effect=lambda w, a: w), \
                patch.object(registry, 'settings_target', return_value=['kilix-settings']):
            self.assertTrue(registry.dispatch(manager, gpu.GPU_WIDGET_ACTION, gesture))
        return boss, tab

    def test_click_opens_the_card_and_right_click_the_widget_menu(self) -> None:
        boss, tab = self.dispatch('single')
        args = boss.run_kitten_with_metadata.call_args
        self.ae(args.args[:2], ('kilix_chrome_widget', ('gpu',)))
        boss, tab = self.dispatch('right')
        cmd = tab.new_window.call_args.kwargs['cmd']
        self.ae(cmd, ['kilix-settings', '--widget', 'gpu'])

    def test_card_kitten_accepts_gpu(self) -> None:
        from kittens.kilix_chrome_widget.main import card
        self.ae(card('gpu')[0], 'GPU')


class Clock:
    """A fake monotonic clock: refreshes are simulated without waiting."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


XORG = (1191, 4718592)  # a display server's resident 4.5 MiB at 0% busy


class TestGpuIdleBackoff(GpuTestCase):
    """A runtime-PM GPU (power/control=auto) must be able to reach RTD3 while
    the widgets poll: the NVIDIA driver needs a use-free 5 s window, so no
    read may reach the driver more often than the backoff allows."""

    def setUp(self) -> None:
        super().setUp()
        self.clock = Clock()
        clock = patch.object(gpu.time, 'monotonic', self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    def refresh_for(self, seconds: float, step: float = 4.0, every: Any = None) -> list[float]:
        """Sample on every 4 s refresh for ``seconds``; return the times NVML was opened."""
        opened: list[float] = []
        end = self.clock.now + seconds
        while self.clock.now <= end:
            before = self.nvml.calls.count('init') if self.nvml else 0
            if every is not None:
                every()
            gpu.gpu_snapshot()
            if self.nvml and self.nvml.calls.count('init') > before:
                opened.append(self.clock.now)
            self.clock.now += step
        return opened

    def test_resident_display_server_allocation_is_idle(self) -> None:
        self.device(NVIDIA, control='auto')
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=0, used=300 * MIB, graphics=[XORG])})
        opened = self.refresh_for(300)
        # One read per backoff, not one per refresh (76 refreshes).
        self.ae(len(opened), 10)
        self.ae(self.nvml.calls.count('init'), self.nvml.calls.count('shutdown'))
        gaps = [b - a for a, b in zip(opened, opened[1:])]
        self.assertGreaterEqual(min(gaps), 30)
        # The display server's memory is still in the total it reports.
        self.ae(dict(gpu.gpu_snapshot().process_memory), {XORG[0]: XORG[1]})

    def test_backoff_lasts_at_least_30_s_and_follows_the_autosuspend_delay(self) -> None:
        dev = self.device(NVIDIA, control='auto')
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=0, graphics=[XORG])})
        gpu.gpu_snapshot()
        self.ae(self.nvml.calls.count('init'), 1)
        for at in (4, 8, 16, 24, 29.9):
            self.clock.now = 1000 + at
            gpu.gpu_snapshot()
        self.ae(self.nvml.calls.count('init'), 1)
        self.clock.now = 1032  # the next 4 s refresh after the backoff
        gpu.gpu_snapshot()
        self.ae(self.nvml.calls.count('init'), 2)
        # A longer autosuspend delay doubles into the backoff, up to 120 s.
        (dev / 'power' / 'autosuspend_delay_ms').write_text('40000\n')
        self.ae(gpu.backoff_seconds(gpu.drm_devices()[0]), 80)
        (dev / 'power' / 'autosuspend_delay_ms').write_text('100000\n')
        self.ae(gpu.backoff_seconds(gpu.drm_devices()[0]), 120)
        (dev / 'power' / 'autosuspend_delay_ms').write_text('2000\n')
        self.ae(gpu.backoff_seconds(gpu.drm_devices()[0]), 30)

    def test_activity_is_utilisation_or_moving_process_memory(self) -> None:
        self.device(NVIDIA, control='auto')
        job = [700, 100 * MIB]
        reading = dict(util=0, graphics=[XORG], compute=[tuple(job)])
        self.nvml = FakeNvml({NVIDIA[0]: reading})

        def grow() -> None:
            job[1] += 64 * MIB
            reading['compute'] = [tuple(job)]
        # The first read has nothing to compare with: idle at 0%.
        gpu.gpu_snapshot()
        self.assertIn(NVIDIA[0], gpu._BACKOFF)
        gpu._BACKOFF.clear()
        self.clock.now += 4
        # Memory that keeps moving at 0% busy: read at every refresh.
        self.ae(len(self.refresh_for(20, every=grow)), 6)
        # Busy: read at every refresh.
        reading['util'] = 35
        self.ae(len(self.refresh_for(20)), 6)
        # A process appearing is activity even at 0%.
        reading['util'] = 0
        gpu.gpu_snapshot(force=True)
        self.assertIn(NVIDIA[0], gpu._BACKOFF)
        gpu._BACKOFF.clear()
        reading['compute'] = [tuple(job), (701, 200 * MIB)]
        gpu.gpu_snapshot(force=True)
        self.assertNotIn(NVIDIA[0], gpu._BACKOFF)
        # Small moves of a resident allocation are not.
        reading['graphics'] = [(XORG[0], XORG[1] + 64 * 1024)]
        gpu.gpu_snapshot(force=True)
        self.assertIn(NVIDIA[0], gpu._BACKOFF)

    def test_pane_chip_survives_the_backoff(self) -> None:
        self.device(NVIDIA, control='auto')
        # A model loaded in a pane and sitting idle: steady memory at 0%.
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=0, graphics=[XORG], compute=[(101, 700 * MIB)])})
        write_process(self.proc, 100, 1)
        write_process(self.proc, 101, 100)
        labels = set()
        opened = self.refresh_for(60, every=lambda: labels.add(gpu.pane_gpu_label(100)))
        self.ae(labels, {'700M'})
        self.ae(len(opened), 2)

    def test_amd_runtime_pm_counters_and_fdinfo_back_off_too(self) -> None:
        self.device(AMD, control='auto', files={'gpu_busy_percent': '0', 'mem_info_vram_used': str(200 * MIB),
                                                 'mem_info_vram_total': str(8 * GIB)})
        shell = write_process(self.proc, 100, 1)
        worker = write_process(self.proc, 101, 100)
        add_fd(shell, 0, '/dev/pts/3')
        add_fd(worker, 3, '/dev/dri/renderD130', 'drm-driver:\tamdgpu\ndrm-pdev:\t0000:03:00.0\n'
               'drm-client-id:\t5\ndrm-resident-vram:\t600 MiB\n')
        read = gpu._read
        paths: list[tuple[float, str]] = []

        def recording_read(path: str, limit: int = 4096) -> str | None:
            paths.append((self.clock.now, path))
            return read(path, limit)
        labels = set()
        with patch.object(gpu, '_read', recording_read), patch.dict(os.environ, {gpu.PANE_GPU_MODE_KEY: 'auto'}):
            self.refresh_for(300, every=lambda: labels.add(gpu.pane_gpu_label(100)))
        busy_reads = [t for t, p in paths if p.endswith('gpu_busy_percent')]
        vram_reads = [t for t, p in paths if p.endswith('mem_info_vram_used')]
        fdinfo_reads = [t for t, p in paths if '/fdinfo/' in p]
        self.ae(len(busy_reads), 10)
        self.ae(vram_reads, busy_reads)
        self.assertGreaterEqual(min(b - a for a, b in zip(busy_reads, busy_reads[1:])), 30)
        # Its clients' fdinfo is read with the counters, not in between...
        self.assertLessEqual(set(fdinfo_reads), set(busy_reads))
        self.assertTrue(fdinfo_reads)
        # ...and the pane chip keeps the value read last.
        self.ae(labels, {'600M'})

    def test_amd_activity_and_pinned_on_are_read_every_refresh(self) -> None:
        dev = self.device(AMD, control='auto', files={'gpu_busy_percent': '20', 'mem_info_vram_used': str(GIB)})
        busy = []
        with patch.object(gpu, '_sysfs_device', wraps=gpu._sysfs_device) as read:
            self.refresh_for(20)
            busy.append(read.call_count)
            (dev / 'gpu_busy_percent').write_text('0\n')
            (dev / 'power' / 'control').write_text('on\n')
            gpu._BACKOFF.clear()
            self.refresh_for(20)
            busy.append(read.call_count - busy[0])
        self.ae(busy, [6, 6])

    def test_a_job_that_wakes_a_sleeping_gpu_is_read_at_once(self) -> None:
        dev = self.device(NVIDIA, control='auto')
        reading = dict(util=0, graphics=[XORG])
        self.nvml = FakeNvml({NVIDIA[0]: reading})
        gpu.gpu_snapshot()
        set_status(dev, 'suspended')
        self.clock.now += 4
        self.assertTrue(gpu.gpu_snapshot().devices[0].asleep)
        # A job starts: the GPU wakes, and it is read on the next refresh,
        # not after a backoff, and is not backed off while its memory moves.
        set_status(dev, 'active')
        reading['compute'] = [(800, 900 * MIB)]
        self.clock.now += 4
        self.ae(dict(gpu.gpu_snapshot().process_memory), {XORG[0]: XORG[1], 800: 900 * MIB})
        self.assertNotIn(NVIDIA[0], gpu._BACKOFF)

    def test_reviewer_window_8_refreshes_over_28_s(self) -> None:
        # GW-R1 check C2, in the suite: Xorg listed, 0% busy, control=auto.
        self.device(NVIDIA, control='auto')
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=0, graphics=[XORG])})
        self.refresh_for(28)
        self.ae((self.nvml.calls.count('init'), self.nvml.calls.count('shutdown')), (1, 1))


class TestGpuNvmlSessions(GpuTestCase):

    def test_two_nvidia_gpus_one_asleep_nvml_is_not_initialised(self) -> None:
        second = ('0000:02:00.0', 0x10de, 0x1001, 'nvidia', ('card2', 'renderD130'))
        self.device(NVIDIA)
        dev = self.device(second, status='suspended', control='auto')
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=40, used=GIB), second[0]: dict(util=0)})
        snapshot = self.snapshot()
        # nvmlInit would attach (and so wake) the sleeping GPU: not called.
        self.ae(self.nvml.calls.count('init'), 0)
        self.ae([d.source for d in snapshot.devices], ['held', 'asleep'])
        self.assertIn('another NVIDIA GPU is asleep', '\n'.join(gpu.card_lines(snapshot)))
        # Both awake: read, and the session is closed (one is runtime-PM).
        set_status(dev, 'active')
        snapshot = self.snapshot()
        self.ae([d.source for d in snapshot.devices], ['nvml', 'nvml'])
        self.ae((self.nvml.calls.count('init'), self.nvml.calls.count('shutdown')), (1, 1))

    def test_a_kept_session_is_closed_when_a_gpu_goes_to_sleep(self) -> None:
        second = ('0000:02:00.0', 0x10de, 0x1001, 'nvidia', ('card2', 'renderD130'))
        self.device(NVIDIA)
        dev = self.device(second)
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=40), second[0]: dict(util=40)})
        self.snapshot()
        self.ae(self.nvml.calls.count('shutdown'), 0)  # both pinned on: kept
        (dev / 'power' / 'control').write_text('auto\n')
        set_status(dev, 'suspended')
        self.snapshot()
        self.ae(self.nvml.calls.count('shutdown'), 1)
        self.assertFalse(gpu._NVML.initialised)

    def test_mixed_control_closes_the_session_even_while_one_backs_off(self) -> None:
        second = ('0000:02:00.0', 0x10de, 0x1001, 'nvidia', ('card2', 'renderD130'))
        self.device(NVIDIA, control='on')
        self.device(second, control='auto')
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=40), second[0]: dict(util=0)})
        for _ in range(3):
            self.snapshot()  # the second GPU is in its backoff after the first
            self.assertFalse(gpu._NVML.initialised)
        self.ae((self.nvml.calls.count('init'), self.nvml.calls.count('shutdown')), (1, 1))
        self.assertIn(second[0], gpu._BACKOFF)

    def test_a_failing_read_still_closes_nvml_and_logs_once(self) -> None:
        self.device(NVIDIA, control='auto')
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=5)})
        self.nvml.fail = 'utilisation'
        with patch.object(gpu, 'log_error') as log:
            for _ in range(3):
                gpu._BACKOFF.clear()
                snapshot = self.snapshot()
        self.ae((self.nvml.calls.count('init'), self.nvml.calls.count('shutdown')), (3, 3))
        self.assertFalse(gpu._NVML.initialised)
        self.ae(log.call_count, 1)
        self.ae((snapshot.devices[0].source, snapshot.devices[0].readable), ('none', False))
        # A broken read on a runtime-PM GPU is not retried at every refresh.
        self.snapshot()
        self.ae(self.nvml.calls.count('init'), 3)

    def test_library_without_get_name_still_reads(self) -> None:
        self.device(NVIDIA, control='auto')
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=5, used=GIB)})
        with patch.object(FakeNvml, 'nvmlDeviceGetName', None):
            device = self.snapshot().devices[0]
        self.ae((device.utilisation, device.name), (5, 'NVIDIA GPU 10de:1001'))
        self.ae(self.nvml.calls.count('shutdown'), 1)

    def test_init_failure_is_retried_later_and_less_often(self) -> None:
        self.device(NVIDIA)
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=5, used=GIB)})
        self.nvml.init_results = [18, 18]  # NVML_ERROR_LIB_RM_VERSION_MISMATCH twice
        clock = Clock()
        with patch.object(gpu.time, 'monotonic', clock):
            self.assertFalse(self.snapshot().devices[0].readable)
            for at in (4, 20, 29):
                clock.now = 1000 + at
                self.snapshot()
            self.ae(self.nvml.calls.count('init'), 1)
            clock.now = 1030
            self.assertFalse(self.snapshot().devices[0].readable)
            self.ae(self.nvml.calls.count('init'), 2)
            clock.now = 1030 + 59
            self.snapshot()
            self.ae(self.nvml.calls.count('init'), 2)  # the wait doubled to 60 s
            clock.now = 1030 + 60
            self.ae(self.snapshot().devices[0].utilisation, 5)
        self.ae(self.loads, 1)  # the library was loaded once

    def test_more_than_128_processes_are_read(self) -> None:
        self.device(NVIDIA)
        many = [(2000 + i, MIB) for i in range(300)]
        self.nvml = FakeNvml({NVIDIA[0]: dict(compute=many)})
        self.ae(len(self.snapshot().process_memory), 300)

    def test_device_without_runtime_pm_attributes_is_read(self) -> None:
        dev = self.device(NVIDIA)
        (dev / 'power' / 'runtime_status').unlink()
        (dev / 'power' / 'control').unlink()
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=7)})
        device = self.snapshot().devices[0]
        self.ae((device.asleep, device.utilisation), (False, 7))

    def test_nouveau_is_not_read_through_nvml(self) -> None:
        nouveau = ('0000:01:00.0', 0x10de, 0x1001, 'nouveau', ('card1', 'renderD129'))
        self.device(nouveau)
        self.nvml = FakeNvml({nouveau[0]: dict(util=50)})
        snapshot = self.snapshot()
        self.ae(self.loads, 0)
        self.ae(snapshot.devices[0].source, 'none')
        self.ae(dict(snapshot.fdinfo_nodes), {'card1': nouveau[0], 'renderD129': nouveau[0]})


class TestGpuPaneCache(GpuTestCase):

    def test_title_bar_redraws_reuse_one_fd_listing_per_refresh(self) -> None:
        self.device(INTEL)
        shell = write_process(self.proc, 100, 1)
        add_fd(shell, 3, '/dev/dri/renderD128', I915_FDINFO.format(client=1, total=1, resident=700 * 1024))
        self.snapshot()
        listdir = os.listdir
        opened: list[str] = []

        def recording_listdir(path: Any) -> list[str]:
            opened.append(str(path))
            return listdir(path)
        with patch('kitty.kilix_gpu.os.listdir', recording_listdir):
            for _ in range(5):
                self.ae(gpu.pane_gpu_bytes(100), 700 * MIB)
        self.ae([p for p in opened if p.endswith('/fd')], [str(self.proc / '100' / 'fd')])
        # The next sample reads it again.
        gpu.gpu_snapshot(force=True)
        with patch('kitty.kilix_gpu.os.listdir', recording_listdir):
            gpu.pane_gpu_bytes(100)
        self.ae(len([p for p in opened if p.endswith('/fd')]), 2)

    def test_fdinfo_is_not_read_once_the_device_falls_asleep(self) -> None:
        dev = self.device(AMD, files={'gpu_busy_percent': '30'})
        shell = write_process(self.proc, 100, 1)
        add_fd(shell, 3, '/dev/dri/renderD130', 'drm-driver:\tamdgpu\ndrm-client-id:\t5\ndrm-resident-vram:\t600 MiB\n')
        self.snapshot()
        set_status(dev, 'suspended')  # after the sample, before the pane refresh
        self.ae(gpu.pane_gpu_bytes(100), 0)


class TestGpuChipColour(GpuTestCase):

    @staticmethod
    def contrast(a: int, b: int) -> float:
        def luminance(rgb: int) -> float:
            parts = [((rgb >> shift) & 255) / 255 for shift in (16, 8, 0)]
            parts = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
            return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]
        hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    def test_chip_reads_on_both_title_bars_and_the_top_bar(self) -> None:
        # Kilix's config/kitty.conf: active and inactive title bar, tab bar.
        for background in (0x3465a4, 0x2e3436, 0x16181a):
            self.assertGreaterEqual(self.contrast(gpu.GPU_CHIP_RGB, background), 4.5, hex(background))


class TestGpuApu(GpuTestCase):

    def test_boot_vga_apu_is_integrated_beside_a_sleeping_nvidia_gpu(self) -> None:
        # GW-R1 check C7: a Ryzen APU behind an internal bridge (bus 05).
        self.device(NVIDIA, status='suspended', control='auto')
        apu = self.device(('0000:05:00.0', 0x1002, 0x1638, 'amdgpu', ('card0', 'renderD128')),
                          files={'gpu_busy_percent': '7', 'mem_info_vram_used': str(300 * MIB),
                                 'mem_info_vram_total': str(512 * MIB)})
        (apu / 'boot_vga').write_text('1\n')
        shown = gpu.primary_device(self.snapshot().devices)
        self.ae((shown.pci, shown.asleep), (NVIDIA[0], True))
        self.ae(gpu.system_gpu_text(), f' {G} {gpu.ASLEEP_TEXT} ')


class TestGpuFdinfoEpochs(GpuTestCase):

    def test_chip_is_read_once_per_backoff_even_when_the_bar_sampled_first(self) -> None:
        # The bar timer takes the sample that reads the idle GPU's counters;
        # the pane timer runs in the next sample, inside the backoff.
        self.device(AMD, control='auto', files={'gpu_busy_percent': '0', 'mem_info_vram_used': str(GIB)})
        shell = write_process(self.proc, 100, 1)
        add_fd(shell, 3, '/dev/dri/renderD130', 'drm-driver:\tamdgpu\ndrm-client-id:\t5\ndrm-resident-vram:\t600 MiB\n')
        clock = Clock()
        read = gpu._read
        fdinfo: list[float] = []

        def recording_read(path: str, limit: int = 4096) -> str | None:
            if '/fdinfo/' in path:
                fdinfo.append(clock.now)
            return read(path, limit)
        with patch.object(gpu.time, 'monotonic', clock), patch.object(gpu, '_read', recording_read):
            gpu.system_gpu_text()  # bar: reads the counters, starts the backoff
            for step in range(1, 7):
                clock.now = 1000 + 4 * step - 2
                kilix_memory._PROCESS_CACHE_UNTIL = 0
                self.ae(gpu.pane_gpu_label(100), '600M')
        self.ae(fdinfo, [1002.0])

    def test_fdinfo_is_read_again_after_each_counter_read(self) -> None:
        # A GPU pinned on (no backoff): each sample reads its counters, and
        # the pane chip follows the client's memory from one sample to the next.
        self.device(AMD, control='on', files={'gpu_busy_percent': '0', 'mem_info_vram_used': str(GIB)})
        shell = write_process(self.proc, 100, 1)
        add_fd(shell, 3, '/dev/dri/renderD130', 'drm-driver:\tamdgpu\ndrm-client-id:\t5\ndrm-resident-vram:\t600 MiB\n')
        self.snapshot()
        self.ae(gpu.pane_gpu_label(100), '600M')
        (shell / 'fdinfo' / '3').write_text('drm-driver:\tamdgpu\ndrm-client-id:\t5\ndrm-resident-vram:\t700 MiB\n')
        self.snapshot()
        kilix_memory._PROCESS_CACHE_UNTIL = 0
        self.ae(gpu.pane_gpu_label(100), '700M')

    def test_a_reused_pid_is_read_afresh_during_the_backoff(self) -> None:
        # The cached fdinfo belongs to one process: a new process that got the
        # same PID (another start time) is read, not given the old value.
        self.device(AMD, control='auto', files={'gpu_busy_percent': '0', 'mem_info_vram_used': str(GIB)})
        write_process(self.proc, 100, 1)
        worker = write_process(self.proc, 101, 100)
        add_fd(worker, 3, '/dev/dri/renderD130', 'drm-driver:\tamdgpu\ndrm-client-id:\t5\ndrm-resident-vram:\t600 MiB\n')
        self.snapshot()
        self.ae(gpu.pane_gpu_bytes(100), 600 * MIB)
        self.assertIn(AMD[0], gpu._BACKOFF)
        # PID 101 exits and the PID is reused inside the backoff: stat field
        # 22 (the start time) differs.
        fields = ['S', '100', '101', '101', *(['0'] * 15), '999', '0', '100']
        (worker / 'stat').write_text(f"101 (pane worker) {' '.join(fields)}\n")
        (worker / 'fdinfo' / '3').write_text('drm-driver:\tamdgpu\ndrm-client-id:\t8\ndrm-resident-vram:\t100 MiB\n')
        gpu.gpu_snapshot(force=True)
        kilix_memory._PROCESS_CACHE_UNTIL = 0
        self.assertIn(AMD[0], gpu._BACKOFF)
        self.ae(gpu.pane_gpu_bytes(100), 100 * MIB)


class TestGpuMergeReview(GpuTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.clock = Clock()
        clock = patch.object(gpu.time, 'monotonic', self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    refresh_for = TestGpuIdleBackoff.refresh_for
    second = ('0000:02:00.0', 0x10de, 0x1001, 'nvidia', ('card3', 'renderD131'))

    def test_mixed_nvidia_backoff_bounds_global_attaches(self) -> None:
        first = self.device(NVIDIA, control='auto')
        self.device(self.second, control='auto')
        for control, util in (('auto', 60), ('on', 0)):
            with self.subTest(control=control):
                gpu.reset_state()
                gpu.set_nvml_loader(self._load)
                self.clock.now = 1000
                (first / 'power' / 'control').write_text(control)
                self.nvml = FakeNvml({NVIDIA[0]: dict(util=util), self.second[0]: dict(util=0, graphics=[XORG])})
                # Model nvmlInit attaching BOTH devices, even if only one is read.
                opened = self.refresh_for(300)
                self.ae(len(opened), 10)
                self.assertGreaterEqual(min(b - a for a, b in zip(opened, opened[1:])), 30)
                self.ae(self.nvml.calls.count('shutdown'), len(opened))
                self.assertFalse(gpu._NVML.initialised)
                self.ae(self.nvml.calls.count('handle ' + NVIDIA[0]), 10)
                self.ae(self.nvml.calls.count('handle ' + self.second[0]), 10)

    def test_held_gpu_is_visible_above_sleeping_gpu_and_recovers(self) -> None:
        self.device(NVIDIA)
        other = self.device(self.second, status='suspending', control='auto')
        write_process(self.proc, 100, 1)
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=90, used=3 * GIB, compute=[(100, 2 * GIB)]),
                              self.second[0]: dict(util=0)})
        for status in ('suspending', 'suspended'):
            set_status(other, status)
            snapshot = self.snapshot()
            self.ae(self.nvml.calls.count('init'), 0)
            self.ae(gpu.primary_device(snapshot.devices).pci, NVIDIA[0])
            for mode in ('always', 'auto'):
                self.ae(gpu.format_system_gpu(gpu.primary_device(snapshot.devices), mode), f' {G} held ')
            self.ae(gpu.format_system_gpu(gpu.primary_device(snapshot.devices), 'off'), '')
            self.ae(gpu.pane_gpu_label(100), '')  # no invented process counters
        set_status(other, 'active')
        self.snapshot()
        self.ae(gpu.system_gpu_text(), f' 90% {G} 3.0 ')
        self.ae(gpu.pane_gpu_label(100), '2.0')
        self.clock.now += 4
        snapshot = self.snapshot()
        self.ae(gpu.system_gpu_text(), f' {G} held ')
        self.assertIn('backoff', '\n'.join(gpu.card_lines(snapshot)))
        self.ae(self.nvml.calls.count('init'), 1)
        self.clock.now += 28
        self.snapshot()
        self.ae(gpu.system_gpu_text(), f' 90% {G} 3.0 ')
        self.ae(gpu.pane_gpu_label(100), '2.0')

    def test_unknown_utilisation_backs_off_and_keeps_pane_memory(self) -> None:
        self.device(NVIDIA, control='auto')
        write_process(self.proc, 100, 1)
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=None, used=700 * MIB, compute=[(100, 600 * MIB)])})
        labels = set()
        opened = self.refresh_for(28, every=lambda: labels.add(gpu.pane_gpu_label(100)))
        self.ae(opened, [1000])
        self.ae(labels, {'600M'})
        self.ae(self.nvml.calls.count('shutdown'), 1)
        self.ae(gpu.system_gpu_text(), f' {G} 700M ')

    def test_non_nvidia_sleepers_do_not_block_nvml(self) -> None:
        self.device(NVIDIA)
        intel = self.device(INTEL, status='suspended', control='auto')
        amd = self.device(AMD, status='suspended', control='auto')
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=90, used=3 * GIB, compute=[(100, 2 * GIB)])})
        write_process(self.proc, 100, 1)
        for status in ('suspended', 'suspending'):
            set_status(intel, status)
            set_status(amd, status)
            self.snapshot()
            self.ae(gpu.system_gpu_text(), f' 90% {G} 3.0 ')
            self.ae(gpu.pane_gpu_label(100), '2.0')
        self.ae(self.nvml.calls.count('init'), 1)
        self.ae(self.nvml.calls.count('shutdown'), 0)

    def test_low_utilisation_is_activity(self) -> None:
        self.device(NVIDIA, control='auto')
        reading = dict(util=1)
        self.nvml = FakeNvml({NVIDIA[0]: reading})
        for util in (1, 4):
            reading['util'] = util
            self.ae(len(self.refresh_for(28)), 8)

    def test_one_mib_memory_movement_is_activity(self) -> None:
        self.device(NVIDIA, control='auto')
        for delta, expected in ((MIB - 1, 2), (MIB, 3), (31 * MIB, 3)):
            with self.subTest(delta=delta):
                gpu.reset_state()
                gpu.set_nvml_loader(self._load)
                reading = dict(util=0, compute=[(100, 100 * MIB)])
                self.nvml = FakeNvml({NVIDIA[0]: reading})
                self.snapshot()
                self.clock.now += 32
                reading['compute'] = [(100, 100 * MIB + delta)]
                self.snapshot()
                self.clock.now += 4
                self.snapshot()
                self.ae(self.nvml.calls.count('init'), expected)

    def test_init_retry_caps_and_resets_after_recovery(self) -> None:
        self.device(NVIDIA, control='auto')
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=50)})
        self.nvml.init_results = [18] * 6
        with patch.object(gpu, 'log_error') as log:
            self.snapshot()
            for attempts, delay in enumerate((30, 60, 120, 240, 480, 600), 1):
                self.clock.now += delay - 1
                self.snapshot()
                self.ae(self.nvml.calls.count('init'), attempts)
                self.clock.now += 1
                self.snapshot()
                self.ae(self.nvml.calls.count('init'), attempts + 1)
            self.ae(log.call_count, 1)
            self.assertTrue(self.snapshot().devices[0].readable)
            self.nvml.init_results = [18]
            self.snapshot()
            self.ae(log.call_count, 2)  # recovered, then failed again
            before = self.nvml.calls.count('init')
            self.clock.now += 29
            self.snapshot()
            self.ae(self.nvml.calls.count('init'), before)
            self.clock.now += 1
            self.assertTrue(self.snapshot().devices[0].readable)
            self.ae(self.nvml.calls.count('init'), before + 1)

    def test_two_failing_devices_log_once_each_until_recovery(self) -> None:
        self.device(NVIDIA)
        self.device(self.second)
        self.nvml = FakeNvml({NVIDIA[0]: dict(util=50), self.second[0]: dict(util=50)})
        self.nvml.fail = 'utilisation'
        with patch.object(gpu, 'log_error') as log:
            for _ in range(10):
                self.snapshot()
            self.ae(log.call_count, 2)
            self.ae(len(set(c.args[0] for c in log.call_args_list)), 2)
            self.nvml.fail = ''
            self.snapshot()
            self.nvml.fail = 'utilisation'
            self.snapshot()
            self.ae(log.call_count, 4)

    def test_process_scan_invalidates_pane_cache_and_evicts_exited_clients(self) -> None:
        self.device(INTEL)
        write_process(self.proc, 100, 1)
        worker = write_process(self.proc, 101, 100)
        add_fd(worker, 3, '/dev/dri/renderD128', I915_FDINFO.format(client=1, total=1, resident=700 * 1024))
        self.snapshot()
        self.ae(gpu.pane_gpu_bytes(100), 700 * MIB)
        samples_before = gpu.SAMPLE_COUNT
        (worker / 'stat').unlink()  # gone from the next process scan
        self.clock.now += 0.25  # still within the SAME GPU sample
        kilix_memory._PROCESS_CACHE_UNTIL = 0
        self.ae(gpu.pane_gpu_bytes(100), 0)
        self.ae(gpu.SAMPLE_COUNT, samples_before)
        self.assertFalse(any(pid == 101 for pid, pci in gpu._FDINFO_CACHE))

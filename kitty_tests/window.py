#!/usr/bin/env python
# License: GPL v3

import os
import tempfile
from base64 import standard_b64encode
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kitty import kilix_memory, kilix_telemetry
from kitty.fast_data_types import GLFW_MOUSE_BUTTON_LEFT, GLFW_PRESS, GLFW_RELEASE
from kitty.kilix_cpu import (
    CPU_LOAD_THRESHOLD,
    format_pane_cpu_load,
    pane_cpu_label,
)
from kitty.kilix_memory import (
    GIB,
    MEMORY_GLYPH,
    MEMORY_WIDGET_ACTION,
    format_pane_memory,
    pane_cpu_cores,
    pane_memory_bytes,
    pane_memory_label,
    pane_memory_segment,
)
from kitty.tabs import MouseEvents, Tab, TabManager
from kitty.types import WindowGeometry
from kitty.window import Window
from kitty.window_title_bar import (
    WindowTitleBarScreen,
    WindowTitleData,
    pane_resource_text,
)

from . import BaseTest


class TestRemoteEcho(BaseTest):

    def test_echo_preserves_numeric_ssh_canaries(self):
        for data in (b'0', b'1234567890', b'9' * 78):
            with self.subTest(data=data):
                received = []
                Window.handle_remote_echo(
                    SimpleNamespace(write_to_child=received.append),
                    memoryview(standard_b64encode(data)),
                )
                self.ae(received, [data])

    def test_echo_rejects_non_numeric_input(self):
        for data in (b'', b'123\n', b'123\r', b'123\r\n', b'\n123',
                     b'12\n34', b'12\t34', b'123\x1b', b'abc', b'1 2'):
            with self.subTest(data=data), patch('kitty.window.log_error'):
                received = []
                Window.handle_remote_echo(
                    SimpleNamespace(write_to_child=received.append),
                    memoryview(standard_b64encode(data)),
                )
                self.ae(received, [])


class TestWindowChrome(BaseTest):

    def test_dynamic_memory_chip_policy_and_hitbox(self) -> None:
        self.assertEqual(format_pane_memory(GIB - 1, 'auto'), '')
        self.assertEqual(format_pane_memory(int(1.14 * GIB), 'auto'), '1.1')
        self.assertEqual(format_pane_memory(64 * 1024**2, 'always'), '64.0M')
        self.assertEqual(format_pane_memory(64 * 1024, 'always'), '64K')
        self.assertEqual(format_pane_memory(100 * GIB, 'off'), '')

        self.set_options()
        title_bar = WindowTitleBarScreen(1, 10, 20)
        title_bar.layout(WindowGeometry(0, 0, 1200, 20, 120, 1))
        memory_text = f' {MEMORY_GLYPH} 100.0 '
        cpu_text = '2.4'
        resource_text = f' {cpu_text} {MEMORY_GLYPH} 100.0 '
        data = WindowTitleData(
            'pane', True, 1, 1, pane_memory_text=memory_text,
            pane_cpu_text=cpu_text)
        with patch('kitty.window_title_bar.chrome_enabled', return_value=True):
            title_bar.render(data, '')
        columns = [
            column
            for column, action in title_bar.button_cols.items()
            if action == MEMORY_WIDGET_ACTION
        ]
        self.assertEqual(len(columns), len(resource_text))
        self.assertEqual(
            pane_resource_text(cpu_text, memory_text), resource_text)
        self.assertEqual(
            pane_resource_text(cpu_text, ''),
            f' {cpu_text} {MEMORY_GLYPH} ',
        )
        self.assertEqual(pane_resource_text('', memory_text), memory_text)

    def test_cpu_policy_uses_each_pane_process_tree(self) -> None:
        self.assertEqual(format_pane_cpu_load(CPU_LOAD_THRESHOLD, 'auto'), '')
        self.assertEqual(format_pane_cpu_load(1.14, 'auto'), '1.1')
        self.assertEqual(format_pane_cpu_load(0.0, 'always'), '0.0')
        self.assertEqual(format_pane_cpu_load(20.0, 'off'), '')

        metrics = {
            100: SimpleNamespace(process_count=2, cpu_cores=1.75),
            200: SimpleNamespace(process_count=1, cpu_cores=0.25),
        }
        with (
            patch('kitty.kilix_cpu.chrome_value', return_value='always'),
            patch(
                'kitty.kilix_memory._telemetry_pane',
                side_effect=lambda pid: metrics.get(pid),
            ),
        ):
            self.assertEqual(pane_cpu_label(100), '1.8')
            self.assertEqual(pane_cpu_label(200), '0.2')

    def test_shared_snapshot_is_read_once_for_all_panes(self) -> None:
        metrics = {
            100: SimpleNamespace(process_count=2, cpu_cores=1.5),
            200: SimpleNamespace(process_count=1, cpu_cores=0.5),
        }
        snapshot = SimpleNamespace(
            hottest_celsius=72.5,
            pane=lambda pid: metrics[pid],
        )
        client = Mock()
        client.snapshot.return_value = snapshot
        with (
            patch.object(kilix_telemetry, '_CLIENT', client),
            patch.object(kilix_telemetry, '_PANE_METRICS', {}),
            patch.object(kilix_telemetry, '_SNAPSHOT', None),
        ):
            self.assertTrue(kilix_telemetry.refresh_panes([200, 100, 100]))
            self.assertIs(kilix_telemetry.pane_metrics(100), metrics[100])
            self.assertEqual(kilix_telemetry.hottest_celsius(), 72.5)
        client.register_panes.assert_called_once_with((100, 200))
        client.snapshot.assert_called_once_with(
            start=False, fallback=False, force=True)

    def test_pane_cpu_local_fallback_uses_tick_deltas(self) -> None:
        def write_process(
            proc: Path, pid: int, ppid: int, ticks: int, start: int
        ) -> None:
            directory = proc / str(pid)
            directory.mkdir(exist_ok=True)
            fields = ['S', str(ppid), str(pid), str(pid), *(['0'] * 18)]
            fields[11] = str(ticks)
            fields[12] = '0'
            fields[19] = str(start)
            fields[21] = '100'
            directory.joinpath('stat').write_text(
                f"{pid} (pane worker) {' '.join(fields)}\n")

        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            write_process(proc, 100, 1, 100, 10)
            write_process(proc, 101, 100, 50, 11)
            write_process(proc, 200, 1, 25, 12)
            with (
                patch.dict(os.environ, {'KILIX_MEMORY_PROC_ROOT': str(proc)}),
                patch('kitty.kilix_memory._telemetry_pane', return_value=None),
                patch(
                    'kitty.kilix_memory.time.monotonic',
                    side_effect=(1.0, 3.0, 3.1),
                ),
            ):
                kilix_memory._PROCESS_CACHE_UNTIL = 0
                kilix_memory._PROCESS_CACHE_ROOT = ''
                kilix_memory._PROCESS_PREVIOUS = {}
                kilix_memory._PROCESS_PREVIOUS_WHEN = 0.0
                self.assertEqual(pane_cpu_cores(100, force=True), 0.0)
                write_process(proc, 100, 1, 200, 10)
                write_process(proc, 101, 100, 150, 11)
                write_process(proc, 200, 1, 75, 12)
                self.assertAlmostEqual(pane_cpu_cores(100, force=True) or 0.0, 1.0)
                self.assertAlmostEqual(pane_cpu_cores(200) or 0.0, 0.25)

    def test_auto_memory_chip_skips_pss_below_rss_threshold(self) -> None:
        with (
            patch('kitty.kilix_memory.pane_memory_mode', return_value='auto'),
            patch(
                'kitty.kilix_memory.pane_memory_rss_bytes',
                return_value=GIB - 1,
            ),
            patch(
                'kitty.kilix_memory.pane_memory_bytes',
                side_effect=AssertionError('PSS scan should be skipped'),
            ),
        ):
            self.assertEqual(pane_memory_label(123), '')

    def test_memory_chip_aggregates_only_the_pane_process_tree(self) -> None:
        def write_process(
            proc: Path, pid: int, ppid: int, rss_pages: int, pss_kib: int
        ) -> None:
            directory = proc / str(pid)
            directory.mkdir()
            fields = ['S', str(ppid), str(pid), str(pid), *(['0'] * 17),
                      str(rss_pages)]
            directory.joinpath('stat').write_text(
                f"{pid} (pane worker) {' '.join(fields)}\n")
            directory.joinpath('smaps_rollup').write_text(
                f"Rss:  {rss_pages * 4} kB\nPss:  {pss_kib} kB\n")

        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            write_process(proc, 100, 1, 100_000, 100_000)
            write_process(proc, 101, 100, 300_000, 1_050_000)
            write_process(proc, 999, 1, 1_500_000, 5_000_000)
            with (
                patch.dict(os.environ, {
                    'KILIX_MEMORY_PROC_ROOT': str(proc)
                }),
                patch(
                    'kitty.kilix_memory.chrome_value',
                    return_value='auto',
                ),
            ):
                kilix_memory._PROCESS_CACHE_UNTIL = 0
                kilix_memory._PROCESS_CACHE_ROOT = ''
                kilix_memory._PROCESS_TOTALS = {}
                total = pane_memory_bytes(100)
                segment = pane_memory_segment(100)
        self.assertEqual(total, 1_150_000 * 1024)
        self.assertIsNotNone(segment)
        self.assertIn('1.1', segment[0])

    def test_shared_settings_remove_and_restore_individual_chrome_buttons(self) -> None:
        self.set_options()
        title_bar = WindowTitleBarScreen(1, 10, 20)
        title_bar.layout(WindowGeometry(0, 0, 800, 20, 80, 1))
        data = WindowTitleData('pane', True, 1, 1)

        with patch(
                'kitty.window_title_bar.chrome_enabled',
                side_effect=lambda key: key != 'KILIX_CHROME_BUTTON_SPLIT_UP'):
            title_bar.render(data, '')
        actions = set(title_bar.button_cols.values())
        self.assertNotIn(
            'launch --location=hsplit-before --cwd=current',
            actions)
        self.assertIn('change_font_size current +2.0', actions)
        self.assertIn('kilix_close_persistent_window', actions)

        with patch('kitty.window_title_bar.chrome_enabled', return_value=True):
            title_bar.render(data, '')
        self.assertIn(
            'launch --location=hsplit-before --cwd=current',
            set(title_bar.button_cols.values()))

    def test_synchronized_input_button_is_depressed_and_configurable(self) -> None:
        self.set_options()
        title_bar = WindowTitleBarScreen(1, 10, 20)
        title_bar.layout(WindowGeometry(0, 0, 1000, 20, 100, 1))
        data = WindowTitleData('pane', True, 1, 1)
        action = 'kilix_toggle_synchronized_input'

        with patch('kitty.window_title_bar.chrome_enabled', return_value=True):
            title_bar.render(data, '')
        columns = [
            col for col, mapped_action in title_bar.button_cols.items()
            if mapped_action == action
        ]
        self.assertEqual(len(columns), 3)
        cell = title_bar.screen.line(0).cursor_from(columns[0])
        normal_colors = (cell.fg, cell.bg)

        with patch('kitty.window_title_bar.chrome_enabled', return_value=True):
            title_bar.render(data._replace(is_synchronized_input=True), '')
        cell = title_bar.screen.line(0).cursor_from(columns[0])
        self.assertEqual((cell.fg, cell.bg), normal_colors[::-1])

        with patch(
                'kitty.window_title_bar.chrome_enabled',
                side_effect=lambda key: key !=
                'KILIX_CHROME_BUTTON_SYNCHRONIZE_INPUT'):
            title_bar.render(data, '')
        self.assertNotIn(action, set(title_bar.button_cols.values()))

    def test_synchronized_input_membership_and_double_click(self) -> None:
        class Pane:
            def __init__(self, window_id: int):
                self.id = window_id

        class Group:
            def __init__(self, pane):
                self.windows = [pane]

        class Windows:
            def __init__(self, panes):
                self.by_id = {pane.id: pane for pane in panes}
                self.groups = [Group(panes[0]), Group(panes[1])]
                self.active_window = panes[0]

            def iter_all_layoutable_groups(self):
                return iter(self.groups)

            def window_for_id(self, window_id):
                return self.by_id.get(window_id)

        first, second, overlay = Pane(1), Pane(2), Pane(3)
        tab = object.__new__(Tab)
        tab.windows = Windows((first, second, overlay))
        tab.kilix_synchronized_input_ids = set()
        tab.kilix_synchronized_input_control_enabled = True
        tab.update_window_title_bars = Mock()

        tab.kilix_toggle_synchronized_input()
        self.assertEqual(tab.kilix_synchronized_input_ids, {1})
        self.assertEqual(tab.kilix_synchronized_input_peer_ids(1), ())

        # The second release of the first double-click selects every base pane.
        tab.kilix_synchronized_input_double_click(1)
        self.assertEqual(tab.kilix_synchronized_input_ids, {1, 2})
        self.assertEqual(tab.kilix_synchronized_input_peer_ids(1), (2,))
        self.assertNotIn(overlay.id, tab.kilix_synchronized_input_ids)

        # A second double-click starts with its normal single-click toggle,
        # then recognizes that all panes were previously selected and clears all.
        tab.kilix_toggle_synchronized_input()
        self.assertEqual(tab.kilix_synchronized_input_ids, {2})
        tab.kilix_synchronized_input_double_click(1)
        self.assertEqual(tab.kilix_synchronized_input_ids, set())

        # From any partially selected state, a double-click selects all.
        tab.kilix_synchronized_input_ids = {1, 99}
        tab.kilix_toggle_synchronized_input()
        tab.kilix_synchronized_input_double_click(1)
        self.assertEqual(tab.kilix_synchronized_input_ids, {1, 2})

        tab.kilix_apply_synchronized_input_setting(False)
        self.assertEqual(tab.kilix_synchronized_input_ids, set())
        self.assertEqual(tab.kilix_synchronized_input_peer_ids(1), ())

    def test_keyboard_button_double_click_is_dispatched_to_the_tab(self) -> None:
        action = 'kilix_toggle_synchronized_input'
        tab = Mock()
        title_bar = SimpleNamespace(
            cell_width=10,
            geometry=SimpleNamespace(left=0),
            button_cols={1: action},
        )
        window = SimpleNamespace(
            id=7, _title_bar_screen=title_bar, tabref=lambda: tab)
        boss = SimpleNamespace(
            window_id_map={window.id: window},
            set_active_window=Mock(),
            combine=Mock(),
        )
        manager = object.__new__(TabManager)
        manager.recent_title_bar_mouse_events = MouseEvents()

        with (
            patch('kitty.tabs.get_boss', return_value=boss),
            patch('kitty.tabs.get_options',
                  return_value=SimpleNamespace(drag_threshold=0)),
            patch('kitty.tabs.get_click_interval', return_value=0.5),
            patch('kitty.tabs.get_window_being_dragged',
                  return_value=(0, False, 0, 0)),
            patch('kitty.tabs.set_window_being_dragged'),
        ):
            for _click in range(2):
                manager.handle_window_title_bar_mouse(
                    window.id, 15, 5, GLFW_MOUSE_BUTTON_LEFT, 0, GLFW_PRESS)
                manager.handle_window_title_bar_mouse(
                    window.id, 15, 5, GLFW_MOUSE_BUTTON_LEFT, 0, GLFW_RELEASE)

        boss.combine.assert_called_once_with(action, window_for_dispatch=window)
        tab.kilix_synchronized_input_double_click.assert_called_once_with(
            window.id)
        self.assertFalse(manager.recent_title_bar_mouse_events)

    def test_title_bar_visibility_restores_after_fullscreen(self) -> None:
        window = object.__new__(Window)
        window.show_title_bar = True
        window.os_window_id = 17
        geometry = WindowGeometry(0, 0, 800, 600, 80, 24)

        with patch('kitty.window.is_os_window_fullscreen', side_effect=(False, True, False)):
            self.assertTrue(window.should_show_title_bar(geometry))
            self.assertFalse(window.should_show_title_bar(geometry))
            self.assertTrue(window.should_show_title_bar(geometry))

        one_row = WindowGeometry(0, 0, 800, 20, 80, 1)
        with patch('kitty.window.is_os_window_fullscreen', return_value=False):
            self.assertFalse(window.should_show_title_bar(one_row))

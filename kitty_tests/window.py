#!/usr/bin/env python
# License: GPL v3

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kitty.fast_data_types import GLFW_MOUSE_BUTTON_LEFT, GLFW_PRESS, GLFW_RELEASE
from kitty.kilix_memory import (
    GIB,
    MEMORY_GLYPH,
    MEMORY_WIDGET_ACTION,
    format_pane_memory,
    pane_memory_bytes,
    pane_memory_label,
    pane_memory_segment,
)
from kitty import kilix_memory
from kitty.tabs import MouseEvents, Tab, TabManager
from kitty.types import WindowGeometry
from kitty.window import Window
from kitty.window_title_bar import (
    WindowTitleBarScreen,
    WindowTitleData,
)

from . import BaseTest


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
        data = WindowTitleData(
            'pane', True, 1, 1, pane_memory_text=memory_text)
        with patch('kitty.window_title_bar.chrome_enabled', return_value=True):
            title_bar.render(data, '')
        columns = [
            column
            for column, action in title_bar.button_cols.items()
            if action == MEMORY_WIDGET_ACTION
        ]
        self.assertEqual(len(columns), len(memory_text))

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
            'combine | launch --location=hsplit --cwd=current | move_window top',
            actions)
        self.assertIn('change_font_size current +2.0', actions)
        self.assertIn('close_window', actions)

        with patch('kitty.window_title_bar.chrome_enabled', return_value=True):
            title_bar.render(data, '')
        self.assertIn(
            'combine | launch --location=hsplit --cwd=current | move_window top',
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

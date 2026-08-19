#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from kitty import kilix_battery, kilix_memory
from kitty.boss import Boss, kilix_desktop_owns_start_menu
from kitty.kilix_chrome import settings as chrome_settings
from kitty.fast_data_types import BOTTOM_EDGE, LEFT_EDGE, Color, Region
from kitty.kilix_battery import (
    CALENDAR_WIDGET_ACTION,
    DATE_WIDGET_ACTION,
    NETWORK_WIDGET_ACTION,
    START_MENU_ACTION,
    THERMAL_WIDGET_ACTION,
    VOLUME_WIDGET_ACTION,
)
from kitty.tab_bar import TabBar, TabBarData, as_rgb
from kitty.utils import color_as_int

from . import BaseTest


def region(left: int, top: int, right: int, bottom: int) -> Region:
    return Region((left, top, right, bottom, right - left, bottom - top))


class DummyBoss:
    class mappings:
        current_keyboard_mode_name = ''

    def tab_for_id(self, tab_id: int) -> None:
        return None


class TestTabBar(BaseTest):

    def test_resource_timer_does_no_process_work_when_disabled(self) -> None:
        kilix_memory._LAST_LABELS = {1: ('old', 'old')}
        with (
            patch('kitty.kilix_memory.pane_memory_mode', return_value='off'),
            patch('kitty.kilix_cpu.pane_cpu_mode', return_value='off'),
            patch('kitty.kilix_telemetry.refresh_panes') as refresh,
            patch('kitty.kilix_memory._refresh_process_cache') as scan,
        ):
            kilix_memory._memory_timer()
        refresh.assert_not_called()
        scan.assert_not_called()
        self.ae(kilix_memory._LAST_LABELS, {})

    def test_windows_key_routes_to_desktop_owned_start_menus(self) -> None:
        class Child:
            foreground_cmdline = ['/usr/bin/python3', '/src/kilix-95/main.py']
            foreground_environ = {'KILIX_DESKTOP_PROVIDER': 'external'}

        class Window:
            child = Child()

            def send_key(self, key: str) -> None:
                self.sent = key

        window = Window()
        self.assertTrue(kilix_desktop_owns_start_menu(window))
        boss = object.__new__(Boss)
        boss.window_for_dispatch = window  # type: ignore[assignment]
        with patch.object(boss, '_kilix_start_main') as menu:
            boss.kilix_windows_key()
        self.ae(window.sent, 'ctrl+escape')
        menu.assert_not_called()

        Child.foreground_cmdline = ['/opt/kilix-icewm/prefix/bin/icewm-session']
        Child.foreground_environ = {'KILIX_DESKTOP_PROVIDER': 'icewm'}
        self.assertTrue(kilix_desktop_owns_start_menu(window))

    def test_windows_key_opens_kilix_menu_in_terminal_pane(self) -> None:
        class Child:
            foreground_cmdline = ['/usr/bin/bash']
            foreground_environ = {'KILIX_DESKTOP_PROVIDER': 'auto'}

        class Window:
            child = Child()

        window = Window()
        self.assertFalse(kilix_desktop_owns_start_menu(window))
        boss = object.__new__(Boss)
        boss.window_for_dispatch = window  # type: ignore[assignment]
        with patch.object(boss, '_kilix_start_main') as menu:
            boss.kilix_windows_key()
        menu.assert_called_once_with(window)

    def test_start_menu_exposes_full_hierarchy(self) -> None:
        boss = object.__new__(Boss)
        window = object()
        with patch.object(boss, '_kilix_start_choose') as choose:
            boss._kilix_start_main(window)  # type: ignore[arg-type]
        title, choices, actions = choose.call_args.args[1:]
        self.ae(title, 'Kilix Start')
        for label in ('Tabs', 'Sessions', 'Programs', 'Options', 'Software',
                      'Tools', 'Places', 'Power'):
            self.assertTrue(any(label in choice for choice in choices), label)
        self.assertEqual(set('tspowklnrduaq'), set(actions))

    def test_start_menu_offers_both_tab_bar_edges(self) -> None:
        boss = object.__new__(Boss)
        window = object()
        with patch.object(boss, '_kilix_start_choose') as choose:
            boss._kilix_start_tab_edge(window)  # type: ignore[arg-type]
        _title, choices, actions = choose.call_args.args[1:]
        self.assertTrue(any('Top' in choice for choice in choices))
        self.assertTrue(any('Bottom' in choice for choice in choices))
        self.assertIn('tab_bar_edge=top', actions['t'])
        self.assertIn('tab_bar_edge=bottom', actions['b'])

    def test_every_start_menu_accelerator_occurs_in_its_label(self) -> None:
        boss = object.__new__(Boss)
        window = object()
        methods = (
            '_kilix_start_main', '_kilix_start_sessions',
            '_kilix_start_programs', '_kilix_start_options',
            '_kilix_start_tab_edge', '_kilix_start_software',
            '_kilix_start_components', '_kilix_start_tools',
            '_kilix_start_places', '_kilix_start_power',
        )
        for method in methods:
            with patch.object(boss, '_kilix_start_choose') as choose:
                getattr(boss, method)(window)
            choices = choose.call_args.args[2]
            for choice in choices:
                key, label = choice.split(':', 1)
                self.assertIn(key.casefold(), label.casefold(), choice)

    def test_start_menu_uses_a_portable_menu_mark(self) -> None:
        with (
            patch.dict(os.environ, {'KILIX_CHROME_START_MENU': '1'}),
            patch('kitty.kilix_windows.in_pleb_session', return_value=True),
            patch('kitty.kilix_chrome.settings.values', return_value=({}, False)),
        ):
            self.ae(kilix_battery.start_menu_segment(), (
                ' ☰ ', START_MENU_ACTION))

    def test_left_start_segment_reserves_tabs_and_is_clickable(self) -> None:
        self.set_options({
            'tab_bar_align': 'center',
            'tab_bar_edge': BOTTOM_EDGE,
            'tab_bar_style': 'separator',
            'tab_title_template': '{title}',
        })
        central = region(0, 0, 1000, 160)
        tab_bar = region(0, 160, 1000, 180)
        boss = DummyBoss()
        with (
            patch('kitty.tab_bar.cell_size_for_window', return_value=(10, 20)),
            patch('kitty.tab_bar.viewport_for_window', return_value=(
                central, tab_bar, 1000, 180, 10, 20)),
            patch('kitty.tab_bar.set_tab_bar_render_data'),
            patch('kitty.tab_bar.get_boss', return_value=boss),
            patch('kitty.tab_bar.ensure_chrome_timers'),
            patch('kitty.tab_bar.start_menu_segment', return_value=(
                ' S ', START_MENU_ACTION)),
            patch('kitty.tab_bar.window_entries', return_value=()),
            patch.object(TabBar, 'right_status_segments', return_value=()),
        ):
            tb = TabBar(1)
            tb.layout()
            tb.update((TabBarData(title='one', tab_id=1, is_active=True),))

        self.ae(tb.left_status_end, 3)
        self.assertGreaterEqual(tb.tab_extents[0].x.start, 3)
        self.ae(tuple(a.action for a in tb.action_extents), (
            START_MENU_ACTION,))
        self.ae(tb.action_at(5, 165), START_MENU_ACTION)
        self.assertNotEqual(tb.tab_id_at(5, 165), 1)

    def test_clock_status_is_bright_and_clickable(self) -> None:
        opts = self.set_options({
            'foreground': Color(0xd3, 0xd7, 0xcf),
            'tab_bar_edge': BOTTOM_EDGE,
            'tab_bar_style': 'separator',
            'tab_title_template': '{title}',
        })
        central = region(0, 0, 1000, 160)
        tab_bar = region(0, 160, 1000, 180)
        boss = DummyBoss()

        with (
            patch.dict(os.environ, {
                'GPU_TERMINAL_SETTINGS_FILE': '/kilix-test/missing-settings.conf',
                'KILIX_CHROME_BATTERY': '0',
                'KILIX_CHROME_CALENDAR': '1',
                'KILIX_CHROME_CLOCK': '1',
                'KILIX_CHROME_CLOCK_FORMAT': 'DATE',
                'KILIX_CHROME_DICTATE': '0',
                'KILIX_CHROME_NETWORK': '1',
                'KILIX_CHROME_SPEAK': '0',
                'KILIX_CHROME_START_MENU': '0',
                'KILIX_CHROME_TEMPERATURE': '0',
                'KILIX_CHROME_VOLUME': '1',
            }),
            patch('kitty.tab_bar.cell_size_for_window', return_value=(10, 20)),
            patch('kitty.tab_bar.viewport_for_window', return_value=(central, tab_bar, 1000, 180, 10, 20)),
            patch('kitty.tab_bar.set_tab_bar_render_data'),
            patch('kitty.tab_bar.get_boss', return_value=boss),
            patch('kitty.tab_bar.window_entries', return_value=()),
            patch('kitty.tab_bar.ensure_chrome_timers'),
            patch('kitty.tab_bar.thermal_segment', return_value=(
                ' thermal ', THERMAL_WIDGET_ACTION, 42)),
            patch('kitty.tab_bar.window_entries', return_value=()),
        ):
            tb = TabBar(1)
            tb.layout()
            segments = tb.right_status_segments()
            tb.update((TabBarData(title='one', tab_id=1, is_active=True),))

        self.ae(tuple(action for _, action, _ in segments), (
            THERMAL_WIDGET_ACTION, VOLUME_WIDGET_ACTION, NETWORK_WIDGET_ACTION,
            CALENDAR_WIDGET_ACTION, DATE_WIDGET_ACTION,
        ))
        self.ae(segments[0][2], 42)
        self.assertTrue(all(
            fg == as_rgb(color_as_int(opts.foreground))
            for _, _, fg in segments[1:]
        ))
        self.ae(tuple(ae.action for ae in tb.action_extents), (
            THERMAL_WIDGET_ACTION, VOLUME_WIDGET_ACTION, NETWORK_WIDGET_ACTION,
            CALENDAR_WIDGET_ACTION, DATE_WIDGET_ACTION,
        ))
        for extent in tb.action_extents:
            x = tb.window_geometry.left + extent.x.start * tb.cell_width + 1
            y = tb.window_geometry.top + 1
            self.ae(tb.action_at(x, y), extent.action)

    def test_thermal_status_uses_hottest_sensor_and_policy_colors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'sys'
            zone = root / 'class' / 'thermal' / 'thermal_zone0'
            hwmon = root / 'class' / 'hwmon' / 'hwmon0'
            zone.mkdir(parents=True)
            hwmon.mkdir(parents=True)
            (zone / 'temp').write_text('65000\n')
            sensor = hwmon / 'temp1_input'
            settings_path = Path(directory) / 'missing-settings.conf'
            with (
                patch.dict(os.environ, {
                    'GPU_TERMINAL_SETTINGS_FILE': str(settings_path),
                    'KILIX_CHROME_TEMPERATURE': '1',
                    'KILIX_THERMAL_SYS_ROOT': str(root),
                }),
                patch.object(chrome_settings, '_cache_signature', None),
            ):
                colors = []
                for raw, shown in (('79940\n', '79.9°'),
                                   ('79960\n', '80.0°'),
                                   ('89940\n', '89.9°'),
                                   ('89960\n', '90.0°')):
                    sensor.write_text(raw)
                    kilix_battery._THERMAL_CACHE_UNTIL = 0.0
                    segment = kilix_battery.thermal_segment()
                    self.assertIsNotNone(segment)
                    text, action, color = segment
                    self.assertIn(shown, text)
                    self.ae(action, THERMAL_WIDGET_ACTION)
                    colors.append(color)
            self.ae(colors, [
                kilix_battery._BATTERY_HIGH,
                kilix_battery._BATTERY_MID,
                kilix_battery._BATTERY_MID,
                kilix_battery._BATTERY_LOW,
            ])

    def test_kilix_temps_source_target_forces_graphics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            project = source / 'kilix-temps'
            executable = project / 'build' / 'kilix-temps'
            executable.parent.mkdir(parents=True)
            executable.write_text('#!/bin/sh\n')
            executable.chmod(0o755)
            with patch.dict(os.environ, {
                    'GPU_TERMINAL_SOURCE_HOME': str(source)}, clear=True), \
                    patch('kitty.kilix_battery.which', return_value=None):
                self.ae(kilix_battery.kilix_temps_target(), (
                    [str(executable), '--graphics'], str(project)))

    def test_kilix_temps_installed_target_precedes_incomplete_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            raw_launcher = source / 'kilix-temps' / 'kilix-temps'
            raw_launcher.parent.mkdir(parents=True)
            raw_launcher.write_text('#!/bin/sh\n')
            raw_launcher.chmod(0o755)
            with patch.dict(os.environ, {
                    'GPU_TERMINAL_SOURCE_HOME': str(source)}, clear=True), \
                    patch('kitty.kilix_battery.which',
                          return_value='/usr/local/bin/kilix-temps'):
                self.ae(kilix_battery.kilix_temps_target(), (
                    ['/usr/local/bin/kilix-temps', '--graphics'], None))

    def test_kilix_temps_falls_back_to_pinned_kilix_installer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kilix = Path(directory) / 'kilix'
            kilix.write_text('#!/bin/sh\n')
            kilix.chmod(0o755)
            with patch.dict(os.environ, {
                    'GPU_TERMINAL_SOURCE_HOME': str(Path(directory) / 'source'),
                    'KILIX_HOME': directory}, clear=True), \
                    patch('kitty.kilix_battery.which', return_value=None):
                self.ae(kilix_battery.kilix_temps_target(), (
                    [str(kilix), 'temps', '--graphics'], None))

    def test_thermal_reader_rejects_non_finite_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'temp'
            for value in ('nan\n', 'inf\n', '-inf\n'):
                path.write_text(value)
                self.assertIsNone(kilix_battery._read_temperature(str(path)))

    def test_thermal_status_prefers_shared_telemetry(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                'kitty.kilix_telemetry.hottest_celsius',
                return_value=83.25,
            ),
            patch(
                'kitty.kilix_battery._read_thermal_info_uncached',
                side_effect=AssertionError('sysfs fallback should not run'),
            ),
        ):
            kilix_battery._THERMAL_CACHE_UNTIL = 0.0
            info = kilix_battery.thermal_info()
        self.assertIsNotNone(info)
        self.ae(info.celsius, 83.2)
        self.ae(info.level, 'yellow')

    def test_horizontal_multi_row_hit_testing_and_hidden_reset(self) -> None:
        self.set_options({
            'tab_bar_edge': BOTTOM_EDGE,
            'tab_bar_style': 'separator',
            'tab_title_template': '{title}',
        })
        central = region(0, 0, 3600, 160)
        tab_bar = region(0, 160, 3600, 200)
        hidden = region(0, 200, 0, 200)
        geometries: list[tuple[int, int, int, int]] = []
        boss = DummyBoss()

        with (
            patch.dict(os.environ, {
                'GPU_TERMINAL_SETTINGS_FILE': '/kilix-test/missing-settings.conf',
                'KILIX_CHROME_BATTERY': '0',
                'KILIX_CHROME_CALENDAR': '0',
                'KILIX_CHROME_CLOCK': '0',
                'KILIX_CHROME_NETWORK': '0',
                'KILIX_CHROME_TEMPERATURE': '0',
                'KILIX_CHROME_VOLUME': '0',
            }),
            patch('kitty.tab_bar.cell_size_for_window', return_value=(10, 20)),
            patch('kitty.tab_bar.viewport_for_window', return_value=(central, tab_bar, 3600, 200, 10, 20)) as viewport,
            patch('kitty.tab_bar.set_tab_bar_render_data', side_effect=lambda *args: geometries.append(args[2:6])),
            patch('kitty.tab_bar.get_boss', return_value=boss),
            patch('kitty.tab_bar.ensure_chrome_timers'),
        ):
            tb = TabBar(1)
            tb.layout()
            tb.update(tuple(TabBarData(title=f'tab-{i}', tab_id=i) for i in range(1, 32)))

            self.ae(len(tb.tab_extents), 31)
            self.ae(tb.tab_extents[29].y, (0, 0))
            self.ae(tb.tab_extents[30].y, (1, 1))
            last = tb.tab_extents[-1]
            self.ae(tb.tab_id_at(last.x.start * 10 + 5, 185), 31)
            self.ae(tb.drag_order_coordinate(20, 181), (181, 20))

            viewport.return_value = (region(0, 0, 3600, 200), hidden, 3600, 200, 10, 20)
            tb.layout()

            self.assertFalse(tb.laid_out_once)
            self.ae(tb.tab_extents, ())
            self.ae(tb.action_extents, ())
            self.ae(geometries[-1], (0, 0, 0, 0))

            viewport.return_value = (central, tab_bar, 3600, 200, 10, 20)
            tb.layout()
            tb.update(tuple(TabBarData(title=f'tab-{i}', tab_id=i) for i in range(1, 4)))

        self.assertTrue(tb.laid_out_once)
        self.ae(len(tb.tab_extents), 3)
        self.assertNotEqual(geometries[-1], (0, 0, 0, 0))

    def test_vertical_tab_bar_hit_testing(self) -> None:
        self.set_options({
            'tab_bar_edge': LEFT_EDGE,
            'tab_bar_style': 'separator',
            'tab_title_template': '{title}',
        })
        central = region(120, 0, 400, 160)
        tab_bar = region(0, 0, 120, 160)
        geometries: list[tuple[int, int, int, int]] = []
        boss = DummyBoss()

        with (
            patch('kitty.tab_bar.cell_size_for_window', return_value=(10, 20)),
            patch('kitty.tab_bar.viewport_for_window', return_value=(central, tab_bar, 400, 160, 10, 20)),
            patch('kitty.tab_bar.set_tab_bar_render_data', side_effect=lambda *args: geometries.append(args[2:6])),
            patch('kitty.tab_bar.get_boss', return_value=boss),
            patch('kitty.tab_bar.ensure_chrome_timers'),
        ):
            tb = TabBar(1)
            tb.layout()
            tb.update((
                TabBarData(title='one', tab_id=1, is_active=True),
                TabBarData(title='two', tab_id=2),
                TabBarData(title='three', tab_id=3),
            ))

        self.assertTrue(tb.is_vertical)
        self.ae(geometries[-1], (0, 0, 120, 160))
        self.ae(tb.drag_axis_coordinate(5, 35), 35)
        self.ae(tb.drag_order_coordinate(5, 35), (35, 0))
        self.ae(tb.tab_id_at(5, 10), 1)
        self.ae(tb.tab_id_at(110, 35), 1)
        self.ae(tb.tab_id_at(60, 55), 2)
        self.ae(tb.tab_id_at(60, 95), 3)
        self.ae(tb.tab_id_at(60, 135), 0)
        self.ae(tb.tab_id_at(180, 10), 0)

    def test_vertical_tab_bar_alignment(self) -> None:
        self.set_options({
            'tab_bar_align': 'end',
            'tab_bar_edge': LEFT_EDGE,
            'tab_bar_style': 'separator',
            'tab_title_template': '{title}',
        })
        central = region(120, 0, 400, 160)
        tab_bar = region(0, 0, 120, 160)
        boss = DummyBoss()

        with (
            patch('kitty.tab_bar.cell_size_for_window', return_value=(10, 20)),
            patch('kitty.tab_bar.viewport_for_window', return_value=(central, tab_bar, 400, 160, 10, 20)),
            patch('kitty.tab_bar.set_tab_bar_render_data'),
            patch('kitty.tab_bar.get_boss', return_value=boss),
            patch('kitty.tab_bar.ensure_chrome_timers'),
        ):
            tb = TabBar(1)
            tb.layout()
            tb.update((
                TabBarData(title='one', tab_id=1, is_active=True),
                TabBarData(title='two', tab_id=2),
            ))

        self.ae(tb.tab_extents[0].y, (4, 5))
        self.ae(tb.tab_extents[1].y, (6, 7))
        self.ae(tb.tab_id_at(5, 10), 0)
        self.ae(tb.tab_id_at(5, 110), 1)
        self.ae(tb.tab_id_at(5, 150), 2)

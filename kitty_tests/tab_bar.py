#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import os
from unittest.mock import patch

from kitty.fast_data_types import BOTTOM_EDGE, LEFT_EDGE, Color, Region
from kitty.kilix_battery import (
    CALENDAR_WIDGET_ACTION,
    DATE_WIDGET_ACTION,
    NETWORK_WIDGET_ACTION,
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
                'KILIX_CHROME_NETWORK': '1',
            }),
            patch('kitty.tab_bar.cell_size_for_window', return_value=(10, 20)),
            patch('kitty.tab_bar.viewport_for_window', return_value=(central, tab_bar, 1000, 180, 10, 20)),
            patch('kitty.tab_bar.set_tab_bar_render_data'),
            patch('kitty.tab_bar.get_boss', return_value=boss),
            patch('kitty.tab_bar.ensure_chrome_timers'),
        ):
            tb = TabBar(1)
            tb.layout()
            segments = tb.right_status_segments()
            tb.update((TabBarData(title='one', tab_id=1, is_active=True),))

        self.ae(tuple(action for _, action, _ in segments), (
            NETWORK_WIDGET_ACTION, CALENDAR_WIDGET_ACTION, DATE_WIDGET_ACTION,
        ))
        self.assertTrue(all(
            fg == as_rgb(color_as_int(opts.foreground))
            for _, _, fg in segments
        ))
        self.ae(tuple(ae.action for ae in tb.action_extents), (
            NETWORK_WIDGET_ACTION, CALENDAR_WIDGET_ACTION, DATE_WIDGET_ACTION,
        ))
        for extent in tb.action_extents:
            x = tb.window_geometry.left + extent.x.start * tb.cell_width + 1
            y = tb.window_geometry.top + 1
            self.ae(tb.action_at(x, y), extent.action)

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

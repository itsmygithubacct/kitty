from types import SimpleNamespace
from unittest.mock import patch

from kitty.fast_data_types import BOTTOM_EDGE, LEFT_EDGE, RIGHT_EDGE, TOP_EDGE, Region
from kitty.kilix_chrome.popup import ChromePopup, dismiss_popup, popup_geometry, popup_target
from kitty.layout.base import lgd
from kitty.layout.interface import Grid, Horizontal, Splits, Stack, Tall
from kitty.types import WindowGeometry
from kitty.window import Window as RealWindow

from . import BaseTest
from .layout import Window, create_layout, create_windows


class TestKilixPopup(BaseTest):
    def test_anchor_and_clamping_use_the_whole_viewport(self):
        bounds, cell = (20, 40, 1020, 640), (10, 20)
        cases = (
            (TOP_EDGE, (20, 0), (20, 40)),
            (BOTTOM_EDGE, (20, 640), (20, 400)),
            (TOP_EDGE, (990, 0), (520, 40)),
            (LEFT_EDGE, (0, 610), (20, 400)),
            (RIGHT_EDGE, (1020, 80), (520, 80)),
        )
        for edge, anchor, position in cases:
            with self.subTest(edge=edge, anchor=anchor):
                g = popup_geometry(bounds, cell, (50, 12), anchor, edge)
                self.ae((g.left, g.top), position)
                self.ae((g.xnum, g.ynum), (50, 12))
                self.assertLessEqual(g.right, bounds[2])
                self.assertLessEqual(g.bottom, bounds[3])
        small = popup_geometry((0, 0, 153, 95), cell, (80, 30), (99, 0), TOP_EDGE)
        self.ae(small, WindowGeometry(3, 0, 153, 80, 15, 4))

    def test_panes_keep_geometry_and_visibility_under_a_popup(self):
        self.set_options({'tab_bar_style': 'hidden'})
        for layout_class in (Splits, Grid, Horizontal, Tall, Stack):
            with self.subTest(layout=layout_class):
                layout = create_layout(layout_class)
                def dimensions(_windows):
                    lgd.central = Region((0, 0, 799, 479, 800, 480))
                    lgd.cell_width, lgd.cell_height = 8, 16
                layout._set_dimensions = dimensions
                windows = create_windows(layout, 3)
                layout(windows)
                owner = windows.active_window
                panes = tuple(windows)
                before = [(w.geometry, w.is_visible_in_layout) for w in panes]
                popup = Window(99)
                popup.kilix_popup = ChromePopup('menu', 50, 12, owner.id)
                popup.geometry = WindowGeometry(0, 0, 400, 192, 50, 12)
                layout.add_window(windows, popup, overlay_for=owner.id)
                layout(windows)
                self.ae(windows.num_groups, 3)
                self.ae([(w.geometry, w.is_visible_in_layout) for w in panes], before)
                self.ae(popup.geometry, WindowGeometry(0, 0, 400, 192, 50, 12))
                self.assertTrue(popup.is_visible_in_layout)
                self.ae(windows.active_window, popup)
                self.ae(windows.active_group.geometry, owner.geometry)
                # Popup-only focus and geometry must not become layout state.
                self.assertNotIn(99, windows.active_group.serialize_layout_state()['window_ids'])
                windows.remove_window(popup)
                layout(windows)
                self.ae(windows.active_window, owner)
                self.ae([(w.geometry, w.is_visible_in_layout) for w in panes], before)

    def test_normal_overlays_still_cover_only_their_pane(self):
        self.set_options({'tab_bar_style': 'hidden'})
        layout = create_layout(Horizontal)
        windows = create_windows(layout, 2)
        owner = windows.active_window
        overlay = Window(77)
        layout.add_window(windows, overlay, overlay_for=owner.id)
        layout.update_visibility(windows)
        self.assertFalse(owner.is_visible_in_layout)
        self.assertTrue(overlay.is_visible_in_layout)
        self.assertTrue(windows.id_map[2].is_visible_in_layout)

    def test_cancelled_popup_cannot_reappear_on_late_ready(self):
        window = SimpleNamespace(kilix_popup=ChromePopup('menu', 50, 12, cancelled=True))
        # No tab/PTY access is valid after cancellation.
        RealWindow.handle_overlay_ready(window, memoryview(b''))

    def test_toggle_replace_and_dismiss_preserve_the_original_target(self):
        class Tab:
            def __contains__(self, window):
                return window in self.windows

            def set_active_window(self, window):
                self.active_window = window

        tab = Tab()
        owner = SimpleNamespace(id=1, kilix_popup=None, tabref=lambda: tab)
        popup = SimpleNamespace(
            id=2, kilix_popup=ChromePopup('menu', 50, 12, 1),
            tabref=lambda: tab, keys_redirected_till_ready_from=0,
            set_visible_in_layout=lambda visible: None)
        tab.windows, tab.active_window = [owner, popup], popup
        closed = []
        boss = SimpleNamespace(window_id_map={1: owner, 2: popup}, mark_window_for_close=closed.append)
        with patch('kitty.fast_data_types.get_boss', return_value=boss):
            self.assertIsNone(popup_target(popup, 'menu'))
            self.assertIs(tab.active_window, owner)
            self.ae(closed, [popup])
            self.assertFalse(dismiss_popup(tab))
            popup.kilix_popup.cancelled = False
            self.assertIs(popup_target(popup, 'calendar'), owner)
            self.assertTrue(popup.kilix_popup.cancelled)

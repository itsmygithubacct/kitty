#!/usr/bin/env python
# License: GPL v3

import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kitty.boss import Boss
from kitty.layout.base import DragOverlayMode
from kitty.tabs import Tab, TabManager, WindowBeingDropped
from kitty.types import WindowGeometry

from . import BaseTest


class TargetTab:

    def __init__(self, window, mode=DragOverlayMode.free):
        self.window = window
        self.current_layout = SimpleNamespace(drag_overlay_mode=mode)

    def __iter__(self):
        return iter((self.window,))


class TestTabDragSplits(BaseTest):

    def test_only_a_single_pane_tab_can_be_merged_by_its_tab_drag(self) -> None:
        main = object()
        overlay = object()
        tab = object.__new__(Tab)
        tab.windows = SimpleNamespace(
            num_groups=1, active_group_main=main, active_window=overlay)
        self.assertIs(tab.window_for_tab_split_drag, main)

        tab.windows.num_groups = 2
        self.assertIsNone(tab.window_for_tab_split_drag)

    def test_drop_target_uses_the_pane_diagonals(self) -> None:
        dest = SimpleNamespace(
            id=20,
            geometry=WindowGeometry(0, 0, 400, 200, 40, 10),
            show_title_bar=False,
        )
        manager = object.__new__(TabManager)
        manager.os_window_id = 9
        manager.tabs = [TargetTab(dest)]
        manager._active_tab_idx = 0
        central = SimpleNamespace(left=100, top=50, right=500, bottom=250)

        with patch(
                'kitty.fast_data_types.viewport_for_window',
                return_value=(central,)):
            self.assertEqual(
                manager._window_drop_target_at(110, 150),
                WindowBeingDropped(dest.id, 1))
            self.assertEqual(
                manager._window_drop_target_at(490, 150),
                WindowBeingDropped(dest.id, 2))
            self.assertEqual(
                manager._window_drop_target_at(300, 60),
                WindowBeingDropped(dest.id, 3))
            self.assertEqual(
                manager._window_drop_target_at(300, 240),
                WindowBeingDropped(dest.id, 4))

        dest.show_title_bar = True
        with (
            patch(
                'kitty.fast_data_types.viewport_for_window',
                return_value=(central,)),
            patch(
                'kitty.fast_data_types.cell_size_for_window',
                return_value=(10, 20)),
            patch(
                'kitty.tabs.get_options',
                return_value=SimpleNamespace(window_title_bar='top')),
        ):
            self.assertEqual(
                manager._window_drop_target_at(300, 55),
                WindowBeingDropped(dest.id, 5))

        dest.show_title_bar = False
        manager.tabs[0].current_layout.drag_overlay_mode = DragOverlayMode.full
        with patch(
                'kitty.fast_data_types.viewport_for_window',
                return_value=(central,)):
            self.assertEqual(
                manager._window_drop_target_at(300, 150),
                WindowBeingDropped(dest.id, 0))

    def test_tab_body_drop_inserts_its_pane_in_the_selected_direction(self) -> None:
        source_tab = SimpleNamespace()
        source = SimpleNamespace(id=10)
        source.tabref = Mock(return_value=source_tab)
        source_tab.window_for_tab_split_drag = source

        target_tab = TargetTab(None)
        dest = SimpleNamespace(id=20)
        dest.tabref = Mock(return_value=target_tab)
        target_tab.window = dest

        manager = object.__new__(TabManager)
        manager.tabs = [target_tab]
        manager._active_tab_idx = 0
        manager._set_drag_target_window = Mock()
        boss = SimpleNamespace(
            window_id_map={dest.id: dest},
            _insert_window_in_direction=Mock(),
        )

        with patch('kitty.tabs.get_boss', return_value=boss):
            self.assertTrue(manager.on_tab_split_drop(
                source_tab, target=WindowBeingDropped(dest.id, 3)))

        manager._set_drag_target_window.assert_called_once_with(0)
        boss._insert_window_in_direction.assert_called_once_with(
            source, dest, 'top')

    def test_cross_os_window_tab_drop_prefers_a_pane_split(self) -> None:
        source_tab = SimpleNamespace(id=42, os_window_id=1)
        target_manager = SimpleNamespace(
            os_window_id=2,
            on_tab_split_drop=Mock(return_value=True),
            on_tab_drop=Mock(),
        )
        boss = object.__new__(Boss)
        boss.os_window_map = {2: target_manager}
        boss.tab_for_id = Mock(return_value=source_tab)
        boss._move_tab_to = Mock()
        boss._clear_tab_drag_targets = Mock()
        central = SimpleNamespace(left=0, top=20, right=800, bottom=600)
        tab_bar = SimpleNamespace(left=0, top=0, right=800, bottom=20)
        mime = f'application/net.kovidgoyal.kitty-tab-{os.getpid()}'

        with patch(
                'kitty.boss.viewport_for_window',
                return_value=(central, tab_bar)):
            boss.on_drop(2, {mime: b'42'}, True, 100, 100)

        target_manager.on_tab_split_drop.assert_called_once_with(
            source_tab, 100, 100)
        target_manager.on_tab_drop.assert_not_called()
        boss._move_tab_to.assert_not_called()
        boss._clear_tab_drag_targets.assert_called_once_with()

    def test_tab_drag_over_another_os_window_previews_the_split(self) -> None:
        source_window = SimpleNamespace(id=10)
        source_tab = SimpleNamespace(
            id=42, os_window_id=1,
            window_for_tab_split_drag=source_window,
        )
        source_manager = SimpleNamespace(
            os_window_id=1,
            on_window_drop_move=Mock(), on_tab_drop_move=Mock())
        target_manager = SimpleNamespace(
            os_window_id=2,
            on_window_drop_move=Mock(), on_tab_drop_move=Mock())
        boss = object.__new__(Boss)
        boss.os_window_map = {1: source_manager, 2: target_manager}
        boss.tab_for_id = Mock(return_value=source_tab)
        central = SimpleNamespace(left=0, top=20, right=800, bottom=600)
        tab_bar = SimpleNamespace(left=0, top=0, right=800, bottom=20)

        with (
            patch(
                'kitty.boss.viewport_for_window',
                return_value=(central, tab_bar)),
            patch(
                'kitty.boss.get_tab_being_dragged',
                return_value=(source_tab.id, True, 0, 0)),
            patch('kitty.boss.change_drag_thumbnail'),
        ):
            boss.on_drop_move(2, 100, 100, True, False)

        source_manager.on_window_drop_move.assert_called_once_with(
            source_window.id, False, 100, 100, body_only=True)
        target_manager.on_window_drop_move.assert_called_once_with(
            source_window.id, True, 100, 100, body_only=True)
        source_manager.on_tab_drop_move.assert_called_once_with(
            source_tab.id, False, 100, 100)
        target_manager.on_tab_drop_move.assert_called_once_with(
            source_tab.id, False, 100, 100)

    def test_wayland_tab_drag_uses_the_previewed_split_target(self) -> None:
        source_tab = SimpleNamespace(id=42)
        target = WindowBeingDropped(20, 2)
        target_manager = SimpleNamespace(
            window_being_dropped=target,
            tab_being_dropped=None,
            on_tab_split_drop=Mock(return_value=True),
        )
        boss = object.__new__(Boss)
        boss.os_window_map = {2: target_manager}
        boss.tab_for_id = Mock(return_value=source_tab)
        boss._clear_tab_drag_targets = Mock()
        boss._move_tab_to = Mock()
        mime = f'application/net.kovidgoyal.kitty-tab-{os.getpid()}'

        with patch(
                'kitty.boss.get_tab_being_dragged',
                return_value=(source_tab.id, True, 0, 0)):
            boss.on_drag_source_finished(
                False, False, mime, 0, {mime: b'42'}, True)

        target_manager.on_tab_split_drop.assert_called_once_with(
            source_tab, target=target)
        boss._clear_tab_drag_targets.assert_called_once_with()
        boss._move_tab_to.assert_not_called()

        target_manager.on_tab_split_drop.reset_mock()
        boss._clear_tab_drag_targets.reset_mock()
        with patch(
                'kitty.boss.get_tab_being_dragged',
                return_value=(source_tab.id, True, 0, 0)):
            boss.on_drag_source_finished(
                False, True, mime, 0, {mime: b'42'}, True)
        target_manager.on_tab_split_drop.assert_not_called()
        boss._clear_tab_drag_targets.assert_called_once_with()

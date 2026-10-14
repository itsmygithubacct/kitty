#!/usr/bin/env python
"""Focused behavioral regressions for the XDND successor's Python paths."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from kitty.boss import Boss
from kitty.window import EdgeWidths


class XDNDPythonRegressions(unittest.TestCase):
    def test_f5_selection_expansion_reaches_child_argv(self):
        boss = Boss.__new__(Boss)
        window = SimpleNamespace(id=7)
        with patch.object(Boss, 'process_stdin_source', return_value=(None, None)), \
             patch('kitty.boss.data_for_at', return_value='chosen text') as selection:
            ans = boss.special_window_for_cmd(['viewer', '@selection'], window=window)
        self.assertEqual(ans.cmd, ['viewer', 'chosen text'])
        selection.assert_called_once_with(window, '@selection')

    def test_f6_spacing_serializes_each_edge(self):
        widths = EdgeWidths()
        widths.left, widths.right, widths.top, widths.bottom = 1, 2, 3, 4
        self.assertEqual(
            list(widths.as_launch_args('margin')),
            ['--spacing=margin-left=1', '--spacing=margin-right=2',
             '--spacing=margin-top=3', '--spacing=margin-bottom=4'],
        )

    def test_f7_selection_routes_to_tab_owning_manager(self):
        boss = Boss.__new__(Boss)
        chosen = SimpleNamespace(id=22, os_window_id=8)
        current = SimpleNamespace(id=11, os_window_id=3)
        window = SimpleNamespace(tabref=lambda: current)
        boss.window_for_dispatch = window
        active_manager = SimpleNamespace(set_active_tab=MagicMock(return_value=False))
        owner_manager = SimpleNamespace(set_active_tab=MagicMock(return_value=True))
        chosen.tab_manager_ref = lambda: owner_manager
        boss.os_window_map = {8: owner_manager}
        with patch.object(Boss, 'all_tabs', new_callable=PropertyMock, return_value=[current, chosen]), \
             patch.object(Boss, 'active_tab_manager', new_callable=PropertyMock, return_value=active_manager), \
             patch.object(Boss, 'choose_entry', autospec=True,
                          side_effect=lambda self, title, entries, callback: callback(chosen.id)), \
             patch.object(Boss, 'focus_os_window', autospec=True) as focus:
            boss.select_tab()
        owner_manager.set_active_tab.assert_called_once_with(chosen)
        active_manager.set_active_tab.assert_not_called()
        focus.assert_called_once_with(boss, chosen.os_window_id)

#!/usr/bin/env python
# License: GPL v3 Copyright: 2026

from types import SimpleNamespace
from unittest.mock import Mock, patch

from kitty.boss import Boss
from kitty.rc.set_spacing import parse_spacing_settings
from kitty.window import EdgeWidths

from . import BaseTest


class TestLaunchSessionCompletion(BaseTest):

    def test_pipe_special_window_uses_expanded_selection_and_keeps_context(self) -> None:
        window = SimpleNamespace(id=23)
        env = {'PIPE_CONTEXT': 'kept'}
        stdin = b'pipe input'
        boss = SimpleNamespace(
            active_window=window,
            process_stdin_source=Mock(return_value=(env, stdin)),
        )
        cwd_from = object()

        with patch('kitty.boss.data_for_at', return_value='chosen text') as get_selection:
            special_window = Boss.special_window_for_cmd(
                boss, ['tool', '@selection'], stdin='selection', cwd_from=cwd_from, as_overlay=True)

        self.ae(special_window.cmd, ['tool', 'chosen text'])
        self.ae(special_window.stdin, stdin)
        self.ae(special_window.env, env)
        self.assertIs(special_window.cwd_from, cwd_from)
        self.ae(special_window.overlay_for, window.id)
        boss.process_stdin_source.assert_called_once_with(window, 'selection')
        get_selection.assert_called_once_with(window, '@selection')

    def test_pipe_special_window_omits_empty_selection_argument(self) -> None:
        window = SimpleNamespace(id=23)
        boss = SimpleNamespace(
            active_window=window,
            process_stdin_source=Mock(return_value=(None, None)),
        )

        with patch('kitty.boss.data_for_at', return_value=''):
            special_window = Boss.special_window_for_cmd(boss, ['tool', '@selection', 'tail'])

        self.ae(special_window.cmd, ['tool', 'tail'])

    def test_edge_spacing_arguments_round_trip_each_explicit_edge(self) -> None:
        widths = EdgeWidths({'left': 1.0, 'right': 2.0, 'top': 3.0, 'bottom': 4.0})

        for prefix in ('padding', 'margin'):
            with self.subTest(prefix=prefix):
                args = tuple(widths.as_launch_args(prefix))
                settings = parse_spacing_settings(arg.split('=', 1)[1] for arg in args)
                self.ae(settings, {
                    f'{prefix}-left': 1.0,
                    f'{prefix}-right': 2.0,
                    f'{prefix}-top': 3.0,
                    f'{prefix}-bottom': 4.0,
                })

    def test_edge_spacing_arguments_omit_default_edges(self) -> None:
        widths = EdgeWidths({'left': None, 'right': 2.0, 'top': None, 'bottom': 4.0})
        args = tuple(widths.as_launch_args('padding'))
        self.ae(args, ('--spacing=padding-right=2.0', '--spacing=padding-bottom=4.0'))
        self.ae(parse_spacing_settings(arg.split('=', 1)[1] for arg in args), {
            'padding-right': 2.0, 'padding-bottom': 4.0,
        })

    def _tab_chooser(self, tabs, current_tab):
        window = SimpleNamespace(tabref=lambda: current_tab)
        boss = SimpleNamespace(
            all_tabs=tabs,
            window_for_dispatch=None,
            active_window=window,
            choose_entry=Mock(),
            focus_os_window=Mock(),
        )
        Boss.select_tab(boss)
        title, entries, callback = boss.choose_entry.call_args.args
        return boss, title, tuple(entries), callback

    def test_select_tab_uses_selected_tabs_owner_and_focuses_its_window(self) -> None:
        current_manager = Mock()
        owner_manager = Mock()
        owner_manager.set_active_tab.return_value = True
        current = SimpleNamespace(
            id=11, name='current', title='current title', num_window_groups=1,
            os_window_id=101, tab_manager_ref=lambda: current_manager,
        )
        foreign = SimpleNamespace(
            id=12, name='foreign', title='foreign title', num_window_groups=2,
            os_window_id=202, tab_manager_ref=lambda: owner_manager,
        )

        boss, title, entries, callback = self._tab_chooser([current, foreign], current)

        self.ae(title, 'Choose a tab to switch to')
        self.ae(entries, (
            (None, 'Current tab: current [1 window]'),
            (foreign.id, 'foreign [2 windows]'),
        ))
        callback(foreign.id)

        owner_manager.set_active_tab.assert_called_once_with(foreign)
        current_manager.set_active_tab.assert_not_called()
        boss.focus_os_window.assert_called_once_with(foreign.os_window_id)

    def test_select_tab_preserves_cancel_and_stale_tab_refusal(self) -> None:
        owner_manager = Mock()
        owner_manager.set_active_tab.return_value = True
        current = SimpleNamespace(
            id=21, name='current', title='current', num_window_groups=1,
            os_window_id=301, tab_manager_ref=lambda: owner_manager,
        )
        foreign = SimpleNamespace(
            id=22, name='foreign', title='foreign', num_window_groups=1,
            os_window_id=302, tab_manager_ref=lambda: owner_manager,
        )
        boss, _, _, callback = self._tab_chooser([current, foreign], current)

        callback(None)
        self.ae(owner_manager.set_active_tab.call_count, 0)
        boss.focus_os_window.assert_not_called()

        boss.all_tabs.remove(foreign)
        callback(foreign.id)
        self.ae(owner_manager.set_active_tab.call_count, 0)
        boss.focus_os_window.assert_not_called()

    def test_select_tab_does_not_focus_when_owner_rejects_stale_tab(self) -> None:
        owner_manager = Mock()
        owner_manager.set_active_tab.return_value = False
        current = SimpleNamespace(
            id=31, name='current', title='current', num_window_groups=1,
            os_window_id=401, tab_manager_ref=lambda: owner_manager,
        )
        foreign = SimpleNamespace(
            id=32, name='foreign', title='foreign', num_window_groups=1,
            os_window_id=402, tab_manager_ref=lambda: owner_manager,
        )
        boss, _, _, callback = self._tab_chooser([current, foreign], current)

        callback(foreign.id)

        owner_manager.set_active_tab.assert_called_once_with(foreign)
        boss.focus_os_window.assert_not_called()

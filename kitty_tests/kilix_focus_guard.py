#!/usr/bin/env python
# License: GPL v3

from types import SimpleNamespace
from unittest.mock import Mock, patch

from kitty.boss import Boss

from . import BaseTest


class FakeTab:

    def __init__(self, *windows):
        self.windows = list(windows)
        self.active_window = self.windows[0]

    def __iter__(self):
        return iter(self.windows)

    def __contains__(self, window):
        return window in self.windows

    def set_active_window(self, window, for_keep_focus=None):
        self.active_window = window


class FakeTabManager:

    def __init__(self, os_window_id, *tabs):
        self.os_window_id = os_window_id
        self.tabs = list(tabs)
        self.active_tab = self.tabs[0]

    def __iter__(self):
        return iter(self.tabs)

    def set_active_tab(self, tab, for_keep_focus=None):
        self.active_tab = tab
        return True


def pane(os_window_id, window_id):
    return SimpleNamespace(os_window_id=os_window_id, id=window_id)


class TestBackgroundRemoteCommandFocus(BaseTest):

    def setUp(self):
        # OS window 1 has the keyboard: tab 1 holds the pane being typed in
        # and a neighbour; tab 2 is a background tab. OS window 2 is elsewhere.
        self.typing, self.neighbour, self.other = pane(1, 1), pane(1, 2), pane(1, 3)
        self.tab1, self.tab2 = FakeTab(self.typing, self.neighbour), FakeTab(self.other)
        self.tm1 = FakeTabManager(1, self.tab1, self.tab2)
        self.far = pane(2, 4)
        self.tm2 = FakeTabManager(2, FakeTab(self.far))
        self.boss = object.__new__(Boss)
        self.boss.os_window_map = {1: self.tm1, 2: self.tm2}
        self.focused = 1
        self.raise_ = Mock(return_value=True)
        for target, value in (
            ('kitty.boss.current_focused_os_window_id', lambda: self.focused),
            ('kitty.boss.last_focused_os_window_id', lambda: 1),
            ('kitty.boss.is_wayland', lambda: False),
        ):
            p = patch(target, side_effect=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch('kitty.boss._focus_os_window', self.raise_)
        p.start()
        self.addCleanup(p.stop)

    def run_cmd(self, command, window=None, self_window=None, **pcmd):
        with patch('kitty.remote_control.handle_cmd', side_effect=lambda *a: command() or {'ok': True}):
            return self.boss._execute_remote_command({'cmd': 'launch', **pcmd}, window, 0, self_window)

    def jump_everywhere(self):
        # What `focus-window`, `focus-tab` or a launch without --keep-focus do.
        self.tm1.set_active_tab(self.tab2)
        self.tab2.set_active_window(self.other)
        self.boss.set_active_window(self.far, switch_os_window_if_needed=True)

    def assert_left_alone(self):
        self.assertIs(self.tm1.active_tab, self.tab1)
        self.assertIs(self.tab1.active_window, self.typing)
        self.raise_.assert_not_called()

    def test_neighbour_pane_cannot_take_focus(self):
        self.assertEqual(self.run_cmd(self.jump_everywhere, window=self.neighbour), {'ok': True})
        self.assert_left_alone()

    def test_neighbour_pane_cannot_take_focus_over_the_socket(self):
        self.run_cmd(self.jump_everywhere, self_window=self.neighbour)
        self.assert_left_alone()

    def test_neighbour_pane_cannot_switch_panes_within_the_tab(self):
        self.run_cmd(lambda: self.tab1.set_active_window(self.neighbour), window=self.neighbour)
        self.assertIs(self.tab1.active_window, self.typing)

    def test_process_outside_any_pane_cannot_take_focus(self):
        self.run_cmd(self.jump_everywhere)
        self.assert_left_alone()

    def test_unfocused_kilix_is_not_raised_over_another_application(self):
        self.focused = 0
        self.run_cmd(lambda: self.boss.set_active_window(self.far, switch_os_window_if_needed=True), window=self.typing)
        self.raise_.assert_not_called()

    def test_pane_being_typed_in_moves_focus(self):
        self.run_cmd(self.jump_everywhere, window=self.typing)
        self.assertIs(self.tm1.active_tab, self.tab2)
        self.raise_.assert_called_once_with(2, True, '')

    def test_pane_being_typed_in_moves_focus_over_the_socket(self):
        self.run_cmd(self.jump_everywhere, self_window=self.typing)
        self.assertIs(self.tm1.active_tab, self.tab2)
        self.raise_.assert_called_once_with(2, True, '')

    def test_blocked_focus_request_reports_it(self):
        for cmd in ('focus-window', 'focus-tab'):
            self.tm1.set_active_tab(self.tab1)
            response = self.run_cmd(self.jump_everywhere, window=self.neighbour, cmd=cmd)
            self.assertFalse(response['ok'], cmd)
            self.assertIn('Focus stays', response['error'])

    def test_background_launch_still_succeeds(self):
        self.assertEqual(self.run_cmd(self.jump_everywhere, window=self.neighbour), {'ok': True})

    def test_focus_request_already_satisfied_succeeds(self):
        response = self.run_cmd(lambda: self.tab1.set_active_window(self.typing), window=self.neighbour, cmd='focus-window')
        self.assertEqual(response, {'ok': True})

    def test_focus_request_without_response_gets_none(self):
        # A caller that asked for no response is not reading one; an error
        # written back into its terminal would be garbage on the screen.
        with patch('kitty.remote_control.handle_cmd', side_effect=lambda *a: self.jump_everywhere()):
            response = self.boss._execute_remote_command(
                {'cmd': 'focus-window', 'no_response': True}, self.neighbour, 0, None)
        self.assertIsNone(response)
        self.assert_left_alone()

    def test_focus_request_from_the_typing_pane_succeeds(self):
        self.assertEqual(self.run_cmd(self.jump_everywhere, window=self.typing, cmd='focus-window'), {'ok': True})

    def test_raising_works_again_after_a_background_command(self):
        self.run_cmd(self.jump_everywhere, window=self.neighbour)
        self.boss.set_active_window(self.far, switch_os_window_if_needed=True)
        self.raise_.assert_called_once_with(2, True, '')

    def test_failed_background_command_still_restores(self):
        def fail():
            self.tm1.set_active_tab(self.tab2)
            raise RuntimeError('boom')
        response = self.run_cmd(fail, window=self.neighbour)
        self.assertFalse(response['ok'])
        self.assertIs(self.tm1.active_tab, self.tab1)
        self.boss.set_active_window(self.far, switch_os_window_if_needed=True)
        self.raise_.assert_called_once_with(2, True, '')

    def test_background_command_exiting_still_restores(self):
        # Command-line parsing inside a command raises SystemExit, which the
        # error handler does not catch.
        def leave():
            self.tm1.set_active_tab(self.tab2)
            raise SystemExit(1)
        with self.assertRaises(SystemExit):
            self.run_cmd(leave, window=self.neighbour)
        self.assertIs(self.tm1.active_tab, self.tab1)
        self.boss.set_active_window(self.far, switch_os_window_if_needed=True)
        self.raise_.assert_called_once_with(2, True, '')

    def test_closing_the_active_tab_is_not_undone(self):
        def close():
            self.tm1.tabs.remove(self.tab1)
            self.tm1.set_active_tab(self.tab2)
        self.run_cmd(close, window=self.other)
        self.assertIs(self.tm1.active_tab, self.tab2)

    def test_closing_the_active_pane_is_not_undone(self):
        def close():
            self.tab1.windows.remove(self.typing)
            self.tab1.set_active_window(self.neighbour)
        self.run_cmd(close, window=self.other)
        self.assertIs(self.tab1.active_window, self.neighbour)

# License: GPL v3
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kitty import desktop_close as close
from . import BaseTest


class Tab(list):
    id = 9
    confirm_close_window_id = 0

    def set_active_window(self, window):
        self.active_window = window


class DesktopClose(BaseTest):
    def setUp(self):
        self.provider = SimpleNamespace(id=3, user_vars={close.MARKER: '1'})
        self.other = SimpleNamespace(id=4, user_vars={})
        self.tab = Tab([self.other, self.provider])
        self.boss = SimpleNamespace(window_id_map={3: self.provider, 4: self.other},
                                    all_tabs=[self.tab], choose=Mock(return_value=None),
                                    close_tab_no_confirm=Mock(),
                                    handle_kilix_persistent_close_confirmation=Mock(), show_error=Mock())

    def prompt(self, **kwargs):
        self.assertTrue(close.show(self.boss, **kwargs))
        return self.boss.choose.call_args.args[1]

    def test_desktop_tab_route_precedes_other_close_dialogs(self):
        from kitty.boss import Boss
        boss = SimpleNamespace(confirm_desktop_close=Mock(return_value=True),
                               confirm_coding_close=Mock(), confirm_qemu_close=Mock())
        Boss.confirm_tab_close(boss, self.tab)
        boss.confirm_desktop_close.assert_called_once_with(tab=self.tab)
        boss.confirm_coding_close.assert_not_called()
        boss.confirm_qemu_close.assert_not_called()

    def test_ordinary_tab_does_not_offer_system_actions(self):
        self.assertFalse(close.show(self.boss, tab=Tab([self.other])))
        self.boss.choose.assert_not_called()

    def test_provider_tab_offers_background_and_keeps_no_as_default(self):
        callback = self.prompt(tab=self.tab)
        self.assertEqual(self.boss.choose.call_args.args[2:],
                         ('y:Yes', 'n:No', 'b:Background', 's:System shut down', 'r:System reboot', 'l:Log out of Pleb'))
        self.assertIn('Background keeps its panes running', self.boss.choose.call_args.args[0])
        self.assertEqual(self.boss.choose.call_args.kwargs['default'], 'n')
        with patch.object(close, 'request_action') as action:
            for answer in ('n', '', 'invalid'):
                callback(answer)
            action.assert_not_called()
        self.boss.close_tab_no_confirm.assert_not_called()

    def test_background_hides_the_tab_instead_of_closing_it(self):
        self.boss.background_tab = Mock()
        self.prompt(tab=self.tab)('b')
        self.boss.background_tab.assert_called_once_with(self.tab)
        self.boss.close_tab_no_confirm.assert_not_called()

    def test_yes_only_closes_the_original_tab(self):
        with patch.object(close, 'request_action') as action:
            self.prompt(tab=self.tab)('y')
            self.boss.close_tab_no_confirm.assert_called_once_with(self.tab)
            action.assert_not_called()

    def test_pane_yes_uses_persistent_close(self):
        self.prompt(window=self.provider)('y')
        self.assertEqual(self.boss.choose.call_args.args[2:],
                         ('y:Yes', 'n:No', 's:System shut down', 'r:System reboot', 'l:Log out of Pleb'))
        self.assertNotIn('Background', self.boss.choose.call_args.args[0])
        self.boss.handle_kilix_persistent_close_confirmation.assert_called_once_with(True, 3)

    def test_system_choices_do_not_close_the_desktop(self):
        callback = self.prompt(tab=self.tab)
        for answer in ('s', 'r', 'l'):
            with patch.object(close, 'request_action') as action:
                callback(answer)
                action.assert_called_once_with(self.boss, answer)
        self.boss.close_tab_no_confirm.assert_not_called()

    def test_replaced_or_finished_provider_cannot_authorize_an_action(self):
        callback = self.prompt(tab=self.tab)
        with patch.object(close, 'request_action') as action:
            self.boss.window_id_map[3] = SimpleNamespace(id=3, user_vars={close.MARKER: '1'})
            callback('s')
            callback('b')
            self.boss.window_id_map[3] = self.provider
            self.provider.user_vars.clear()
            callback('r')
            action.assert_not_called()
        self.boss.close_tab_no_confirm.assert_not_called()

    def test_restarted_provider_in_same_pane_invalidates_dialog(self):
        self.provider.user_vars[close.MARKER] = 'first-launch'
        callback = self.prompt(tab=self.tab)
        self.provider.user_vars.clear()
        self.provider.user_vars[close.MARKER] = 'second-launch'
        with patch.object(close, 'request_action') as action:
            for choice in ('y', 'b', 's', 'r', 'l'):
                callback(choice)
            action.assert_not_called()
        self.boss.close_tab_no_confirm.assert_not_called()

    def test_removed_tab_ignores_pending_answer(self):
        callback = self.prompt(tab=self.tab)
        self.boss.all_tabs = []
        with patch.object(close, 'request_action') as action:
            callback('b')
            callback('l')
            action.assert_not_called()

    def test_system_commands_are_exact_and_logout_is_session_scoped(self):
        self.assertEqual(close.action_command('s', {}), ['systemctl', '--no-ask-password', 'poweroff'])
        self.assertEqual(close.action_command('r', {}), ['systemctl', '--no-ask-password', 'reboot'])
        self.assertEqual(close.action_command('l', {'XDG_SESSION_ID': 'c4'}),
                         ['loginctl', 'terminate-session', '--', 'c4'])
        for session in ('', '../session', '4; reboot'):
            with self.assertRaises(ValueError):
                close.action_command('l', {'XDG_SESSION_ID': session})

    def test_denied_system_action_reports_error_without_closing(self):
        timers = []
        def start_thread(*, target, daemon):
            return SimpleNamespace(start=target)
        with patch.object(close, 'Thread', side_effect=start_thread), \
             patch.object(close, 'add_timer', side_effect=lambda f, *args: timers.append(f)), \
             patch.object(close.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr='Access denied')):
            close.request_action(self.boss, 's')
            timers.pop()(0)
        self.assertIn('Access denied', self.boss.show_error.call_args.args[1])
        self.boss.close_tab_no_confirm.assert_not_called()

from types import SimpleNamespace
from unittest.mock import Mock, patch

from kitty.kilix_chrome.layouts import LAYOUT_ACTION, show_layout_menu
from kitty.kilix_chrome.registry import dispatch
from kitty.tab_bar import TabBar

from . import BaseTest


class TestLayoutMenu(BaseTest):
    def make_menu(self, enabled=('splits', 'grid'), active='grid'):
        self.set_options()
        tab = SimpleNamespace(id=10, enabled_layouts=list(enabled), mark_tab_bar_dirty=Mock())

        def goto(name):
            tab.current_layout = SimpleNamespace(name=name.partition(':')[0], full_name=name)

        def enable(names):
            tab.enabled_layouts = list(names)
            # Match Tab.set_enabled_layouts, including option-bearing layouts.
            if tab.current_layout.name not in names:
                goto(names[0])

        goto(active)
        tab.goto_layout = Mock(side_effect=goto)
        tab.set_enabled_layouts = Mock(side_effect=enable)
        owner = SimpleNamespace(id=20, tabref=lambda: tab)
        boss = SimpleNamespace(window_id_map={20: owner}, choose=Mock())
        return boss, owner, tab

    def test_switch_and_manage_layouts(self):
        boss, owner, tab = self.make_menu()
        show_layout_menu(boss, owner)
        args = boss.choose.call_args.args
        self.assertIn('2:2. ● Grid', args)
        for choice in args[2:]:
            key, label = choice.split(':', 1)
            self.assertIn(key.lower(), label.lower())
        self.assertIn('1:1.   Splits — drag in all four directions', args)
        self.ae(boss.choose.call_args.kwargs['kilix_popup'].action, LAYOUT_ACTION)
        args[1]('1')
        self.ae(tab.current_layout.name, 'splits')
        show_layout_menu(boss, owner, True)
        boss.choose.call_args.args[1]('2')
        self.ae(tab.enabled_layouts, ['splits'])
        boss.choose.call_args.args[1]('1')
        self.ae(tab.enabled_layouts, ['splits'])  # Cannot disable the last layout.
        boss.choose.call_args.args[1]('2')
        self.ae(tab.enabled_layouts, ['splits', 'grid'])
        self.ae(tab.current_layout.name, 'splits')

    def test_disable_active_and_preserve_configured_variants(self):
        boss, owner, tab = self.make_menu(('splits', 'tall:bias=60'), 'tall:bias=60')
        show_layout_menu(boss, owner, True)
        boss.choose.call_args.args[1]('2')  # Enable grid without resetting tall options.
        self.ae(tab.current_layout.full_name, 'tall:bias=60')
        boss.choose.call_args.args[1]('3')  # Disable active tall; select a remaining layout.
        self.ae(tab.enabled_layouts, ['splits', 'grid'])
        self.ae(tab.current_layout.name, 'splits')

    def test_cancel_and_stale_owner_do_nothing(self):
        boss, owner, tab = self.make_menu()
        show_layout_menu(boss, owner)
        callback = boss.choose.call_args.args[1]
        callback('')
        callback('bad')
        boss.window_id_map.clear()
        callback('1')
        tab.goto_layout.assert_not_called()
        tab.set_enabled_layouts.assert_not_called()

    def test_chrome_control_and_dispatch_use_originating_tab(self):
        boss, owner, tab = self.make_menu()
        manager = SimpleNamespace(active_tab=SimpleNamespace(active_window=owner))
        with patch('kitty.fast_data_types.get_boss', return_value=boss), \
                patch('kitty.kilix_chrome.popup.popup_target', return_value=owner):
            self.assertTrue(dispatch(manager, LAYOUT_ACTION))
        self.assertIs(boss.choose.call_args.kwargs['window'], owner)
        with patch('kitty.tab_bar.start_menu_segment', return_value=None), \
                patch('kitty.tab_bar.get_options', return_value=SimpleNamespace(foreground=0)), \
                patch('kitty.tab_bar.color_as_int', return_value=0):
            self.ae(TabBar.left_status_segments(None)[0][:2], (' Layout ▾ ', LAYOUT_ACTION))

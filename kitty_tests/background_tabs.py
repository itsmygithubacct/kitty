#!/usr/bin/env python
# License: GPL v3
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kitty.boss import Boss, _BACKGROUND_KEYS, backgrounded_tab_label
from kitty.fast_data_types import (
    GLFW_MOUSE_BUTTON_LEFT,
    GLFW_MOUSE_BUTTON_RIGHT,
    GLFW_PRESS,
    GLFW_RELEASE,
)
from kitty.tabs import MouseEvents, TabManager

from . import BaseTest


class Pane:
    def __init__(self, title, overlay_for=None):
        self.title = title
        self.overlay_for = overlay_for


class FakeTab:
    def __init__(self, tab_id, title, panes=(), backgrounded=False):
        self.id = tab_id
        self.effective_title = title
        self.name = title
        self.backgrounded = backgrounded
        self.os_window_id = 1
        self.confirm_close_window_id = 0
        self._panes = list(panes)
        self.tm = None
        self.active_window = None

    def __iter__(self):
        return iter(self._panes)

    def __len__(self):
        return len(self._panes)

    def tab_manager_ref(self):
        return self.tm


class Manager:
    def __init__(self, tabs, active=0):
        self.tabs = list(tabs)
        self.active_index = active
        self.active_tab_history = deque(tab.id for tab in self.tabs)
        self.dirty = 0
        self.created = None
        self.fail_switch = False
        self.activated = []
        for tab in self.tabs:
            tab.tm = self

    @property
    def active_tab(self):
        if not self.tabs:
            return None
        return self.tabs[self.active_index]

    def set_active_tab(self, tab):
        self.activated.append(tab)
        if self.fail_switch or tab not in self.tabs:
            return False
        if self.tabs.index(tab) != self.active_index:
            # The real tab index setter records the tab being left.
            self.active_tab_history.append(self.active_tab.id)
            self.active_index = self.tabs.index(tab)
        if tab.backgrounded:
            tab.backgrounded = False
        return True

    def new_tab(self, location='last'):
        self.created = location
        if self.fail_switch:
            return None
        created = FakeTab(1000 + len(self.tabs), 'new')
        created.tm = self
        self.active_tab_history.append(self.active_tab.id)
        self.tabs.append(created)
        self.active_index = len(self.tabs) - 1
        return created

    def mark_tab_bar_dirty(self):
        self.dirty += 1

    def __iter__(self):
        return iter(self.tabs)

    def tab_for_id(self, tab_id):
        for tab in self.tabs:
            if tab.id == tab_id:
                return tab
        return None


def boss_with(tabs):
    boss = object.__new__(Boss)
    manager = Manager(tabs)
    boss.os_window_map = {10**9: manager}
    return boss, manager


class BackgroundTabs(BaseTest):
    def test_label_names_the_tab_and_its_panes(self):
        same = FakeTab(1, 'Shell', [Pane('Shell'), Pane('ask', overlay_for=1)])
        self.ae(backgrounded_tab_label(same), 'Shell')
        split = FakeTab(2, 'Work', [Pane('editor'), Pane('shell'), Pane('logs')])
        self.ae(backgrounded_tab_label(split), 'Work — editor, shell, logs')
        crowded = FakeTab(3, 'Work', [Pane(f'p{i}') for i in range(6)])
        self.ae(backgrounded_tab_label(crowded), 'Work — p0, p1, p2, p3 +2')
        blank = FakeTab(4, '', [])
        blank.effective_title = ''
        self.ae(backgrounded_tab_label(blank), 'tab')
        long = FakeTab(5, 'x' * 200, [])
        self.ae(len(backgrounded_tab_label(long)), 80)

    def test_sessions_lists_backgrounded_tabs_first(self):
        bare = object.__new__(Boss)
        window = object()
        with patch.object(bare, '_kilix_start_choose') as choose:
            bare._kilix_start_sessions(window)
        title, choices, actions = choose.call_args.args[1:]
        self.ae(title, 'Kilix Start — Sessions')
        self.ae(choices[0], 'g:Backgrounded tabs')
        self.assertIn('g', choices[0].split(':', 1)[1].casefold())
        for label in ('Kilix persistent panes', 'Tmux session manager', 'Back'):
            self.assertTrue(any(label in choice for choice in choices), label)
        self.ae(actions['g'], bare._kilix_start_backgrounded)

        boss, _manager = boss_with([
            FakeTab(1, 'one', backgrounded=True),
            FakeTab(2, 'two'),
            FakeTab(3, 'three', backgrounded=True),
        ])
        with patch.object(boss, '_kilix_start_choose') as choose:
            boss._kilix_start_sessions(window)
        self.ae(choose.call_args.args[2][0], 'g:Backgrounded tabs (2)')

    def test_backgrounded_menu_restores_the_selected_tab(self):
        window = object()
        empty = object.__new__(Boss)
        with patch.object(empty, '_kilix_start_choose') as choose:
            empty._kilix_start_backgrounded(window)
        choices, actions = choose.call_args.args[2:]
        self.ae(choices, ('z:No backgrounded tabs [Z]',))
        key, label = choices[0].split(':', 1)
        self.assertIn(key.casefold(), label.casefold())
        self.ae(actions['z'], empty._kilix_start_sessions)

        first = FakeTab(11, 'Alpha', [Pane('editor')])
        second = FakeTab(22, 'Beta', [Pane('shell')])
        first.backgrounded = second.backgrounded = True
        boss, _manager = boss_with([first, second])
        with patch.object(boss, '_kilix_start_choose') as choose, \
                patch.object(boss, 'restore_backgrounded_tab') as restore:
            boss._kilix_start_backgrounded(window)
            _title, choices, actions = choose.call_args.args[1:]
            self.ae(choices[0], '1:1 Alpha — editor')
            self.ae(choices[1], '2:2 Beta — shell')
            self.ae(choices[-1], 'z:◂ Back [Z]')
            actions['1'](window)
            actions['2'](window)
        restore.assert_any_call(11)
        restore.assert_any_call(22)
        self.ae(restore.call_args_list[0].args, (11,))

    def test_backgrounded_menu_keeps_every_accelerator_in_its_label(self):
        tabs = [FakeTab(i, f'page {i}', [Pane(f'pane {i}')], backgrounded=True) for i in range(36)]
        boss, _manager = boss_with(tabs)
        with patch.object(boss, '_kilix_start_choose') as choose:
            boss._kilix_start_backgrounded(object())
        title, choices, actions = choose.call_args.args[1:]
        self.ae(title, f'Backgrounded tabs ({len(_BACKGROUND_KEYS)} of 36)')
        self.ae(len(choices), len(_BACKGROUND_KEYS) + 1)
        seen = []
        for choice in choices:
            key, label = choice.split(':', 1)
            self.assertIn(key.casefold(), label.casefold(), choice)
            seen.append(key)
        self.ae(seen[:-1], list(_BACKGROUND_KEYS))
        self.ae(seen[-1], 'z')
        self.ae(actions['z'], boss._kilix_start_sessions)

    def test_background_hides_the_active_tab_and_forgets_it(self):
        def fresh():
            return [FakeTab(i, f't{i}') for i in (1, 2, 3)]

        one, two, three = fresh()
        boss, manager = boss_with([one, two, three])
        manager.active_index = 1
        boss.background_tab(two)
        self.assertTrue(two.backgrounded)
        self.ae(manager.active_tab, three)
        self.assertNotIn(2, manager.active_tab_history)
        self.assertIsInstance(manager.active_tab_history, deque)
        self.ae(manager.dirty, 1)
        self.assertIsNone(manager.created)

        boss.background_tab(two)
        self.ae(manager.dirty, 1)

        one, two, three = fresh()
        boss, manager = boss_with([one, two, three])
        manager.active_index = 2
        boss.background_tab(three)
        self.ae(manager.active_tab, one)

        one, two = FakeTab(1, 't1'), FakeTab(2, 't2')
        hidden = FakeTab(4, 'hidden', backgrounded=True)
        boss, manager = boss_with([one, hidden, two])
        manager.active_index = 2
        boss.background_tab(two)
        self.ae(manager.active_tab, one)

    def test_background_of_the_last_visible_tab_opens_a_replacement(self):
        only = FakeTab(1, 'only')
        boss, manager = boss_with([only])
        boss.background_tab(only)
        self.assertTrue(only.backgrounded)
        self.assertIs(manager.active_tab, manager.tabs[-1])
        self.assertIsNot(manager.active_tab, only)
        self.ae(manager.created, 'last')
        self.assertNotIn(1, manager.active_tab_history)

        stuck = FakeTab(7, 'stuck')
        boss, manager = boss_with([stuck])
        manager.fail_switch = True
        original = list(manager.active_tab_history)
        boss.background_tab(stuck)
        self.assertFalse(stuck.backgrounded)
        self.ae(manager.active_tab, stuck)
        self.ae(list(manager.active_tab_history), original)
        self.ae(manager.dirty, 0)

    def test_failed_switch_leaves_the_tab_visible(self):
        one, two = FakeTab(1, 'one'), FakeTab(2, 'two')
        boss, manager = boss_with([one, two])
        manager.fail_switch = True
        original = list(manager.active_tab_history)
        boss.background_tab(one)
        self.assertFalse(one.backgrounded)
        self.ae(manager.active_tab, one)
        self.ae(list(manager.active_tab_history), original)
        self.ae(manager.dirty, 0)

    def test_background_of_an_inactive_tab_does_not_switch(self):
        one, two = FakeTab(1, 'one'), FakeTab(2, 'two')
        boss, manager = boss_with([one, two])
        manager.active_tab_history = deque([2, 1, 2])
        boss.background_tab(two)
        self.assertTrue(two.backgrounded)
        self.ae(manager.active_tab, one)
        self.ae(list(manager.active_tab_history), [1])
        self.ae(manager.activated, [])

        missing = FakeTab(9, 'gone')
        missing.tm = manager
        boss.background_tab(missing)
        self.assertFalse(missing.backgrounded)
        orphan = FakeTab(8, 'orphan')
        boss.background_tab(orphan)
        self.assertFalse(orphan.backgrounded)

    def test_focusing_a_backgrounded_tab_restores_it(self):
        tab = FakeTab(3, 'hidden', backgrounded=True)
        manager = SimpleNamespace(
            os_window_id=0, tabs=[tab], active_tab_history=deque(),
            tab_bar_should_be_visible=False,
            set_active_tab_idx=lambda idx: setattr(tab, 'idx', idx))
        with patch('kitty.tabs.is_tab_bar_visible', return_value=False):
            self.assertTrue(TabManager.set_active_tab(manager, tab))
        self.assertFalse(tab.backgrounded)
        self.ae(tab.idx, 0)
        stranger = FakeTab(4, 'other', backgrounded=True)
        with patch('kitty.tabs.is_tab_bar_visible', return_value=False):
            self.assertFalse(TabManager.set_active_tab(manager, stranger))
        self.assertTrue(stranger.backgrounded)

    def test_tab_bar_omits_backgrounded_tabs(self):
        shown, hidden = FakeTab(1, 'shown'), FakeTab(2, 'hidden')
        manager = SimpleNamespace(tabs=[shown, hidden], active_tab=shown)
        with patch('kitty.tabs.get_options', return_value=SimpleNamespace(tab_bar_filter='')):
            self.ae(TabManager.tabs_to_be_shown_in_tab_bar.fget(manager), manager.tabs)
            hidden.backgrounded = True
            self.ae(list(TabManager.tabs_to_be_shown_in_tab_bar.fget(manager)), [shown])
        hidden.backgrounded = True
        shown.backgrounded = True
        other = FakeTab(3, 'other')
        manager.tabs = [shown, hidden, other]
        manager.active_tab = shown
        matched = SimpleNamespace(match_tabs=lambda expr, all_tabs=None: iter([hidden, other]))
        with patch('kitty.tabs.get_options', return_value=SimpleNamespace(tab_bar_filter='title:x')), \
                patch('kitty.tabs.get_boss', return_value=matched):
            self.ae(list(TabManager.tabs_to_be_shown_in_tab_bar.fget(manager)), [other])

    def test_restore_focuses_the_same_tab(self):
        tab = FakeTab(5, 'Alpha', backgrounded=True)
        boss, manager = boss_with([tab])
        with patch.object(boss, 'focus_os_window') as focus:
            boss.restore_backgrounded_tab(5)
        self.ae(manager.activated, [tab])
        self.assertFalse(tab.backgrounded)
        focus.assert_called_once_with(tab.os_window_id)
        focus.reset_mock()
        boss.restore_backgrounded_tab(404)
        focus.assert_not_called()

    def test_close_confirmation_offers_background_and_keeps_yes_as_default(self):
        window = SimpleNamespace(id=4)
        tab = FakeTab(6, 'page', [Pane('shell'), Pane('editor')])
        tab.active_window = window
        boss = object.__new__(Boss)
        boss.confirm_desktop_close = Mock(return_value=False)
        boss.close_windows_with_confirmation_msg = Mock(return_value=('Busy', 2))
        boss.window_id_map = {}
        boss.os_window_map = {7: SimpleNamespace(active_tab=tab)}
        boss.confirm = Mock(return_value=SimpleNamespace(id=99))
        with patch('kitty.boss.current_focused_os_window_id', return_value=7), \
                patch('kitty.boss.get_options', return_value=SimpleNamespace(confirm_os_window_close=(1, False))):
            boss.confirm_tab_close(tab)
        boss.confirm.assert_called_once()
        self.ae(boss.confirm.call_args.kwargs['extra_choices'], ('b;yellow:Background',))
        self.ae(boss.confirm.call_args.kwargs['title'], 'Close tab?')
        self.assertIn('Background keeps its panes running', boss.confirm.call_args.args[0])
        self.ae(tab.confirm_close_window_id, 99)

        captured = {}

        def run_kitten(_kitten, cmd, **kwargs):
            captured['cmd'] = cmd
            kwargs['custom_callback']({'response': 'b'}, 0, boss)
            kwargs['action_on_removal'](1, boss)
            return object()

        answers = []
        with patch.object(boss, 'run_kitten_with_metadata', side_effect=run_kitten):
            with self.assertRaises(AssertionError):
                Boss.confirm(boss, 'Close?', lambda result: answers.append(result), extra_choices=('b;yellow:Background',))
        self.assertIn('--type=choices', captured['cmd'])
        self.assertIn('y;green:Yes', captured['cmd'])
        self.assertIn('n;red:No', captured['cmd'])
        self.assertIn('b;yellow:Background', captured['cmd'])
        self.ae(captured['cmd'][captured['cmd'].index('--default') + 1], 'y')
        self.ae(answers, ['b'])

        def yesno(_kitten, cmd, **kwargs):
            captured['yesno'] = cmd
            kwargs['custom_callback']({'response': 'y'}, 0, boss)
            kwargs['action_on_removal'](1, boss)
            return object()

        with patch.object(boss, 'run_kitten_with_metadata', side_effect=yesno):
            with self.assertRaises(AssertionError):
                Boss.confirm(boss, 'Close?', lambda result: answers.append(result))
        self.assertIn('--type=yesno', captured['yesno'])
        self.assertNotIn('-c', captured['yesno'])
        self.ae(answers[-1], True)

    def test_confirmation_backgrounds_only_for_the_background_letter(self):
        tab = FakeTab(6, 'page')
        tab.confirm_close_window_id = 3
        boss = object.__new__(Boss)
        boss.os_window_map = {1: [tab]}
        boss.background_tab = Mock()
        boss.close_tab_no_confirm = Mock()
        for answer in (False, '', 'n', 'x'):
            boss.handle_close_tab_confirmation(answer, tab.id)
        boss.background_tab.assert_not_called()
        boss.close_tab_no_confirm.assert_not_called()
        self.ae(tab.confirm_close_window_id, 0)
        boss.handle_close_tab_confirmation('b', tab.id)
        boss.background_tab.assert_called_once_with(tab)
        boss.close_tab_no_confirm.assert_not_called()
        boss.handle_close_tab_confirmation('y', tab.id)
        boss.handle_close_tab_confirmation(True, tab.id)
        self.ae(boss.close_tab_no_confirm.call_args_list[0].args, (tab,))
        self.ae(boss.close_tab_no_confirm.call_args_list[1].args, (tab,))
        boss.handle_close_tab_confirmation('b', 404)
        self.ae(boss.background_tab.call_count, 1)

    def test_right_click_opens_tab_actions_without_activating(self):
        tab = FakeTab(4, 'Work', [Pane('editor')])
        window = SimpleNamespace(id=8, tabref=lambda: tab)
        tab.active_window = window
        manager = Manager([tab])
        boss, _stored = boss_with([tab])
        boss.os_window_map = {10**9: manager}
        tab.tm = manager
        events = MouseEvents()
        bar = SimpleNamespace(
            recent_tab_bar_mouse_events=events,
            tab_bar=SimpleNamespace(action_at=lambda x, y: None, tab_id_at=lambda x, y: 4),
            tab_for_id=manager.tab_for_id,
            active_tab=None,
            set_active_tab=Mock(),
            new_tab=Mock(),
        )
        with patch('kitty.tabs.get_tab_being_dragged', return_value=(0, False, 0.0, 0.0)), \
                patch('kitty.tabs.get_click_interval', return_value=1.0), \
                patch('kitty.tabs.set_tab_being_dragged') as drag, \
                patch('kitty.tabs.get_boss', return_value=boss), \
                patch.object(boss, '_kilix_start_choose') as choose:
            TabManager.handle_tab_bar_mouse(bar, 10, 2, GLFW_MOUSE_BUTTON_RIGHT, 0, GLFW_PRESS)
            choose.assert_not_called()
            TabManager.handle_tab_bar_mouse(bar, 10, 2, GLFW_MOUSE_BUTTON_RIGHT, 0, GLFW_RELEASE)
        bar.set_active_tab.assert_not_called()
        drag.assert_not_called()
        title, choices, actions = choose.call_args.args[1:]
        self.ae(title, 'Tab — Work')
        self.ae(choices, ('r:Rename', 'b:Background tab', 'c:Close tab', 'n:New tab', 'o:Close others'))
        for choice in choices:
            key, label = choice.split(':', 1)
            self.assertIn(key.casefold(), label.casefold(), choice)

        renamed = {}
        boss.set_tab_title = lambda: renamed.setdefault('window', boss.window_for_dispatch)
        boss.background_tab = Mock()
        boss.close_tab = Mock()
        actions['r'](window)
        self.ae(manager.activated, [tab])
        self.ae(renamed['window'], window)
        self.assertIsNone(boss.window_for_dispatch)
        actions['b'](window)
        boss.background_tab.assert_called_once_with(tab)
        actions['c'](window)
        boss.close_tab.assert_called_once_with(tab)
        actions['n'](window)
        self.ae(manager.created, 'after')

        other = FakeTab(5, 'other')
        hidden = FakeTab(6, 'hidden', backgrounded=True)
        manager.tabs = [tab, other, hidden]
        for item in manager.tabs:
            item.tm = manager
        boss.close_tab.reset_mock()
        actions['o'](window)
        self.ae([call.args[0] for call in boss.close_tab.call_args_list], [other])

    def test_right_click_on_the_new_tab_button_does_nothing(self):
        boss = SimpleNamespace(show_tab_bar_menu=Mock())
        bar = SimpleNamespace(
            recent_tab_bar_mouse_events=MouseEvents(),
            tab_bar=SimpleNamespace(action_at=lambda x, y: None, tab_id_at=lambda x, y: -1),
            active_tab=None,
            new_tab=Mock(),
            set_active_tab=Mock(),
        )
        with patch('kitty.tabs.get_tab_being_dragged', return_value=(0, False, 0.0, 0.0)), \
                patch('kitty.tabs.get_click_interval', return_value=1.0), \
                patch('kitty.tabs.set_tab_being_dragged'), \
                patch('kitty.tabs.get_boss', return_value=boss):
            TabManager.handle_tab_bar_mouse(bar, 3, 2, GLFW_MOUSE_BUTTON_RIGHT, 0, GLFW_PRESS)
            TabManager.handle_tab_bar_mouse(bar, 3, 2, GLFW_MOUSE_BUTTON_RIGHT, 0, GLFW_RELEASE)
            bar.recent_tab_bar_mouse_events.clear()
            bar.tab_bar.tab_id_at = lambda x, y: 4
            bar.tab_for_id = lambda tab_id: SimpleNamespace(id=tab_id)
            TabManager.handle_tab_bar_mouse(bar, 10, 2, GLFW_MOUSE_BUTTON_LEFT, 0, GLFW_PRESS)
            TabManager.handle_tab_bar_mouse(bar, 10, 2, GLFW_MOUSE_BUTTON_LEFT, 0, GLFW_RELEASE)
        boss.show_tab_bar_menu.assert_not_called()
        bar.new_tab.assert_not_called()
        bar.set_active_tab.assert_called_once()

    def test_tab_menu_without_a_host_window_does_not_open(self):
        tab = FakeTab(4, 'Work')
        boss = object.__new__(Boss)
        boss.os_window_map = {}
        with patch.object(boss, '_kilix_start_choose') as choose, \
                patch('kitty.boss.current_focused_os_window_id', return_value=0), \
                patch('kitty.boss.last_focused_os_window_id', return_value=0), \
                patch('kitty.boss.current_os_window', return_value=None):
            boss.show_tab_bar_menu(tab)
        choose.assert_not_called()

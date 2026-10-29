from types import SimpleNamespace
from unittest.mock import Mock, patch

from kitty import fast_data_types as keys
from kitty.boss import Boss
from kitty.kilix_chrome.start_key import LoneSuperKey

from . import BaseTest


class TestStartKey(BaseTest):
    def event(self, key=None, action=None, mods=0):
        return SimpleNamespace(key=key or keys.GLFW_FKEY_LEFT_SUPER,
                               action=keys.GLFW_PRESS if action is None else action, mods=mods)

    def test_lone_left_and_right_only_fire_on_release_once(self):
        for key in (keys.GLFW_FKEY_LEFT_SUPER, keys.GLFW_FKEY_RIGHT_SUPER):
            state = LoneSuperKey()
            self.ae(state.event(self.event(key), (1, 2)), (True, False))
            self.ae(state.event(self.event(key, keys.GLFW_REPEAT), (1, 2)), (True, False))
            self.ae(state.event(self.event(key), (1, 2)), (True, False))
            self.ae(state.event(self.event(key, keys.GLFW_RELEASE), (1, 2)), (True, True))
            self.ae(state.event(self.event(key, keys.GLFW_RELEASE), (1, 2)), (True, False))

    def test_combinations_pass_through_and_do_not_open_start(self):
        for key in (ord('x'), keys.GLFW_FKEY_LEFT_SHIFT, keys.GLFW_FKEY_RIGHT_ALT):
            state = LoneSuperKey()
            state.event(self.event(), (1, 2))
            self.ae(state.event(self.event(key, mods=keys.GLFW_MOD_SUPER), (1, 2)), (False, False))
            self.ae(state.event(self.event(action=keys.GLFW_RELEASE), (1, 2)), (True, False))
        state = LoneSuperKey()
        state.event(self.event(mods=keys.GLFW_MOD_CONTROL), (1, 2))
        self.ae(state.event(self.event(action=keys.GLFW_RELEASE), (1, 2)), (True, False))
        state.event(self.event(), (1, 2))
        state.event(self.event(keys.GLFW_FKEY_RIGHT_SUPER), (1, 2))
        self.ae(state.event(self.event(action=keys.GLFW_RELEASE), (1, 2)), (True, False))
        self.ae(state.event(self.event(keys.GLFW_FKEY_RIGHT_SUPER, keys.GLFW_RELEASE), (1, 2)), (True, False))

    def test_focus_switch_mouse_and_orphaned_repeats_cancel(self):
        state = LoneSuperKey()
        state.event(self.event(), (1, 2))
        self.ae(state.event(self.event(action=keys.GLFW_RELEASE), (1, 3)), (True, False))
        state.event(self.event(), (1, 2))
        state.cancel()
        self.ae(state.event(self.event(action=keys.GLFW_RELEASE), (1, 2)), (True, False))
        state.event(self.event(), (1, 2))
        state.reset()
        state.event(self.event(action=keys.GLFW_REPEAT), (1, 2))
        self.ae(state.event(self.event(action=keys.GLFW_RELEASE), (1, 2)), (True, False))

    def test_boss_uses_the_top_left_start_path_and_allows_disabling(self):
        boss = SimpleNamespace(active_window=SimpleNamespace(id=2, os_window_id=1), kilix_show_start_menu=Mock())
        with patch('kitty.boss.is_macos', False), patch('kitty.kilix_chrome.settings.chrome_enabled', return_value=True):
            self.assertTrue(Boss.kilix_super_key_event(boss, self.event()))
            boss.kilix_show_start_menu.assert_not_called()
            self.assertTrue(Boss.kilix_super_key_event(boss, self.event(action=keys.GLFW_RELEASE)))
            boss.kilix_show_start_menu.assert_called_once()
        with patch('kitty.kilix_chrome.settings.chrome_enabled', return_value=False):
            self.assertFalse(Boss.kilix_super_key_event(boss, self.event()))

    def test_platform_and_unavailable_menu_preserve_raw_keys(self):
        # Exercise the real settings platform gate, even with both preferences
        # explicitly enabled. Native Windows must never consume this key.
        for platform, macos, visible, supported in (
                ('win32', False, '1', False), ('darwin', True, '1', False),
                ('linux', False, '0', False), ('linux', False, '1', True)):
            with self.subTest(platform=platform, visible=visible):
                boss = SimpleNamespace(active_window=SimpleNamespace(id=2, os_window_id=1), kilix_show_start_menu=Mock())
                values = {'KILIX_CHROME_SUPER_START': '1', 'KILIX_CHROME_START_MENU': visible}
                with patch('kitty.boss.is_macos', macos), \
                        patch('kitty.kilix_chrome.settings.sys.platform', platform), \
                        patch('kitty.kilix_chrome.settings.values', return_value=(values, True)):
                    self.ae(Boss.kilix_super_key_event(boss, self.event()), supported)
                    self.ae(Boss.kilix_super_key_event(boss, self.event(action=keys.GLFW_RELEASE)), supported)
                self.ae(boss.kilix_show_start_menu.call_count, int(supported))
                self.assertFalse(boss._kilix_super_key.pressed)

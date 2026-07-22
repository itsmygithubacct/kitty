#!/usr/bin/env python
# License: GPL v3

from unittest.mock import patch

from kitty.types import WindowGeometry
from kitty.window import Window
from kitty.window_title_bar import (
    WindowTitleBarScreen,
    WindowTitleData,
)

from . import BaseTest


class TestWindowChrome(BaseTest):

    def test_shared_settings_remove_and_restore_individual_chrome_buttons(self) -> None:
        self.set_options()
        title_bar = WindowTitleBarScreen(1, 10, 20)
        title_bar.layout(WindowGeometry(0, 0, 800, 20, 80, 1))
        data = WindowTitleData('pane', True, 1, 1)

        with patch(
                'kitty.window_title_bar.chrome_enabled',
                side_effect=lambda key: key != 'KILIX_CHROME_BUTTON_SPLIT_UP'):
            title_bar.render(data, '')
        actions = set(title_bar.button_cols.values())
        self.assertNotIn(
            'combine | launch --location=hsplit --cwd=current | move_window top',
            actions)
        self.assertIn('change_font_size current +2.0', actions)
        self.assertIn('close_window', actions)

        with patch('kitty.window_title_bar.chrome_enabled', return_value=True):
            title_bar.render(data, '')
        self.assertIn(
            'combine | launch --location=hsplit --cwd=current | move_window top',
            set(title_bar.button_cols.values()))

    def test_title_bar_visibility_restores_after_fullscreen(self) -> None:
        window = object.__new__(Window)
        window.show_title_bar = True
        window.os_window_id = 17
        geometry = WindowGeometry(0, 0, 800, 600, 80, 24)

        with patch('kitty.window.is_os_window_fullscreen', side_effect=(False, True, False)):
            self.assertTrue(window.should_show_title_bar(geometry))
            self.assertFalse(window.should_show_title_bar(geometry))
            self.assertTrue(window.should_show_title_bar(geometry))

        one_row = WindowGeometry(0, 0, 800, 20, 80, 1)
        with patch('kitty.window.is_os_window_fullscreen', return_value=False):
            self.assertFalse(window.should_show_title_bar(one_row))

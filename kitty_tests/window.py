#!/usr/bin/env python
# License: GPL v3

from unittest.mock import patch

from kitty.types import WindowGeometry
from kitty.window import Window

from . import BaseTest


class TestWindowChrome(BaseTest):

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

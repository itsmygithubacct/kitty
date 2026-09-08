#!/usr/bin/env python

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from kitty.kilix_chrome import settings
from kitty.kilix_chrome.providers import (
    CALENDAR_WIDGET_ACTION, DATE_WIDGET_ACTION, NETWORK_WIDGET_ACTION,
    VOLUME_WIDGET_ACTION,
)
from kitty.kilix_chrome.registry import SYSTEM_WIDGETS, dispatch, segments_for

from . import BaseTest


class TestKilixChrome(BaseTest):

    def test_settings_file_is_authoritative_and_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'settings.conf'
            filename.write_text('KILIX_CHROME_VOLUME=0\n')
            with patch.dict(os.environ, {
                    'GPU_TERMINAL_SETTINGS_FILE': str(filename),
                    'KILIX_CHROME_VOLUME': '1'}):
                settings._cache_signature = None
                self.assertFalse(settings.chrome_enabled('KILIX_CHROME_VOLUME'))
                filename.write_text('KILIX_CHROME_VOLUME=1\n')
                settings._cache_signature = None
                self.assertTrue(settings.chrome_enabled('KILIX_CHROME_VOLUME'))

    def test_registry_owns_order_and_segment_selection(self) -> None:
        self.ae(tuple(widget.key for widget in SYSTEM_WIDGETS),
                ('volume', 'network', 'clock'))
        with patch.dict(os.environ, {
                'GPU_TERMINAL_SETTINGS_FILE': '/missing/kilix-settings',
                'KILIX_CHROME_VOLUME': '1',
                'KILIX_CHROME_NETWORK': '1',
                'KILIX_CHROME_CALENDAR': '1',
                'KILIX_CHROME_CLOCK': '1',
                'KILIX_CHROME_CLOCK_FORMAT': 'CLOCK'}):
            settings._cache_signature = None
            segments = segments_for(('volume', 'network', 'clock'), 42)
        self.ae(tuple(action for _, action, _ in segments), (
            VOLUME_WIDGET_ACTION, NETWORK_WIDGET_ACTION,
            CALENDAR_WIDGET_ACTION, DATE_WIDGET_ACTION))
        self.assertTrue(all(fg == 42 for _, _, fg in segments))

    def test_registry_dispatches_volume_without_tabs_widget_branches(self) -> None:
        calls: list[dict[str, object]] = []

        class Tab:
            windows = ()
            def new_window(self, **kwargs: object) -> None:
                calls.append(kwargs)

        class Window:
            id = 17
            kilix_popup = None

            def tabref(self) -> Tab:
                return Tab()

        class ActiveTab:
            active_window = Window()

        class Manager:
            active_tab = ActiveTab()

        with patch('kitty.kilix_chrome.registry.volume_target',
                   side_effect=lambda mode: ['kilix-volume', mode]):
            for gesture in ('single', 'double', 'right'):
                self.assertTrue(dispatch(Manager(), VOLUME_WIDGET_ACTION, gesture))
        popups = [call.pop('kilix_popup') for call in calls]
        self.assertTrue(all(popup.action.startswith(VOLUME_WIDGET_ACTION + ':') for popup in popups))
        self.ae(calls, [
            {'use_shell': False, 'cmd': ['kilix-volume', 'compact'],
             'override_title': 'Volume', 'overlay_for': 17},
            {'use_shell': False, 'cmd': ['kilix-volume', 'full'],
             'override_title': 'Volume Control', 'overlay_for': 17},
            {'use_shell': False, 'cmd': ['kilix-volume', 'settings'],
             'override_title': 'Volume Settings', 'overlay_for': 17},
        ])
        self.assertFalse(dispatch(Manager(), 'not-a-widget'))

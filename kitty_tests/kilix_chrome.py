#!/usr/bin/env python

import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kitty.kilix_chrome import settings
from kitty.kilix_chrome.providers import (
    CALENDAR_WIDGET_ACTION,
    DATE_WIDGET_ACTION,
    NETWORK_WIDGET_ACTION,
    VOLUME_WIDGET_ACTION,
)
from kitty.kilix_chrome.registry import SYSTEM_WIDGETS, dispatch, segments_for

from . import BaseTest


class TestKilixChrome(BaseTest):

    def test_clock_uses_pacific_timezone_and_dst_without_restart(self) -> None:
        from kitty.kilix_chrome import providers
        try:
            for month, expected in ((1, '12:00 PST'), (7, '13:00 PDT')):
                with patch.dict(os.environ, {'TZ': 'America/Los_Angeles'}), \
                        patch.object(providers, 'datetime') as clock, \
                        patch.object(providers, 'chrome_enabled', return_value=True), \
                        patch.object(providers, 'chrome_value', return_value='%H:%M %Z'):
                    clock.now.return_value = datetime(2026, month, 15, 20, tzinfo=timezone.utc)
                    self.ae(providers.clock_segment(), f' {expected} ')
            with patch.dict(os.environ, {'TZ': 'UTC'}):
                self.ae(providers.local_now().utcoffset().total_seconds(), 0)
        finally:
            time.tzset()

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
                ('weather', 'volume', 'network', 'clock'))
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

    def test_clock_default_am_pm_and_explicit_24_hour(self):
        from kitty.kilix_chrome.providers import clock_segment
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'settings.conf'
            filename.write_text('KILIX_CHROME_CLOCK=1\n')
            with patch.dict(os.environ, {'GPU_TERMINAL_SETTINGS_FILE': str(filename)}):
                for hour, expected in ((0, '12:05 AM'), (12, '12:05 PM'), (23, '11:05 PM')):
                    with patch('kitty.kilix_chrome.providers.local_now', return_value=datetime(2026, 9, 27, hour, 5)):
                        self.ae(clock_segment(), f' 2026-09-27 {expected} ')
                filename.write_text('KILIX_CHROME_CLOCK_FORMAT=%H:%M\n')
                with patch('kitty.kilix_chrome.providers.local_now', return_value=datetime(2026, 9, 27, 23, 5)):
                    self.ae(clock_segment(), ' 23:05 ')

    def test_system_readout_click_opens_memory_and_right_click_settings(self):
        from kitty.kilix_memory import MEMORY_WIDGET_ACTION
        tab = SimpleNamespace(new_window=Mock(), kilix_show_memory_widget=Mock())
        owner = SimpleNamespace(id=17, tabref=lambda: tab)
        manager = SimpleNamespace(active_tab=SimpleNamespace(active_window=owner))
        with patch('kitty.kilix_chrome.popup.popup_target', return_value=owner), \
                patch('kitty.kilix_chrome.registry.settings_target', return_value=['kilix', 'settings']):
            self.assertTrue(dispatch(manager, MEMORY_WIDGET_ACTION, 'single'))
            self.assertTrue(dispatch(manager, MEMORY_WIDGET_ACTION, 'double'))
            tab.kilix_show_memory_widget.assert_called_once_with()
            self.assertTrue(dispatch(manager, MEMORY_WIDGET_ACTION, 'right'))
            self.ae(tab.new_window.call_args.kwargs['override_title'], 'System CPU and RAM')
            self.ae(tab.new_window.call_args.kwargs['cmd'], ['kilix', 'settings', '--widget', 'system'])

    def test_right_click_routes_are_specific_and_voice_left_click_falls_through(self):
        from kitty.kilix_chrome.registry import THERMAL_WIDGET_ACTION
        from kitty.kilix_chrome.weather import WEATHER_ACTION
        from kitty.kilix_voice import DICTATE_ACTION, SPEAK_ACTION
        tab = SimpleNamespace(new_window=Mock())
        owner = SimpleNamespace(id=17, tabref=lambda: tab)
        manager = SimpleNamespace(active_tab=SimpleNamespace(active_window=owner))
        routes = {THERMAL_WIDGET_ACTION: 'temperature', NETWORK_WIDGET_ACTION: 'network',
                  SPEAK_ACTION: 'speech', DICTATE_ACTION: 'microphone', WEATHER_ACTION: 'weather'}
        with patch('kitty.kilix_chrome.popup.popup_target', return_value=owner) as target, \
                patch('kitty.kilix_chrome.registry.settings_target', return_value=['kilix', 'settings']):
            for action, kind in routes.items():
                self.assertTrue(dispatch(manager, action, 'right'))
                call = tab.new_window.call_args.kwargs
                self.ae(call['cmd'], ['kilix', 'settings', '--widget', kind])
                self.ae(call['kilix_popup'].action, action + ':right')
            target.reset_mock()
            for action in (SPEAK_ACTION, DICTATE_ACTION):
                self.assertFalse(dispatch(manager, action, 'single'))
                self.assertFalse(dispatch(manager, action, 'double'))
            target.assert_not_called()

    def test_clock_right_click_opens_only_clock_settings(self):
        tab = SimpleNamespace(new_window=Mock())
        owner = SimpleNamespace(id=17, tabref=lambda: tab)
        manager = SimpleNamespace(active_tab=SimpleNamespace(active_window=owner))
        with patch('kitty.kilix_chrome.popup.popup_target', return_value=owner), \
                patch('kitty.kilix_chrome.registry.settings_target', return_value=['kilix', 'settings']):
            for action in (DATE_WIDGET_ACTION, CALENDAR_WIDGET_ACTION):
                self.assertTrue(dispatch(manager, action, 'right'))
                args = tab.new_window.call_args.kwargs
                self.ae(args['cmd'], ['kilix', 'settings', '--clock'])
                self.ae(args['override_title'], 'Clock Settings')

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from kitty import kilix_battery as battery
from . import BaseTest


class TestBatteryVisibility(BaseTest):
    def test_charging_bolt_in_both_display_modes(self):
        for show_percent in (True, False):
            for percent in (None, 0, 40, 99):
                for status in ('charging', 'discharging', 'not charging', 'unknown'):
                    with self.subTest(show_percent=show_percent, percent=percent, status=status), \
                            patch.object(battery, '_BATTERY_SHOW_PERCENT', show_percent), \
                            patch.object(battery, 'battery_info', return_value=battery.BatteryInfo(percent, status)):
                        text, action, color = battery.battery_segment()
                        self.ae(chr(0xf0e7) in text, status == 'charging')
                        self.assertIn(battery._battery_glyph(percent), text)
                        self.ae('%' in text, show_percent)
                        self.ae(action, battery.BATTERY_TOGGLE_ACTION)
                        self.ae(color, battery._battery_color(percent))

    def test_charging_transition_refreshes_at_same_percentage(self):
        with patch.object(battery, '_BATTERY_LAST_SIGNATURE', (40, 'discharging')), \
                patch.object(battery, '_BATTERY_CACHE_UNTIL', 0), \
                patch.object(battery, 'battery_info') as info, \
                patch.object(battery, '_invalidate_all_chrome') as invalidate:
            for status in ('charging', 'discharging'):
                info.return_value = battery.BatteryInfo(40, status)
                battery._battery_timer()
                invalidate.assert_called_once_with()
                self.ae(chr(0xf0e7) in battery.battery_segment()[0], status == 'charging')
                invalidate.reset_mock()
                battery._battery_timer()
                invalidate.assert_not_called()

    def test_charging_idle_unknown_and_live_full_transitions(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {'KILIX_BATTERY_SUPPLY_DIR': tmp}), patch.object(battery, 'chrome_enabled', return_value=True):
            pack = Path(tmp) / 'BAT0'
            pack.mkdir()
            (pack / 'type').write_text('Battery')
            for status in ('Charging', 'Discharging', 'Not charging', 'Unknown'):
                (pack / 'status').write_text(status)
                for percent in (0, 40, 99, 100, 99):
                    (pack / 'capacity').write_text(str(percent))
                    result = battery._read_battery_info_uncached()
                    if percent == 100:
                        self.assertIsNone(result)
                    else:
                        self.ae(result.percent, percent)
                        self.ae(result.status, status.lower())
            for value in ('', 'bad', 'nan', 'inf', '-1', '101'):
                (pack / 'capacity').write_text(value)
                self.assertIsNone(battery._read_battery_info_uncached().percent)
            (pack / 'present').write_text('0')
            self.assertIsNone(battery._read_battery_info_uncached())

    def test_full_pack_does_not_hide_low_or_unreadable_pack(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {'KILIX_BATTERY_SUPPLY_DIR': tmp}), patch.object(battery, 'chrome_enabled', return_value=True):
            for name, capacity in [('BAT0', '100'), ('BAT1', '98')]:
                pack = Path(tmp) / name
                pack.mkdir()
                (pack / 'type').write_text('Battery')
                (pack / 'capacity').write_text(capacity)
                (pack / 'status').write_text('Charging')
            self.ae(battery._read_battery_info_uncached().percent, 99)
            (Path(tmp) / 'BAT1/capacity').unlink()
            self.assertIsNone(battery._read_battery_info_uncached().percent)
            (Path(tmp) / 'BAT1/capacity').write_text('100')
            self.assertIsNone(battery._read_battery_info_uncached())

    def test_unknown_indicator_and_disabled_setting(self):
        with patch.object(battery, 'battery_info', return_value=battery.BatteryInfo(None, 'unknown')):
            self.assertIn('?', battery.battery_segment()[0])
        with patch.object(battery, 'chrome_enabled', return_value=False):
            self.assertIsNone(battery._read_battery_info_uncached())

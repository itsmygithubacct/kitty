import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from kitty import kilix_battery as battery
from . import BaseTest


class TestBatteryVisibility(BaseTest):
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

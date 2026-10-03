#!/usr/bin/env python

from unittest.mock import patch

from kittens.kilix_chrome_widget.main import card

from . import BaseTest


class TestKilixChromeWidget(BaseTest):
    def test_temperature_display_converts_without_changing_sensor_policy(self) -> None:
        from kitty.kilix_battery import temperature_text, _thermal_level
        with patch('kitty.kilix_battery.chrome_value', return_value='fahrenheit'):
            self.assertEqual(temperature_text(0), '32.0°F')
            self.assertEqual(temperature_text(100), '212.0°F')
            self.assertEqual(temperature_text(None), '--°F')
            self.assertEqual(_thermal_level(90), 'red')
        with patch('kitty.kilix_battery.chrome_value', return_value='celsius'):
            self.assertEqual(temperature_text(100), '100.0°C')
            self.assertEqual(_thermal_level(90), 'red')


    def test_network_card_handles_connected_and_offline_states(self) -> None:
        result = type('Result', (), {'stdout': 'wifi:connected:Home\n'})()
        with patch('kittens.kilix_chrome_widget.main.subprocess.run',
                   return_value=result):
            self.assertIn('Home', card('network'))
        with patch('kittens.kilix_chrome_widget.main.subprocess.run',
                   side_effect=OSError):
            self.assertIn('No connected interface', card('network'))

    def test_battery_and_temperature_cards_have_action_hints(self) -> None:
        battery = type('Battery', (), {'percent': 73, 'status': 'charging'})()
        thermal = type('Thermal', (), {'celsius': 61.25, 'level': 'green'})()
        with patch('kitty.kilix_battery.battery_info', return_value=battery):
            lines = card('battery')
            self.assertIn('73%', lines)
            self.assertIn('Double-click toggles percentage · Esc close', lines)
        with patch('kitty.kilix_battery.thermal_info', return_value=thermal), \
                patch('kitty.kilix_battery.chrome_value', return_value='celsius'):
            lines = card('temperature')
            self.assertIn('61.2°C', lines)
            self.assertIn('Double-click for dashboard · Esc close', lines)

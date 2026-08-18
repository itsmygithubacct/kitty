#!/usr/bin/env python

from unittest.mock import patch

from kittens.kilix_chrome_widget.main import card

from . import BaseTest


class TestKilixChromeWidget(BaseTest):

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
        with patch('kitty.kilix_battery.thermal_info', return_value=thermal):
            lines = card('temperature')
            self.assertIn('61.2°C', lines)
            self.assertIn('Double-click for dashboard · Esc close', lines)

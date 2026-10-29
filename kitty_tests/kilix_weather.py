import io
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from kitty.kilix_chrome import weather as module

from . import BaseTest


class Process:
    def __init__(self, result=None):
        self.returncode = None
        self.output = json.dumps(result or {}).encode()
        self.stdout = io.BytesIO()
        self.killed = False

    def poll(self): return self.returncode
    def communicate(self): return self.output, None
    def kill(self): self.killed, self.returncode = True, -9
    def wait(self, timeout=None): return self.returncode


class TestKilixWeather(BaseTest):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'settings.conf'
        self.env = patch.dict(os.environ, {'GPU_TERMINAL_SETTINGS_FILE': str(self.path)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = {'KILIX_WEATHER_ZIP': '00501', 'KILIX_WEATHER_REFRESH': '60'}
        self.save()
        self.weather = module.Weather()
        self.reading = module.Reading('00501', 'Test place', 10, 2, 1000, 1001)

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(''.join(f'{key}={value}\n' for key, value in self.config.items()))
        temporary.replace(self.path)

    def test_provider_requires_exact_us_zip_and_valid_numeric_data(self):
        responses = [
            {'results': [{'country_code': 'DE', 'postcodes': ['00501']},
                         {'country_code': 'US', 'postcodes': ['00501'], 'latitude': 40, 'longitude': -73,
                          'name': 'Test place', 'admin1': 'Test state'}]},
            {'current': {'temperature_2m': 10, 'weather_code': 2, 'time': 1000}},
        ]
        with patch.object(module, 'json_request', side_effect=responses) as request:
            result = module.fetch('00501')
        self.ae(result.celsius, 10)
        self.ae(result.place, 'Test place, Test state')
        self.assertIn('countryCode=US', request.call_args_list[0].args[0])
        self.assertIn('timeformat=unixtime', request.call_args_list[1].args[0])
        for response in ({'results': []}, {'results': [{'country_code': 'US', 'postcodes': ['00502']}]},
                         {'results': [{'country_code': 'DE', 'postcodes': ['00501']}]}):
            with patch.object(module, 'json_request', return_value=response), self.assertRaises(ValueError):
                module.fetch('00501')
        for bad in ('1234', '12345-0001', '１２３４５', '12345\n', '../../'):
            with patch.object(module, 'json_request') as request, self.assertRaises(ValueError):
                module.fetch(bad)
            request.assert_not_called()
        for invalid in (float('nan'), float('inf'), None, True, '10', -101):
            with self.assertRaises(ValueError):
                module.Reading.parse({**vars(self.reading), 'celsius': invalid}, '00501')

    def test_json_response_bound_and_timeout(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'x' * (module.MAX_RESPONSE + 1)
        with patch.object(module, 'urlopen', return_value=response) as urlopen, self.assertRaises(ValueError):
            module.json_request('https://api.open-meteo.com/')
        self.ae(urlopen.call_args.kwargs['timeout'], 6)

    def test_single_worker_schedule_manual_and_refresh_request_coalescing(self):
        process = Process({'reading': vars(self.reading)})
        with patch.object(module.time, 'monotonic', return_value=100), \
                patch.object(module.time, 'time', return_value=1001), \
                patch.object(module.subprocess, 'Popen', return_value=process) as launch, \
                patch('kitty.constants.kitty_exe', return_value='kitty'):
            self.weather.tick()
            for _ in range(10):
                self.weather.tick()
            launch.assert_called_once()
            self.ae(self.weather.due, 3700)
            self.config['KILIX_WEATHER_REQUEST'] = '1'
            self.save()
            self.weather.tick()
            self.assertFalse(self.weather.pending)
            process.returncode = 0
            self.weather.tick()
            self.ae(self.weather.reading, self.reading)
            self.config['KILIX_WEATHER_REFRESH'] = 'manual'
            self.save()
            self.weather.tick()
            self.ae(self.weather.due, float('inf'))
            self.config['KILIX_WEATHER_REQUEST'] = '2'
            self.save()
            self.weather.tick()
            self.ae(launch.call_count, 2)

    def test_location_change_cancels_and_discards_old_result_without_overlap(self):
        old, new = Process({'reading': vars(self.reading)}), Process()
        with patch.object(module.subprocess, 'Popen', side_effect=[old, new]) as launch, \
                patch('kitty.constants.kitty_exe', return_value='kitty'):
            self.weather.tick()
            self.config['KILIX_WEATHER_ZIP'] = '90210'
            self.save()
            self.weather.tick()
            self.assertTrue(old.killed)
            self.assertIsNone(self.weather.reading)
            self.ae(launch.call_count, 2)
            self.ae(launch.call_args.args[0][-1], '90210')
            self.config['KILIX_CHROME_WEATHER'] = '0'
            self.save()
            self.weather.tick()
            self.assertTrue(new.killed)
            self.assertIsNone(self.weather.process)
            self.assertFalse(self.weather.pending)

    def test_timeout_offline_and_cache_keep_stale_data_for_same_zip(self):
        self.weather.sync(100, 1001)
        self.weather.reading = self.reading
        process = Process()
        self.weather.process, self.weather.deadline = process, 120
        self.weather.poll(121)
        self.assertTrue(process.killed)
        self.weather.poll(122)
        self.ae(self.weather.reading, self.reading)
        self.assertIn('timed out', self.weather.error)
        self.assertIn('stale', self.weather.text())
        self.ae(module.load_reading('00501'), self.reading)
        self.assertIsNone(module.load_reading('90210'))
        self.ae(module.cache_path().stat().st_mode & 0o777, 0o600)
        failed = Process({'error': 'offline'})
        failed.returncode = 0
        self.weather.process = failed
        self.weather.poll(123)
        self.ae(self.weather.reading, self.reading)
        self.ae(self.weather.error, 'offline')

    def test_daily_arming_clock_changes_and_suspend_do_not_repeat(self):
        self.config.update(KILIX_WEATHER_REFRESH='daily', KILIX_WEATHER_TIME='07:30')
        self.save()
        morning = datetime(2026, 10, 29, 6).timestamp()
        self.weather.sync(100, morning)
        self.weather.pending = False
        self.ae(self.weather.daily_due, datetime(2026, 10, 29, 7, 30).timestamp())
        with patch.object(module.time, 'monotonic', return_value=200), \
                patch.object(module.time, 'time', return_value=datetime(2026, 10, 29, 9).timestamp()), \
                patch.object(self.weather, 'start', side_effect=lambda n, w: self.weather.arm(n, w)) as start:
            self.weather.tick()
            self.weather.tick()
            start.assert_called_once()
        self.ae(self.weather.daily_due, datetime(2026, 10, 30, 7, 30).timestamp())
        self.config['KILIX_WEATHER_TIME'] = '10:00'
        self.save()
        self.weather.sync(300, datetime(2026, 10, 29, 9).timestamp())
        self.ae(self.weather.daily_due, datetime(2026, 10, 29, 10).timestamp())

    def test_no_location_no_network_and_shutdown_reaps_worker_and_timer(self):
        self.config['KILIX_WEATHER_ZIP'] = ''
        self.save()
        with patch.object(self.weather, 'start') as start:
            self.weather.tick()
            start.assert_not_called()
        process = Process()
        self.weather.process, self.weather.timer = process, 42
        with patch('kitty.fast_data_types.remove_timer') as remove:
            self.weather.stop()
            self.weather.stop()
        remove.assert_called_once_with(42)
        self.assertTrue(process.killed)
        self.assertTrue(process.stdout.closed)
        self.assertIsNone(self.weather.process)

    def test_one_timer_for_all_windows_and_no_synchronous_fetch(self):
        self.config['KILIX_WEATHER_ZIP'] = ''
        self.save()
        with patch.object(module, 'weather', self.weather), \
                patch('kitty.fast_data_types.add_timer', return_value=42) as timer, \
                patch('kitty.fast_data_types.get_boss', return_value=Mock(all_tab_managers=[])), \
                patch.object(module, 'fetch') as fetch:
            for _ in range(12):
                module.ensure_weather_timer()
            timer.assert_called_once()
            fetch.assert_not_called()

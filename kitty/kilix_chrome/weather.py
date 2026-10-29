"""ZIP weather: one cancellable fetch process and one schedule per Kilix instance.

Only worker_main performs networking. The UI polls a bounded child result;
even a stuck DNS resolver is killed at the overall deadline. Configuration
uses the shared settings file; the private cache is never a source of choices.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .settings import chrome_enabled, chrome_value
from .settings import path as settings_path

WEATHER_ACTION = 'kilix_show_weather_widget'
REFRESH_CHOICES = ('manual', '15', '30', '60', '180', 'daily')
FETCH_TIMEOUT = 20.0
STALE_SECONDS = 2 * 60 * 60
MAX_RESPONSE = 256 * 1024
ATTRIBUTION = 'Weather: Open-Meteo (CC BY 4.0) · Location: GeoNames'
CONDITIONS = {
    0: 'Clear', 1: 'Mostly clear', 2: 'Partly cloudy', 3: 'Overcast',
    45: 'Fog', 48: 'Freezing fog', 51: 'Light drizzle', 53: 'Drizzle',
    55: 'Heavy drizzle', 56: 'Freezing drizzle', 57: 'Freezing drizzle',
    61: 'Light rain', 63: 'Rain', 65: 'Heavy rain', 66: 'Freezing rain',
    67: 'Heavy freezing rain', 71: 'Light snow', 73: 'Snow', 75: 'Heavy snow',
    77: 'Snow grains', 80: 'Rain showers', 81: 'Rain showers',
    82: 'Heavy showers', 85: 'Snow showers', 86: 'Heavy snow showers',
    95: 'Thunderstorm', 96: 'Thunderstorm / hail', 97: 'Heavy thunderstorm',
    99: 'Thunderstorm / hail',
}


def valid_zip(value: str) -> bool:
    return re.fullmatch(r'[0-9]{5}', value) is not None


def clean_text(value: object, limit: int = 80) -> str:
    return ''.join(c for c in str(value) if c.isprintable())[:limit]


def cache_path() -> Path:
    # Also follows an isolated GPU_TERMINAL_SETTINGS_FILE during testing.
    return Path(settings_path()).parent / 'kilix' / 'weather.json'


def number(value: object, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Missing numeric weather data')
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError('Invalid weather data')
    return float(value)


@dataclass(frozen=True)
class Reading:
    zip: str
    place: str
    celsius: float
    code: int
    observed: float
    fetched: float

    @classmethod
    def parse(cls, raw: object, zip_code: str) -> Reading:
        if not isinstance(raw, dict) or raw.get('zip') != zip_code or not valid_zip(zip_code):
            raise ValueError('Weather location does not match the selected ZIP')
        code = number(raw.get('code'), 0, 999)
        if not code.is_integer():
            raise ValueError('Invalid weather condition code')
        return cls(zip_code, clean_text(raw.get('place', zip_code)),
                   number(raw.get('celsius'), -100, 70), int(code),
                   number(raw.get('observed'), 1, 1e11), number(raw.get('fetched'), 1, 1e11))

    def stale(self, now: float) -> bool:
        return now - self.observed > STALE_SECONDS or self.observed > now + 1800 or self.fetched > now + 300

    def temperature(self) -> str:
        if chrome_value('KILIX_TEMPERATURE_UNIT', 'fahrenheit') == 'celsius':
            return f'{self.celsius:.0f}°C'
        return f'{self.celsius * 9 / 5 + 32:.0f}°F'

    @property
    def condition(self) -> str:
        return CONDITIONS.get(self.code, 'Unknown conditions')


def json_request(url: str) -> dict:
    request = Request(url, headers={'User-Agent': 'Kilix-Weather/0.2.2', 'Accept': 'application/json'})
    with urlopen(request, timeout=6) as response:
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise ValueError('Weather response was too large')
    value = json.loads(data)
    if not isinstance(value, dict) or value.get('error'):
        raise ValueError('Weather provider returned an invalid response')
    return value


def fetch(zip_code: str) -> Reading:
    if not valid_zip(zip_code):
        raise ValueError('Enter a five-digit US ZIP code')
    geo = json_request('https://geocoding-api.open-meteo.com/v1/search?' + urlencode({
        'name': zip_code, 'countryCode': 'US', 'count': 100, 'language': 'en', 'format': 'json',
    }))
    results = geo.get('results', [])
    if not isinstance(results, list):
        raise ValueError('Invalid ZIP lookup response')
    matches = [r for r in results if isinstance(r, dict) and r.get('country_code') == 'US'
               and isinstance(r.get('postcodes'), list) and zip_code in r['postcodes']]
    if not matches:
        raise ValueError('ZIP not found by the weather provider; check the five digits')
    place = matches[0]
    latitude = number(place.get('latitude'), -90, 90)
    longitude = number(place.get('longitude'), -180, 180)
    weather = json_request('https://api.open-meteo.com/v1/forecast?' + urlencode({
        'latitude': latitude, 'longitude': longitude,
        'current': 'temperature_2m,weather_code', 'temperature_unit': 'celsius',
        'timeformat': 'unixtime', 'timezone': 'GMT', 'forecast_days': 1,
    }))
    current = weather.get('current')
    if not isinstance(current, dict):
        raise ValueError('Current conditions are unavailable')
    return Reading.parse({
        'zip': zip_code, 'place': ', '.join(clean_text(place.get(k, '')) for k in ('name', 'admin1')),
        'celsius': current.get('temperature_2m'), 'code': current.get('weather_code'),
        'observed': current.get('time'), 'fetched': time.time(),
    }, zip_code)


def worker_main() -> None:
    try:
        reading = fetch(sys.argv[1])
        result = {'reading': vars(reading)}
    except HTTPError as error:
        result = {'error': 'Weather provider is busy; try later' if error.code == 429 else
                  f'Weather provider unavailable (HTTP {error.code})'}
    except (URLError, OSError, TimeoutError):
        result = {'error': 'Weather unavailable: offline or request timed out'}
    except (ValueError, KeyError, TypeError, IndexError):
        result = {'error': 'ZIP not found or invalid weather response; check the ZIP and retry'}
    # Small enough for a pipe even when the parent has not polled yet.
    with suppress(BrokenPipeError):
        print(json.dumps(result, ensure_ascii=True))


def next_daily(now: float, clock: str) -> float:
    hour, minute = map(int, clock.split(':'))
    local = datetime.fromtimestamp(now)
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due <= local:
        due += timedelta(days=1)
    # A naive local timestamp lets libc apply DST on the scheduled date.
    return due.timestamp()


def load_reading(zip_code: str) -> Reading | None:
    try:
        with cache_path().open(encoding='utf-8') as stream:
            raw = json.loads(stream.read(8193))
        return Reading.parse(raw.get('reading'), zip_code)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


class Weather:
    def __init__(self) -> None:
        self.identity: tuple[str, str] = ('', '')
        self.zip = ''
        self.reading: Reading | None = None
        self.error = ''
        self.process: subprocess.Popen | None = None
        self.discard = False
        self.deadline = 0.0
        self.pending = False
        self.schedule = ('60', '07:00')
        self.request = ''
        self.due = float('inf')
        self.daily_due = float('inf')
        self.last_daily_date = ''
        self.timer: int | None = None
        self.last_text = ''

    def publish(self) -> None:
        target = cache_path()
        try:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            data = {'zip': self.zip, 'reading': vars(self.reading) if self.reading else None,
                    'error': self.error, 'loading': self.process is not None and not self.discard}
            fd, temporary = tempfile.mkstemp(prefix='.weather-', dir=target.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(data, stream, ensure_ascii=True)
                os.replace(temporary, target)
            finally:
                with suppress(OSError):
                    os.unlink(temporary)
        except OSError:
            # A read-only cache must not prevent weather from working in memory.
            pass

    def arm(self, now: float, wall: float) -> None:
        mode, clock = self.schedule
        self.due = now + int(mode) * 60 if mode.isdigit() else float('inf')
        self.daily_due = next_daily(wall, clock) if mode == 'daily' else float('inf')

    def cancel_fetch(self) -> None:
        if self.process is not None:
            self.discard = True
            with suppress(OSError):
                self.process.kill()

    def sync(self, now: float, wall: float) -> None:
        raw_zip = chrome_value('KILIX_WEATHER_ZIP', '').strip()
        zip_code = raw_zip if chrome_enabled('KILIX_CHROME_WEATHER') and valid_zip(raw_zip) else ''
        identity = (settings_path(), zip_code)
        if identity != self.identity:
            self.cancel_fetch()
            self.identity, self.zip = identity, zip_code
            self.reading = load_reading(zip_code) if zip_code else None
            self.error = ''
            self.pending = bool(zip_code)
            self.last_daily_date = ''
            self.arm(now, wall)
        mode = chrome_value('KILIX_WEATHER_REFRESH', '60')
        clock = chrome_value('KILIX_WEATHER_TIME', '07:00')
        if mode not in REFRESH_CHOICES:
            mode = '60'
        if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', clock):
            clock = '07:00'
        if (mode, clock) != self.schedule:
            self.schedule = (mode, clock)
            self.last_daily_date = ''
            self.arm(now, wall)
        request = chrome_value('KILIX_WEATHER_REQUEST', '0')
        if request != self.request:
            if self.request and zip_code and self.process is None:
                self.pending = True
            self.request = request
        if not zip_code:
            self.pending = False

    def start(self, now: float, wall: float) -> None:
        from ..constants import clear_handled_signals, is_windows, kitty_exe
        self.pending = False
        self.arm(now, wall)
        try:
            self.process = subprocess.Popen([
                kitty_exe(), '+runpy', 'from kitty.kilix_chrome.weather import worker_main; worker_main()', self.zip,
            ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                preexec_fn=None if is_windows else clear_handled_signals,
                creationflags=subprocess.CREATE_NO_WINDOW if is_windows else 0)
            self.deadline, self.discard = now + FETCH_TIMEOUT, False
        except OSError:
            self.error = 'Could not start weather fetch; refresh to retry'
        self.publish()

    def poll(self, now: float) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            if now >= self.deadline and not self.discard:
                self.cancel_fetch()
                self.error = 'Weather request timed out; refresh to retry'
                self.publish()
            return
        # The worker has exited and its result fits in a pipe. No UI wait.
        output, _ = process.communicate()
        self.process = None
        if not self.discard:
            try:
                result = json.loads(output)
                if process.returncode != 0 or not isinstance(result, dict):
                    raise ValueError('Worker failed')
                if 'error' in result:
                    self.error = clean_text(result['error'], 160)
                else:
                    self.reading = Reading.parse(result['reading'], self.zip)
                    self.error = ''
            except (ValueError, KeyError, TypeError):
                self.error = 'Weather fetch failed; refresh to retry'
        self.discard = False
        self.publish()

    def tick(self) -> None:
        now, wall = time.monotonic(), time.time()
        self.sync(now, wall)
        self.poll(now)
        if self.zip and self.process is None:
            today = datetime.fromtimestamp(wall).date().isoformat()
            daily = wall >= self.daily_due and today != self.last_daily_date
            if self.pending or now >= self.due or daily:
                if daily:
                    self.last_daily_date = today
                self.start(now, wall)

    def text(self) -> str:
        if not self.zip:
            return ' Weather: ZIP '
        if self.reading is not None:
            stale = bool(self.error) or self.reading.stale(time.time())
            return f' {self.reading.temperature()} {self.reading.condition}{" (stale)" if stale else ""} '
        return ' Weather: unavailable ' if self.error else ' Weather: loading… '

    def stop(self) -> None:
        if self.timer is not None:
            from ..fast_data_types import remove_timer
            remove_timer(self.timer)
            self.timer = None
        self.cancel_fetch()
        if self.process is not None:
            # Only used at shutdown. Reap the killed worker and close its pipe.
            with suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=0.25)
            if self.process.stdout is not None:
                self.process.stdout.close()
            self.process = None


weather = Weather()


def ensure_weather_timer() -> None:
    from ..fast_data_types import add_timer, get_boss, mark_os_window_dirty, remove_timer

    def tick(timer_id: int | None = None) -> None:
        weather.tick()
        text = weather.text()
        if text != weather.last_text:
            weather.last_text = text
            for manager in get_boss().all_tab_managers:
                manager.mark_tab_bar_dirty()
                mark_os_window_dirty(manager.os_window_id)
        if not chrome_enabled('KILIX_CHROME_WEATHER') and weather.process is None and weather.timer is not None:
            remove_timer(weather.timer)
            weather.timer = None

    if weather.timer is None and chrome_enabled('KILIX_CHROME_WEATHER'):
        weather.timer = add_timer(tick, 1.0, True)
        tick()


def weather_segment() -> tuple[str, str] | None:
    return (weather.text(), WEATHER_ACTION) if chrome_enabled('KILIX_CHROME_WEATHER') else None


def weather_card() -> tuple[str, ...]:
    zip_code = chrome_value('KILIX_WEATHER_ZIP', '').strip()
    reading = load_reading(zip_code)
    state = ''
    with suppress(OSError, ValueError, AttributeError):
        with cache_path().open(encoding='utf-8') as stream:
            raw = json.loads(stream.read(8193))
        if raw.get('zip') == zip_code:
            state = clean_text(raw.get('error', ''), 160)
    if reading is None:
        details = (state or 'Weather loading…', 'Right-click Weather to check the ZIP or refresh.')
    else:
        stale = ' · STALE' if state or reading.stale(time.time()) else ''
        details = (f'{reading.place} ({zip_code})',
                   f'{reading.temperature()} · {reading.condition}{stale}',
                   'Conditions at ' + datetime.fromtimestamp(reading.observed).astimezone().strftime('%Y-%m-%d %H:%M %Z'),
                   state or 'Current conditions are weather-model estimates.')
    return ('Weather', '', *details, '', ATTRIBUTION,
            'open-meteo.com · Free API for non-commercial use', 'Right-click for ZIP, refresh and schedule · Esc close')

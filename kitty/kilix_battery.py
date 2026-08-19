#!/usr/bin/env python

import math
import os
import time
from glob import iglob
from typing import NamedTuple

from .rgb import to_color
from .utils import color_as_int, log_error, which
from .kilix_chrome.providers import (
    CALENDAR_GLYPH, CALENDAR_WIDGET_ACTION, DATE_WIDGET_ACTION, NETWORK_GLYPH,
    NETWORK_WIDGET_ACTION, START_MENU_ACTION, VOLUME_GLYPH,
    VOLUME_WIDGET_ACTION, clock_segment, clock_segments, network_segment,
    start_menu_segment, volume_segment, volume_target,
)
from .kilix_chrome.settings import chrome_enabled, chrome_value, ensure_timer
from .kilix_chrome.lifecycle import (
    ensure_chrome_timers, ensure_clock_timer, invalidate_all,
)


class BatteryInfo(NamedTuple):
    percent: int
    status: str


class ThermalInfo(NamedTuple):
    celsius: float
    level: str


BATTERY_TOGGLE_ACTION = 'kilix_toggle_battery_percent'
THERMAL_WIDGET_ACTION = 'kilix_show_thermal_widget'
THERMOMETER_GLYPH = chr(0xf2c9)
_THERMAL_CACHE: ThermalInfo | None = None
_THERMAL_CACHE_ROOT = ''
_THERMAL_CACHE_UNTIL = 0.0
_THERMAL_LAST_SIGNATURE: tuple[int, str] | None = None
_THERMAL_TIMER_STARTED = False
_THERMAL_CACHE_SECONDS = 3.0
_THERMAL_REFRESH_SECONDS = 5.0
_BATTERY_SHOW_PERCENT = True
_BATTERY_CACHE: BatteryInfo | None = None
_BATTERY_CACHE_UNTIL = 0.0
_BATTERY_LAST_SIGNATURE: tuple[int, str] | None = None
_BATTERY_TIMER_STARTED = False
_BATTERY_CACHE_SECONDS = 10.0
_BATTERY_REFRESH_SECONDS = 30.0
_BATTERY_LOW = (color_as_int(to_color('#ef2929')) << 8) | 2
_BATTERY_MID = (color_as_int(to_color('#fce94f')) << 8) | 2
_BATTERY_HIGH = (color_as_int(to_color('#8ae234')) << 8) | 2
_THERMAL_UNKNOWN = (color_as_int(to_color('#888a85')) << 8) | 2


def kilix_temps_target() -> tuple[list[str], str | None] | None:
    """Resolve the graphical dashboard without relying on the caller's cwd."""
    # An explicitly installed command is the most reliable target. In
    # particular, do not let an incomplete development checkout shadow it.
    if executable := which('kilix-temps'):
        return [executable, '--graphics'], None
    source_home = os.environ.get('GPU_TERMINAL_SOURCE_HOME') or os.path.join(
        os.path.expanduser('~'), 'gpu_terminal')
    project = os.path.join(
        os.path.abspath(os.path.expanduser(source_home)), 'kilix-temps')
    candidate = os.path.join(project, 'build', 'kilix-temps')
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return [candidate, '--graphics'], project
    # Every Kilix launch exports KILIX_HOME. Its `temps` command installs the
    # exact dashboard/graphics closure selected by this Kilix checkout before
    # starting it, which also makes a fresh standalone Kilix checkout useful.
    if kilix_home := os.environ.get('KILIX_HOME'):
        kilix = os.path.join(kilix_home, 'kilix')
        if os.path.isfile(kilix) and os.access(kilix, os.X_OK):
            return [kilix, 'temps', '--graphics'], None
    return None


kilix_volume_target = volume_target


def _thermal_sys_root() -> str:
    value = os.environ.get('KILIX_THERMAL_SYS_ROOT') or '/sys'
    return os.path.abspath(os.path.expanduser(value))


def _read_temperature(path: str) -> float | None:
    value = _read_number(path)
    if value is None:
        return None
    celsius = value / 1000.0 if abs(value) > 1000.0 else value
    if not math.isfinite(celsius) or celsius <= 0.0 or celsius > 250.0:
        return None
    return celsius


def _thermal_level(celsius: float) -> str:
    if celsius >= 90.0:
        return 'red'
    if celsius >= 80.0:
        return 'yellow'
    return 'green'


def _display_temperature(celsius: float) -> float:
    # Derive both text and policy color from the same rounded value so a sensor
    # just below a boundary cannot display that boundary in the lower color.
    return float(f'{celsius:.1f}')


def _read_thermal_info_uncached() -> ThermalInfo | None:
    root = _thermal_sys_root()
    paths = (
        *iglob(os.path.join(root, 'class', 'thermal', 'thermal_zone*', 'temp')),
        *iglob(os.path.join(root, 'class', 'hwmon', 'hwmon*', 'temp*_input')),
    )
    readings = [value for path in paths
                if (value := _read_temperature(path)) is not None]
    if not readings:
        return None
    celsius = _display_temperature(max(readings))
    return ThermalInfo(celsius, _thermal_level(celsius))


def _telemetry_thermal_info() -> ThermalInfo | None:
    try:
        from .kilix_telemetry import hottest_celsius
        value = hottest_celsius(refresh=True)
    except Exception:
        return None
    if value is None or not math.isfinite(value) or value <= 0.0 or value > 250.0:
        return None
    celsius = _display_temperature(value)
    return ThermalInfo(celsius, _thermal_level(celsius))


def thermal_info() -> ThermalInfo | None:
    global _THERMAL_CACHE, _THERMAL_CACHE_ROOT, _THERMAL_CACHE_UNTIL
    root = _thermal_sys_root()
    use_telemetry = 'KILIX_THERMAL_SYS_ROOT' not in os.environ
    cache_root = 'kilix-telemetry' if use_telemetry else root
    now = time.monotonic()
    if cache_root != _THERMAL_CACHE_ROOT or now >= _THERMAL_CACHE_UNTIL:
        _THERMAL_CACHE = _telemetry_thermal_info() if use_telemetry else None
        if _THERMAL_CACHE is None:
            _THERMAL_CACHE = _read_thermal_info_uncached()
        _THERMAL_CACHE_ROOT = cache_root
        _THERMAL_CACHE_UNTIL = now + _THERMAL_CACHE_SECONDS
    return _THERMAL_CACHE


def _thermal_color(info: ThermalInfo | None) -> int:
    if info is None:
        return _THERMAL_UNKNOWN
    if info.level == 'red':
        return _BATTERY_LOW
    if info.level == 'yellow':
        return _BATTERY_MID
    return _BATTERY_HIGH


def _thermal_signature(info: ThermalInfo | None) -> tuple[float, str] | None:
    return None if info is None else (round(info.celsius, 1), info.level)


def thermal_segment() -> tuple[str, str, int] | None:
    global _THERMAL_LAST_SIGNATURE
    if not chrome_enabled('KILIX_CHROME_TEMPERATURE', '0'):
        return None
    info = thermal_info()
    _THERMAL_LAST_SIGNATURE = _thermal_signature(info)
    # The cached reading is rounded by _display_temperature before its level is
    # selected, so this text and the policy color always describe one value.
    temperature = '--' if info is None else f'{info.celsius:.1f}'
    return (
        f' {THERMOMETER_GLYPH} {temperature}° ',
        THERMAL_WIDGET_ACTION,
        _thermal_color(info),
    )


def _read_text(path: str) -> str:
    try:
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def _read_number(path: str) -> float | None:
    try:
        return float(_read_text(path))
    except ValueError:
        return None


def _battery_supply_root() -> str:
    return os.environ.get('KILIX_BATTERY_SUPPLY_DIR') or '/sys/class/power_supply'


def _iter_battery_dirs() -> list[str]:
    root = _battery_supply_root()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    ans = []
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        typ = _read_text(os.path.join(path, 'type')).lower()
        if typ == 'battery' or (not typ and name.startswith(('BAT', 'CMB'))):
            ans.append(path)
    return ans


def _read_charge_pair(path: str) -> tuple[float, float] | None:
    for cur_name, full_name in (
        ('energy_now', 'energy_full'),
        ('charge_now', 'charge_full'),
        ('energy_now', 'energy_full_design'),
        ('charge_now', 'charge_full_design'),
    ):
        cur = _read_number(os.path.join(path, cur_name))
        full = _read_number(os.path.join(path, full_name))
        if cur is not None and full and full > 0:
            return cur, full
    return None


def _read_battery_info_uncached() -> BatteryInfo | None:
    if not chrome_enabled('KILIX_CHROME_BATTERY'):
        return None
    total_now = total_full = 0.0
    capacities: list[float] = []
    statuses: list[str] = []
    for path in _iter_battery_dirs():
        status = _read_text(os.path.join(path, 'status'))
        statuses.append(status)
        pair = _read_charge_pair(path)
        if pair is not None:
            cur, full = pair
            total_now += cur
            total_full += full
        elif (cap := _read_number(os.path.join(path, 'capacity'))) is not None:
            capacities.append(cap)
    if not any(s.lower() == 'discharging' for s in statuses):
        return None
    if total_full > 0:
        pct = round(total_now * 100 / total_full)
    elif capacities:
        pct = round(sum(capacities) / len(capacities))
    else:
        return None
    return BatteryInfo(max(0, min(100, int(pct))), 'discharging')


def battery_info() -> BatteryInfo | None:
    global _BATTERY_CACHE, _BATTERY_CACHE_UNTIL
    now = time.monotonic()
    if now >= _BATTERY_CACHE_UNTIL:
        _BATTERY_CACHE = _read_battery_info_uncached()
        _BATTERY_CACHE_UNTIL = now + _BATTERY_CACHE_SECONDS
    return _BATTERY_CACHE


def _battery_signature(info: BatteryInfo | None) -> tuple[int, str] | None:
    return None if info is None else (info.percent, info.status)


def _battery_color(percent: int) -> int:
    if percent <= 20:
        return _BATTERY_LOW
    if percent <= 50:
        return _BATTERY_MID
    return _BATTERY_HIGH


def _battery_glyph(percent: int) -> str:
    if percent < 10:
        return chr(0xf0083)  # battery alert
    if percent >= 95:
        return chr(0xf0079)  # battery full
    return chr(0xf007a + max(0, min(8, percent // 10 - 1)))


def battery_segment() -> tuple[str, str, int] | None:
    global _BATTERY_LAST_SIGNATURE
    info = battery_info()
    _BATTERY_LAST_SIGNATURE = _battery_signature(info)
    if info is None:
        return None
    glyph = _battery_glyph(info.percent)
    if _BATTERY_SHOW_PERCENT:
        text = f' {info.percent:3d}% {glyph} '
    else:
        text = f' {glyph} '
    return text, BATTERY_TOGGLE_ACTION, _battery_color(info.percent)


def _invalidate_all_chrome() -> None:
    """Compatibility alias for extensions importing the pre-0.2 helper."""
    invalidate_all()


def toggle_battery_percent() -> None:
    global _BATTERY_SHOW_PERCENT
    _BATTERY_SHOW_PERCENT = not _BATTERY_SHOW_PERCENT
    _invalidate_all_chrome()


def _battery_timer(timer_id: int | None = None) -> None:
    global _BATTERY_CACHE_UNTIL, _BATTERY_LAST_SIGNATURE
    _BATTERY_CACHE_UNTIL = 0.0
    sig = _battery_signature(battery_info())
    if sig != _BATTERY_LAST_SIGNATURE:
        _BATTERY_LAST_SIGNATURE = sig
        _invalidate_all_chrome()


def _thermal_timer(timer_id: int | None = None) -> None:
    global _THERMAL_CACHE_UNTIL, _THERMAL_LAST_SIGNATURE
    if not chrome_enabled('KILIX_CHROME_TEMPERATURE', '0'):
        return
    _THERMAL_CACHE_UNTIL = 0.0
    sig = _thermal_signature(thermal_info())
    if sig != _THERMAL_LAST_SIGNATURE:
        _THERMAL_LAST_SIGNATURE = sig
        _invalidate_all_chrome()


def ensure_chrome_settings_timer() -> None:
    ensure_timer(_invalidate_all_chrome)


def ensure_battery_timer() -> None:
    global _BATTERY_TIMER_STARTED
    if _BATTERY_TIMER_STARTED or not chrome_enabled('KILIX_CHROME_BATTERY'):
        return
    _BATTERY_TIMER_STARTED = True
    try:
        from .fast_data_types import add_timer
        add_timer(_battery_timer, _BATTERY_REFRESH_SECONDS, True)
    except Exception as e:
        log_error(f'Failed to start kilix battery chrome timer: {e}')


def ensure_thermal_timer() -> None:
    global _THERMAL_TIMER_STARTED
    if _THERMAL_TIMER_STARTED or not chrome_enabled(
            'KILIX_CHROME_TEMPERATURE', '0'):
        return
    _THERMAL_TIMER_STARTED = True
    try:
        from .fast_data_types import add_timer
        add_timer(_thermal_timer, _THERMAL_REFRESH_SECONDS, True)
    except Exception as e:
        log_error(f'Failed to start kilix thermal chrome timer: {e}')

#!/usr/bin/env python

import os
import time
from typing import NamedTuple

from .rgb import to_color
from .utils import color_as_int, log_error


class BatteryInfo(NamedTuple):
    percent: int
    status: str


BATTERY_TOGGLE_ACTION = 'kilix_toggle_battery_percent'
VOLUME_WIDGET_ACTION = 'kilix_show_volume_widget'
NETWORK_WIDGET_ACTION = 'kilix_show_network_widget'
CALENDAR_WIDGET_ACTION = 'kilix_show_calendar_widget'
DATE_WIDGET_ACTION = 'kilix_show_date_widget'
VOLUME_GLYPH = chr(0xf028)
NETWORK_GLYPH = chr(0xf1eb)
CALENDAR_GLYPH = chr(0xf073)
_CHROME_SETTINGS_TIMER_STARTED = False
_CHROME_SETTINGS_LAST_SIGNATURE: tuple[object, ...] | None = None
_CHROME_SETTINGS_CACHE_SIGNATURE: tuple[object, ...] | None = None
_CHROME_SETTINGS_CACHE: dict[str, str] = {}
_CHROME_SETTINGS_CACHE_EXISTS = False
_CHROME_SETTINGS_REFRESH_SECONDS = 1.0
_CLOCK_TIMER_STARTED = False
_CLOCK_LAST_TEXT = ''
_CLOCK_REFRESH_SECONDS = 15.0
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


def _shared_settings_path() -> str:
    override = os.environ.get('GPU_TERMINAL_SETTINGS_FILE')
    if override:
        return os.path.abspath(os.path.expanduser(override))
    root = os.environ.get('GPU_TERMINAL_HOME') or os.path.join(
        os.path.expanduser('~'), '.local', 'gpu_terminal')
    return os.path.join(os.path.abspath(os.path.expanduser(root)), 'settings.conf')


def _shared_settings_signature() -> tuple[object, ...]:
    path = _shared_settings_path()
    try:
        stat = os.stat(path)
    except OSError:
        return (path, 'missing')
    return (
        path, stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns,
        stat.st_mode, stat.st_size,
    )


def _shared_settings() -> tuple[dict[str, str], bool]:
    global _CHROME_SETTINGS_CACHE, _CHROME_SETTINGS_CACHE_EXISTS
    global _CHROME_SETTINGS_CACHE_SIGNATURE
    signature = _shared_settings_signature()
    if signature != _CHROME_SETTINGS_CACHE_SIGNATURE:
        values: dict[str, str] = {}
        exists = signature[-1] != 'missing'
        if exists:
            try:
                with open(str(signature[0]), encoding='utf-8', errors='replace') as stream:
                    for line in stream:
                        line = line.strip()
                        if not line or line.startswith('#') or '=' not in line:
                            continue
                        key, value = line.split('=', 1)
                        key = key.strip()
                        if key and key.replace('_', '').isalnum() and not key[0].isdigit():
                            values[key] = value.strip()
            except OSError:
                exists = False
                values = {}
        _CHROME_SETTINGS_CACHE = values
        _CHROME_SETTINGS_CACHE_EXISTS = exists
        _CHROME_SETTINGS_CACHE_SIGNATURE = signature
    return _CHROME_SETTINGS_CACHE, _CHROME_SETTINGS_CACHE_EXISTS


def chrome_value(name: str, default: str = '1') -> str:
    values, exists = _shared_settings()
    if exists:
        return values.get(name, default)
    # Backward-compatible migration fallback for a fork launched without the
    # Kilix wrapper. Normal Kilix launches create the shared file first.
    return os.environ.get(name, default)


def chrome_enabled(name: str, default: str = '1') -> bool:
    return chrome_value(name, default).lower() not in (
        '', '0', 'no', 'false', 'off', 'disabled')


def volume_segment() -> tuple[str, str] | None:
    if not chrome_enabled('KILIX_CHROME_VOLUME'):
        return None
    return f' {VOLUME_GLYPH} ', VOLUME_WIDGET_ACTION


def network_segment() -> tuple[str, str] | None:
    if not chrome_enabled('KILIX_CHROME_NETWORK'):
        return None
    return f' {NETWORK_GLYPH} ', NETWORK_WIDGET_ACTION


def clock_segment() -> str | None:
    if not chrome_enabled('KILIX_CHROME_CLOCK'):
        return None
    fmt = chrome_value('KILIX_CHROME_CLOCK_FORMAT', '%Y-%m-%d %H:%M') or '%Y-%m-%d %H:%M'
    try:
        text = time.strftime(fmt)
    except Exception:
        text = time.strftime('%Y-%m-%d %H:%M')
    return f' {text} '


def clock_segments() -> tuple[tuple[str, str], ...]:
    """Clickable calendar button followed by the configured date/time text."""
    ans: list[tuple[str, str]] = []
    if chrome_enabled('KILIX_CHROME_CALENDAR'):
        ans.append((f' {CALENDAR_GLYPH}', CALENDAR_WIDGET_ACTION))
    clock = clock_segment()
    if clock is not None:
        ans.append((clock, DATE_WIDGET_ACTION))
    return tuple(ans)


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
    from .fast_data_types import get_boss, mark_os_window_dirty
    for tm in get_boss().all_tab_managers:
        tm.mark_tab_bar_dirty()
        for tab in tm:
            tab.update_window_title_bars()
        mark_os_window_dirty(tm.os_window_id)


def _clock_timer(timer_id: int | None = None) -> None:
    global _CLOCK_LAST_TEXT
    text = clock_segment() or ''
    if text != _CLOCK_LAST_TEXT:
        _CLOCK_LAST_TEXT = text
        _invalidate_all_chrome()


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


def _chrome_settings_timer(timer_id: int | None = None) -> None:
    global _CHROME_SETTINGS_LAST_SIGNATURE
    signature = _shared_settings_signature()
    if signature != _CHROME_SETTINGS_LAST_SIGNATURE:
        _CHROME_SETTINGS_LAST_SIGNATURE = signature
        _shared_settings()
        _invalidate_all_chrome()


def ensure_chrome_settings_timer() -> None:
    global _CHROME_SETTINGS_LAST_SIGNATURE, _CHROME_SETTINGS_TIMER_STARTED
    if _CHROME_SETTINGS_TIMER_STARTED:
        return
    _CHROME_SETTINGS_TIMER_STARTED = True
    _CHROME_SETTINGS_LAST_SIGNATURE = _shared_settings_signature()
    try:
        from .fast_data_types import add_timer
        add_timer(_chrome_settings_timer, _CHROME_SETTINGS_REFRESH_SECONDS, True)
    except Exception as e:
        log_error(f'Failed to start kilix chrome settings timer: {e}')


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


def ensure_clock_timer() -> None:
    global _CLOCK_LAST_TEXT, _CLOCK_TIMER_STARTED
    if _CLOCK_TIMER_STARTED or not chrome_enabled('KILIX_CHROME_CLOCK'):
        return
    _CLOCK_TIMER_STARTED = True
    _CLOCK_LAST_TEXT = clock_segment() or ''
    try:
        from .fast_data_types import add_timer
        add_timer(_clock_timer, _CLOCK_REFRESH_SECONDS, True)
    except Exception as e:
        log_error(f'Failed to start kilix clock chrome timer: {e}')


def ensure_chrome_timers() -> None:
    ensure_chrome_settings_timer()
    ensure_clock_timer()
    ensure_battery_timer()

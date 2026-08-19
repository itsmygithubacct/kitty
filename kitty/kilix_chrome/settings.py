"""Shared, live-reloaded settings for Kilix chrome."""

import os
from collections.abc import Callable

_cache_signature: tuple[object, ...] | None = None
_cache: dict[str, str] = {}
_cache_exists = False
_last_signature: tuple[object, ...] | None = None
_timer_started = False
REFRESH_SECONDS = 1.0


def path() -> str:
    override = os.environ.get('GPU_TERMINAL_SETTINGS_FILE')
    if override:
        return os.path.abspath(os.path.expanduser(override))
    root = os.environ.get('GPU_TERMINAL_HOME') or os.path.join(
        os.path.expanduser('~'), '.local', 'gpu_terminal')
    return os.path.join(os.path.abspath(os.path.expanduser(root)), 'settings.conf')


def signature() -> tuple[object, ...]:
    filename = path()
    try:
        stat = os.stat(filename)
    except OSError:
        return (filename, 'missing')
    return (filename, stat.st_dev, stat.st_ino, stat.st_mtime_ns,
            stat.st_ctime_ns, stat.st_mode, stat.st_size)


def values() -> tuple[dict[str, str], bool]:
    global _cache, _cache_exists, _cache_signature
    current = signature()
    if current != _cache_signature:
        parsed: dict[str, str] = {}
        exists = current[-1] != 'missing'
        if exists:
            try:
                with open(str(current[0]), encoding='utf-8', errors='replace') as stream:
                    for raw in stream:
                        line = raw.strip()
                        if not line or line.startswith('#') or '=' not in line:
                            continue
                        key, value = line.split('=', 1)
                        key = key.strip()
                        if key and key.replace('_', '').isalnum() and not key[0].isdigit():
                            parsed[key] = value.strip()
            except OSError:
                exists, parsed = False, {}
        _cache, _cache_exists, _cache_signature = parsed, exists, current
    return _cache, _cache_exists


def chrome_value(name: str, default: str = '1') -> str:
    settings, exists = values()
    return settings.get(name, default) if exists else os.environ.get(name, default)


def chrome_enabled(name: str, default: str = '1') -> bool:
    return chrome_value(name, default).lower() not in (
        '', '0', 'no', 'false', 'off', 'disabled')


def ensure_timer(invalidate: Callable[[], None]) -> None:
    global _last_signature, _timer_started
    if _timer_started:
        return
    _timer_started, _last_signature = True, signature()

    def tick(timer_id: int | None = None) -> None:
        global _last_signature
        current = signature()
        if current != _last_signature:
            _last_signature = current
            values()
            invalidate()
    try:
        from ..fast_data_types import add_timer
        add_timer(tick, REFRESH_SECONDS, True)
    except Exception as e:
        from ..utils import log_error
        log_error(f'Failed to start kilix chrome settings timer: {e}')

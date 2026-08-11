#!/usr/bin/env python
"""Low-overhead system CPU-load reporting for Kilix pane chrome."""

from __future__ import annotations

import math
import os
import time

from .kilix_battery import chrome_value


PANE_CPU_MODE_KEY = 'KILIX_CHROME_PANE_CPU_MODE'
PANE_CPU_MODE_DEFAULT = 'auto'
PANE_CPU_MODES = ('auto', 'always', 'off')
CPU_LOAD_THRESHOLD = 1.0
_CACHE_SECONDS = 1.5
_LOAD_CACHE_UNTIL = 0.0
_LOAD_CACHE_ROOT = ''
_LOAD_CACHE: float | None = None


def pane_cpu_mode() -> str:
    mode = chrome_value(
        PANE_CPU_MODE_KEY, PANE_CPU_MODE_DEFAULT).strip().lower()
    return mode if mode in PANE_CPU_MODES else PANE_CPU_MODE_DEFAULT


def _proc_root() -> str:
    value = os.environ.get('KILIX_CPU_PROC_ROOT') or '/proc'
    return os.path.abspath(os.path.expanduser(value))


def _read_load_average(root: str) -> float | None:
    try:
        with open(os.path.join(root, 'loadavg'), encoding='ascii') as stream:
            value = float(stream.read(128).split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return value if math.isfinite(value) and value >= 0.0 else None


def cpu_load_average(force: bool = False) -> float | None:
    """Return the cached one-minute Linux load average."""
    global _LOAD_CACHE, _LOAD_CACHE_ROOT, _LOAD_CACHE_UNTIL
    root = _proc_root()
    now = time.monotonic()
    if not force and root == _LOAD_CACHE_ROOT and now < _LOAD_CACHE_UNTIL:
        return _LOAD_CACHE
    _LOAD_CACHE = _read_load_average(root)
    _LOAD_CACHE_ROOT = root
    _LOAD_CACHE_UNTIL = now + _CACHE_SECONDS
    return _LOAD_CACHE


def format_pane_cpu_load(
    load_average: float | None,
    mode: str = PANE_CPU_MODE_DEFAULT,
) -> str:
    """Format the load shown to the left of the shared CPU/RAM chip."""
    mode = mode.strip().lower()
    if mode not in PANE_CPU_MODES:
        mode = PANE_CPU_MODE_DEFAULT
    if mode == 'off' or load_average is None:
        return ''
    value = float(load_average)
    if not math.isfinite(value) or value < 0.0:
        return ''
    if mode == 'auto' and value <= CPU_LOAD_THRESHOLD:
        return ''
    return f'{value:.1f}'


def pane_cpu_label(force: bool = False) -> str:
    mode = pane_cpu_mode()
    if mode == 'off':
        return ''
    return format_pane_cpu_load(cpu_load_average(force=force), mode)

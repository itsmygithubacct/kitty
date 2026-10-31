#!/usr/bin/env python
"""Whole-system CPU load and RAM use for the Kilix tab-bar status area."""

from __future__ import annotations

import os
import sys
import time
from typing import NamedTuple

from .kilix_chrome.settings import chrome_value
from .kilix_cpu import format_pane_cpu_load
from .kilix_memory import MEMORY_GLYPH, MEMORY_WIDGET_ACTION, format_pane_memory
from .utils import log_error

# Same policy names as the pane chip, but the bar shows the system by default.
SYSTEM_LOAD_MODE_KEY = 'KILIX_CHROME_SYSTEM_LOAD_MODE'
SYSTEM_LOAD_MODE_DEFAULT = 'always'
SYSTEM_LOAD_MODES = ('auto', 'always', 'off')
_CACHE_SECONDS = 1.5
_REFRESH_SECONDS = 4.0


class SystemLoad(NamedTuple):
    cpu_cores: float | None
    memory_used_bytes: int | None


class _CpuTimes(NamedTuple):
    total: int
    idle: int
    logical_cpus: int


_PREVIOUS_TIMES: _CpuTimes | None = None
_CACHE: SystemLoad = SystemLoad(None, None)
_CACHE_UNTIL = 0.0
# The text each OS window's tab bar last drew, by OS window id.
_DRAWN_TEXT: dict[int, str] = {}
_TIMER_STARTED = False


def system_load_mode() -> str:
    mode = chrome_value(
        SYSTEM_LOAD_MODE_KEY, SYSTEM_LOAD_MODE_DEFAULT).strip().lower()
    return mode if mode in SYSTEM_LOAD_MODES else SYSTEM_LOAD_MODE_DEFAULT


def _proc_root() -> str:
    value = os.environ.get('KILIX_SYSTEM_PROC_ROOT') or '/proc'
    return os.path.abspath(os.path.expanduser(value))


def _read_cpu_times(root: str) -> _CpuTimes | None:
    try:
        with open(os.path.join(root, 'stat'), encoding='ascii', errors='replace') as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    total = idle = -1
    logical_cpus = 0
    for line in lines:
        name, _, rest = line.partition(' ')
        if name == 'cpu':
            try:
                # user nice system idle iowait irq softirq steal; guest time is
                # already included in user and nice, so it is not added again.
                fields = [int(x) for x in rest.split()[:8]]
            except ValueError:
                return None
            if len(fields) < 4:
                return None
            total = sum(fields)
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        elif name.startswith('cpu') and name[3:].isdigit():
            logical_cpus += 1
    if total < 0 or logical_cpus < 1:
        return None
    return _CpuTimes(total, idle, logical_cpus)


def _read_memory_used(root: str) -> int | None:
    values: dict[str, int] = {}
    try:
        with open(os.path.join(root, 'meminfo'), encoding='ascii', errors='replace') as f:
            for line in f:
                key, _, rest = line.partition(':')
                if key in ('MemTotal', 'MemAvailable'):
                    try:
                        values[key] = int(rest.split()[0]) * 1024
                    except (IndexError, ValueError):
                        return None
    except OSError:
        return None
    if 'MemTotal' not in values or 'MemAvailable' not in values:
        return None
    return max(0, values['MemTotal'] - values['MemAvailable'])


def _sample() -> SystemLoad:
    global _PREVIOUS_TIMES
    if sys.platform == 'win32' and not os.environ.get('KILIX_SYSTEM_PROC_ROOT'):
        from .kilix_system_load_windows import read_cpu_times, read_memory_used
        native_times = read_cpu_times()
        times = _CpuTimes(*native_times) if native_times is not None else None
        memory = read_memory_used()
    else:
        root = _proc_root()
        times = _read_cpu_times(root)
        memory = _read_memory_used(root)
    cores = None
    previous = _PREVIOUS_TIMES
    if times is not None and previous is not None and times.logical_cpus == previous.logical_cpus:
        elapsed = times.total - previous.total
        if elapsed > 0:
            busy = elapsed - max(0, times.idle - previous.idle)
            cores = max(0.0, min(1.0, busy / elapsed)) * times.logical_cpus
    _PREVIOUS_TIMES = times
    return SystemLoad(cores, memory)


def system_load(force: bool = False) -> SystemLoad:
    """Return the cached sample; CPU is None until two samples exist."""
    global _CACHE, _CACHE_UNTIL
    now = time.monotonic()
    if force or now >= _CACHE_UNTIL:
        _CACHE = _sample()
        _CACHE_UNTIL = now + _CACHE_SECONDS
    return _CACHE


def format_system_load(load: SystemLoad, mode: str = SYSTEM_LOAD_MODE_DEFAULT) -> str:
    """Format CPU cores left of the RAM chip and GiB in use right of it."""
    mode = mode.strip().lower()
    if mode not in SYSTEM_LOAD_MODES:
        mode = SYSTEM_LOAD_MODE_DEFAULT
    if mode == 'off':
        return ''
    cpu_text = format_pane_cpu_load(load.cpu_cores, mode)
    memory_text = ''
    if load.memory_used_bytes is not None:
        label = format_pane_memory(load.memory_used_bytes, mode)
        if label:
            memory_text = f' {MEMORY_GLYPH} {label} '
    from .window_title_bar import pane_resource_text
    return pane_resource_text(cpu_text, memory_text)


def system_load_segment(os_window_id: int = 0) -> tuple[str, str | None, int] | None:
    mode = system_load_mode()
    text = '' if mode == 'off' else format_system_load(system_load(), mode)
    _DRAWN_TEXT[os_window_id] = text
    if not text:
        return None
    from .window_title_bar import _MEMORY_CHIP_COLOR
    # Native packages do not ship the Linux memory dashboard/settings command.
    # Show the counters without advertising an unavailable click action.
    action = None if sys.platform == 'win32' else MEMORY_WIDGET_ACTION
    return text, action, _MEMORY_CHIP_COLOR


def _system_load_timer(timer_id: int | None = None) -> None:
    del timer_id
    if not _DRAWN_TEXT:
        return
    mode = system_load_mode()
    text = '' if mode == 'off' else format_system_load(system_load(force=True), mode)
    try:
        from .fast_data_types import get_boss, mark_os_window_dirty
        # Only the tab bar shows this; leave every pane title bar alone. A bar
        # that has not drawn the segment since its last refresh (hidden,
        # vertical, fullscreen, or a closed window) is not woken again: it
        # records fresh text whenever it next draws.
        for manager in get_boss().all_tab_managers:
            drawn = _DRAWN_TEXT.get(manager.os_window_id)
            if drawn is None or drawn == text:
                continue
            del _DRAWN_TEXT[manager.os_window_id]
            manager.mark_tab_bar_dirty()
            mark_os_window_dirty(manager.os_window_id)
    except Exception as error:
        log_error(f'Failed to refresh kilix system load chrome: {error}')


def ensure_system_load_timer() -> None:
    global _TIMER_STARTED
    if _TIMER_STARTED or system_load_mode() == 'off':
        return
    _TIMER_STARTED = True
    # Prime the CPU counters so the first refresh can report a delta.
    system_load(force=True)
    try:
        from .fast_data_types import add_timer
        add_timer(_system_load_timer, _REFRESH_SECONDS, True)
    except Exception as error:
        log_error(f'Failed to start kilix system load chrome timer: {error}')

#!/usr/bin/env python
"""Low-overhead per-pane process-tree memory accounting for Kilix chrome."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .kilix_chrome.settings import chrome_value
from .utils import log_error, which

PANE_MEMORY_MODE_KEY = 'KILIX_CHROME_PANE_MEMORY_MODE'
PANE_MEMORY_MODE_DEFAULT = 'auto'
PANE_MEMORY_MODES = ('auto', 'always', 'off')
MEMORY_WIDGET_ACTION = 'kilix_show_memory_widget'
MEMORY_GLYPH = chr(0xf035b)  # Nerd Fonts Material Design "memory" chip
KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB
_CACHE_SECONDS = 1.5
_REFRESH_SECONDS = 2.0
_PROCESS_CACHE_UNTIL = 0.0
_PROCESS_CACHE_ROOT = ''
_PROCESS_CACHE: dict[int, 'ProcessSample'] = {}
_PROCESS_CHILDREN: dict[int, tuple[int, ...]] = {}
_PROCESS_TREES: dict[int, tuple[int, ...]] = {}
_PROCESS_PROPORTIONAL: dict[int, int] = {}
_PROCESS_TOTALS: dict[int, int] = {}
_PROCESS_CPU_CORES: dict[int, float] = {}
_PROCESS_PREVIOUS: dict[int, tuple[int, int]] = {}
_PROCESS_PREVIOUS_WHEN = 0.0
_LAST_LABELS: dict[int, tuple[str, str]] = {}
_TIMER_STARTED = False


@dataclass(frozen=True)
class ProcessSample:
    ppid: int
    rss_bytes: int
    start_ticks: int
    cpu_ticks: int


def pane_memory_mode() -> str:
    mode = chrome_value(
        PANE_MEMORY_MODE_KEY, PANE_MEMORY_MODE_DEFAULT).strip().lower()
    return mode if mode in PANE_MEMORY_MODES else PANE_MEMORY_MODE_DEFAULT


def format_pane_memory(memory_bytes: int, mode: str = 'auto') -> str:
    """Format a dynamic chip label, or return empty when policy hides it."""
    mode = mode.strip().lower()
    if mode not in PANE_MEMORY_MODES:
        mode = PANE_MEMORY_MODE_DEFAULT
    value = max(0, int(memory_bytes))
    if mode == 'off' or (mode == 'auto' and value < GIB):
        return ''
    if value >= GIB:
        # The chip glyph supplies the unit context; keep the GiB value itself
        # to the requested one-decimal display (for example, "1.1").
        return f'{value / GIB:.1f}'
    if value >= MIB:
        amount = value / MIB
        return f'{amount:.1f}M' if amount < 100 else f'{amount:.0f}M'
    return f'{value / KIB:.0f}K'


def _proc_root() -> str:
    value = os.environ.get('KILIX_MEMORY_PROC_ROOT') or '/proc'
    return os.path.abspath(os.path.expanduser(value))


def _page_size() -> int:
    try:
        return max(1, int(os.sysconf('SC_PAGE_SIZE')))
    except (OSError, ValueError):
        return 4096


def _clock_ticks() -> int:
    try:
        return max(1, int(os.sysconf('SC_CLK_TCK')))
    except (OSError, ValueError):
        return 100


def _read_process_stat(path: str, page_size: int) -> ProcessSample | None:
    try:
        with open(path, encoding='utf-8', errors='replace') as stream:
            text = stream.read()
    except OSError:
        return None
    closing = text.rfind(')')
    if closing < 0:
        return None
    fields = text[closing + 1:].split()
    # fields begins with stat field 3 (state), making PPID index 1 and RSS
    # index 21. The parenthesized command can itself contain spaces or ')',
    # hence the rfind rather than splitting the whole document.
    if len(fields) <= 21:
        return None
    try:
        return ProcessSample(
            ppid=max(0, int(fields[1])),
            rss_bytes=max(0, int(fields[21])) * page_size,
            start_ticks=max(0, int(fields[19])),
            cpu_ticks=max(0, int(fields[11]) + int(fields[12])),
        )
    except ValueError:
        return None


def _refresh_process_cache(force: bool = False) -> dict[int, ProcessSample]:
    global _PROCESS_CACHE, _PROCESS_CACHE_ROOT, _PROCESS_CACHE_UNTIL
    global _PROCESS_CPU_CORES, _PROCESS_PREVIOUS, _PROCESS_PREVIOUS_WHEN
    global _PROCESS_CHILDREN, _PROCESS_PROPORTIONAL, _PROCESS_TOTALS
    global _PROCESS_TREES
    root = _proc_root()
    now = time.monotonic()
    if (
        not force
        and root == _PROCESS_CACHE_ROOT
        and now < _PROCESS_CACHE_UNTIL
    ):
        return _PROCESS_CACHE
    page_size = _page_size()
    samples: dict[int, ProcessSample] = {}
    try:
        names = os.listdir(root)
    except OSError:
        names = ()
    for name in names:
        if not name.isdigit():
            continue
        sample = _read_process_stat(
            os.path.join(root, name, 'stat'), page_size)
        if sample is not None:
            samples[int(name)] = sample
    if root != _PROCESS_CACHE_ROOT:
        _PROCESS_PREVIOUS = {}
        _PROCESS_PREVIOUS_WHEN = 0.0
    elapsed = max(0.0, now - _PROCESS_PREVIOUS_WHEN)
    denominator = _clock_ticks() * elapsed
    cpu_cores: dict[int, float] = {}
    for pid, sample in samples.items():
        previous = _PROCESS_PREVIOUS.get(pid)
        value = 0.0
        if (
            previous is not None
            and previous[0] == sample.start_ticks
            and denominator > 0.0
        ):
            value = max(0, sample.cpu_ticks - previous[1]) / denominator
        cpu_cores[pid] = value
    child_lists: dict[int, list[int]] = defaultdict(list)
    for pid, sample in samples.items():
        child_lists[sample.ppid].append(pid)
    _PROCESS_CACHE = samples
    _PROCESS_CHILDREN = {
        ppid: tuple(pids) for ppid, pids in child_lists.items()
    }
    _PROCESS_TREES = {}
    _PROCESS_PROPORTIONAL = {}
    _PROCESS_TOTALS = {}
    _PROCESS_CPU_CORES = cpu_cores
    _PROCESS_PREVIOUS = {
        pid: (sample.start_ticks, sample.cpu_ticks)
        for pid, sample in samples.items()
    }
    _PROCESS_PREVIOUS_WHEN = now
    _PROCESS_CACHE_ROOT = root
    _PROCESS_CACHE_UNTIL = now + _CACHE_SECONDS
    return samples


def _telemetry_pane(root_pid: int) -> Any | None:
    try:
        from .kilix_telemetry import pane_metrics
        metrics = pane_metrics(root_pid)
    except Exception:
        return None
    return metrics if getattr(metrics, 'process_count', 0) > 0 else None


def _proportional_bytes(pid: int, fallback: int) -> int:
    """Read PSS for one process, falling back to its inexpensive RSS."""
    if pid in _PROCESS_PROPORTIONAL:
        return _PROCESS_PROPORTIONAL[pid]
    path = os.path.join(_proc_root(), str(pid), 'smaps_rollup')
    value = fallback
    try:
        with open(path, encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if not line.startswith('Pss:'):
                    continue
                fields = line.split()
                if len(fields) >= 2:
                    value = max(0, int(fields[1])) * KIB
                break
    except (OSError, ValueError):
        pass
    _PROCESS_PROPORTIONAL[pid] = value
    return value


def _pane_processes(
    root_pid: int,
    samples: dict[int, ProcessSample],
) -> tuple[int, ...]:
    cached = _PROCESS_TREES.get(root_pid)
    if cached is not None:
        return cached
    descendants: list[int] = []
    pending = [root_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if pid not in samples:
            continue
        descendants.append(pid)
        pending.extend(_PROCESS_CHILDREN.get(pid, ()))
    result = tuple(descendants)
    _PROCESS_TREES[root_pid] = result
    return result


def pane_memory_rss_bytes(root_pid: int) -> int:
    """Return the inexpensive aggregate RSS for a pane process tree."""
    try:
        root_pid = int(root_pid)
    except (TypeError, ValueError):
        return 0
    if root_pid <= 0:
        return 0
    if (metrics := _telemetry_pane(root_pid)) is not None:
        return max(0, int(metrics.rss_bytes))
    samples = _refresh_process_cache()
    return sum(
        samples[pid].rss_bytes
        for pid in _pane_processes(root_pid, samples)
    )


def pane_memory_bytes(root_pid: int) -> int:
    """Return PSS/RSS for a pane's shell and all of its descendants."""
    try:
        root_pid = int(root_pid)
    except (TypeError, ValueError):
        return 0
    if root_pid <= 0:
        return 0
    if (metrics := _telemetry_pane(root_pid)) is not None:
        return max(0, int(metrics.proportional_bytes))
    samples = _refresh_process_cache()
    if root_pid in _PROCESS_TOTALS:
        return _PROCESS_TOTALS[root_pid]
    if root_pid not in samples:
        return 0
    total = sum(
        _proportional_bytes(pid, samples[pid].rss_bytes)
        for pid in _pane_processes(root_pid, samples)
    )
    _PROCESS_TOTALS[root_pid] = total
    return total


def pane_cpu_cores(root_pid: int, *, force: bool = False) -> float | None:
    """Return process-tree CPU in logical cores from telemetry or local fallback."""
    try:
        root_pid = int(root_pid)
    except (TypeError, ValueError):
        return None
    if root_pid <= 0:
        return None
    if (metrics := _telemetry_pane(root_pid)) is not None:
        return max(0.0, float(metrics.cpu_cores))
    samples = _refresh_process_cache(force=force)
    if root_pid not in samples:
        return None
    return sum(
        _PROCESS_CPU_CORES.get(pid, 0.0)
        for pid in _pane_processes(root_pid, samples)
    )


def pane_memory_label(root_pid: int) -> str:
    mode = pane_memory_mode()
    if mode == 'off':
        return ''
    if mode == 'auto' and pane_memory_rss_bytes(root_pid) < GIB:
        # PSS cannot exceed the process tree's aggregate RSS, so this avoids
        # opening smaps_rollup at all for the common below-threshold case.
        return ''
    return format_pane_memory(pane_memory_bytes(root_pid), mode)


def pane_memory_segment(root_pid: int) -> tuple[str, str] | None:
    label = pane_memory_label(root_pid)
    if not label:
        return None
    return f' {MEMORY_GLYPH} {label} ', MEMORY_WIDGET_ACTION


def kilix_memory_target() -> tuple[list[str], str | None] | None:
    """Resolve Kilix Memory without depending on the caller's cwd."""
    if executable := which('kilix-memory'):
        return [executable, '--graphics'], None
    source_home = os.environ.get('GPU_TERMINAL_SOURCE_HOME') or os.path.join(
        os.path.expanduser('~'), 'gpu_terminal')
    source_home = os.path.abspath(os.path.expanduser(source_home))
    project = os.path.join(source_home, 'kilix-memory')
    executable = os.path.join(project, 'build', 'kilix-memory')
    if os.path.isfile(executable) and os.access(executable, os.X_OK):
        return [executable, '--graphics'], project
    kilix_home = os.environ.get('KILIX_HOME') or os.path.join(
        source_home, 'kilix')
    kilix = os.path.join(kilix_home, 'kilix')
    if os.path.isfile(kilix) and os.access(kilix, os.X_OK):
        return [kilix, 'memory', '--graphics'], None
    return None


def _memory_timer(timer_id: int | None = None) -> None:
    del timer_id
    global _LAST_LABELS
    from .fast_data_types import get_boss, mark_os_window_dirty
    from .kilix_cpu import pane_cpu_label, pane_cpu_mode
    current: dict[int, tuple[str, str]] = {}
    try:
        boss = get_boss()
    except Exception:
        return
    roots: set[int] = set()
    for manager in boss.all_tab_managers:
        for tab in manager:
            for window in tab:
                child = getattr(window, 'child', None)
                try:
                    pid = int(getattr(child, 'process_tree_root_pid', 0))
                except (TypeError, ValueError):
                    pid = 0
                if pid > 0:
                    roots.add(pid)
    from .kilix_telemetry import refresh_panes
    memory_mode = pane_memory_mode()
    shared = refresh_panes(tuple(roots), register=memory_mode != 'off')
    if not shared and (memory_mode != 'off' or pane_cpu_mode() != 'off'):
        _refresh_process_cache(force=True)
    for manager in boss.all_tab_managers:
        manager_changed = False
        for tab in manager:
            tab_changed = False
            for window in tab:
                child = getattr(window, 'child', None)
                pid = getattr(child, 'process_tree_root_pid', 0)
                label = pane_memory_label(pid) if pid else ''
                cpu_label = pane_cpu_label(pid) if pid else ''
                resource_labels = (cpu_label, label)
                current[window.id] = resource_labels
                if _LAST_LABELS.get(window.id) != resource_labels:
                    tab_changed = True
            if tab_changed:
                tab.update_window_title_bars()
                manager_changed = True
        if manager_changed:
            mark_os_window_dirty(manager.os_window_id)
    _LAST_LABELS = current


def ensure_pane_memory_timer() -> None:
    global _TIMER_STARTED
    if _TIMER_STARTED:
        return
    _TIMER_STARTED = True
    try:
        from .fast_data_types import add_timer
        add_timer(_memory_timer, _REFRESH_SECONDS, True)
    except Exception as error:
        log_error(f'Failed to start kilix pane memory timer: {error}')

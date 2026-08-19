#!/usr/bin/env python
"""Low-overhead process-tree CPU reporting for Kilix pane chrome."""

from __future__ import annotations

import math

from .kilix_chrome.settings import chrome_value

PANE_CPU_MODE_KEY = 'KILIX_CHROME_PANE_CPU_MODE'
PANE_CPU_MODE_DEFAULT = 'auto'
PANE_CPU_MODES = ('auto', 'always', 'off')
CPU_LOAD_THRESHOLD = 1.0


def pane_cpu_mode() -> str:
    mode = chrome_value(
        PANE_CPU_MODE_KEY, PANE_CPU_MODE_DEFAULT).strip().lower()
    return mode if mode in PANE_CPU_MODES else PANE_CPU_MODE_DEFAULT


def format_pane_cpu_load(
    cpu_cores: float | None,
    mode: str = PANE_CPU_MODE_DEFAULT,
) -> str:
    """Format pane CPU cores shown to the left of the shared CPU/RAM chip."""
    mode = mode.strip().lower()
    if mode not in PANE_CPU_MODES:
        mode = PANE_CPU_MODE_DEFAULT
    if mode == 'off' or cpu_cores is None:
        return ''
    value = float(cpu_cores)
    if not math.isfinite(value) or value < 0.0:
        return ''
    if mode == 'auto' and value <= CPU_LOAD_THRESHOLD:
        return ''
    return f'{value:.1f}'


def pane_cpu_label(root_pid: int, force: bool = False) -> str:
    mode = pane_cpu_mode()
    if mode == 'off':
        return ''
    from .kilix_memory import pane_cpu_cores
    return format_pane_cpu_load(pane_cpu_cores(root_pid, force=force), mode)

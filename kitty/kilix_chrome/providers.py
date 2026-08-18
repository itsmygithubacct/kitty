"""Small providers for non-sensor Kilix top-bar widgets."""

import os
import time

from ..utils import which
from .settings import chrome_enabled, chrome_value

VOLUME_WIDGET_ACTION = 'kilix_show_volume_widget'
NETWORK_WIDGET_ACTION = 'kilix_show_network_widget'
CALENDAR_WIDGET_ACTION = 'kilix_show_calendar_widget'
DATE_WIDGET_ACTION = 'kilix_show_date_widget'
VOLUME_GLYPH = chr(0xf028)
NETWORK_GLYPH = chr(0xf1eb)
CALENDAR_GLYPH = chr(0xf073)


def volume_segment() -> tuple[str, str] | None:
    return ((f' {VOLUME_GLYPH} ', VOLUME_WIDGET_ACTION)
            if chrome_enabled('KILIX_CHROME_VOLUME') else None)


def network_segment() -> tuple[str, str] | None:
    return ((f' {NETWORK_GLYPH} ', NETWORK_WIDGET_ACTION)
            if chrome_enabled('KILIX_CHROME_NETWORK') else None)


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
    ans: list[tuple[str, str]] = []
    if chrome_enabled('KILIX_CHROME_CALENDAR'):
        ans.append((f' {CALENDAR_GLYPH}', CALENDAR_WIDGET_ACTION))
    if (clock := clock_segment()) is not None:
        ans.append((clock, DATE_WIDGET_ACTION))
    return tuple(ans)


def volume_target() -> list[str] | None:
    if executable := which('kilix-volume'):
        return [executable]
    if kilix_home := os.environ.get('KILIX_HOME'):
        kilix = os.path.join(kilix_home, 'kilix')
        if os.path.isfile(kilix) and os.access(kilix, os.X_OK):
            return [kilix, 'volume']
    if fallback := (which('pulsemixer') or which('alsamixer')):
        return [fallback]
    return None

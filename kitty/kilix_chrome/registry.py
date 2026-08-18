"""Ordered widget registry and action dispatch for the Kilix top bar."""

from collections.abc import Callable
from dataclasses import dataclass

from .providers import (
    CALENDAR_WIDGET_ACTION, DATE_WIDGET_ACTION, NETWORK_WIDGET_ACTION,
    VOLUME_WIDGET_ACTION, clock_segments, network_segment, volume_segment,
    volume_target,
)

Segment = tuple[str, str | None, int]


@dataclass(frozen=True)
class Widget:
    key: str
    segments: Callable[[int], tuple[Segment, ...]]


def _one(provider: Callable[[], tuple[str, str] | None]) -> Callable[[int], tuple[Segment, ...]]:
    def get(foreground: int) -> tuple[Segment, ...]:
        item = provider()
        return () if item is None else ((item[0], item[1], foreground),)
    return get


def _clock(foreground: int) -> tuple[Segment, ...]:
    return tuple((text, action, foreground) for text, action in clock_segments())


SYSTEM_WIDGETS = (
    Widget('volume', _one(volume_segment)),
    Widget('network', _one(network_segment)),
    Widget('clock', _clock),
)


def system_segments(foreground: int) -> tuple[Segment, ...]:
    return tuple(segment for widget in SYSTEM_WIDGETS
                 for segment in widget.segments(foreground))


def segments_for(keys: tuple[str, ...], foreground: int) -> tuple[Segment, ...]:
    wanted = frozenset(keys)
    return tuple(segment for widget in SYSTEM_WIDGETS if widget.key in wanted
                 for segment in widget.segments(foreground))


def dispatch(tab_manager: object, action: str) -> bool:
    """Handle a registered system-widget action, returning whether it matched."""
    if action == VOLUME_WIDGET_ACTION:
        target = tab_manager.active_tab.active_window if tab_manager.active_tab else None  # type: ignore[attr-defined]
        if target is not None:
            from ..fast_data_types import get_boss
            cmd = volume_target()
            if cmd is None:
                get_boss().show_error(
                    'Volume control unavailable',
                    'Kilix Volume could not be resolved, and neither pulsemixer nor alsamixer was found.')
            elif (tab := target.tabref()) is not None:
                tab.new_window(use_shell=False, cmd=cmd,
                               override_title='Volume Control', overlay_for=target.id)
        return True
    if action == NETWORK_WIDGET_ACTION:
        target = tab_manager.active_tab.active_window if tab_manager.active_tab else None  # type: ignore[attr-defined]
        if target is not None:
            from ..fast_data_types import get_boss
            from ..utils import which
            executable = which('nmtui')
            if executable is None:
                get_boss().show_error('Network settings unavailable',
                                      'nmtui was not found. Install NetworkManager to use this widget.')
            elif (tab := target.tabref()) is not None:
                tab.new_window(use_shell=False, cmd=[executable],
                               override_title='Network Connections', overlay_for=target.id)
        return True
    if action in (CALENDAR_WIDGET_ACTION, DATE_WIDGET_ACTION):
        target = tab_manager.active_tab.active_window if tab_manager.active_tab else None  # type: ignore[attr-defined]
        if target is not None:
            from ..fast_data_types import get_boss
            mode = 'calendar' if action == CALENDAR_WIDGET_ACTION else 'date'
            get_boss().run_kitten_with_metadata('kilix_clock', (mode,), window=target)
        return True
    return False

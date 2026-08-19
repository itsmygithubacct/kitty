"""Ordered widget registry and action dispatch for the Kilix top bar."""

from collections.abc import Callable
from dataclasses import dataclass

from .providers import (
    CALENDAR_WIDGET_ACTION, DATE_WIDGET_ACTION, NETWORK_WIDGET_ACTION,
    START_MENU_ACTION, VOLUME_WIDGET_ACTION, clock_segments, network_segment,
    volume_segment, settings_target, volume_target,
)

Segment = tuple[str, str | None, int]
BATTERY_TOGGLE_ACTION = 'kilix_toggle_battery_percent'
THERMAL_WIDGET_ACTION = 'kilix_show_thermal_widget'
GESTURE_ACTIONS = frozenset((
    VOLUME_WIDGET_ACTION, NETWORK_WIDGET_ACTION, CALENDAR_WIDGET_ACTION,
    DATE_WIDGET_ACTION, BATTERY_TOGGLE_ACTION, THERMAL_WIDGET_ACTION,
))


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


def dispatch(tab_manager: object, action: str, gesture: str = 'single') -> bool:
    """Handle a registered system-widget action, returning whether it matched."""
    if action == START_MENU_ACTION:
        # Deliberately not in GESTURE_ACTIONS. This arm opens the menu whatever
        # the gesture, and the gesture path dispatches on every qualifying
        # release: a second left release arrives as 'double' and a right
        # release as 'right', so joining that set would open the menu twice on
        # a double click and again on a right click. The plain path is guarded
        # by a click count of one, which is exactly the one-open-per-click
        # behaviour the badge wants.
        from ..fast_data_types import get_boss
        get_boss().kilix_show_start_menu()
        return True
    if action == VOLUME_WIDGET_ACTION:
        target = tab_manager.active_tab.active_window if tab_manager.active_tab else None  # type: ignore[attr-defined]
        if target is not None:
            from ..fast_data_types import get_boss
            mode = {'single': 'compact', 'double': 'full',
                    'right': 'settings'}.get(gesture, 'full')
            cmd = volume_target(mode)
            if cmd is None:
                get_boss().show_error(
                    'Volume control unavailable',
                    'Kilix Volume could not be resolved, and neither pulsemixer nor alsamixer was found.')
            elif (tab := target.tabref()) is not None:
                title = 'Volume Control' if mode == 'full' else (
                    'Volume Settings' if mode == 'settings' else 'Volume')
                tab.new_window(use_shell=False, cmd=cmd,
                               override_title=title, overlay_for=target.id)
        return True
    if action == BATTERY_TOGGLE_ACTION:
        if gesture == 'right':
            target = tab_manager.active_tab.active_window if tab_manager.active_tab else None  # type: ignore[attr-defined]
            if target is not None and (cmd := settings_target()) is not None and (
                    tab := target.tabref()) is not None:
                tab.new_window(use_shell=False, cmd=cmd,
                               override_title='Kilix Settings', overlay_for=target.id)
        elif gesture == 'double':
            from ..kilix_battery import toggle_battery_percent
            toggle_battery_percent()
        else:
            target = tab_manager.active_tab.active_window if tab_manager.active_tab else None  # type: ignore[attr-defined]
            if target is not None:
                from ..fast_data_types import get_boss
                get_boss().run_kitten_with_metadata(
                    'kilix_chrome_widget', ('battery',), window=target)
        return True
    if action == THERMAL_WIDGET_ACTION:
        if gesture == 'right':
            target = tab_manager.active_tab.active_window if tab_manager.active_tab else None  # type: ignore[attr-defined]
            if target is not None and (cmd := settings_target()) is not None and (
                    tab := target.tabref()) is not None:
                tab.new_window(use_shell=False, cmd=cmd,
                               override_title='Kilix Settings', overlay_for=target.id)
        elif gesture == 'double':
            from ..kilix_battery import kilix_temps_target
            target = kilix_temps_target()
            if target is None:
                from ..fast_data_types import get_boss
                get_boss().show_error(
                    'Kilix Temps unavailable',
                    'Neither an installed Kilix Temps dashboard nor a Kilix installer could be found.')
            else:
                from ..tabs import SpecialWindow
                cmd, cwd = target
                tab_manager.new_tab(SpecialWindow(  # type: ignore[attr-defined]
                    cmd, override_title='Kilix Temps', cwd=cwd))
        else:
            target = tab_manager.active_tab.active_window if tab_manager.active_tab else None  # type: ignore[attr-defined]
            if target is not None:
                from ..fast_data_types import get_boss
                get_boss().run_kitten_with_metadata(
                    'kilix_chrome_widget', ('temperature',), window=target)
        return True
    if action == NETWORK_WIDGET_ACTION:
        target = tab_manager.active_tab.active_window if tab_manager.active_tab else None  # type: ignore[attr-defined]
        if target is not None:
            from ..fast_data_types import get_boss
            from ..utils import which
            if gesture == 'right':
                cmd = settings_target()
                if cmd is None:
                    get_boss().show_error('Kilix Settings unavailable',
                                          'The Kilix settings command could not be resolved.')
                elif (tab := target.tabref()) is not None:
                    tab.new_window(use_shell=False, cmd=cmd,
                                   override_title='Kilix Settings', overlay_for=target.id)
                return True
            if gesture == 'single':
                get_boss().run_kitten_with_metadata(
                    'kilix_chrome_widget', ('network',), window=target)
                return True
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
            if gesture == 'right':
                cmd = settings_target()
                if cmd is None:
                    get_boss().show_error('Kilix Settings unavailable',
                                          'The Kilix settings command could not be resolved.')
                elif (tab := target.tabref()) is not None:
                    tab.new_window(use_shell=False, cmd=cmd,
                                   override_title='Kilix Settings', overlay_for=target.id)
                return True
            mode = 'calendar' if action == CALENDAR_WIDGET_ACTION else 'date'
            get_boss().run_kitten_with_metadata('kilix_clock', (mode,), window=target)
        return True
    return False

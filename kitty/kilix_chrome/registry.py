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
    """Handle chrome gestures through the shared, viewport-level popup layer."""
    from ..fast_data_types import get_boss
    from .popup import ChromePopup, popup_target

    if action == START_MENU_ACTION:
        get_boss().kilix_show_start_menu()
        return True
    if action not in GESTURE_ACTIONS:
        return False
    target = tab_manager.active_tab.active_window if tab_manager.active_tab else None  # type: ignore[attr-defined]
    popup_action = f'{action}:{gesture}'
    if target is None or (target := popup_target(target, popup_action)) is None:
        return True
    tab = target.tabref()
    if tab is None:
        return True

    def command(cmd: list[str] | None, title: str, columns: int, lines: int) -> None:
        if cmd is None:
            get_boss().show_error(f'{title} unavailable', f'The {title} command could not be resolved.')
        else:
            tab.new_window(use_shell=False, cmd=cmd, override_title=title,
                           overlay_for=target.id,
                           kilix_popup=ChromePopup(popup_action, columns, lines))

    if action == VOLUME_WIDGET_ACTION:
        mode = {'single': 'compact', 'double': 'full', 'right': 'settings'}.get(gesture, 'full')
        title = {'compact': 'Volume', 'full': 'Volume Control', 'settings': 'Volume Settings'}[mode]
        columns, lines = {'compact': (48, 12), 'full': (76, 24), 'settings': (52, 14)}[mode]
        command(volume_target(mode), title, columns, lines)
    elif gesture == 'right':
        command(settings_target(), 'Kilix Settings', 90, 30)
    elif action == BATTERY_TOGGLE_ACTION and gesture == 'double':
        from ..kilix_battery import toggle_battery_percent
        toggle_battery_percent()
    elif action == THERMAL_WIDGET_ACTION and gesture == 'double':
        from ..kilix_battery import kilix_temps_target
        from ..tabs import SpecialWindow
        dashboard = kilix_temps_target()
        if dashboard is None:
            get_boss().show_error('Kilix Temps unavailable',
                                  'Neither an installed Kilix Temps dashboard nor a Kilix installer could be found.')
        else:
            cmd, cwd = dashboard
            tab_manager.new_tab(SpecialWindow(cmd, override_title='Kilix Temps', cwd=cwd))  # type: ignore[attr-defined]
    elif action == NETWORK_WIDGET_ACTION and gesture == 'double':
        from ..utils import which
        executable = which('nmtui')
        command([executable] if executable else None, 'Network Connections', 80, 26)
    elif action in (CALENDAR_WIDGET_ACTION, DATE_WIDGET_ACTION):
        mode = 'calendar' if action == CALENDAR_WIDGET_ACTION else 'date'
        # The date card can switch to the calendar without reopening its PTY.
        get_boss().run_kitten_with_metadata('kilix_clock', (mode,), window=target,
            kilix_popup=ChromePopup(popup_action, 34 if mode == 'calendar' else 46, 14))
    else:
        kind = {BATTERY_TOGGLE_ACTION: 'battery', THERMAL_WIDGET_ACTION: 'temperature',
                NETWORK_WIDGET_ACTION: 'network'}[action]
        get_boss().run_kitten_with_metadata('kilix_chrome_widget', (kind,), window=target,
            kilix_popup=ChromePopup(popup_action, 48, 8))
    return True

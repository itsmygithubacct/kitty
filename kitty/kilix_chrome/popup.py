"""Transient chrome surfaces, positioned independently of the pane layout."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..fast_data_types import BOTTOM_EDGE, LEFT_EDGE, RIGHT_EDGE, get_options, viewport_for_window
from ..types import WindowGeometry

if TYPE_CHECKING:
    from ..tabs import Tab
    from ..window import Window


@dataclass
class ChromePopup:
    action: str
    columns: int
    lines: int
    owner_id: int = 0
    cancelled: bool = False


def popup_geometry(
    bounds: tuple[int, int, int, int], cell: tuple[int, int],
    size: tuple[int, int], anchor: tuple[int, int], edge: int,
) -> WindowGeometry:
    """Fit whole terminal cells into the content viewport, next to the bar."""
    left, top, right, bottom = bounds
    cw, ch = cell
    cols = max(1, min(size[0], (right - left) // cw))
    rows = max(1, min(size[1], (bottom - top) // ch))
    width, height = cols * cw, rows * ch
    x, y = anchor
    if edge == BOTTOM_EDGE:
        y = bottom - height
    elif edge == RIGHT_EDGE:
        x = right - width
    elif edge == LEFT_EDGE:
        x = left
    else:
        y = top
    x = max(left, min(x, right - width))
    y = max(top, min(y, bottom - height))
    return WindowGeometry(x, y, x + width, y + height, cols, rows)


def layout_popups(tab: 'Tab') -> None:
    manager = tab.tab_manager_ref()
    if manager is None:
        return
    for window in tab.windows:
        popup = window.kilix_popup
        if popup is None or popup.cancelled:
            continue
        central, _bar, _vw, _vh, cw, ch = viewport_for_window(tab.os_window_id)
        bar = manager.tab_bar
        anchor = (central.left, central.top)
        if bar.laid_out_once:
            action = popup.action.partition(':')[0]
            for extent in bar.action_extents:
                if extent.action == action:
                    anchor = (bar.window_geometry.left + extent.x.start * cw,
                              bar.window_geometry.top + extent.y.start * ch)
                    break
        geometry = popup_geometry(
            (central.left, central.top, central.left + central.width, central.top + central.height),
            (cw, ch), (popup.columns, popup.lines), anchor, get_options().tab_bar_edge)
        window.set_geometry(geometry)


def current_popup(tab: 'Tab') -> 'Window | None':
    return next((w for w in reversed(tuple(tab.windows))
                 if w.kilix_popup is not None and not w.kilix_popup.cancelled), None)


def dismiss_popup(tab: 'Tab', restore_focus: bool = True) -> bool:
    from ..fast_data_types import get_boss
    window = current_popup(tab)
    if window is None or (popup := window.kilix_popup) is None:
        return False
    popup.cancelled = True
    window.set_visible_in_layout(False)
    # Restore the original terminal before the asynchronous child exit. A late
    # ready notification/result must not revive a dismissed menu or launch an app.
    boss = get_boss()
    if window.keys_redirected_till_ready_from:
        from ..fast_data_types import buffer_keys_in_window, set_redirect_keys_to_overlay
        set_redirect_keys_to_overlay(tab.os_window_id, tab.id, window.keys_redirected_till_ready_from, 0)
        buffer_keys_in_window(tab.os_window_id, tab.id, window.id, False)
        window.keys_redirected_till_ready_from = 0
    if restore_focus and (owner := boss.window_id_map.get(popup.owner_id)) is not None and owner in tab:
        tab.set_active_window(owner)
    boss.mark_window_for_close(window)
    return True


def popup_target(window: 'Window', action: str) -> 'Window | None':
    """Toggle the same control, or replace its card while retaining its owner."""
    from ..fast_data_types import get_boss
    tab = window.tabref()
    if tab is None:
        return None
    old = current_popup(tab)
    if old is not None and (popup := old.kilix_popup) is not None:
        target = get_boss().window_id_map.get(popup.owner_id)
        toggle = popup.action == action
        dismiss_popup(tab)
        if toggle:
            return None
        if target is not None and target in tab:
            return target
    return window if window.kilix_popup is None else None

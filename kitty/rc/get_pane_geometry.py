#!/usr/bin/env python
# License: GPLv3
"""Report the live pane render area without process or environment data."""
import json
from typing import TYPE_CHECKING

from .base import (
    MATCH_WINDOW_OPTION,
    ArgsType,
    Boss,
    PayloadGetType,
    PayloadType,
    RCOptions,
    RemoteCommand,
    RemoteControlErrorWithoutTraceback,
    ResponseType,
    Window,
)

if TYPE_CHECKING:
    from kitty.cli_stub import GetPaneGeometryRCOptions as CLIOptions


class GetPaneGeometry(RemoteCommand):
    protocol_spec = __doc__ = '''
    match/str: The single pane whose geometry is requested
    '''
    short_desc = 'Report the live render geometry of one pane'
    desc = ('Return pane geometry as JSON. Render bounds and cells use framebuffer pixels; '
            'the OS window origin and size use window-system coordinates. Absolute screen '
            'coordinates are available on X11. No process arguments or environment are returned.')
    options_spec = MATCH_WINDOW_OPTION

    def message_to_kitty(self, global_opts: RCOptions, opts: 'CLIOptions', args: ArgsType) -> PayloadType:
        return {'match': opts.match}

    def response_from_kitty(self, boss: Boss, window: Window | None,
                            payload_get: PayloadGetType) -> ResponseType:
        from kitty.constants import is_wayland
        from kitty.fast_data_types import cell_size_for_window, current_focused_os_window_id, get_os_window_pos, get_os_window_size, os_window_is_invisible
        windows = self.windows_for_match_payload(boss, window, payload_get)
        if len(windows) != 1:
            raise RemoteControlErrorWithoutTraceback('Select exactly one live pane')
        pane = windows[0]
        metrics = get_os_window_size(pane.os_window_id)
        if metrics is None or pane.destroyed:
            raise RemoteControlErrorWithoutTraceback('The pane no longer has a live OS window')
        tab = pane.tabref()
        manager = tab.tab_manager_ref() if tab else None
        active_tab = bool(manager and manager.active_tab is tab)
        screen_supported = not is_wayland()
        origin = get_os_window_pos(pane.os_window_id) if screen_supported else None
        return json.dumps({
            'version': 1, 'pane_id': pane.id,
            'render_rect': pane.content_geometry,
            'grid': [pane.screen.columns, pane.screen.lines],
            'cell_size': cell_size_for_window(pane.os_window_id),
            'framebuffer_size': [metrics['framebuffer_width'], metrics['framebuffer_height']],
            'window_size': [metrics['width'], metrics['height']],
            'screen_origin': origin,
            'screen_coordinates_supported': bool(screen_supported and origin is not None),
            'visible': bool(active_tab and pane.is_visible_in_layout
                            and not os_window_is_invisible(pane.os_window_id)),
            'focused': bool(active_tab and tab.active_window is pane
                            and current_focused_os_window_id() == pane.os_window_id),
        })


get_pane_geometry = GetPaneGeometry()

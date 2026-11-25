#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kilix contributors

from typing import TYPE_CHECKING

from .base import MATCH_WINDOW_OPTION, ArgsType, Boss, PayloadGetType, PayloadType, RCOptions, RemoteCommand, ResponseType, Window

if TYPE_CHECKING:
    from kitty.cli_stub import EmbedRCOptions as CLIOptions


class Embed(RemoteCommand):

    protocol_spec = __doc__ = '''
    match/str: The embedded window
    token/str: The token returned when the window was launched with --type=embed
    size/str: New size in cells, COLUMNSxROWS, or empty to keep it
    cell_size/str: The embedding program's cell size in pixels, WIDTHxHEIGHT, or empty to keep it
    selection/bool: Also answer with the window's selected text
    '''

    short_desc = 'Resize an embedded window or describe it'
    desc = (
        'An embedded window is one launched with :code:`launch --type=embed`: it is drawn by another program,'
        ' which reads its cells from a map file. This sets its size in cells (and the cell size in pixels that'
        ' programs in it are told), and prints the window id, map file and size as JSON. The token printed by'
        ' the launch is required.'
    )
    options_spec = MATCH_WINDOW_OPTION + '''\n
--token
The token printed when the window was launched.


--size
New size in cells, :code:`COLUMNSxROWS`.


--cell-size
The embedding program's cell size in pixels, :code:`WIDTHxHEIGHT`.


--selection
type=bool-set
Also answer with the window's selected text, as :code:`selection`. Sent on the
connection that carried the window's mouse input, it sees every event before it.
'''

    def message_to_kitty(self, global_opts: RCOptions, opts: 'CLIOptions', args: ArgsType) -> PayloadType:
        if not opts.match:
            self.fatal('--match is required')
        if not opts.token:
            self.fatal('--token is required')
        return {'match': opts.match, 'token': opts.token, 'size': opts.size, 'cell_size': opts.cell_size,
                'selection': opts.selection}

    def response_from_kitty(self, boss: Boss, window: Window | None, payload_get: PayloadGetType) -> ResponseType:
        import json

        from kitty.embed import parse_size
        token = payload_get('token')
        windows = [w for w in self.windows_for_match_payload(boss, window, payload_get) if w and w.embed is not None]
        if len(windows) != 1 or not windows[0].embed or not windows[0].embed.token_matches(token):
            raise ValueError('No embedded window matches with that token')
        w = windows[0]
        state = w.embed
        assert state is not None
        size = (payload_get('size') or '').strip()
        cell_size = (payload_get('cell_size') or '').strip()
        if size or cell_size:
            cols, rows = parse_size(size, 'size') if size else (state.cols, state.rows)
            cw, ch = parse_size(cell_size, 'cell_size') if cell_size else (state.cell_width, state.cell_height)
            state.resize(w.id, cols, rows, cw, ch)
            w.set_geometry(w.geometry)
        ans = state.as_dict(w.id)
        ans['pid'] = w.child.pid or 0
        if payload_get('selection'):
            ans['selection'] = w.text_for_selection()
        return json.dumps(ans)


embed = Embed()

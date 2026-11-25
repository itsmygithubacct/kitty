#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kilix contributors

from typing import TYPE_CHECKING

from .base import MATCH_WINDOW_OPTION, ArgsType, Boss, PayloadGetType, PayloadType, RCOptions, RemoteCommand, ResponseType, Window

if TYPE_CHECKING:
    from kitty.cli_stub import EmbedInputRCOptions as CLIOptions

KINDS = ('text', 'bytes', 'paste', 'key', 'mouse', 'scroll', 'scroll-to', 'focus')


class EmbedInput(RemoteCommand):

    protocol_spec = __doc__ = '''
    match/str: The embedded window
    token/str: The token returned when the window was launched with --type=embed
    mods/int: Modifier bits for mouse and scroll: 1 shift, 2 alt, 4 ctrl, 8 super
    event/list.str: KIND followed by its arguments, as on the command line
    '''

    short_desc = 'Send input to an embedded window'
    desc = (
        'Send text, a paste, keys, a mouse or wheel event, or a focus change to a window launched with'
        ' :code:`launch --type=embed`, as if it came from the window on screen: mouse tracking programs get'
        ' their encoding, otherwise the engine selects text, opens links and scrolls back. The token printed'
        ' by the launch is required. The event is KIND then its arguments:\n\n'
        ':code:`text TEXT...` types the text; :code:`bytes BASE64` writes those bytes unchanged (keys the caller'
        ' already encoded); :code:`paste TEXT...` pastes it (bracketed when the program asked);'
        ' :code:`key KEY...` sends keys such as :code:`ctrl+c`;'
        ' :code:`mouse ACTION BUTTON X Y [PX PY [LEFT_HALF]]` with ACTION 0 press, 1 release, 2 move and BUTTON'
        ' 0 left, 1 right, 2 middle or -1 none; :code:`scroll LINES X Y [PX PY]` with LINES positive to scroll'
        ' back; :code:`scroll-to OFFSET` shows the history OFFSET lines up from the live rows (0 is live),'
        ' whatever the program asked for; :code:`focus in|out`. X and Y are cells, PX and PY pixels from the window\'s top left.'
    )
    options_spec = MATCH_WINDOW_OPTION + '''\n
--token
The token printed when the window was launched.


--mods
type=int
default=0
Modifier bits for mouse and scroll: 1 shift, 2 alt, 4 ctrl, 8 super.
'''
    args = RemoteCommand.Args(spec='KIND [ARGS ...]', json_field='event', minimum_count=1)

    def message_to_kitty(self, global_opts: RCOptions, opts: 'CLIOptions', args: ArgsType) -> PayloadType:
        if not opts.match:
            self.fatal('--match is required')
        if not opts.token:
            self.fatal('--token is required')
        return {'match': opts.match, 'token': opts.token, 'mods': opts.mods, 'event': args}

    def response_from_kitty(self, boss: Boss, window: Window | None, payload_get: PayloadGetType) -> ResponseType:
        from kitty.fast_data_types import embed_mouse_event, embed_scroll_event
        token = payload_get('token')
        windows = [w for w in self.windows_for_match_payload(boss, window, payload_get) if w and w.embed is not None]
        if len(windows) != 1 or not windows[0].embed or not windows[0].embed.token_matches(token):
            raise ValueError('No embedded window matches with that token')
        w = windows[0]
        event = payload_get('event') or []
        if not isinstance(event, list) or not all(isinstance(x, str) for x in event) or len(event) > 66:
            raise ValueError('event must be a short list of strings')
        mods = payload_get('mods') or 0
        if not isinstance(mods, int) or isinstance(mods, bool) or not 0 <= mods < 256:
            raise ValueError('mods must be an integer from 0 to 255')
        kind, f = parse_event(event)
        if kind == 'text':
            if f['text']:
                w.write_to_child(f['text'])
                w.scroll_end()
        elif kind == 'bytes':
            if f['data']:
                w.write_to_child(f['data'])
                w.scroll_end()
        elif kind == 'paste':
            if f['text']:
                w.paste_text(f['text'])
        elif kind == 'key':
            if f['keys']:
                w.send_key(*f['keys'])
        elif kind == 'mouse':
            embed_mouse_event(w.id, f['action'], f['button'], mods, f['x'], f['y'], f['px'], f['py'], f['left_half'])
        elif kind == 'scroll':
            embed_scroll_event(w.id, f['lines'], mods, f['x'], f['y'], f['px'], f['py'])
        elif kind == 'scroll-to':
            # Absolute, from where the view is when this arrives: a scrollbar
            # dragged faster than frames come back never drifts.
            screen = w.screen
            if screen.is_main_linebuf():
                delta = f['offset'] - screen.scrolled_by
                if delta:
                    screen.scroll(abs(delta), delta > 0)
        else:
            w.focus_changed(f['focused'])
        return None


def parse_event(args: list[str]) -> tuple[str, dict]:
    """KIND [ARGS ...] -> (kind, fields). Raises ValueError for anything else."""
    if not args or args[0] not in KINDS:
        raise ValueError(f'KIND must be one of {", ".join(KINDS)}')
    kind, rest = args[0], args[1:]

    def ints(values: list[str], low: int, high: int) -> list[int]:
        if not low <= len(values) <= high:
            raise ValueError(f'Wrong number of arguments for {kind}')
        try:
            return [int(v) for v in values]
        except ValueError:
            raise ValueError(f'The arguments for {kind} must be integers') from None

    if kind in ('text', 'paste'):
        return kind, {'text': ' '.join(rest)}
    if kind == 'bytes':
        import base64
        import binascii
        if len(rest) != 1 or len(rest[0]) > 87384:
            raise ValueError('bytes takes one base64 argument of at most 64 KiB')
        try:
            return kind, {'data': base64.b64decode(rest[0], validate=True)}
        except (binascii.Error, ValueError):
            raise ValueError('bytes needs valid base64') from None
    if kind == 'key':
        if len(rest) > 64:
            raise ValueError('At most 64 keys at a time')
        return kind, {'keys': rest}
    if kind == 'mouse':
        v = ints(rest, 4, 7)
        action, button, x, y = v[:4]
        if action not in (0, 1, 2) or not -1 <= button <= 7 or x < 0 or y < 0 or (action != 2 and button < 0):
            raise ValueError('mouse needs ACTION 0-2, BUTTON -1-7 (not -1 for a press or release) and X, Y >= 0')
        px, py = (v[4], v[5]) if len(v) >= 6 else (0, 0)
        left_half = bool(v[6]) if len(v) == 7 else True
        return kind, {'action': action, 'button': button, 'x': x, 'y': y, 'px': px, 'py': py, 'left_half': left_half}
    if kind == 'scroll':
        v = ints(rest, 3, 5)
        if v[1] < 0 or v[2] < 0:
            raise ValueError('scroll needs X, Y >= 0')
        px, py = (v[3], v[4]) if len(v) == 5 else (0, 0)
        return kind, {'lines': v[0], 'x': v[1], 'y': v[2], 'px': px, 'py': py}
    if kind == 'scroll-to':
        offset = ints(rest, 1, 1)[0]
        if not 0 <= offset <= 100_000_000:
            raise ValueError('scroll-to needs an OFFSET from 0')
        return kind, {'offset': offset}
    if rest not in (['in'], ['out']):
        raise ValueError('focus takes in or out')
    return kind, {'focused': rest == ['in']}


embed_input = EmbedInput()

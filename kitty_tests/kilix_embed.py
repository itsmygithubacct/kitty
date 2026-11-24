#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kilix contributors

"""Embedded windows: the parts that need no OS window. The cell map, input
and resizing through a running engine are tested end to end by
kilix-embed-pane (tests/engine/run.sh)."""

import os
import stat
import tempfile
from unittest.mock import patch

from kitty.embed import EmbedState, clamp_cell_pixels, clamp_cells, embed_dir, parse_size
from kitty.fast_data_types import EMBED_MAX_COLS, EMBED_MAX_ROWS
from kitty.rc.embed_input import KINDS, parse_event
from kitty.types import WindowGeometry

from . import BaseTest


class TestKilixEmbed(BaseTest):

    def test_sizes(self):
        self.ae(parse_size('80x24', 'size'), (80, 24))
        self.ae(parse_size(' 8x16 ', 'cell'), (8, 16))
        self.ae(parse_size('', 'cell', allow_empty=True), (0, 0))
        for bad in ('', '80', '80x', 'x24', '-1x2', '80x24x1', '123456x2', '8 x 16'):
            with self.assertRaises(ValueError, msg=bad):
                parse_size(bad, 'size')
        self.ae(clamp_cells(1, 0), (2, 1))
        self.ae(clamp_cells(10 ** 6, 10 ** 6), (EMBED_MAX_COLS, EMBED_MAX_ROWS))
        self.ae(clamp_cell_pixels(0, 16), (0, 0))
        self.ae(clamp_cell_pixels(10 ** 6, 10 ** 6), (512, 1024))

    def test_token(self):
        a, b = EmbedState(80, 24), EmbedState(80, 24)
        self.ae(len(a.token), 32)
        self.assertNotEqual(a.token, b.token)
        self.assertTrue(a.token_matches(a.token))
        for bad in (b.token, a.token.upper(), a.token[:-1], '', None, 12, [a.token], a.token + '\0'):
            self.assertFalse(a.token_matches(bad), bad)
        # The token is only in the launch answer, never in a description.
        self.assertNotIn('token', a.as_dict(3))
        self.ae(a.as_dict(3, include_token=True)['token'], a.token)

    def test_launch_options_and_geometry(self):
        s = EmbedState.from_launch_options('100x30', '9x18')
        self.ae((s.cols, s.rows, s.cell_width, s.cell_height), (100, 30, 9, 18))
        g = s.geometry(WindowGeometry(5, 7, 0, 0, 0, 0, None), (8, 16))
        self.ae((g.left, g.top, g.right, g.bottom, g.xnum, g.ynum), (5, 7, 905, 547, 100, 30))
        s = EmbedState.from_launch_options('10x4', '')
        g = s.geometry(WindowGeometry(0, 0, 0, 0, 0, 0, None), (8, 16))
        self.ae((g.right, g.bottom), (80, 64))
        with self.assertRaises(ValueError):
            EmbedState.from_launch_options('big', '')

    def test_map_folder_is_private(self):
        with tempfile.TemporaryDirectory() as d, patch('kitty.embed.runtime_dir', return_value=d):
            path = embed_dir()
            self.ae(stat.S_IMODE(os.stat(path).st_mode), 0o700)
            os.rmdir(path)
            os.symlink(d, path)
            with self.assertRaises(PermissionError):
                embed_dir()

    def test_input_events(self):
        self.ae(KINDS, ('text', 'bytes', 'paste', 'key', 'mouse', 'scroll', 'scroll-to', 'focus'))
        self.ae(parse_event(['text', 'a', 'b']), ('text', {'text': 'a b'}))
        self.ae(parse_event(['bytes', 'G1tB']), ('bytes', {'data': b'\x1b[A'}))
        self.ae(parse_event(['paste', 'x\ny']), ('paste', {'text': 'x\ny'}))
        self.ae(parse_event(['key', 'ctrl+c', 'enter']), ('key', {'keys': ['ctrl+c', 'enter']}))
        self.ae(parse_event(['mouse', '0', '0', '3', '4']),
                ('mouse', {'action': 0, 'button': 0, 'x': 3, 'y': 4, 'px': 0, 'py': 0, 'left_half': True}))
        self.ae(parse_event(['mouse', '2', '-1', '3', '4', '30', '70', '0'])[1]['left_half'], False)
        self.ae(parse_event(['scroll', '-3', '1', '2']),
                ('scroll', {'lines': -3, 'x': 1, 'y': 2, 'px': 0, 'py': 0}))
        self.ae(parse_event(['scroll-to', '0']), ('scroll-to', {'offset': 0}))
        self.ae(parse_event(['focus', 'out']), ('focus', {'focused': False}))
        for bad in (
            [], ['nope'], ['bytes'], ['bytes', 'a', 'b'], ['bytes', '!!!'], ['bytes', 'A' * 87388],
            ['key'] + ['a'] * 65, ['mouse', '0', '0', '1'], ['mouse', '3', '0', '1', '1'],
            ['mouse', '0', '-1', '1', '1'], ['mouse', '1', '-1', '1', '1'], ['mouse', '0', '8', '1', '1'],
            ['mouse', '0', '0', '-1', '1'], ['mouse', '0', 'x', '1', '1'], ['scroll', '1', '1'],
            ['scroll', '1', '-1', '1'], ['scroll-to'], ['scroll-to', '-1'], ['scroll-to', '1', '2'],
            ['scroll-to', '100000001'], ['focus'], ['focus', 'maybe'],
        ):
            with self.assertRaises(ValueError, msg=bad):
                parse_event(bad)

#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kilix contributors

"""Embedded windows (``launch --type=embed``).

An embedded window is an ordinary window whose OS window is a hidden host
that is never shown or rendered. Another program draws it: the engine writes
the window's cells into a file (the "KEPM" cell map, see embed-export.c) that
the program maps read-only, and the program sends input back with
``embed-input``. The size comes from that program, not from a layout.

Each embedded window has a random token. ``embed`` and ``embed-input`` refuse
a request without it, so a caller that may only use unauthenticated remote
control can drive the windows it created and no others.
"""

import hmac
import os
import re
import secrets
from typing import Any

from .constants import runtime_dir
from .fast_data_types import EMBED_MAX_COLS, EMBED_MAX_ROWS, embed_attach_map, embed_detach_map, embed_force_frame, embed_set_child_pid
from .types import WindowGeometry

EMBED_HOST_CLASS = 'kilix-embed-host'
SIZE = re.compile(r'(\d{1,5})x(\d{1,5})')


def parse_size(text: str, what: str, allow_empty: bool = False) -> tuple[int, int]:
    text = (text or '').strip()
    if not text and allow_empty:
        return 0, 0
    m = SIZE.fullmatch(text)
    if m is None:
        raise ValueError(f'{what} must be WIDTHxHEIGHT, not {text!r}')
    return int(m.group(1)), int(m.group(2))


def clamp_cells(cols: int, rows: int) -> tuple[int, int]:
    return max(2, min(int(cols), EMBED_MAX_COLS)), max(1, min(int(rows), EMBED_MAX_ROWS))


def clamp_cell_pixels(width: int, height: int) -> tuple[int, int]:
    if not width or not height:
        return 0, 0
    return max(1, min(int(width), 512)), max(1, min(int(height), 1024))


def embed_dir() -> str:
    path = os.path.join(runtime_dir(), 'embed')
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if not os.path.isdir(path) or os.path.islink(path) or st.st_uid != os.getuid():
        raise PermissionError(f'Refusing an embed directory that is not a private folder: {path}')
    os.chmod(path, 0o700)
    return path


class EmbedState:

    def __init__(self, cols: int, rows: int, cell_width: int = 0, cell_height: int = 0) -> None:
        self.token = secrets.token_hex(16)
        self.cols, self.rows = clamp_cells(cols, rows)
        self.cell_width, self.cell_height = clamp_cell_pixels(cell_width, cell_height)
        self.map_path = ''

    @classmethod
    def from_launch_options(cls, size: str, cell_size: str) -> 'EmbedState':
        cols, rows = parse_size(size, '--embed-size')
        cw, ch = parse_size(cell_size, '--embed-cell-size', allow_empty=True)
        return cls(cols, rows, cw, ch)

    def token_matches(self, token: Any) -> bool:
        return isinstance(token, str) and hmac.compare_digest(token.encode(), self.token.encode())

    def attach(self, window_id: int, child_pid: int = 0) -> None:
        path = os.path.join(embed_dir(), f'{os.getpid()}-{window_id}-{secrets.token_hex(6)}.kepm')
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            embed_attach_map(window_id, fd)
            if child_pid > 0:
                embed_set_child_pid(window_id, child_pid)
        except Exception:
            os.unlink(path)
            raise
        finally:
            os.close(fd)
        self.map_path = path

    def detach(self, window_id: int) -> None:
        embed_detach_map(window_id)
        if self.map_path:
            try:
                os.unlink(self.map_path)
            except FileNotFoundError:
                pass
            self.map_path = ''

    def geometry(self, g: WindowGeometry, engine_cell: tuple[int, int]) -> WindowGeometry:
        cw = self.cell_width or engine_cell[0]
        ch = self.cell_height or engine_cell[1]
        return WindowGeometry(g.left, g.top, g.left + self.cols * cw, g.top + self.rows * ch, self.cols, self.rows, g.spaces)

    def resize(self, window_id: int, cols: int, rows: int, cell_width: int | None = None, cell_height: int | None = None) -> None:
        self.cols, self.rows = clamp_cells(cols, rows)
        if cell_width is not None and cell_height is not None:
            self.cell_width, self.cell_height = clamp_cell_pixels(cell_width, cell_height)
        embed_force_frame(window_id)

    def as_dict(self, window_id: int, include_token: bool = False) -> dict[str, Any]:
        ans: dict[str, Any] = {
            'id': window_id, 'map': self.map_path, 'cols': self.cols, 'rows': self.rows,
            'cell_width': self.cell_width, 'cell_height': self.cell_height,
        }
        if include_token:
            ans['token'] = self.token
        return ans

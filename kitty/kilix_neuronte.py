#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kilix
"""Kilix chrome symbols drawn by Neuronte nets instead of a font file.

Neuronte is a small neural renderer: a weight file maps a character and a
point in its cell to coverage. This module hands the engine a short list of
characters (the tab status mark, the bell, the symbols on Kilix's bars and
title-bar buttons) and draws each of them from the first weight file that was
trained on it. fonts.c asks once per character; a character no net was trained
on is left to the fonts exactly as before, so a machine without Neuronte, or
with older weights, shows what it always showed.

Where Neuronte is found follows the convention Kilix 95 already uses:
``NEURONTE_HOME``, or ``$GPU_TERMINAL_SOURCE_HOME/neuronte`` (by default
``~/.local/gpu_terminal/sources/neuronte``), with the library at
``build/libneuronte.so`` (``NEURONTE_LIB`` overrides it) and weights in
``weights/``.

Two characters have a drawing of their own here, used only while no net has
been trained on them: the eight moon phases and the bell. They are plain
geometry written for Kilix (a ring with the dark part filled; a dome, a rim
and a clapper), not copies of any font's glyphs. ``source_of()`` says which
of the three (a net, the stand-in, the fonts) draws a character.

The Neuronte handle is not thread-safe. Everything here runs on the engine's
main thread, which is the only thread that renders cells.
"""

from __future__ import annotations

import ctypes
import math
import os
from collections.abc import Callable, Sequence

from .kilix_chrome.settings import chrome_value

ENABLED_KEY = 'KILIX_NEURONTE'            # auto (default) or off
WEIGHTS_KEY = 'KILIX_NEURONTE_WEIGHTS'    # weight file names, first match draws
# The chrome's own bundle first, then the terminal bundles that hold the rest.
WEIGHTS_DEFAULT = (
    'neuronte-kilix-chrome-v1.nrt neuronte-term-rest-v1.nrt neuronte-term-dingbat-v1.nrt neuronte-split-v9.nrt')

MOONS = tuple(range(0x1f311, 0x1f319))
BELL = 0x1f56d
STATUS_MARKS = MOONS + (0x2022, 0x25cf, BELL)
# The Nerd Font code points Kilix's own bars print (measured, not the font's
# whole set): pane title buttons, resource chips and the top-bar widgets.
NERD_SYMBOLS = (
    0xeab9, 0xf028, 0xf073, 0xf0e7, 0xf1eb, 0xf2c9, 0xf2db,
    *range(0xf0079, 0xf0084), 0xf0091, 0xf0156, 0xf0219, 0xf0293, 0xf030c, 0xf035b, 0xf036c, 0xf036d,
    0xf05cb, 0xf072e, 0xf0731, 0xf0734, 0xf0737, 0xf08ae, 0xf0fb2,
)
# The weather widget's symbols, for when it prints them as text.
WEATHER_SYMBOLS = (0x2600, 0x2601, 0x26c5, 0x26c8, 0x1f324, 0x1f326, 0x1f327, 0x1f328, 0x1f32b)
CHROME_CODEPOINTS = tuple(sorted(set(STATUS_MARKS + NERD_SYMBOLS + WEATHER_SYMBOLS)))

# A scalar no bundle is trained on. A weight file without a table of its
# trained characters answers "trained" for everything, this one included; such
# a file cannot say what it draws, so it is not used.
_NEVER_TRAINED = 0x10fffd
MIN_CELL, MAX_CELL = 4, 512


def neuronte_root(environ: dict[str, str] | os._Environ[str] = os.environ) -> str:
    root = environ.get('NEURONTE_HOME')
    if root:
        return os.path.abspath(os.path.expanduser(root))
    sources = environ.get('GPU_TERMINAL_SOURCE_HOME') or '~/.local/gpu_terminal/sources'
    return os.path.join(os.path.abspath(os.path.expanduser(sources)), 'neuronte')


# Stand-in drawings {{{

def _supersample(width: int, height: int, inside: Callable[[float, float], bool], samples: int = 4) -> bytes:
    """Coverage of a shape given as a test on pixel coordinates."""
    out = bytearray(width * height)
    step = 1.0 / samples
    offsets = tuple((i + 0.5) * step for i in range(samples))
    scale = 255.0 / (samples * samples)
    for y in range(height):
        for x in range(width):
            hits = 0
            for oy in offsets:
                for ox in offsets:
                    if inside(x + ox, y + oy):
                        hits += 1
            out[y * width + x] = int(hits * scale + 0.5)
    return bytes(out)


# Where the terminator crosses the disc's middle row, as a share of the
# radius, for the eight phases. A true crescent (0.71) is a sliver too thin
# to see in a ten-pixel cell, so the crescents and gibbous phases are drawn
# at 0.4: every phase is a different shape at terminal sizes.
_TERMINATOR = (1.0, 0.4, 0.0, -0.4, -1.0, -0.4, 0.0, 0.4)


def moon_is_lit(phase: int, dx: float, dy: float, radius: float) -> bool:
    """Is the point (dx, dy) from the disc's centre in the lit part?

    Phase 0 is the new moon and 4 the full moon; the moon waxes from the right
    as it is seen from the northern hemisphere. The terminator is half an
    ellipse through the poles.
    """
    phase %= 8
    half_chord = math.sqrt(max(0.0, radius * radius - dy * dy))
    edge = half_chord * _TERMINATOR[phase]
    if phase == 0:
        return False
    return dx > edge if phase <= 4 else dx < -edge


def draw_moon(phase: int, width: int, height: int) -> bytes:
    """A ring with the dark part filled: the full moon is an empty ring."""
    cx, cy = width / 2.0, height / 2.0
    radius = max(1.0, (min(width, height) - 1) / 2.0)
    ring = max(1.0, radius * 0.2)
    inner = radius - ring

    def inside(x: float, y: float) -> bool:
        dx, dy = x - cx, y - cy
        d2 = dx * dx + dy * dy
        if d2 > radius * radius:
            return False
        if d2 >= inner * inner:
            return True
        # The phase is drawn on the disc inside the ring.
        return not moon_is_lit(phase, dx, dy, inner)

    return _supersample(width, height, inside)


def draw_bell(width: int, height: int) -> bytes:
    """A bell: a knob, a dome that widens to a rim, and a clapper under it."""
    size = float(min(width, height))
    cx = width / 2.0
    top = (height - size) / 2.0  # the square the bell is drawn in, centred

    def inside(x: float, y: float) -> bool:
        u = (x - cx) / size          # -0.5 .. 0.5 across
        v = (y - top) / size         # 0 .. 1 down
        if v < 0.0 or v > 1.0:
            return False
        if v < 0.12:                 # knob
            return abs(u) <= 0.07
        if v < 0.40:                 # dome: a half ellipse
            t = (0.40 - v) / 0.28
            return abs(u) <= 0.27 * math.sqrt(max(0.0, 1.0 - t * t))
        if v < 0.66:                 # body, widening slowly
            return abs(u) <= 0.27 + 0.05 * (v - 0.40) / 0.26
        if v < 0.78:                 # the rim flares out
            return abs(u) <= min(0.5, 0.32 + 0.16 * (v - 0.66) / 0.12)
        if v < 0.84:                 # a gap between rim and clapper
            return False
        du, dv = u, v - 0.91         # clapper
        return du * du + dv * dv <= 0.085 * 0.085

    return _supersample(width, height, inside)


def stand_in(codepoint: int, width: int, height: int) -> bytes | None:
    if 0x1f311 <= codepoint <= 0x1f318:
        return draw_moon(codepoint - 0x1f311, width, height)
    if codepoint == BELL:
        return draw_bell(width, height)
    return None


def has_stand_in(codepoint: int) -> bool:
    return 0x1f311 <= codepoint <= 0x1f318 or codepoint == BELL

# }}}


class Nets:
    """The opened weight files, asked in order."""

    def __init__(self, library_path: str, weight_paths: Sequence[str]) -> None:
        self.lib: ctypes.CDLL | None = None
        self.handles: list[tuple[str, ctypes.c_void_p]] = []
        self.problems: list[str] = []
        if not library_path or not os.path.isfile(library_path):
            self.problems.append(f'no Neuronte library at {library_path or "(unset)"}')
            return
        try:
            lib = ctypes.CDLL(library_path)
            lib.neuronte_open_file.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
            lib.neuronte_open_file.restype = ctypes.c_int
            lib.neuronte_untrained.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            lib.neuronte_untrained.restype = ctypes.c_int
            lib.neuronte_coverage.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.POINTER(ctypes.c_float)]
            lib.neuronte_coverage.restype = ctypes.c_int
            lib.neuronte_close.argtypes = [ctypes.c_void_p]
            lib.neuronte_close.restype = None
        except (OSError, AttributeError) as error:
            self.problems.append(f'cannot load {library_path}: {error}')
            return
        self.lib = lib
        for path in weight_paths:
            if not os.path.isfile(path):
                continue
            handle = ctypes.c_void_p()
            code = lib.neuronte_open_file(os.fsencode(path), ctypes.byref(handle))
            if code != 0 or not handle:
                self.problems.append(f'cannot open {path}: error {code}')
                continue
            if lib.neuronte_untrained(handle, _NEVER_TRAINED) != 1:
                self.problems.append(f'{path} has no table of its trained characters; not used')
                lib.neuronte_close(handle)
                continue
            self.handles.append((os.path.basename(path), handle))

    def net_for(self, codepoint: int) -> tuple[str, ctypes.c_void_p] | None:
        if self.lib is None:
            return None
        for name, handle in self.handles:
            if self.lib.neuronte_untrained(handle, codepoint) == 0:
                return name, handle
        return None

    def coverage(self, codepoint: int, width: int, height: int) -> bytes | None:
        found = self.net_for(codepoint)
        if found is None or self.lib is None:
            return None
        buf = (ctypes.c_float * (width * height))()
        if self.lib.neuronte_coverage(found[1], codepoint, width, height, 0, buf) != 0:
            return None
        return bytes(min(255, max(0, int(value * 255.0 + 0.5))) for value in buf)


class GlyphSource:
    """What the engine calls: can this character be drawn, and its mask."""

    def __init__(self, nets: Nets | None, use_stand_ins: bool = True) -> None:
        self.nets = nets
        self.use_stand_ins = use_stand_ins

    def source_of(self, codepoint: int) -> str:
        """The weight file's name, 'stand-in', or 'font'."""
        if self.nets is not None and (found := self.nets.net_for(codepoint)) is not None:
            return found[0]
        if self.use_stand_ins and has_stand_in(codepoint):
            return 'stand-in'
        return 'font'

    def can_draw(self, codepoint: int) -> bool:
        return self.source_of(codepoint) != 'font'

    def draw(self, codepoint: int, width: int, height: int) -> bytes | None:
        if not (MIN_CELL <= width <= MAX_CELL and MIN_CELL <= height <= MAX_CELL):
            return None
        if self.nets is not None and (mask := self.nets.coverage(codepoint, width, height)) is not None:
            return mask
        return stand_in(codepoint, width, height) if self.use_stand_ins else None


def weight_paths(root: str, names: str) -> list[str]:
    ans = []
    for name in names.replace(',', ' ').split():
        # A name, never a path: the setting cannot point outside the weights.
        if name == os.path.basename(name) and name.endswith('.nrt'):
            ans.append(os.path.join(root, 'weights', name))
    return ans


_source: GlyphSource | None = None


def build_source(environ: dict[str, str] | os._Environ[str] = os.environ) -> GlyphSource | None:
    if chrome_value(ENABLED_KEY, 'auto').strip().lower() in ('0', 'no', 'false', 'off', 'disabled'):
        return None
    root = neuronte_root(environ)
    library = environ.get('NEURONTE_LIB') or os.path.join(root, 'build', 'libneuronte.so')
    nets = Nets(library, weight_paths(root, chrome_value(WEIGHTS_KEY, WEIGHTS_DEFAULT) or WEIGHTS_DEFAULT))
    return GlyphSource(nets if nets.handles else None)


def source() -> GlyphSource | None:
    return _source


def source_of(codepoint: int) -> str:
    return 'font' if _source is None else _source.source_of(codepoint)


def text_form(symbol: str, group: Sequence[int]) -> str:
    """``symbol`` as one-colour text if nets draw every character of ``group``.

    A widget's symbols change together or not at all: a bar that mixed colour
    emoji with one-colour drawings would look broken. An emoji selector is
    replaced by the text selector, which also makes the symbol one cell wide.
    """
    if _source is None or not symbol or ord(symbol[0]) not in group:
        return symbol
    if any(_source.source_of(cp) in ('font', 'stand-in') for cp in group):
        return symbol
    return symbol[0] + '\ufe0e'


def install() -> None:
    """Give the engine the list and the two callables. Called when fonts are set up."""
    global _source
    from .fast_data_types import set_kilix_glyph_source
    try:
        _source = build_source()
    except Exception as error:  # a broken install must not stop the terminal
        from .utils import log_error
        log_error(f'Neuronte chrome symbols are off: {error}')
        _source = None
    if _source is None:
        set_kilix_glyph_source((), None, None)
    else:
        set_kilix_glyph_source(CHROME_CODEPOINTS, _source.can_draw, _source.draw)

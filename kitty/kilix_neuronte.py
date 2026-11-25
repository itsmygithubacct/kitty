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

Most of the bar symbols are drawn two cells wide, as the symbol font draws
them: across the symbol's cell and the blank cell Kilix prints after it. Which
symbols is a setting (``KILIX_NEURONTE_TWO_CELL``). A weight file named in
``KILIX_NEURONTE_TWO_CELL_WEIGHTS`` holds drawings made for that two-cell box;
it is asked for twice the height and each pair of rows is averaged, because
the library scales a drawing by the width of the box alone. A symbol with no
such drawing is drawn two cells wide from its one-cell drawing made larger,
and where no blank cell follows a symbol its one-cell drawing is used. A
symbol outside the private-use areas (the weather symbols) is drawn two cells
wide on Kilix's own bars only: in a window's text it stays in its cell.

The weather widget's colour symbols are drawn by Neuronte's colour nets in
place of the emoji font, on the bars only and only when the colour weight
files (``KILIX_NEURONTE_COLOUR_WEIGHTS``) were trained on all nine of them:
a widget whose look changed with the weather would look broken. A colour net
draws a symbol as two halves, one cell each, of up to six layers; the layers
are put together here with the file's dark palette.

A weight file may have a cells file: the masks Neuronte's setup drew ahead
for the characters and sizes it expects to be asked for, kept in one file
that every process maps read-only (``neuronte_cells.h``). Its floats are the
ones the net would compute, so a mask is taken from there when the file holds
it and from the net when it does not, when there is no file, or when the
library is one without the cells calls. The files are looked for in
``/var/lib/neuronte/cells`` (``NEURONTE_CELLS_DIR`` overrides it) under the
weight file's name with ``.cells`` for ``.nrt``; ``KILIX_NEURONTE_CELLS=off``
leaves them alone. Colour nets have no cells files.

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
# The chrome's own bundles first (two-cell drawings, then the one-cell ones
# they fall back to), then the terminal bundles that hold the rest.
WEIGHTS_DEFAULT = (
    'neuronte-kilix-chrome-v2.nrt neuronte-kilix-chrome-v1.nrt'
    ' neuronte-term-rest-v1.nrt neuronte-term-dingbat-v1.nrt neuronte-split-v9.nrt')
TWO_CELL_KEY = 'KILIX_NEURONTE_TWO_CELL'  # auto (default), off, or code points such as "U+F0219 U+F073"
TWO_CELL_WEIGHTS_KEY = 'KILIX_NEURONTE_TWO_CELL_WEIGHTS'  # the files whose drawings are made for two cells
TWO_CELL_WEIGHTS_DEFAULT = 'neuronte-kilix-chrome-v2.nrt'
COLOUR_WEIGHTS_KEY = 'KILIX_NEURONTE_COLOUR_WEIGHTS'   # colour weight files, first one trained on a symbol draws it
COLOUR_WEIGHTS_DEFAULT = 'neuronte-weather-colour-v1.nrt neuronte-emoji-v1.nrt'
CELLS_KEY = 'KILIX_NEURONTE_CELLS'      # auto (default): masks from a weight file's cells file when it has one; off
CELLS_DIR_ENV = 'NEURONTE_CELLS_DIR'    # where the cells files are, when not in CELLS_DIR_DEFAULT
CELLS_DIR_DEFAULT = '/var/lib/neuronte/cells'
# What the cells calls are told of how this module asks a net: the library's
# own choice of samples, the regular style, the weight file's own snap
# threshold (neuronte_set_snap() is never called here).
CELLS_SAMPLES, CELLS_STYLE, CELLS_SNAP_FILE = 0, 0, -1
# Who draws the weather widget's symbol. colour (default): the colour nets,
# when they have all nine, else the emoji font. on: one colour, by the nets.
# off: the emoji font.
WEATHER_KEY = 'KILIX_NEURONTE_WEATHER'
_OFF = ('0', 'no', 'false', 'off', 'disabled', 'none')
_ON = ('1', 'yes', 'true', 'on')
WEATHER_COLOUR, WEATHER_ONE_COLOUR, WEATHER_FONT = 'colour', 'one-colour', 'font'

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
# The symbols drawn across two cells where a blank cell follows: every pane
# button and bar widget symbol the nets were asked for, and the weather. Not
# the moons, the dot or the bell, which sit in one cell of a tab title. A
# two-cell weight file holds its drawings of these, and only these, for the
# two-cell box.
TWO_CELL_SYMBOLS = frozenset((
    0xeab9, 0xf028, 0xf073, 0xf0e7, 0xf1eb, 0xf2c9,
    *range(0xf0079, 0xf0084), 0xf0091, 0xf0156, 0xf0219, 0xf0293, 0xf030c, 0xf035b, 0xf036c, 0xf036d,
    0xf05cb, 0xf072e, 0xf0731, 0xf0734, 0xf0737, 0xf08ae,
    *WEATHER_SYMBOLS,
))
# Asked of a two-cell file: the box at twice its height, rows averaged in pairs.
ROWS_PER_PIXEL = 2
# The share of a two-cell box, each way, that a one-cell drawing made larger
# may cover: what the symbol font's ink covers (15 of 18 pixels).
ENLARGED_FILL = 5 / 6
_INK = 48   # coverage, of 255, that counts as ink when a drawing is measured
# Colour nets: the dark palette; entry 0 of a palette is the caller's colour
# and entry 1 clears what is under it.
PALETTE_THEME, PALETTE_ENTRIES, ENTRY_OWN, ENTRY_CLEAR = 0, 32, 0, 1
OWN_COLOUR = (0xe6, 0xe6, 0xe6)   # what entry 0 is painted with when the text colour is not known

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


def two_cell_symbols(setting: str) -> frozenset[int]:
    """The characters to draw two cells wide, from the setting's value."""
    words = setting.replace(',', ' ').split()
    if not words or words == ['auto']:
        return TWO_CELL_SYMBOLS
    ans = set()
    for word in words:
        if word.lower() in _OFF:
            continue
        digits = word[2:] if word[:2].lower() in ('u+', '0x') else word
        try:
            codepoint = int(digits, 16)
        except ValueError:
            continue
        if codepoint in CHROME_CODEPOINTS:   # the engine asks about no other character
            ans.add(codepoint)
    return frozenset(ans)


def pair_rows(mask: bytes, width: int, height: int, rows: int = ROWS_PER_PIXEL) -> bytes:
    """``mask`` is ``width`` by ``height * rows``; average each ``rows`` rows into one."""
    out = bytearray(width * height)
    for y in range(height):
        base = y * rows * width
        for x in range(width):
            total = 0
            for r in range(rows):
                total += mask[base + r * width + x]
            out[y * width + x] = (total + rows // 2) // rows
    return bytes(out)


def ink_box(mask: bytes, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Left, top, right and bottom (exclusive) of the pixels that are ink."""
    rows = [y for y in range(height) if max(mask[y * width:(y + 1) * width]) >= _INK]
    if not rows:
        return None
    columns = [x for x in range(width) if max(mask[x::width]) >= _INK]
    return columns[0], rows[0], columns[-1] + 1, rows[-1] + 1


def centred(mask: bytes, width: int, box: tuple[int, int, int, int], out_width: int, out_height: int) -> bytes:
    """The ``box`` of ``mask`` in the middle of an empty ``out_width`` by ``out_height`` mask."""
    left, top, right, bottom = box
    out = bytearray(out_width * out_height)
    x0 = (out_width - (right - left)) // 2
    y0 = (out_height - (bottom - top)) // 2
    for y in range(top, bottom):
        oy = y0 + y - top
        if not 0 <= oy < out_height:
            continue
        for x in range(left, right):
            ox = x0 + x - left
            if 0 <= ox < out_width:
                out[oy * out_width + ox] = mask[y * width + x]
    return bytes(out)


def is_private_use(codepoint: int) -> bool:
    return 0xe000 <= codepoint <= 0xf8ff or 0xf0000 <= codepoint <= 0xffffd or 0x100000 <= codepoint <= 0x10fffd


def weather_mode(setting: str) -> str:
    word = setting.strip().lower()
    if word in _ON or word in ('one-colour', 'one-color'):
        return WEATHER_ONE_COLOUR
    if word in _OFF or word in ('emoji', 'font'):
        return WEATHER_FONT
    return WEATHER_COLOUR


def composite(
    layers: Sequence[tuple[Sequence[float], int]], palette: Sequence[tuple[int, int, int]],
    count: int, own: tuple[int, int, int] = OWN_COLOUR,
) -> bytes:
    """Put a colour glyph's layers together, back to front.

    Each layer is a coverage mask of ``count`` pixels and a palette entry.
    Entry 1 clears what is under it, entry 0 (and any entry that is not a
    colour) paints in ``own``, any other paints in the palette's colour. The
    answer is four bytes a pixel: red, green, blue, and an alpha the colour is
    not multiplied by.
    """
    out = bytearray(4 * count)
    for i in range(count):
        red = green = blue = alpha = 0.0
        for mask, entry in layers:
            cover = min(1.0, max(0.0, mask[i]))
            if cover <= 0.0:
                continue
            keep = 1.0 - cover
            red, green, blue, alpha = red * keep, green * keep, blue * keep, alpha * keep
            if entry == ENTRY_CLEAR:
                continue
            r, g, b = own if entry == ENTRY_OWN or not 0 <= entry < len(palette) else palette[entry]
            red, green, blue, alpha = red + r * cover, green + g * cover, blue + b * cover, alpha + cover
        if alpha > 0.0:
            out[4 * i:4 * i + 4] = (
                min(255, int(red / alpha + 0.5)), min(255, int(green / alpha + 0.5)),
                min(255, int(blue / alpha + 0.5)), min(255, int(alpha * 255.0 + 0.5)))
    return bytes(out)


def side_by_side(left: bytes, right: bytes, half_width: int, height: int, unit: int = 4) -> bytes:
    """Two halves of ``half_width`` by ``height`` pixels, ``unit`` bytes each, as one picture."""
    row = half_width * unit
    return b''.join(left[y * row:(y + 1) * row] + right[y * row:(y + 1) * row] for y in range(height))


def cells_directory(environ: dict[str, str] | os._Environ[str], setting: str = 'auto') -> str:
    """Where the cells files are looked for; '' when the setting says to leave them alone."""
    if setting.strip().lower() in _OFF:
        return ''
    return environ.get(CELLS_DIR_ENV) or CELLS_DIR_DEFAULT


def cells_path(directory: str, weights_path: str) -> str:
    """The cells file of a weight file: its name with .cells for .nrt, in the cells directory."""
    name = os.path.basename(weights_path)
    return os.path.join(directory, (name[:-4] if name.endswith('.nrt') else name) + '.cells')


class Nets:
    """The opened weight files, asked in order."""

    def __init__(
        self, library_path: str, weight_paths: Sequence[str], colour_paths: Sequence[str] = (), cells_dir: str = '',
    ) -> None:
        self.lib: ctypes.CDLL | None = None
        self.handles: list[tuple[str, ctypes.c_void_p]] = []
        # Cells files: a weight file's handle -> its opened cells file; and how often one answered.
        self.cells: dict[int, ctypes.c_void_p] = {}
        self.cells_found = self.cells_asked = 0
        # Colour files: name, handle, the most layers a glyph has, the palette.
        self.colour_handles: list[tuple[str, ctypes.c_void_p, int, list[tuple[int, int, int]]]] = []
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
        if colour_paths:
            self.open_colour_files(colour_paths)
        if cells_dir:
            self.open_cells_files(weight_paths, cells_dir)

    def open_cells_files(self, weight_paths: Sequence[str], directory: str) -> None:
        """Open the cells file of every opened weight file that has one."""
        lib = self.lib
        assert lib is not None
        try:   # an older library has no cells calls: then every mask comes from a net
            lib.neuronte_cells_open.argtypes = [
                ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
            lib.neuronte_cells_open.restype = ctypes.c_int
            lib.neuronte_cells_find.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_int]
            lib.neuronte_cells_find.restype = ctypes.POINTER(ctypes.c_float)
        except AttributeError:
            return
        opened = dict(self.handles)
        for path in weight_paths:
            handle = opened.get(os.path.basename(path))
            kept = cells_path(directory, path)
            if handle is None or not os.path.isfile(kept):
                continue
            cells = ctypes.c_void_p()
            # The library refuses a cells file made from other weights, and a damaged one.
            code = lib.neuronte_cells_open(os.fsencode(kept), os.fsencode(path), 0, ctypes.byref(cells))
            if code != 0 or not cells:
                self.problems.append(f'cells file {kept} is not used: error {code}')
                continue
            self.cells[handle.value or 0] = cells

    def from_cells(self, handle: ctypes.c_void_p, codepoint: int, width: int, height: int) -> bytes | None:
        """The mask as the weight file's cells file holds it; None when it does not, and the net draws it."""
        cells = self.cells.get(handle.value or 0)
        if cells is None or self.lib is None:
            return None
        self.cells_asked += 1
        kept = self.lib.neuronte_cells_find(cells, codepoint, width, height, CELLS_SAMPLES, CELLS_STYLE, CELLS_SNAP_FILE)
        if not kept:
            return None
        self.cells_found += 1
        return bytes(min(255, max(0, int(value * 255.0 + 0.5))) for value in kept[:width * height])

    def open_colour_files(self, paths: Sequence[str]) -> None:
        lib = self.lib
        assert lib is not None
        try:   # an older library has no colour calls: then there are no colour files
            lib.neuronte_layers.argtypes = [ctypes.c_void_p]
            lib.neuronte_layers.restype = ctypes.c_int
            lib.neuronte_palette.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_uint8)]
            lib.neuronte_palette.restype = ctypes.c_int
            lib.neuronte_coverage_layers_half.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_uint8)]
            lib.neuronte_coverage_layers_half.restype = ctypes.c_int
        except AttributeError as error:
            self.problems.append(f'the Neuronte library has no colour calls: {error}')
            return
        for path in paths:
            if not os.path.isfile(path):
                continue
            handle = ctypes.c_void_p()
            code = lib.neuronte_open_file(os.fsencode(path), ctypes.byref(handle))
            if code != 0 or not handle:
                self.problems.append(f'cannot open {path}: error {code}')
                continue
            layers = lib.neuronte_layers(handle)
            rgb = (ctypes.c_uint8 * (3 * PALETTE_ENTRIES))()
            has_table = lib.neuronte_untrained(handle, _NEVER_TRAINED) == 1
            has_palette = lib.neuronte_palette(handle, PALETTE_THEME, rgb) == 0 and any(rgb)
            if not (has_table and has_palette and 1 <= layers <= 64):
                self.problems.append(f'{path} is not a colour file with a table of its trained characters; not used')
                lib.neuronte_close(handle)
                continue
            palette = [(rgb[3 * i], rgb[3 * i + 1], rgb[3 * i + 2]) for i in range(PALETTE_ENTRIES)]
            self.colour_handles.append((os.path.basename(path), handle, layers, palette))

    def colour_trained(self, codepoint: int) -> list[str]:
        """The colour files trained on the character, in the order they are asked."""
        if self.lib is None:
            return []
        return [name for name, handle, _, _ in self.colour_handles if self.lib.neuronte_untrained(handle, codepoint) == 0]

    def colour_half(self, name: str, codepoint: int, half: int, width: int, height: int) -> bytes | None:
        """One half (0 left, 1 right) of a colour glyph, ``width`` by ``height``, four bytes a pixel."""
        if self.lib is None or not (MIN_CELL <= width <= MAX_CELL and MIN_CELL <= height <= MAX_CELL):
            return None
        for file_name, handle, layers, palette in self.colour_handles:
            if file_name == name:
                break
        else:
            return None
        count = width * height
        masks = (ctypes.c_float * (layers * count))()
        entries = (ctypes.c_uint8 * layers)()
        if self.lib.neuronte_coverage_layers_half(handle, codepoint, half, width, height, 0, masks, entries) != 0:
            return None
        return composite([(masks[k * count:(k + 1) * count], entries[k]) for k in range(layers)], palette, count)

    def trained(self, codepoint: int) -> list[tuple[str, ctypes.c_void_p]]:
        """The files trained on the character, in the order they are asked."""
        if self.lib is None:
            return []
        return [(name, handle) for name, handle in self.handles
                if self.lib.neuronte_untrained(handle, codepoint) == 0]

    def net_for(self, codepoint: int) -> tuple[str, ctypes.c_void_p] | None:
        found = self.trained(codepoint)
        return found[0] if found else None

    def coverage_from(self, handle: ctypes.c_void_p, codepoint: int, width: int, height: int) -> bytes | None:
        if self.lib is None or not (MIN_CELL <= width <= MAX_CELL and MIN_CELL <= height <= MAX_CELL):
            return None
        if (kept := self.from_cells(handle, codepoint, width, height)) is not None:
            return kept
        buf = (ctypes.c_float * (width * height))()
        if self.lib.neuronte_coverage(handle, codepoint, width, height, 0, buf) != 0:
            return None
        return bytes(min(255, max(0, int(value * 255.0 + 0.5))) for value in buf)

    def coverage(self, codepoint: int, width: int, height: int) -> bytes | None:
        found = self.net_for(codepoint)
        return None if found is None else self.coverage_from(found[1], codepoint, width, height)


class GlyphSource:
    """What the engine calls: can this character be drawn, and its mask."""

    def __init__(
        self, nets: Nets | None, use_stand_ins: bool = True,
        two_cell: frozenset[int] = frozenset(), two_cell_files: frozenset[str] = frozenset(),
        weather: str = WEATHER_FONT,
    ) -> None:
        self.nets = nets
        self.use_stand_ins = use_stand_ins
        self.two_cell = two_cell              # characters to draw two cells wide
        self.two_cell_files = two_cell_files  # weight files whose drawings of TWO_CELL_SYMBOLS are two cells wide
        self.weather = weather                # who draws the weather widget's colour symbols
        self._weather_sources: dict[int, str] | None = None

    def made_for_two_cells(self, name: str, codepoint: int) -> bool:
        return name in self.two_cell_files and codepoint in TWO_CELL_SYMBOLS

    def one_cell_net(self, codepoint: int) -> tuple[str, ctypes.c_void_p] | None:
        """The first file whose drawing of the character is made for one cell."""
        if self.nets is not None:
            for name, handle in self.nets.trained(codepoint):
                if not self.made_for_two_cells(name, codepoint):
                    return name, handle
        return None

    def two_cell_net(self, codepoint: int) -> tuple[str, ctypes.c_void_p, bool] | None:
        """The file that draws the character two cells wide, and whether its drawing is made for that."""
        if self.nets is None or codepoint not in self.two_cell:
            return None
        for name, handle in self.nets.trained(codepoint):
            made = self.made_for_two_cells(name, codepoint)
            if made or self.use_stand_ins:
                return name, handle, made
        return None

    def source_of(self, codepoint: int) -> str:
        """Who draws the character in one cell: the weight file's name, 'stand-in', or 'font'."""
        if (found := self.one_cell_net(codepoint)) is not None:
            return found[0]
        if self.use_stand_ins and has_stand_in(codepoint):
            return 'stand-in'
        return 'font'

    def two_cell_source_of(self, codepoint: int) -> str:
        """Who draws it across two cells: a file's name, 'NAME, enlarged', or '' for nobody."""
        found = self.two_cell_net(codepoint)
        if found is None:
            return ''
        return found[0] if found[2] else f'{found[0]}, enlarged'

    def weather_sources(self) -> dict[int, str]:
        """Who draws each weather symbol in colour; empty unless all nine are drawn.

        A colour file's name: the first colour file trained on the symbol. The
        nine change together or not at all, as the widget's one-colour symbols
        do.
        """
        if self._weather_sources is None:
            found: dict[int, str] = {}
            if self.nets is not None and self.weather == WEATHER_COLOUR:
                for codepoint in WEATHER_SYMBOLS:
                    if (names := self.nets.colour_trained(codepoint)):
                        found[codepoint] = names[0]
            self._weather_sources = found if len(found) == len(WEATHER_SYMBOLS) else {}
        return self._weather_sources

    def colour_source_of(self, codepoint: int) -> str:
        """Who draws the character in colour on a bar: a colour file's name, or '' for the emoji font."""
        return self.weather_sources().get(codepoint, '')

    def can_draw(self, codepoint: int) -> int:
        """What the engine is told about a character, as bits.

        1: drawn in one cell. 2: drawn across two cells where a blank cell
        follows. 4: that two-cell drawing is for Kilix's own bars only (every
        character outside the private-use areas). 8: drawn in colour in place
        of a wide colour emoji, on the bars.
        """
        bits = 1 if self.source_of(codepoint) != 'font' else 0
        if self.two_cell_net(codepoint) is not None:
            bits |= 2 if is_private_use(codepoint) else 2 | 4
        if self.colour_source_of(codepoint):
            bits |= 8
        return bits

    def draw_colour(self, codepoint: int, width: int, height: int) -> bytes | None:
        """The colour drawing for a box ``width`` (two cells) by ``height``: four bytes a pixel, or None."""
        name = self.colour_source_of(codepoint)
        half = width // 2
        if not name or self.nets is None or width != 2 * half or not (MIN_CELL <= half and MIN_CELL <= height <= MAX_CELL):
            return None
        left = self.nets.colour_half(name, codepoint, 0, half, height)
        right = self.nets.colour_half(name, codepoint, 1, half, height)
        return None if left is None or right is None else side_by_side(left, right, half, height)

    def draw(self, codepoint: int, width: int, height: int, cells: int = 1) -> bytes | None:
        if not (MIN_CELL <= width <= MAX_CELL and MIN_CELL <= height <= MAX_CELL):
            return None
        if cells == 2:
            return self.draw_two_cells(codepoint, width, height)
        if self.nets is not None and (found := self.one_cell_net(codepoint)) is not None:
            if (mask := self.nets.coverage_from(found[1], codepoint, width, height)) is not None:
                return mask
        return stand_in(codepoint, width, height) if self.use_stand_ins else None

    def draw_two_cells(self, codepoint: int, width: int, height: int) -> bytes | None:
        found = self.two_cell_net(codepoint)
        if found is None or self.nets is None or height * ROWS_PER_PIXEL > MAX_CELL:
            return None
        _, handle, made = found
        if made:
            tall = self.nets.coverage_from(handle, codepoint, width, height * ROWS_PER_PIXEL)
            return None if tall is None else pair_rows(tall, width, height)
        return self.enlarged(handle, codepoint, width, height)

    def enlarged(self, handle: ctypes.c_void_p, codepoint: int, width: int, height: int) -> bytes | None:
        """A one-cell drawing made larger and centred in a two-cell box.

        This stands in for a drawing made for two cells. The net is asked for
        a cell twice the size, the ink is measured, and the net is asked again
        at the size where the ink covers ENLARGED_FILL of the box.
        """
        assert self.nets is not None
        cell_w, cell_h = width, 2 * height    # the one-cell drawing at twice the cell
        mask = self.nets.coverage_from(handle, codepoint, cell_w, cell_h)
        if mask is None or (box := ink_box(mask, cell_w, cell_h)) is None:
            return None
        factor = min(ENLARGED_FILL * width / (box[2] - box[0]), ENLARGED_FILL * height / (box[3] - box[1]), 1.5)
        fitted_w, fitted_h = max(MIN_CELL, round(cell_w * factor)), max(MIN_CELL, round(cell_h * factor))
        if (fitted_w, fitted_h) != (cell_w, cell_h):
            fitted = self.nets.coverage_from(handle, codepoint, fitted_w, fitted_h)
            if fitted is not None and (fitted_box := ink_box(fitted, fitted_w, fitted_h)) is not None:
                mask, box, cell_w = fitted, fitted_box, fitted_w
        return centred(mask, cell_w, box, width, height)


def weight_paths(root: str, names: str) -> list[str]:
    ans = []
    for name in names.replace(',', ' ').split():
        # A name, never a path: the setting cannot point outside the weights.
        if name == os.path.basename(name) and name.endswith('.nrt'):
            ans.append(os.path.join(root, 'weights', name))
    return ans


_source: GlyphSource | None = None


def build_source(environ: dict[str, str] | os._Environ[str] = os.environ) -> GlyphSource | None:
    if chrome_value(ENABLED_KEY, 'auto').strip().lower() in _OFF:
        return None
    root = neuronte_root(environ)
    library = environ.get('NEURONTE_LIB') or os.path.join(root, 'build', 'libneuronte.so')
    weather = weather_mode(chrome_value(WEATHER_KEY, WEATHER_COLOUR))
    colour_files = chrome_value(COLOUR_WEIGHTS_KEY, COLOUR_WEIGHTS_DEFAULT) or COLOUR_WEIGHTS_DEFAULT
    nets = Nets(
        library, weight_paths(root, chrome_value(WEIGHTS_KEY, WEIGHTS_DEFAULT) or WEIGHTS_DEFAULT),
        weight_paths(root, colour_files) if weather == WEATHER_COLOUR else (),
        cells_directory(environ, chrome_value(CELLS_KEY, 'auto')))
    two_cell_files = chrome_value(TWO_CELL_WEIGHTS_KEY, TWO_CELL_WEIGHTS_DEFAULT) or TWO_CELL_WEIGHTS_DEFAULT
    return GlyphSource(
        nets if nets.handles or nets.colour_handles else None,
        two_cell=two_cell_symbols(chrome_value(TWO_CELL_KEY, 'auto')),
        two_cell_files=frozenset(os.path.basename(path) for path in weight_paths(root, two_cell_files)),
        weather=weather)


def source() -> GlyphSource | None:
    return _source


def source_of(codepoint: int) -> str:
    return 'font' if _source is None else _source.source_of(codepoint)


def text_form(symbol: str, group: Sequence[int]) -> str:
    """``symbol`` as one-colour text if nets draw every character of ``group``.

    A widget's symbols change together or not at all: a bar that mixed colour
    emoji with one-colour drawings would look broken. An emoji selector is
    replaced by the text selector, which also makes the symbol one cell wide;
    a symbol drawn two cells wide gets the blank cell it is drawn into, so
    the text is as wide as it was with the emoji.
    """
    if _source is None or not symbol or ord(symbol[0]) not in group:
        return symbol
    if any(_source.source_of(cp) in ('font', 'stand-in') and not _source.two_cell_source_of(cp) for cp in group):
        return symbol
    return symbol[0] + '\ufe0e' + (' ' if _source.two_cell_source_of(ord(symbol[0])) else '')


def weather_symbol(symbol: str) -> str:
    """The weather widget's symbol: the colour emoji unless one colour is asked for.

    The colour emoji is drawn by the colour nets where they have all nine
    symbols and by the emoji font otherwise; the text is the same either way.
    """
    if weather_mode(chrome_value(WEATHER_KEY, WEATHER_COLOUR)) != WEATHER_ONE_COLOUR:
        return symbol
    return text_form(symbol, WEATHER_SYMBOLS)


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
        set_kilix_glyph_source(CHROME_CODEPOINTS, _source.can_draw, _source.draw, _source.draw_colour)

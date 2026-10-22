#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

from kitty.rgb import to_color

from . import BaseTest


class TestOKLCHGamutMapping(BaseTest):

    def test_css_color_4_reference_vectors(self):
        # RGB bytes were calculated independently from the CSS Color 4
        # conversion and local-MINDE equations, then rounded to nearest byte.
        cases = (
            ('oklch(0.5 0.05 180)', (66, 109, 100)),
            ('oklch(0.5 0.1 180)', (0, 117, 101)),
            ('oklch(50% 0.05 180)', (66, 109, 100)),
            ('oklch(0.7 0.4 25)', (255, 86, 85)),
            # This input reaches loop termination; it does not take the JND-margin return.
            ('oklch(0.06548 0.1 85)', (3, 0, 0)),
            ('oklch(0.5 0 180)', (99, 99, 99)),
            ('oklch(0.25 0 90)', (34, 34, 34)),
            ('lab(70 50 -30)', (238, 134, 227)),
            ('lab(50 100 100)', (245, 0, 13)),
            ('lab(50 0 0)', (119, 119, 119)),
            # This neutral checks the chromaticity-derived D50 white and full-precision matrix.
            ('lab(53 0 0)', (127, 127, 127)),
            ('oklch(0 0.4 25)', (0, 0, 0)),
            ('oklch(1 0.4 25)', (255, 255, 255)),
            ('lab(0 0 0)', (0, 0, 0)),
            ('lab(100 0 0)', (255, 255, 255)),
        )
        for spec, expected in cases:
            with self.subTest(spec=spec):
                color = to_color(spec)
                self.assertIsNotNone(color, f'Failed to parse: {spec}')
                self.assertEqual(expected, (color.red, color.green, color.blue), spec)

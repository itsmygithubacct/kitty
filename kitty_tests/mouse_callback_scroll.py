#!/usr/bin/env python
# License: GPL v3

import ctypes
import math
from pathlib import Path

from kitty.fast_data_types import test_scale_scroll

from . import BaseTest


SCROLL_LINES = 0
SCROLL_V120 = 1
SCROLL_HIGHRES = 2
NO_TRACKING = 0
TRACKING = 1


class TestMouseCallbackScroll(BaseTest):

    def test_pending_click_restart_guard_precedes_owner_dereferences(self):
        source = (Path(__file__).resolve().parents[1] / 'kitty' / 'state.c').read_text()
        dispatch = source.split('dispatch_pending_clicks(id_type timer_id UNUSED, void *data UNUSED) {', 1)[1]
        dispatch = dispatch.split('\nbool\nupdate_ime_position_for_window', 1)[0]
        for loop_guard in (
            'for (size_t o = 0; !dispatched && o < global_state.num_os_windows; o++)',
            'for (size_t t = 0; !dispatched && t < osw->num_tabs; t++)',
            'for (size_t w = 0; !dispatched && w < qtab->num_windows; w++)',
            'for (size_t i = 0; !dispatched && i < window->pending_clicks.num; i++)',
        ):
            self.assertIn(loop_guard, dispatch)
        self.assertIn('dispatched = true;\n                            send_pending_click_to_window(window, i);', dispatch)
        self.assertIn('} while (dispatched);', dispatch)

    def test_grabbed_zero_multiplier_suppresses_scroll_and_clears_fraction(self):
        for offset_type in (SCROLL_LINES, SCROLL_V120, SCROLL_HIGHRES):
            for multiplier in (0.0, -0.0):
                scroll, pending = test_scale_scroll(
                    TRACKING, 20.0, offset_type, 4.0, 20, multiplier, multiplier, 3
                )
                self.assertEqual(scroll, 0)
                self.assertEqual(pending, 0.0)

    def test_nonfinite_scroll_input_and_multiplier_never_reach_integer_conversion(self):
        for invalid in (math.nan, math.inf, -math.inf):
            scroll, pending = test_scale_scroll(TRACKING, 1.0, SCROLL_LINES, 4.0, 20, invalid, 1.0, 3)
            self.assertEqual((scroll, pending), (0, 0.0))
            scroll, pending = test_scale_scroll(TRACKING, 1.0, SCROLL_HIGHRES, 4.0, 20, 1.0, invalid, 3)
            self.assertEqual((scroll, pending), (0, 0.0))
            scroll, pending = test_scale_scroll(TRACKING, invalid, SCROLL_V120, 4.0, 20, 1.0, 1.0, 3)
            self.assertEqual((scroll, pending), (0, 0.0))
        scroll, pending = test_scale_scroll(NO_TRACKING, 1e300, SCROLL_LINES, 4.0, 20, 1.0, 1.0, 3)
        self.assertEqual((scroll, pending), (0, 0.0))

        source = (Path(__file__).resolve().parents[1] / 'kitty' / 'mouse.c').read_text()
        self.assertIn(
            'if (isfinite(delta_pixels) && screen_apply_pixel_scroll(screen, delta_pixels)',
            source,
        )

    def test_grabbed_finite_multipliers_preserve_only_direction(self):
        cases = (
            (1.0, SCROLL_LINES, 1.0, 0.01, 100.0),
            (1.0, SCROLL_LINES, -1.0, -0.01, -100.0),
            (120.0, SCROLL_V120, 1.0, 0.01, 100.0),
            (120.0, SCROLL_V120, -1.0, -0.01, -100.0),
            (20.0, SCROLL_HIGHRES, 1.0, 100.0, 0.01),
            (20.0, SCROLL_HIGHRES, -1.0, -100.0, -0.01),
        )
        for offset, offset_type, expected, wheel, touch in cases:
            scroll, pending = test_scale_scroll(TRACKING, offset, offset_type, 0.0, 20, wheel, touch, 3)
            self.assertEqual(scroll, expected)
            self.assertEqual(pending, 0.0)

    def test_ungrabbed_fractional_and_minimum_line_policies_remain_separate(self):
        scroll, pending = test_scale_scroll(NO_TRACKING, 5.0, SCROLL_HIGHRES, 0.0, 20, 1.0, 1.0, 3)
        self.assertEqual((scroll, pending), (0, 5.0))
        scroll, pending = test_scale_scroll(NO_TRACKING, 15.0, SCROLL_HIGHRES, pending, 20, 1.0, 1.0, 3)
        self.assertEqual((scroll, pending), (1, 0.0))

        scroll, _ = test_scale_scroll(NO_TRACKING, 0.2, SCROLL_LINES, 0.0, 20, 1.0, 1.0, 3)
        self.assertEqual(scroll, 3)
        scroll, _ = test_scale_scroll(NO_TRACKING, -0.2, SCROLL_LINES, 0.0, 20, 1.0, 1.0, 3)
        self.assertEqual(scroll, -3)


C_INT_BITS = ctypes.sizeof(ctypes.c_int) * 8
C_INT_MIN = -(1 << (C_INT_BITS - 1))
C_INT_MAX = (1 << (C_INT_BITS - 1)) - 1


class TestMouseCallbackScrollBoundaries(BaseTest):

    def test_actual_seam_rejects_nonpositive_cell_sizes(self):
        for offset_type, offset in ((SCROLL_HIGHRES, 20.0), (SCROLL_V120, 120.0)):
            for cell_size in (0, -1):
                with self.assertRaises(ValueError):
                    test_scale_scroll(
                        NO_TRACKING, offset, offset_type, 4.0, cell_size, 1.0, 1.0, 0
                    )

    def test_negative_minimum_line_adjustment_refuses_int_overflow(self):
        for offset in (-1.0, 1.0):
            scroll, pending = test_scale_scroll(
                NO_TRACKING, offset, SCROLL_LINES, 4.0, 1, 1.0, 1.0, C_INT_MIN
            )
            self.assertEqual((scroll, pending), (0, 0.0))

    def test_negative_minimum_line_adjustment_checks_final_int_bounds(self):
        cases = (
            (-1.0, C_INT_MIN + 2, C_INT_MIN + 1),
            (-1.0, C_INT_MIN + 1, None),
            (1.0, -(C_INT_MAX - 1), C_INT_MAX),
            (1.0, -C_INT_MAX, None),
        )
        for offset, min_lines, expected in cases:
            scroll, pending = test_scale_scroll(
                NO_TRACKING, offset, SCROLL_LINES, 4.0, 1, 1.0, 1.0, min_lines
            )
            if expected is None:
                self.assertEqual((scroll, pending), (0, 0.0))
            else:
                self.assertEqual((scroll, pending), (expected, 0.0))

        scroll, pending = test_scale_scroll(
            NO_TRACKING, 0.2, SCROLL_LINES, 4.0, 1, 1.0, 1.0, C_INT_MAX
        )
        self.assertEqual((scroll, pending), (C_INT_MAX, 0.0))

    def test_int_min_and_int_max_boundaries_of_the_integer_conversion(self):
        # scroll_round_to_int rejects INT_MIN as well as INT_MAX + 1 because callers
        # take abs() of the result; the values just inside both bounds stay valid.
        int_min, int_max = float(C_INT_MIN), float(C_INT_MAX)
        for offset_type, pending, cell_size in ((SCROLL_LINES, 0.0, 1), (SCROLL_HIGHRES, 0.0, 1)):
            with self.subTest(offset_type=offset_type):
                for rejected in (int_min, int_max + 1.0, 2.0 * int_max):
                    self.assertEqual(
                        test_scale_scroll(NO_TRACKING, rejected, offset_type, pending, cell_size, 1.0, 1.0, 3),
                        (0, 0.0))
                self.assertEqual(
                    test_scale_scroll(NO_TRACKING, int_min + 1.0, offset_type, pending, cell_size, 1.0, 1.0, 3),
                    (C_INT_MIN + 1, 0.0))
                self.assertEqual(
                    test_scale_scroll(NO_TRACKING, int_max, offset_type, pending, cell_size, 1.0, 1.0, 3),
                    (C_INT_MAX, 0.0))

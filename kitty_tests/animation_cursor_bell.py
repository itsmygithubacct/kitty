from math import isclose

from kitty.fast_data_types import Cursor, test_cursor_blink_easing_function, test_special_cursor_colors, test_visual_bell_intensity
from kitty.options.utils import EasingFunction, parse_animation
from kitty_tests import BaseTest, parse_bytes


class TestAnimationCursorBell(BaseTest):

    def test_bezier_bisection_keeps_lookup_bracket_and_sample_spacing(self):
        low_end = EasingFunction('cubic-bezier', cubic_bezier_points=(0, 1 / 3, 0, 2 / 3))
        high_end = EasingFunction('cubic-bezier', cubic_bezier_points=(1, 1 / 3, 1, 2 / 3))
        # Tighten the x-error tolerance while staying within monotonic_t_to_ms's int range.
        long_duration = 2_000_000.0
        self.set_options({'cursor_blink_interval': (1.0, low_end, low_end)})
        # For both curves y(t) = t; the x coordinates have cube roots at these values.
        self.assertTrue(isclose(test_cursor_blink_easing_function(0.2, True, long_duration), 0.2 ** (1 / 3), abs_tol=1e-6))
        self.assertAlmostEqual(test_cursor_blink_easing_function(0.0, True, long_duration), 0.0)
        self.assertAlmostEqual(test_cursor_blink_easing_function(1.0, True, long_duration), 1.0)
        self.set_options({'cursor_blink_interval': (1.0, high_end, high_end)})
        self.assertAlmostEqual(test_cursor_blink_easing_function(0.999999, True, long_duration), 0.99, delta=2e-6)
        self.set_options({'cursor_blink_interval': (1.0, low_end, low_end)})
        self.assertAlmostEqual(test_cursor_blink_easing_function(0.000001, True, long_duration), 0.01, delta=2e-6)

    def test_special_cursor_preserves_higher_contrast_fallback(self):
        default_fg, default_bg = 0xFFFFFF, 0x000000
        self.assertEqual(test_special_cursor_colors(0x777777, 0x707070, default_fg, default_bg, True), (default_bg, default_fg))
        self.assertEqual(test_special_cursor_colors(0xFFFFFF, 0x000000, 0x808080, 0x808080, True), (0x000000, 0xFFFFFF))
        self.assertEqual(test_special_cursor_colors(0x404040, 0x404040, default_fg, default_bg, True), (default_bg, default_fg))
        self.assertEqual(test_special_cursor_colors(0x404040, 0x202020, default_fg, default_bg, False), (default_bg, default_fg))

    def test_visual_bell_uses_full_configured_duration(self):
        duration = parse_animation('0.4 linear linear', interval=0.0)
        self.set_options({'visual_bell_duration': duration})
        intensity, active = test_visual_bell_intensity(0.2, True)
        self.assertAlmostEqual(intensity, 1.0)
        self.assertTrue(active)
        intensity, active = test_visual_bell_intensity(0.3, True)
        self.assertAlmostEqual(intensity, 0.5)
        self.assertTrue(active)
        intensity, active = test_visual_bell_intensity(0.401, True)
        self.assertEqual(intensity, 0.0)
        self.assertFalse(active)
        intensity, active = test_visual_bell_intensity(0.2, False)
        self.assertEqual(intensity, 0.0)
        self.assertFalse(active)

        self.set_options({'visual_bell_duration': (0.0, EasingFunction(), EasingFunction())})
        intensity, active = test_visual_bell_intensity(0.0, True)
        self.assertEqual(intensity, 0.0)
        self.assertFalse(active)

    def test_text_blink_truth_assignment_preserves_cursor_flags(self):
        cursor = Cursor()
        self.assertFalse(cursor.text_blink)
        cursor.blink = False
        cursor.text_blink = True
        self.assertTrue(cursor.text_blink)
        self.assertFalse(cursor.blink)
        clone = cursor.copy()
        self.assertTrue(clone.text_blink)
        self.assertFalse(clone.blink)
        cursor.blink = True
        self.assertTrue(cursor.blink)
        self.assertTrue(cursor.text_blink)
        self.assertFalse(clone.blink)

        class FailingTruth:
            def __bool__(self):
                raise ValueError('truth conversion failed')

        with self.assertRaisesRegex(ValueError, 'truth conversion failed'):
            cursor.text_blink = FailingTruth()
        self.assertTrue(cursor.text_blink)
        with self.assertRaises(TypeError):
            del cursor.text_blink
        self.assertTrue(cursor.text_blink)

        screen = self.create_screen()
        parse_bytes(screen, b'\x1b[5m')
        self.assertTrue(screen.cursor.text_blink)
        parse_bytes(screen, b'\x1b[25m')
        self.assertFalse(screen.cursor.text_blink)

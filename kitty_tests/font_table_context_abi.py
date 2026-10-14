#!/usr/bin/env python
# License: GPL v3 Copyright: 2026

import tempfile
import struct
import unittest
from pathlib import Path

from kitty.fast_data_types import (
    test_composed_candidate_callback,
    test_parse_stat_font_table,
    test_specialize_font_descriptor_args,
)
from kitty.fonts.common import all_fonts_map, face_from_descriptor
from kitty.fonts.render import create_face

from . import BaseTest
from .fonts import testing_font_data


class TestFontTableContextABI(BaseTest):

    @staticmethod
    def stat_table(*, axis_entry_size=8, axis_count=0, axis_offset=20, value_count=0, value_offset=20, payload=b''):
        header = struct.pack('>HHHHIHIH', 1, 2, axis_entry_size, axis_count, axis_offset, value_count, value_offset, 0)
        return header + payload

    def test_stat_reader_through_face_api(self):
        # Exercise the shared reader through the platform Face.get_variable_data
        # API on an installed variable font with actual STAT design-axis records.
        candidates = all_fonts_map(False)['variable_map']
        for descriptors in candidates.values():
            for descriptor in descriptors:
                data = face_from_descriptor(descriptor).get_variable_data()
                axes = data['design_axes']
                if axes:
                    for axis in axes:
                        self.assertEqual(len(axis['tag']), 4)
                        self.assertIsInstance(axis['name'], str)
                        self.assertIsInstance(axis['ordering'], int)
                        self.assertIsInstance(axis['values'], list)
                    return
        self.skipTest('No installed variable font exposes STAT design-axis records')

    def test_missing_stat_table_through_face_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'FiraCode-Medium.otf'
            path.write_bytes(testing_font_data(path.name))
            data = create_face(str(path)).get_variable_data()
        self.assertEqual(data['design_axes'], ())
        self.assertEqual(data['multi_axis_styles'], ())
        self.assertEqual(data['elided_fallback_name'], '')

    def test_stat_parser_rejects_axis_records_smaller_than_the_fixed_fields(self):
        for size in (0, 7):
            with self.subTest(axis_entry_size=size), self.assertRaisesRegex(ValueError, 'smaller than 8 bytes'):
                test_parse_stat_font_table(self.stat_table(
                    axis_entry_size=size, axis_count=1, payload=b'\0' * 8,
                ))

    def test_stat_parser_bounds_axis_and_value_records(self):
        missing_axis = test_parse_stat_font_table(self.stat_table(axis_count=1, axis_offset=0xFFFFFFFF))
        self.assertEqual(missing_axis['design_axes'], ())

        truncated_axis = test_parse_stat_font_table(self.stat_table(
            axis_count=2, value_offset=31,
            payload=b'wght' + struct.pack('>HH', 257, 3) + b'xyz',
        ))
        self.assertEqual(len(truncated_axis['design_axes']), 1)
        self.assertEqual(truncated_axis['design_axes'][0]['tag'], 'wght')
        self.assertEqual(truncated_axis['design_axes'][0]['ordering'], 3)

        extended_axis = test_parse_stat_font_table(self.stat_table(
            axis_entry_size=10, axis_count=1, payload=b'wdth' + struct.pack('>HH', 258, 4) + b'xy',
        ))
        self.assertEqual(extended_axis['design_axes'][0]['tag'], 'wdth')
        self.assertEqual(extended_axis['design_axes'][0]['ordering'], 4)

        truncated_values = test_parse_stat_font_table(self.stat_table(
            axis_count=1, value_count=2, value_offset=28,
            payload=b'wght' + struct.pack('>HH', 257, 3) + struct.pack('>H', 0),
        ))
        self.assertEqual(truncated_values['design_axes'][0]['values'], [])
        self.assertEqual(truncated_values['multi_axis_styles'], ())

    def test_composed_candidate_invokes_callback_with_original_context(self):
        covered, context_preserved, calls = test_composed_candidate_callback()
        self.assertTrue(covered)
        self.assertTrue(context_preserved)
        self.assertEqual(calls, (ord('A'), 0x0328, 0x0104))

    def test_specialization_adapter_parses_fractional_double_arguments(self):
        descriptor = object()
        expected = (13.123456789, 91.234567891, 137.345678912)
        parsed_descriptor, font_size, dpi_x, dpi_y = test_specialize_font_descriptor_args(
            descriptor, *expected,
        )
        self.assertIs(parsed_descriptor, descriptor)
        self.assertEqual((font_size, dpi_x, dpi_y), expected)

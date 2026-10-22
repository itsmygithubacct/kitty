#!/usr/bin/env python
# License: GPLv3

from kitty.fast_data_types import base64_decode, base64_decode_into

from . import BaseTest


class TestBase64Malformed(BaseTest):

    def decode_into(self, src: bytes) -> bytes:
        output = bytearray(32)
        written = base64_decode_into(src, output)
        return bytes(output[:written])

    def assert_decodes(self, src: bytes, expected: bytes) -> None:
        self.assertEqual(base64_decode(src), expected)
        self.assertEqual(self.decode_into(src), expected)

    def test_valid_empty_padded_and_unpadded_inputs(self):
        for src, expected in (
            (b'', b''),
            (b'TQ==', b'M'),
            (b'TWE=', b'Ma'),
            (b'TQ', b'M'),
            (b'TWE', b'Ma'),
            (b'AP8=', b'\x00\xff'),
            (b'AP8', b'\x00\xff'),
        ):
            with self.subTest(src=src):
                self.assert_decodes(src, expected)

    def test_decode_into_checks_output_capacity(self):
        with self.assertRaises(TypeError):
            base64_decode_into(b'TQ==', bytearray(1))

    def test_malformed_inputs_raise_value_error(self):
        for src in (b'!TQ==', b'TWE!', b'TW=Z', b'TQ=', b'A'):
            with self.subTest(src=src):
                with self.assertRaises(ValueError):
                    base64_decode(src)
                with self.assertRaises(ValueError):
                    base64_decode_into(src, bytearray(32))

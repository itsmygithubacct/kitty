# License: GPL v3 Copyright: 2026

from . import BaseTest, parse_bytes


CSI_PARAM_LIMIT = 256


def semicolon_params(count):
    return b';'.join([b'1'] * count)


def colon_sgr_params(count):
    groups, remainder = divmod(count, 5)
    if remainder not in (0, 1):
        raise ValueError(f'unsupported SGR parameter count: {count}')
    body = b';'.join([b'38:2:1:2:3'] * groups)
    if remainder:
        body += (b';' if body else b'') + b'0'
    return body


def record_events(events):
    def record(_window_id, *args):
        events.append(tuple(args))
    return record


def events_named(events, name):
    return [event[1] for event in events if len(event) > 1 and event[0] == name]


class TestCSIParameterBoundary(BaseTest):

    def test_semicolon_parameter_trailers_at_255_and_256(self):
        for count in (CSI_PARAM_LIMIT - 1, CSI_PARAM_LIMIT):
            with self.subTest(count=count):
                screen = self.create_screen()
                events = []
                body = semicolon_params(count)
                parse_bytes(screen, b'\x1b[' + body + b'm', record_events(events))
                self.ae(events_named(events, 'error'), [])
                self.ae(events_named(events, 'select_graphic_rendition'), [';'.join(['1'] * count)])

    def test_colon_parameter_trailers_at_255_and_256(self):
        for count in (CSI_PARAM_LIMIT - 1, CSI_PARAM_LIMIT):
            with self.subTest(count=count):
                screen = self.create_screen()
                events = []
                body = colon_sgr_params(count)
                parse_bytes(screen, b'\x1b[' + body + b'm', record_events(events))
                self.ae(events_named(events, 'error'), [])
                reports = events_named(events, 'select_graphic_rendition')
                self.assertTrue(reports)
                self.ae(reports[-1], '0' if count % 5 else '38:2:1:2:3')

    def test_256_parameter_trailers_survive_split_input(self):
        cases = (
            (';', semicolon_params(CSI_PARAM_LIMIT)),
            (':', colon_sgr_params(CSI_PARAM_LIMIT)),
        )
        for separator, body in cases:
            with self.subTest(separator=separator):
                screen = self.create_screen()
                events = []
                callback = record_events(events)
                parse_bytes(screen, b'\x1b[' + body, callback)
                self.ae(events_named(events, 'error'), [])
                parse_bytes(screen, b'm', callback)
                self.ae(events_named(events, 'error'), [])
                self.assertTrue(events_named(events, 'select_graphic_rendition'))

    def test_separator_requesting_parameter_257_is_rejected_after_split_input(self):
        cases = (
            (b';', semicolon_params(CSI_PARAM_LIMIT)),
            (b':', colon_sgr_params(CSI_PARAM_LIMIT)),
        )
        for separator, body in cases:
            with self.subTest(separator=separator.decode('ascii')):
                screen = self.create_screen()
                events = []
                callback = record_events(events)
                parse_bytes(screen, b'\x1b[' + body, callback)
                parse_bytes(screen, separator + b'1m', callback)
                self.ae(
                    events_named(events, 'error'),
                    ['CSI escape code has too many parameters, ignoring it'],
                )
                parse_bytes(screen, b'\x1b[0m', callback)
                self.ae(events_named(events, 'select_graphic_rendition')[-1], '0')

    def test_empty_semicolon_parameter_257_is_rejected(self):
        screen = self.create_screen()
        events = []
        parse_bytes(screen, b'\x1b[' + b';' * CSI_PARAM_LIMIT + b'1m', record_events(events))
        self.ae(
            events_named(events, 'error'),
            ['CSI escape code has too many parameters, ignoring it'],
        )
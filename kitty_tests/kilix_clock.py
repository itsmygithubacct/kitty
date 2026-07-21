#!/usr/bin/env python

import re
from datetime import datetime, timedelta, timezone

from kittens.kilix_clock.main import (
    CALENDAR_WIDTH,
    DATE_WIDTH,
    calendar_card,
    date_card,
    date_card_text,
    shifted_month,
)

from . import BaseTest


ANSI = re.compile(r'\x1b\[[0-9:;]*m')


class TestKilixClock(BaseTest):

    def test_month_navigation_crosses_years(self) -> None:
        self.ae(shifted_month(2026, 1, -1), (2025, 12))
        self.ae(shifted_month(2026, 12, 1), (2027, 1))
        self.ae(shifted_month(2026, 7, 18), (2028, 1))

    def test_calendar_card_has_stable_geometry_and_today(self) -> None:
        now = datetime(2026, 7, 21, 14, 5, 9, tzinfo=timezone.utc)
        lines = calendar_card(now, 2026, 7)
        plain = tuple(ANSI.sub('', line) for line in lines)

        self.assertTrue(all(len(line) == CALENDAR_WIDTH for line in plain))
        self.assertIn('July 2026', '\n'.join(plain))
        self.assertIn('21', '\n'.join(plain))
        self.assertIn('14:05:09', '\n'.join(plain))

    def test_date_card_shows_local_details(self) -> None:
        zone = timezone(timedelta(hours=-7), 'PDT')
        now = datetime(2026, 7, 21, 14, 5, 9, tzinfo=zone)
        details = date_card_text(now)
        lines = tuple(ANSI.sub('', line) for line in date_card(now))

        self.ae(details, (
            'Tuesday', 'July 21, 2026', '14:05:09',
            'PDT · UTC−07:00', '2026-07-21',
        ))
        self.assertTrue(all(len(line) == DATE_WIDTH for line in lines))

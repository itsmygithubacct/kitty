#!/usr/bin/env python
"""Interactive calendar and date/time cards for the Kilix tab-bar clock."""

import calendar
import sys
from datetime import datetime
from typing import Any

from kitty.key_encoding import EventType, KeyEvent
from kitty.utils import ScreenSize

from ..tui.handler import Handler
from ..tui.loop import Loop
from ..tui.operations import styled


CALENDAR_WIDTH = 34
DATE_WIDTH = 46


def shifted_month(year: int, month: int, delta: int) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + delta
    shifted_year, zero_based_month = divmod(absolute, 12)
    return shifted_year, zero_based_month + 1


def month_weeks(year: int, month: int) -> tuple[tuple[int, ...], ...]:
    weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(year, month)
    return tuple(tuple(week) for week in weeks)


def utc_offset_text(now: datetime) -> str:
    offset = now.utcoffset()
    if offset is None:
        return 'local time'
    total_minutes = round(offset.total_seconds() / 60)
    sign = '+' if total_minutes >= 0 else '−'
    hours, minutes = divmod(abs(total_minutes), 60)
    zone = now.tzname() or 'local'
    return f'{zone} · UTC{sign}{hours:02d}:{minutes:02d}'


def date_card_text(now: datetime) -> tuple[str, ...]:
    return (
        now.strftime('%A'),
        f'{now.strftime("%B")} {now.day}, {now.year}',
        now.strftime('%H:%M:%S'),
        utc_offset_text(now),
        now.strftime('%Y-%m-%d'),
    )


def _framed_line(rendered: str, visible_width: int, inner_width: int) -> str:
    spare = max(0, inner_width - visible_width)
    left = spare // 2
    return '│' + ' ' * left + rendered + ' ' * (spare - left) + '│'


def _frame(lines: list[tuple[str, int]], width: int) -> tuple[str, ...]:
    inner = width - 2
    ans = ['╭' + '─' * inner + '╮']
    ans.extend(_framed_line(text, visible_width, inner) for text, visible_width in lines)
    ans.append('╰' + '─' * inner + '╯')
    return tuple(ans)


def calendar_card(now: datetime, year: int, month: int) -> tuple[str, ...]:
    title = f'‹  {calendar.month_name[month]} {year}  ›'
    lines: list[tuple[str, int]] = [
        (styled(title, fg='cyan', bold=True), len(title)),
        ('', 0),
        (styled('Su Mo Tu We Th Fr Sa', fg='white', bold=True), 20),
    ]
    weeks = list(month_weeks(year, month))
    weeks.extend([(0,) * 7] * (6 - len(weeks)))
    for week in weeks:
        rendered: list[str] = []
        for day in week:
            token = '  ' if day == 0 else f'{day:2d}'
            if day and (year, month, day) == (now.year, now.month, now.day):
                token = styled(token, fg='black', bg='cyan', bold=True)
            rendered.append(token)
        lines.append((' '.join(rendered), 20))
    clock = now.strftime('%H:%M:%S')
    lines.extend([
        ('', 0),
        (styled(clock, fg='yellow', bold=True), len(clock)),
        (styled('←/→ month · T today · Esc close', fg='black', fg_intense=True), 31),
    ])
    return _frame(lines, CALENDAR_WIDTH)


def date_card(now: datetime) -> tuple[str, ...]:
    weekday, date, clock, zone, iso_date = date_card_text(now)
    lines = [
        (styled('Date & Time', fg='cyan', bold=True), 11),
        ('', 0),
        (styled(weekday, fg='white', bold=True), len(weekday)),
        (styled(date, fg='white', bold=True), len(date)),
        ('', 0),
        (styled(clock, fg='yellow', bold=True), len(clock)),
        (zone, len(zone)),
        (styled(iso_date, fg='black', fg_intense=True), len(iso_date)),
        ('', 0),
        (styled('C calendar · Esc close', fg='black', fg_intense=True), 22),
    ]
    return _frame(lines, DATE_WIDTH)


class ClockWidget(Handler):

    def __init__(self, mode: str) -> None:
        self.mode = mode
        now = datetime.now().astimezone()
        self.year, self.month = now.year, now.month
        self._timer: Any = None

    def initialize(self) -> None:
        self.cmd.set_cursor_visible(False)
        self.cmd.set_line_wrapping(False)
        self.cmd.set_window_title('Calendar' if self.mode == 'calendar' else 'Date & Time')
        self.draw_screen()
        self._schedule_tick()

    def finalize(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def _schedule_tick(self) -> None:
        self._timer = self.asyncio_loop.call_later(1.0, self._tick)

    def _tick(self) -> None:
        self.draw_screen()
        self._schedule_tick()

    @Handler.atomic_update
    def draw_screen(self) -> None:
        now = datetime.now().astimezone()
        lines = (
            calendar_card(now, self.year, self.month)
            if self.mode == 'calendar' else date_card(now)
        )
        width = CALENDAR_WIDTH if self.mode == 'calendar' else DATE_WIDTH
        left = max(0, (self.screen_size.cols - width) // 2)
        top = max(0, (self.screen_size.rows - len(lines)) // 2)
        self.cmd.clear_screen()
        for row, line in enumerate(lines):
            self.cmd.set_cursor_position(left, top + row)
            self.write(line)

    def on_resize(self, new_size: ScreenSize) -> None:
        super().on_resize(new_size)
        self.draw_screen()

    def _change_month(self, delta: int) -> None:
        self.year, self.month = shifted_month(self.year, self.month, delta)
        self.draw_screen()

    def _show_today(self) -> None:
        now = datetime.now().astimezone()
        self.year, self.month = now.year, now.month
        self.draw_screen()

    def on_text(self, text: str, in_bracketed_paste: bool = False) -> None:
        for char in text.lower():
            if char == 'q':
                self.quit_loop(0)
            elif char == 'c':
                self.mode = 'calendar'
                self.cmd.set_window_title('Calendar')
                self._show_today()
            elif self.mode == 'calendar' and char in ('h', '<'):
                self._change_month(-1)
            elif self.mode == 'calendar' and char in ('l', '>'):
                self._change_month(1)
            elif self.mode == 'calendar' and char == 't':
                self._show_today()

    def on_key(self, key_event: KeyEvent) -> None:
        if key_event.type is EventType.RELEASE:
            return
        if key_event.matches('esc'):
            self.quit_loop(0)
        elif self.mode == 'calendar' and key_event.matches('left'):
            self._change_month(-1)
        elif self.mode == 'calendar' and key_event.matches('right'):
            self._change_month(1)
        elif self.mode == 'calendar' and key_event.matches('home'):
            self._show_today()

    def on_interrupt(self) -> None:
        self.quit_loop(0)

    def on_eot(self) -> None:
        self.quit_loop(0)


def main(args: list[str]) -> None:
    mode = args[1] if len(args) > 1 else 'date'
    if mode not in ('calendar', 'date'):
        raise SystemExit(f'Unknown Kilix clock widget: {mode}')
    loop = Loop()
    loop.loop(ClockWidget(mode))
    raise SystemExit(loop.return_code)


if __name__ == '__main__':
    main(sys.argv)
elif __name__ == '__doc__':
    cd = sys.cli_docs  # type: ignore
    cd['usage'] = '[calendar|date]'
    cd['options'] = lambda: ''
    cd['help_text'] = 'Show the Kilix calendar or local date/time widget'
    cd['short_desc'] = 'Show a calendar or date/time widget'

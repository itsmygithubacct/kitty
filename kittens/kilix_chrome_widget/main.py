#!/usr/bin/env python
"""Compact status cards opened by single-clicking Kilix chrome widgets."""

import subprocess
import sys
from typing import Any

from kitty.fast_data_types import truncate_point_for_length, wcswidth
from kitty.key_encoding import EventType, KeyEvent
from kitty.utils import ScreenSize

from ..tui.handler import Handler
from ..tui.loop import Loop
from ..tui.operations import styled


def _network() -> tuple[str, ...]:
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'TYPE,STATE,CONNECTION', 'device'],
            capture_output=True, text=True, timeout=2, check=False)
        rows = [line.split(':', 2) for line in result.stdout.splitlines()]
    except (OSError, subprocess.SubprocessError):
        rows = []
    connected = [row for row in rows if len(row) == 3 and row[1] == 'connected']
    if not connected:
        return ('Network', '', 'No connected interface', '',
                'Double-click for NetworkManager · Esc close')
    kind, _state, name = connected[0]
    return ('Network', '', name or kind, kind, '',
            'Double-click for NetworkManager · Esc close')


def _battery() -> tuple[str, ...]:
    from kitty.kilix_battery import battery_info
    info = battery_info()
    if info is None:
        detail = ('No battery detected', '')
    else:
        detail = (f'{info.percent}%', info.status.title())
    return ('Battery', '', *detail, '',
            'Double-click toggles percentage · Esc close')


def _thermal() -> tuple[str, ...]:
    from kitty.kilix_battery import thermal_info
    info = thermal_info()
    detail = ('No temperature sensor', '') if info is None else (
        f'{info.celsius:.1f}°C', info.level.title())
    return ('Temperature', '', *detail, '',
            'Double-click for dashboard · Esc close')


def card(kind: str) -> tuple[str, ...]:
    return {'network': _network, 'battery': _battery,
            'temperature': _thermal}[kind]()


class ChromeCard(Handler):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._timer: Any = None

    def initialize(self) -> None:
        self.cmd.set_cursor_visible(False)
        self.cmd.set_line_wrapping(False)
        self.cmd.set_window_title(self.kind.title())
        self.draw_screen()
        self._timer = self.asyncio_loop.call_later(2.0, self._tick)

    def finalize(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def _tick(self) -> None:
        self.draw_screen()
        self._timer = self.asyncio_loop.call_later(2.0, self._tick)

    @Handler.atomic_update
    def draw_screen(self) -> None:
        lines = card(self.kind)
        width = min(self.screen_size.cols, max(38, max(map(wcswidth, lines)) + 4))
        if width < 4 or self.screen_size.rows < 3:
            self.cmd.clear_screen()
            return
        lines = lines[:max(0, self.screen_size.rows - 2)]
        inner = width - 2
        lines = tuple(line[:truncate_point_for_length(line, inner)] for line in lines)
        left = max(0, (self.screen_size.cols - width) // 2)
        top = max(0, (self.screen_size.rows - len(lines) - 2) // 2)
        self.cmd.clear_screen()
        framed = ('╭' + '─' * (width - 2) + '╮', *(
            '│' + ' ' * ((inner - wcswidth(line)) // 2) + line +
            ' ' * ((inner - wcswidth(line) + 1) // 2) + '│' for line in lines),
            '╰' + '─' * (width - 2) + '╯')
        for row, line in enumerate(framed):
            self.cmd.set_cursor_position(left, top + row)
            self.write(styled(line, fg='cyan' if row in (0, len(framed)-1) else 'white'))

    def on_resize(self, new_size: ScreenSize) -> None:
        super().on_resize(new_size)
        self.draw_screen()

    def on_key(self, key_event: KeyEvent) -> None:
        if key_event.type is not EventType.RELEASE and key_event.matches('esc'):
            self.quit_loop(0)

    def on_text(self, text: str, in_bracketed_paste: bool = False) -> None:
        if any(char.lower() == 'q' for char in text):
            self.quit_loop(0)

    on_interrupt = on_eot = lambda self: self.quit_loop(0)


def main(args: list[str]) -> None:
    kind = args[1] if len(args) > 1 else 'network'
    if kind not in ('network', 'battery', 'temperature'):
        raise SystemExit(f'Unknown Kilix chrome widget: {kind}')
    loop = Loop()
    loop.loop(ChromeCard(kind))
    raise SystemExit(loop.return_code)


if __name__ == '__main__':
    main(sys.argv)
elif __name__ == '__doc__':
    cd = sys.cli_docs  # type: ignore
    cd['usage'] = '[network|battery|temperature]'
    cd['options'] = lambda: ''
    cd['help_text'] = 'Show a compact Kilix top-bar status card'
    cd['short_desc'] = 'Show a Kilix chrome widget'

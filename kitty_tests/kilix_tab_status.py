#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kilix

import json
import os
import struct
import tempfile
import time
from unittest.mock import patch

from kitty import kilix_neuronte as neuronte
from kitty import kilix_tab_status as status
from kitty.fast_data_types import set_kilix_glyph_source, test_render_line
from kitty.kilix_tab_status import (
    BELL_MARK,
    IDLE_DOT,
    MOON_PHASES,
    TabClock,
    card_lines,
    card_payload,
    classify_load,
    format_duration,
    parse_high_cores,
    parse_settings,
    template_fields,
)
from kitty.tab_bar import (
    NativeWindowLayout,
    TabBar,
    TabBarData,
    apply_title_template,
    native_window_layout,
    tab_slot_lengths,
)

from . import BaseTest, parse_bytes
from .fonts import FontBaseTest
from .tab_bar import DummyBoss, region


def conf(**values: str) -> status.Settings:
    return parse_settings(lambda key, default: values.get(key, default), cpu_count=12)


class TestLoadClasses(BaseTest):

    def test_defaults(self) -> None:
        c = conf()
        self.assertTrue(c.enabled)
        self.ae(c.moderate_cores, 1.0)
        self.ae(c.high_cores, 6.0)        # half of twelve logical CPUs
        self.ae(c.active_cores, 0.1)
        self.ae(c.idle_after, 5.0)
        self.ae(c.step, 1.0)
        self.ae(c.software_step, 4.0)
        self.ae(c.colors, ('#8ae234', '#fce94f', '#ef2929'))

    def test_classification_boundaries(self) -> None:
        for cores, level in (
            (0.0, 'low'), (0.99, 'low'), (1.0, 'moderate'), (5.99, 'moderate'),
            (6.0, 'high'), (40.0, 'high'), (float('nan'), 'low'), (-1.0, 'low'),
        ):
            with self.subTest(cores=cores):
                self.ae(classify_load(cores, 1.0, 6.0), level)

    def test_high_threshold_forms(self) -> None:
        self.ae(parse_high_cores('50%', 1.0, 12), 6.0)
        self.ae(parse_high_cores('50%', 1.0, 2), 2.0)     # never below two cores
        self.ae(parse_high_cores('25%', 1.0, 64), 16.0)
        self.ae(parse_high_cores('3.5', 1.0, 12), 3.5)
        self.ae(parse_high_cores('0.5', 1.0, 12), 1.0)    # never below moderate
        for junk in ('', 'many', '-3', '0%', '250%', 'nan%', 'inf'):
            with self.subTest(junk=junk):
                self.ae(parse_high_cores(junk, 1.0, 12), 6.0)

    def test_settings_are_read_and_bad_values_fall_back(self) -> None:
        c = conf(KILIX_TAB_STATUS_MODERATE_CORES='0.5', KILIX_TAB_STATUS_HIGH_CORES='2',
                 KILIX_TAB_STATUS_ACTIVE_CORES='0.25', KILIX_TAB_STATUS_IDLE_AFTER_SECONDS='30',
                 KILIX_TAB_STATUS_STEP_SECONDS='2', KILIX_TAB_STATUS_HIGH_COLOR='#FF0000')
        self.ae((c.moderate_cores, c.high_cores, c.active_cores, c.idle_after, c.step),
                (0.5, 2.0, 0.25, 30.0, 2.0))
        self.ae(c.colors[2], '#ff0000')
        c = conf(KILIX_TAB_STATUS_MODERATE_CORES='x', KILIX_TAB_STATUS_IDLE_AFTER_SECONDS='0',
                 KILIX_TAB_STATUS_STEP_SECONDS='0.01', KILIX_TAB_STATUS_LOW_COLOR='green')
        self.ae((c.moderate_cores, c.idle_after, c.step, c.colors[0]), (1.0, 5.0, 1.0, '#8ae234'))
        for off in ('0', 'no', 'off', 'false'):
            self.assertFalse(conf(KILIX_TAB_STATUS=off).enabled)

    def test_the_moon_steps_slowly_where_the_cpu_draws_the_screen(self) -> None:
        from kitty.fast_data_types import kilix_software_rendering
        from kitty.kilix_tab_status import step_interval
        c = conf()
        self.ae(step_interval(c, False), 1.0)
        self.ae(step_interval(c, True), 4.0)
        slow = conf(KILIX_TAB_STATUS_STEP_SECONDS='10')        # slower by choice: slower everywhere
        self.ae((step_interval(slow, False), step_interval(slow, True)), (10.0, 10.0))
        same = conf(KILIX_TAB_STATUS_SOFTWARE_STEP_SECONDS='0.5')  # asked not to slow down
        self.ae((step_interval(same, False), step_interval(same, True)), (1.0, 1.0))
        self.ae(conf(KILIX_TAB_STATUS_SOFTWARE_STEP_SECONDS='never').software_step, 4.0)
        for renderer, software in (
            ('llvmpipe (LLVM 19.1.7, 256 bits)', True), ('softpipe', True), ('Software Rasterizer', True),
            ('SWRAST', True), (None, True),
            ('Mesa Intel(R) UHD Graphics 630 (CFL GT2)', False), ('NVIDIA GeForce RTX 3070/PCIe/SSE2', False),
            ('AMD Radeon Graphics (radeonsi, renoir)', False), ('virgl (Mesa Intel(R) UHD Graphics 630)', False),
            ('', False),
        ):
            with self.subTest(renderer=renderer):
                self.assertIs(kilix_software_rendering(renderer), software)
        self.assertIsInstance(kilix_software_rendering(), bool)

    def test_the_step_timer_runs_at_the_chosen_interval(self) -> None:
        added: list[float] = []
        removed: list[int] = []

        def add_timer(callback: object, interval: float, repeats: bool) -> int:
            added.append(interval)
            return len(added)

        software = [False]
        c = conf()
        with patch.object(status, '_timer_id', None), patch.object(status, '_timer_interval', 0.0), \
                patch('kitty.fast_data_types.add_timer', add_timer), \
                patch('kitty.fast_data_types.remove_timer', removed.append), \
                patch('kitty.fast_data_types.kilix_software_rendering', lambda: software[0]):
            status._ensure_timer(c)
            status._ensure_timer(c)              # already running at that interval: left alone
            self.ae((added, removed), ([1.0], []))
            software[0] = True
            status._ensure_timer(c)              # replaced by the slower one
            self.ae((added, removed), ([1.0, 4.0], [1]))
            status._stop_timer()
            self.ae(removed, [1, 2])


class TestIdleClock(BaseTest):

    def test_new_tab_is_idle_since_it_opened(self) -> None:
        clock = TabClock()
        clock.state(1, 100.0)
        self.assertFalse(clock.state(1, 100.0).working)
        self.ae(clock.idle_seconds(1, 160.0), 60.0)

    def test_output_starts_work_and_quiet_ends_it(self) -> None:
        c, clock = conf(), TabClock()
        clock.state(1, 0.0)
        self.assertTrue(clock.note_output(1, 10.0))       # resting -> working
        self.assertFalse(clock.note_output(1, 12.0))      # already working
        self.assertIsNone(clock.idle_seconds(1, 13.0))
        # The engine keeps the time of the latest output; settle reads it.
        self.assertFalse(clock.settle(1, 20.0, 24.9, c))  # 4.9 s quiet: still working
        self.assertTrue(clock.settle(1, 20.0, 25.0, c))   # 5 s quiet: rests
        self.assertFalse(clock.settle(1, 20.0, 26.0, c))  # reported once
        # The idle time counts from the last output, not from when it settled.
        self.ae(clock.idle_seconds(1, 80.0), 60.0)

    def test_cpu_alone_keeps_a_silent_tab_working(self) -> None:
        c, clock = conf(), TabClock()
        clock.state(1, 0.0)
        self.assertTrue(clock.note_load(1, 0.5, 4.0, c))  # above the 0.1 core floor
        self.assertTrue(clock.state(1, 4.0).working)
        for now in (8.0, 12.0, 16.0):
            clock.note_load(1, 0.5, now, c)
            self.assertFalse(clock.settle(1, 0.0, now + 3.9, c))
        clock.note_load(1, 0.02, 20.0, c)                 # under the floor
        # Last busy sample was at 16: one late sample must not rest the tab.
        self.assertFalse(clock.settle(1, 0.0, 21.5, c))
        self.assertTrue(clock.settle(1, 0.0, 22.0, c))
        self.ae(clock.idle_seconds(1, 46.0), 30.0)

    def test_load_below_the_floor_does_not_start_work(self) -> None:
        c, clock = conf(), TabClock()
        clock.state(1, 0.0)
        self.assertFalse(clock.note_load(1, 0.05, 4.0, c))
        self.assertFalse(clock.state(1, 4.0).working)
        self.ae(clock.idle_seconds(1, 9.0), 9.0)
        self.assertFalse(clock.note_load(1, 0.0, 8.0, conf(KILIX_TAB_STATUS_ACTIVE_CORES='0')))
        self.assertFalse(clock.state(1, 8.0).working)

    def test_level_change_asks_for_a_redraw(self) -> None:
        c, clock = conf(), TabClock()
        clock.note_output(1, 0.0)
        self.assertTrue(clock.note_load(1, 1.5, 4.0, c))
        self.ae(clock.state(1, 4.0).level, 'moderate')
        self.assertFalse(clock.note_load(1, 1.7, 8.0, c))
        self.assertTrue(clock.note_load(1, 7.0, 12.0, c))
        self.ae(clock.state(1, 12.0).level, 'high')

    def test_closed_tabs_are_forgotten(self) -> None:
        clock = TabClock()
        clock.note_output(1, 0.0)
        clock.note_output(2, 0.0)
        clock.forget_all_but({2})
        self.ae(set(clock.tabs), {2})
        self.assertTrue(clock.any_working())
        clock.forget_all_but(set())
        self.assertFalse(clock.any_working())

    def test_durations(self) -> None:
        for seconds, text in ((0, '0 s'), (59.9, '59 s'), (60, '1 min 0 s'), (725, '12 min 5 s'),
                              (3600, '1 h 0 min'), (11520, '3 h 12 min'), (183600, '2 d 3 h'), (-5, '0 s')):
            self.ae(format_duration(seconds), text)


class TestTemplate(BaseTest):

    def test_fields(self) -> None:
        c, clock = conf(), TabClock()
        clock.state(7, 0.0)
        f = template_fields(clock, 7, 42.0, c)
        self.ae(f['status_symbol'], IDLE_DOT)
        self.ae(f['status_color'], '\x1b[38:2:138:226:52m')   # #8ae234
        self.ae(f['status'], f"{f['status_color']}{IDLE_DOT} ")
        self.ae((f['load_level'], f['tab_working'], f['tab_idle_seconds']), ('low', False, 42))
        clock.note_output(7, 50.0)
        clock.note_load(7, 2.0, 50.0, c)
        f = template_fields(clock, 7, 51.0, c)
        self.ae(f['status_symbol'], MOON_PHASES[0])
        self.ae(f['status_color'], '\x1b[38:2:252:233:79m')   # #fce94f
        self.ae((f['load_level'], f['tab_working'], f['tab_idle_seconds'], f['tab_load_cores']),
                ('moderate', True, 0, 2.0))
        clock.note_load(7, 9.0, 54.0, c)
        self.ae(template_fields(clock, 7, 54.0, c)['status_color'], '\x1b[38:2:239:41:41m')  # #ef2929

    def test_the_moon_steps_through_eight_phases_one_cell_each(self) -> None:
        self.ae(len(MOON_PHASES), 8)
        self.ae([ord(p[0]) for p in MOON_PHASES], list(range(0x1f311, 0x1f319)))
        self.assertTrue(all(p[1:] == '\ufe0e' for p in MOON_PHASES))
        c, clock = conf(), TabClock()
        clock.note_output(1, 0.0)
        seen = []
        for _ in range(9):
            seen.append(template_fields(clock, 1, 1.0, c)['status_symbol'])
            clock.step += 1
        self.ae(seen, list(MOON_PHASES) + [MOON_PHASES[0]])
        s = self.create_screen(cols=8, lines=1)
        for mark in (*MOON_PHASES, IDLE_DOT, BELL_MARK):
            s.cursor.x = 0
            s.draw(mark)
            self.ae(s.cursor.x, 1, f'U+{ord(mark[0]):04X} takes one cell')

    def test_off_gives_empty_fields(self) -> None:
        f = template_fields(TabClock(), 1, 1.0, conf(KILIX_TAB_STATUS='0'))
        self.ae((f['status'], f['status_symbol'], f['status_color']), ('', '', ''))
        self.ae(template_fields(TabClock(), -1, 1.0, conf())['status'], '')  # the new-tab button

    def test_title_template_draws_the_mark(self) -> None:
        # The unseen-output dot stays configured (pane title bars use it) and
        # is not prefixed to a template that draws the status mark.
        self.set_options({'tab_title_template': '{index}: {status}{fmt.fg.tab}{bell_symbol}{title}',
                          'bell_on_tab': BELL_MARK + ' ', 'tab_activity_symbol': '* '})
        c, clock = conf(), TabClock()
        clock.state(3, 0.0)
        clock.note_output(4, 0.0)
        clock.note_load(4, 8.0, 0.0, c)
        with (
            patch('kitty.tab_bar.viewport_for_window', return_value=(
                region(0, 20, 1000, 200), region(0, 0, 1000, 20), 1000, 200, 10, 20)),
            patch('kitty.tab_bar.cell_size_for_window', return_value=(10, 20)),
            patch('kitty.tab_bar.set_tab_bar_render_data'),
            patch('kitty.tab_bar.get_boss', return_value=DummyBoss()),
            patch('kitty.tab_bar.ensure_chrome_timers'),
            patch('kitty.tab_bar.fields_for_tab', side_effect=lambda tab_id: template_fields(clock, tab_id, 1.0, c)),
        ):
            bar = TabBar(1)
            idle = apply_title_template(bar.draw_data, TabBarData(title='rest', tab_id=3), 1)
            busy = apply_title_template(bar.draw_data, TabBarData(
                title='work', tab_id=4, needs_attention=True, has_activity_since_last_focus=True), 2)
            self.set_options({'tab_title_template': '{index}: {title}', 'tab_activity_symbol': '* '})
            plain = apply_title_template(TabBar(1).draw_data, TabBarData(
                title='work', tab_id=4, has_activity_since_last_focus=True), 2)
            button = apply_title_template(bar.draw_data, TabBarData(title='+'), 3)
        self.assertTrue(idle.startswith(f'1: \x1b[38:2:138:226:52m{IDLE_DOT} \x1b['), idle)
        self.assertTrue(idle.endswith('mrest'), idle)
        self.assertTrue(busy.startswith(f'2: \x1b[38:2:239:41:41m{MOON_PHASES[0]} \x1b['), busy)
        self.assertTrue(busy.endswith(f'm{BELL_MARK} work'), busy)
        self.assertNotIn('*', idle + busy)
        self.assertIn('* ', plain)   # a template without the mark keeps the old dot
        self.ae(button, '+')

    def test_card(self) -> None:
        c, clock = conf(), TabClock()
        clock.state(5, 100.0)
        payload = card_payload(clock, 5, 2, 'build', 400.0, 1_000_000.0, c, cpu_count=12)
        self.ae(json.loads(json.dumps(payload)), payload)   # crosses a process boundary
        self.ae(payload['idle_since'], 999_700.0)
        lines = card_lines(payload, 1_000_060.0)
        self.ae(lines[0], 'Tab 2: build')
        self.assertIn('Idle for 6 min 0 s', lines)
        self.assertIn('Load 0.0 of 12 cores \u00b7 low', lines)
        clock.note_output(5, 401.0)
        clock.note_load(5, 1.25, 401.0, c)
        lines = card_lines(card_payload(clock, 5, 2, 'build', 402.0, 1_000_100.0, c, cpu_count=12), 1_000_100.0)
        self.assertIn('Working', lines)
        self.assertIn('Load 1.2 of 12 cores \u00b7 moderate', lines)
        self.assertFalse(any(line.startswith('Idle') for line in lines))


class TestBarLayout(BaseTest):

    def test_tabs_share_every_cell_of_the_row(self) -> None:
        # 100 cells, three tabs: 33 + 33 + 34, each less its separating cell.
        self.ae(tab_slot_lengths(100, [None, None, None]), [32, 32, 33])
        self.ae(sum(n + 1 for n in tab_slot_lengths(100, [None, None, None])), 100)
        # The button takes four cells and its separator; the tabs get the other 95.
        lengths = tab_slot_lengths(100, [None, None, None, 4])
        self.ae(lengths, [30, 31, 31, 4])
        self.ae(sum(n + 1 for n in lengths), 100)
        for budget in range(40, 200):
            for tabs in range(1, 9):
                lengths = tab_slot_lengths(budget, [None] * tabs + [4])
                self.ae(sum(n + 1 for n in lengths), budget)
                self.assertLessEqual(max(lengths[:-1]) - min(lengths[:-1]), 1)
        self.ae(tab_slot_lengths(3, [None, None, None, 4]), [1, 1, 1, 4])  # never below one cell
        self.ae(tab_slot_lengths(10, [4]), [4])

    def entries(self, titles: tuple[str, ...]):
        def entries_for(width: int = 18):
            return tuple((' ' + (t if len(t) <= width else t[:width - 1] + '\u2026') + ' ', f'w:{i}')
                         for i, t in enumerate(titles))
        return entries_for

    def test_taskbar_reserves_what_it_draws(self) -> None:
        one = self.entries(('QEMU (plebian-019) - noVNC',))
        self.ae(native_window_layout(one, 14, 3, 60), NativeWindowLayout(14, (16,), 0, 16))
        # A short title is not padded out to the configured width...
        self.ae(native_window_layout(self.entries(('xterm',)), 14, 3, 60), NativeWindowLayout(14, (7,), 0, 7))
        # ...nor to the width of a longer title beside it.
        three = self.entries(('alpha window', 'beta', 'gamma window title'))
        self.ae(native_window_layout(three, 14, 3, 60), NativeWindowLayout(14, (14, 6, 16), 0, 42))
        self.ae(native_window_layout(three, 14, 3, 42), NativeWindowLayout(14, (14, 6, 16), 0, 42))
        # Too narrow for all three at 14: the labels shorten two cells at a time.
        self.ae(native_window_layout(three, 14, 3, 41), NativeWindowLayout(12, (14, 6, 14), 0, 40))
        # Too narrow even at six: show what fits and count the rest.
        self.ae(native_window_layout(three, 14, 3, 20), NativeWindowLayout(6, (8, 6), 1, 20))
        self.ae(native_window_layout(three, 14, 3, 19), NativeWindowLayout(6, (8, 6), 1, 17))
        self.ae(native_window_layout(three, 14, 3, 12), NativeWindowLayout(6, (8,), 2, 11))
        self.ae(native_window_layout(three, 14, 3, 5), NativeWindowLayout(6, (), 3, 0))
        self.ae(native_window_layout(lambda width=18: (), 14, 3, 60), NativeWindowLayout(0, (), 0, 0))

    def bar(self, titles: tuple[str, ...], tabs: int, columns: int = 190) -> TabBar:
        self.set_options({'tab_bar_style': 'powerline', 'tab_powerline_style': 'slanted',
                          'tab_title_template': '{index}: {title}'})
        with (
            patch('kitty.tab_bar.cell_size_for_window', return_value=(10, 20)),
            patch('kitty.tab_bar.viewport_for_window', return_value=(
                region(0, 60, columns * 10, 200), region(0, 0, columns * 10, 60), columns * 10, 200, 10, 20)),
            patch('kitty.tab_bar.set_tab_bar_render_data'),
            patch('kitty.tab_bar.get_boss', return_value=DummyBoss()),
            patch('kitty.tab_bar.ensure_chrome_timers'),
            patch.object(TabBar, 'left_status_segments', return_value=()),
            patch.object(TabBar, 'right_status_segments', return_value=((' 12.0 cpu ', None, 0),)),
            patch('kitty.tab_bar.window_entries', side_effect=self.entries(titles)),
            patch('kitty.tab_bar.fields_for_tab', return_value={}),
        ):
            bar = TabBar(1)
            bar.layout()
            bar.update(tuple(TabBarData(title=f'page {i}', tab_id=i + 1, is_active=i == 0) for i in range(tabs))
                       + (TabBarData(title='+'),))
        return bar

    def test_the_bars_say_that_they_are_bars(self) -> None:
        # What the engine draws on Kilix's own bars only depends on this mark.
        from kitty.window_title_bar import WindowTitleBarScreen
        self.assertTrue(self.bar(('xterm',), 3).screen.kilix_bar)
        self.assertTrue(WindowTitleBarScreen(1, 10, 20).screen.kilix_bar)
        self.assertFalse(self.create_screen().kilix_bar)     # a window's text

    def test_no_blank_run_between_taskbar_and_widgets(self) -> None:
        for titles in (('QEMU (plebian-019) - noVNC',), ('xterm',), ('alpha window', 'beta', 'gamma window title')):
            for tabs in (1, 3, 7, 8, 9, 16):
                with self.subTest(titles=titles, tabs=tabs):
                    bar = self.bar(titles, tabs)
                    self.ae(len(bar.window_extents), len(titles))
                    row = str(bar.screen.line(0))
                    # The last taskbar entry ends one cell before the widgets...
                    self.ae(bar.window_extents[-1].x.end, bar.right_status_start - 1)
                    self.ae(row[bar.right_status_start:].rstrip(), ' 12.0 cpu')
                    # ...and the first one starts where the last page of the row ends.
                    page_end = max(e.x.end for e in bar.tab_extents if e.y.start == 0)
                    self.ae(bar.window_extents[0].x.start, page_end)
                    # The cells held back from the pages are the cells the taskbar drew.
                    self.ae(bar.window_reserve, bar.window_extents[-1].x.end - bar.window_extents[0].x.start)
                    # Every entry is its own label and nothing more: the last cell of
                    # each is the label's closing space, with a letter just before it.
                    for extent in bar.window_extents:
                        drawn = row[extent.x.start:extent.x.end]
                        self.ae(drawn, ' ' + drawn.strip() + ' ')
        bar = self.bar(('alpha window', 'beta', 'gamma window title'), 3)
        self.ae([e.x.end - e.x.start for e in bar.window_extents], [14, 6, 16])

    def test_first_row_holds_eight_tabs_and_the_button_stays_small(self) -> None:
        bar = self.bar((), 9)
        self.ae([e.y.start for e in bar.tab_extents], [0] * 8 + [1, 1])
        widths = [e.x.end - e.x.start for e in bar.tab_extents]
        self.assertLessEqual(max(widths[:8]) - min(widths[:8]), 1)
        self.ae(widths[9], 4)                       # "+", a pad, a space, the separator
        # The eight pages end where the widgets begin (one cell apart).
        self.ae(max(e.x.end for e in bar.tab_extents if e.y.start == 0), bar.right_status_start - 1)
        # One page and the button on the second row: the page takes the row.
        self.ae(bar.tab_extents[9].x.end, bar.screen.columns - 1)


class TestStandIns(BaseTest):

    def lit(self, mask: bytes, width: int, columns: range) -> int:
        return sum(mask[y * width + x] for y in range(len(mask) // width) for x in columns)

    def test_moon_phases_differ_and_wax_from_the_right(self) -> None:
        w, h = 10, 20
        masks = [neuronte.draw_moon(p, w, h) for p in range(8)]
        self.ae(len(set(masks)), 8)
        self.assertTrue(all(len(m) == w * h for m in masks))
        ink = [sum(m) for m in masks]
        # Ink is the ring and the dark part: most at the new moon, least at the full.
        self.ae(max(range(8), key=ink.__getitem__), 0)
        self.ae(min(range(8), key=ink.__getitem__), 4)
        self.assertTrue(ink[0] > ink[1] > ink[2] > ink[3] > ink[4] < ink[5] < ink[6] < ink[7] < ink[0])
        left, right = range(0, w // 2), range(w // 2, w)
        for phase in (1, 2, 3):   # waxing: lit (empty) on the right, dark (inked) on the left
            self.assertGreater(self.lit(masks[phase], w, left), self.lit(masks[phase], w, right))
        for phase in (5, 6, 7):   # waning: the mirror image
            self.assertGreater(self.lit(masks[phase], w, right), self.lit(masks[phase], w, left))
            self.ae(masks[phase], b''.join(
                masks[8 - phase][y * w:(y + 1) * w][::-1] for y in range(h)))
        # The moon stays inside its cell: the corners are empty.
        for m in masks:
            self.ae((m[0], m[w - 1], m[-w], m[-1]), (0, 0, 0, 0))

    def test_bell_is_not_a_disc(self) -> None:
        w, h = 10, 20
        bell = neuronte.draw_bell(w, h)
        self.ae(len(bell), w * h)
        self.assertGreater(sum(bell), 0)
        rows = [sum(bell[y * w:(y + 1) * w]) for y in range(h)]
        inked = [y for y, total in enumerate(rows) if total]
        # Narrow at the top (the knob), widest low down (the rim), then a gap and the clapper.
        self.assertLess(rows[inked[0]], max(rows) / 3)
        self.assertGreater(max(range(h), key=rows.__getitem__), (inked[0] + inked[-1]) / 2)
        self.assertLess(rows[inked[-1]], max(rows) / 3)   # the clapper is the last thing, and small
        self.assertNotIn(bell, [neuronte.draw_moon(p, w, h) for p in range(8)])

    def test_what_draws_what_without_nets(self) -> None:
        source = neuronte.GlyphSource(None)
        for cp in neuronte.MOONS + (neuronte.BELL,):
            self.ae(source.source_of(cp), 'stand-in')
            self.assertTrue(source.can_draw(cp))
            self.ae(len(source.draw(cp, 9, 18)), 9 * 18)
        for cp in (0x25cf, 0xf0156, 0x2600, ord('a')):
            self.ae(source.source_of(cp), 'font')
            self.assertFalse(source.can_draw(cp))
            self.assertIsNone(source.draw(cp, 9, 18))
        self.assertIsNone(source.draw(neuronte.BELL, 2, 2))        # too small a cell
        self.assertFalse(neuronte.GlyphSource(None, use_stand_ins=False).can_draw(neuronte.BELL))

    def test_the_list_is_sorted_and_holds_the_measured_symbols(self) -> None:
        cps = neuronte.CHROME_CODEPOINTS
        self.ae(list(cps), sorted(set(cps)))
        for cp in (0x1f311, 0x1f318, 0x25cf, 0x1f56d, 0xeab9, 0xf0079, 0xf0083, 0xf0156, 0xf08ae, 0x26c8):
            self.assertIn(cp, cps)
        # Text, box drawing and the tab separators stay with the engine.
        for cp in (ord('a'), 0x2500, 0xe0b0, 0xe0bc):
            self.assertNotIn(cp, cps)

    def test_a_widget_switches_to_text_symbols_only_as_a_whole(self) -> None:
        class Half(neuronte.GlyphSource):
            def source_of(self, codepoint: int) -> str:
                return 'net.nrt' if codepoint in (0x2600, 0x2601) else 'font'

        class Whole(neuronte.GlyphSource):
            def source_of(self, codepoint: int) -> str:
                return 'net.nrt'

        sun = '\u2600\ufe0f'
        with patch.object(neuronte, '_source', None):
            self.ae(neuronte.text_form(sun, neuronte.WEATHER_SYMBOLS), sun)
        with patch.object(neuronte, '_source', Half(None)):
            self.ae(neuronte.text_form(sun, neuronte.WEATHER_SYMBOLS), sun)
        with patch.object(neuronte, '_source', Whole(None)):
            self.ae(neuronte.text_form(sun, neuronte.WEATHER_SYMBOLS), '\u2600\ufe0e')
            self.ae(neuronte.text_form('?', neuronte.WEATHER_SYMBOLS), '?')
            self.ae(neuronte.text_form('', neuronte.WEATHER_SYMBOLS), '')

    def test_which_symbols_are_two_cells_wide(self) -> None:
        wide = neuronte.TWO_CELL_SYMBOLS
        self.assertTrue(wide <= set(neuronte.CHROME_CODEPOINTS))
        # Everything on the bars but the marks that sit in one cell of a tab title.
        self.assertFalse(wide & set(neuronte.STATUS_MARKS))
        self.assertTrue(set(neuronte.WEATHER_SYMBOLS) <= wide)
        self.ae(set(neuronte.NERD_SYMBOLS) - wide, {0xf2db, 0xf0fb2})    # the two no net was asked for
        self.ae(neuronte.two_cell_symbols('auto'), wide)
        self.ae(neuronte.two_cell_symbols(''), wide)
        for off in ('off', 'none', '0', 'no'):
            self.ae(neuronte.two_cell_symbols(off), frozenset())
        self.ae(neuronte.two_cell_symbols('U+F0219, 0xf073 f0156 u+1f56d'), {0xf0219, 0xf073, 0xf0156, neuronte.BELL})
        # Not a code point, and a character the engine never asks about.
        self.ae(neuronte.two_cell_symbols('wide U+0041 U+F0219'), {0xf0219})
        # The two-cell drawings are asked first, the one-cell ones they fall back to next.
        names = neuronte.WEIGHTS_DEFAULT.split()
        self.ae(names[:2], ['neuronte-kilix-chrome-v2.nrt', 'neuronte-kilix-chrome-v1.nrt'])
        self.assertIn(neuronte.TWO_CELL_WEIGHTS_DEFAULT, names)

    def test_mask_arithmetic(self) -> None:
        # Two rows of four become one: each pixel the mean of the pair, rounded.
        self.ae(neuronte.pair_rows(bytes((0, 255, 10, 3, 0, 255, 20, 4, 100, 0, 0, 0, 50, 0, 1, 0)), 4, 2),
                bytes((0, 255, 15, 4, 75, 0, 1, 0)))
        mask = bytearray(6 * 5)
        for x, y, value in ((2, 1, 255), (4, 3, 200), (0, 0, neuronte._INK - 1)):
            mask[y * 6 + x] = value
        self.ae(neuronte.ink_box(bytes(mask), 6, 5), (2, 1, 5, 4))     # faint pixels are not ink
        self.assertIsNone(neuronte.ink_box(bytes(30), 6, 5))
        out = neuronte.centred(bytes(mask), 6, (2, 1, 5, 4), 9, 7)
        self.ae(len(out), 9 * 7)
        self.ae(neuronte.ink_box(out, 9, 7), (3, 2, 6, 5))             # the 3 by 3 box in the middle of 9 by 7
        self.ae((out[2 * 9 + 3], out[4 * 9 + 5]), (255, 200))
        # A box larger than the mask it goes into is cut, not wrapped.
        self.ae(len(neuronte.centred(bytes([255]) * 30, 6, (0, 0, 6, 5), 4, 3)), 12)

    def test_who_draws_one_cell_and_who_draws_two(self) -> None:
        v2, v1, rest = 'chrome-v2.nrt', 'chrome-v1.nrt', 'rest.nrt'
        log, cross, bell, key = 0xf0219, 0xf0156, neuronte.BELL, 0xf030c

        class FakeNets:
            # v2 holds two-cell drawings and the bell; v1 one-cell drawings.
            files = {v2: {log, bell}, v1: {log, cross, bell}, rest: {0x2600}}

            def __init__(self) -> None:
                self.asked: list[tuple[str, int, int, int]] = []

            def trained(self, cp: int) -> list[tuple[str, str]]:
                return [(name, name) for name, cps in self.files.items() if cp in cps]

            def coverage_from(self, handle: str, cp: int, width: int, height: int) -> bytes:
                self.asked.append((handle, cp, width, height))
                if handle == v2:
                    # Rows alternate full and empty: averaged in pairs they are half.
                    return bytes(255 if y % 2 == 0 else 0 for y in range(height) for _ in range(width))
                # A block in the middle third of the cell, each way.
                return bytes(255 if width // 3 <= x < 2 * width // 3 and height // 3 <= y < 2 * height // 3 else 0
                             for y in range(height) for x in range(width))

        nets = FakeNets()
        source = neuronte.GlyphSource(nets, two_cell=neuronte.TWO_CELL_SYMBOLS, two_cell_files=frozenset({v2}))
        # One cell: never the two-cell drawing. The bell is one cell in every file.
        self.ae([source.source_of(cp) for cp in (log, cross, bell, key, 0x2600)], [v1, v1, v2, 'font', rest])
        self.ae([source.two_cell_source_of(cp) for cp in (log, cross, bell, key, 0x2600)],
                [v2, f'{v1}, enlarged', '', '', f'{rest}, enlarged'])
        self.ae([source.can_draw(cp) for cp in (log, cross, bell, key, ord('a'))], [3, 3, 1, 0, 0])
        # The two-cell file is asked for twice the height and the rows are averaged.
        self.ae(source.draw(log, 18, 17, 2), bytes([128]) * (18 * 17))
        self.ae(nets.asked, [(v2, log, 18, 34)])
        del nets.asked[:]
        self.ae(len(source.draw(log, 9, 17, 1)), 9 * 17)
        self.ae(nets.asked, [(v1, log, 9, 17)])
        # No two-cell drawing: the one-cell drawing, larger, centred, filling 5/6 of the box.
        del nets.asked[:]
        mask = source.draw(cross, 18, 18, 2)
        self.ae(nets.asked[0], (v1, cross, 18, 36))
        self.ae(len(nets.asked), 2)
        left, top, right, bottom = neuronte.ink_box(mask, 18, 18)
        self.assertLessEqual(abs(left + right - 18), 1)                 # centred, to the pixel
        self.assertLessEqual(abs(top + bottom - 18), 1)
        self.assertLessEqual(max(right - left, bottom - top), 15)       # 5/6 of 18
        self.assertGreaterEqual(bottom - top, 13)                       # and not left small
        self.assertIsNone(source.draw(bell, 18, 18, 2))                 # the bell is never two cells
        self.assertIsNone(source.draw(log, 18, 300, 2))                 # twice the height is past the library's limit
        # Switched off: nothing is two cells, and the two-cell file is still not asked for one cell.
        narrow = neuronte.GlyphSource(nets, two_cell=frozenset(), two_cell_files=frozenset({v2}))
        self.ae([narrow.can_draw(cp) for cp in (log, cross, bell)], [1, 1, 1])
        self.ae(narrow.source_of(log), v1)
        # Only the two-cell drawing exists: two cells where a blank follows, the fonts elsewhere.
        FakeNets.files = {v2: {log}}
        only = neuronte.GlyphSource(FakeNets(), two_cell=neuronte.TWO_CELL_SYMBOLS, two_cell_files=frozenset({v2}))
        self.ae((only.source_of(log), only.two_cell_source_of(log), only.can_draw(log)), ('font', v2, 2))
        self.assertIsNone(only.draw(log, 9, 18, 1))
        # Without stand-ins a one-cell drawing is not made larger.
        FakeNets.files = {v1: {cross}}
        strict = neuronte.GlyphSource(FakeNets(), use_stand_ins=False, two_cell=neuronte.TWO_CELL_SYMBOLS)
        self.ae((strict.can_draw(cross), strict.two_cell_source_of(cross)), (1, ''))

    def test_weather_keeps_its_colour_unless_asked(self) -> None:
        class Whole(neuronte.GlyphSource):
            def source_of(self, codepoint: int) -> str:
                return 'net.nrt'

        class Wide(Whole):
            def two_cell_source_of(self, codepoint: int) -> str:
                return 'wide.nrt'

        sun = '\u2600\ufe0f'
        with tempfile.TemporaryDirectory() as root:
            settings = os.path.join(root, 'settings.conf')
            with patch.dict(os.environ, {'GPU_TERMINAL_SETTINGS_FILE': settings}):
                for text, source, expected in (
                    ('', Whole(None), sun),                                  # the default is the colour emoji
                    ('KILIX_NEURONTE_WEATHER=off\n', Wide(None), sun),
                    ('KILIX_NEURONTE_WEATHER=colour\n', Wide(None), sun),    # in colour the text does not change
                    ('KILIX_NEURONTE_WEATHER=nonsense\n', Wide(None), sun),
                    ('KILIX_NEURONTE_WEATHER=one-colour\n', Wide(None), '\u2600\ufe0e '),
                    ('KILIX_NEURONTE_WEATHER=on\n', None, sun),              # no nets: nothing to switch to
                    ('KILIX_NEURONTE_WEATHER=on\n', Whole(None), '\u2600\ufe0e'),
                    ('KILIX_NEURONTE_WEATHER=on\n', Wide(None), '\u2600\ufe0e '),  # with the blank it is drawn into
                ):
                    with open(settings + '.new', 'w') as f:
                        f.write(text)
                    os.replace(settings + '.new', settings)
                    with patch.object(neuronte, '_source', source):
                        self.ae(neuronte.weather_symbol(sun), expected, (text, source))

    def test_colour_layers_are_put_together(self) -> None:
        palette = [(0, 0, 0), (0, 0, 0), (255, 0, 0), (0, 0, 255)] + [(9, 9, 9)] * 28
        own = (10, 20, 30)

        def one(*layers: tuple[float, int]) -> tuple[int, ...]:
            return tuple(neuronte.composite([([cover], entry) for cover, entry in layers], palette, 1, own))

        self.ae(one(), (0, 0, 0, 0))
        self.ae(one((1.0, 2)), (255, 0, 0, 255))
        self.ae(one((0.5, 2)), (255, 0, 0, 128))               # the colour is not multiplied by the alpha
        self.ae(one((1.0, 2), (0.5, 3)), (128, 0, 128, 255))   # the later layer is in front
        self.ae(one((1.0, 3), (1.0, 2)), (255, 0, 0, 255))
        self.ae(one((1.0, 2), (0.0, 3)), (255, 0, 0, 255))     # an empty layer changes nothing
        self.ae(one((1.0, 2), (1.0, 1)), (0, 0, 0, 0))         # entry 1 clears
        self.ae(one((1.0, 2), (0.5, 1)), (255, 0, 0, 128))
        self.ae(one((1.0, 1), (1.0, 2)), (255, 0, 0, 255))     # and only what is under it
        self.ae(one((1.0, 0)), own + (255,))                   # entry 0 is the caller's colour
        self.ae(one((1.0, 32)), own + (255,))                  # as is an entry that is not a colour
        self.ae(one((7.0, 2)), (255, 0, 0, 255))               # coverage is cut to 0..1
        self.ae(one((-1.0, 2)), (0, 0, 0, 0))
        # Every pixel by itself, in order.
        self.ae(neuronte.composite([([1.0, 0.0, 1.0], 2), ([0.0, 0.0, 1.0], 3)], palette, 3),
                bytes((255, 0, 0, 255, 0, 0, 0, 0, 0, 0, 255, 255)))
        # Two halves, two pixels wide and two high, row by row.
        self.ae(neuronte.side_by_side(b'aAbBcCdD', b'eEfFgGhH', 2, 2, unit=2), b'aAbBeEfFcCdDgGhH')
        self.ae([neuronte.weather_mode(word) for word in ('', 'colour', 'Color', 'x', ' ON ', 'one-colour', 'off', 'emoji', 'stand-in')],
                ['colour', 'colour', 'colour', 'colour', 'one-colour', 'one-colour', 'font', 'font', 'colour'])
        self.ae([neuronte.is_private_use(cp) for cp in (0xdfff, 0xe000, 0xf8ff, 0xf900, 0xf0219, 0x10fffd, 0x2600, 0x1f327)],
                [False, True, True, False, True, True, False, False])

    def test_who_draws_the_weather_in_colour(self) -> None:
        weather, emoji, mono = 'weather-colour.nrt', 'emoji.nrt', 'chrome-v1.nrt'
        nine = neuronte.WEATHER_SYMBOLS
        sun, cloud, log = 0x2600, 0x2601, 0xf0219

        class FakeNets:
            colour_files: dict[str, set[int]] = {}
            files: dict[str, set[int]] = {mono: set(nine) | {log}}

            def __init__(self) -> None:
                self.asked: list[tuple] = []

            def colour_trained(self, cp: int) -> list[str]:
                return [name for name, cps in self.colour_files.items() if cp in cps]

            def colour_half(self, name: str, cp: int, half: int, width: int, height: int) -> bytes:
                self.asked.append((name, cp, half, width, height))
                return bytes((half + 1, 0, 0, 255)) * (width * height)

            def trained(self, cp: int) -> list[tuple[str, str]]:
                return [(name, name) for name, cps in self.files.items() if cp in cps]

            def coverage_from(self, handle: str, cp: int, width: int, height: int) -> bytes:
                return bytes(255 if width // 3 <= x < 2 * width // 3 and height // 3 <= y < 2 * height // 3 else 0
                             for y in range(height) for x in range(width))

        def source(mode: str, **colour_files: set[int]) -> neuronte.GlyphSource:
            FakeNets.colour_files = {name.replace('_', '-') + '.nrt': cps for name, cps in colour_files.items()}
            return neuronte.GlyphSource(FakeNets(), two_cell=neuronte.TWO_CELL_SYMBOLS, weather=mode)

        # All nine in the colour files: each by the first file trained on it.
        s = source('colour', weather_colour=set(nine), emoji={cloud})
        self.ae({s.colour_source_of(cp) for cp in nine}, {weather})
        self.ae(s.colour_source_of(log), '')
        s = source('colour', emoji={cloud}, weather_colour=set(nine) - {cloud})
        self.ae([s.colour_source_of(cp) for cp in (sun, cloud)], [weather, emoji])
        # Two cells on the bars only, and colour, for a weather symbol; a private-use symbol two cells anywhere.
        self.ae([s.can_draw(cp) for cp in (sun, cloud, log, ord('a'))], [15, 15, 3, 0])
        # The two halves, each one cell, side by side.
        picture = s.draw_colour(cloud, 18, 17)
        self.ae(s.nets.asked, [(emoji, cloud, 0, 9, 17), (emoji, cloud, 1, 9, 17)])
        self.ae(picture, (bytes((1, 0, 0, 255)) * 9 + bytes((2, 0, 0, 255)) * 9) * 17)
        self.assertIsNone(s.draw_colour(cloud, 19, 17))      # not two whole cells
        self.assertIsNone(s.draw_colour(cloud, 6, 17))       # cells too small for the library
        self.assertIsNone(s.draw_colour(log, 18, 17))        # not a weather symbol
        # One short of nine: none is drawn in colour, the emoji font keeps them all.
        s = source('colour', weather_colour=set(nine) - {0x26c8}, emoji={cloud})
        self.ae([s.colour_source_of(cp) for cp in nine], [''] * 9)
        self.ae([s.can_draw(cp) for cp in (sun, cloud)], [7, 7])
        self.assertIsNone(s.draw_colour(cloud, 18, 17))
        # No other word brings a one-colour drawing in as a colour one: the removed
        # look-ahead value is the default now, and the default is all nine or none.
        s = source(neuronte.weather_mode('stand-in'), emoji={cloud})
        self.ae([s.colour_source_of(cp) for cp in (sun, cloud)], ['', ''])
        self.ae(s.can_draw(sun), 7)
        self.assertIsNone(s.draw_colour(sun, 18, 18))
        self.ae(s.nets.asked, [])
        # Asked for one colour, or for the emoji font: the colour files draw nothing.
        for mode in ('one-colour', 'font'):
            s = source(mode, weather_colour=set(nine))
            self.ae(([s.colour_source_of(cp) for cp in nine], s.can_draw(sun)), ([''] * 9, 7), mode)
        # No nets at all.
        none = neuronte.GlyphSource(None, weather='colour')
        self.ae((none.colour_source_of(sun), none.can_draw(sun), none.draw_colour(sun, 18, 18)), ('', 0, None))

    def test_the_engine_is_handed_all_three_callables(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            settings = os.path.join(root, 'settings.conf')
            with (
                patch.dict(os.environ, {'NEURONTE_HOME': root, 'GPU_TERMINAL_SETTINGS_FILE': settings}),
                patch('kitty.fast_data_types.set_kilix_glyph_source') as hand_over,
                patch.object(neuronte, '_source', None),
            ):
                neuronte.install()
                source = neuronte.source()
                hand_over.assert_called_once_with(neuronte.CHROME_CODEPOINTS, source.can_draw, source.draw, source.draw_colour)
                self.ae(source.weather, 'colour')            # the default
                with open(settings, 'w') as f:
                    f.write('KILIX_NEURONTE=off\n')
                hand_over.reset_mock()
                neuronte.install()
                hand_over.assert_called_once_with((), None, None)
                self.assertIsNone(neuronte.source())

    def test_where_neuronte_is_looked_for(self) -> None:
        self.ae(neuronte.neuronte_root({'NEURONTE_HOME': '/opt/nrt'}), '/opt/nrt')
        self.ae(neuronte.neuronte_root({'GPU_TERMINAL_SOURCE_HOME': '/srv/sources'}), '/srv/sources/neuronte')
        self.ae(neuronte.neuronte_root({}), os.path.expanduser('~/.local/gpu_terminal/sources/neuronte'))
        self.ae(neuronte.weight_paths('/r', 'a.nrt, b.nrt ../c.nrt /etc/d.nrt e.txt'),
                ['/r/weights/a.nrt', '/r/weights/b.nrt'])

    def test_missing_library_or_weights_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            nets = neuronte.Nets(os.path.join(root, 'build', 'libneuronte.so'), [os.path.join(root, 'weights', 'x.nrt')])
            self.ae(nets.handles, [])
            self.assertIsNone(nets.net_for(0x25cf))
            self.assertIsNone(nets.coverage(0x25cf, 10, 20))
            with patch.dict(os.environ, {'NEURONTE_HOME': root, 'GPU_TERMINAL_SETTINGS_FILE': os.path.join(root, 'none')}):
                os.environ.pop('NEURONTE_LIB', None)
                source = neuronte.build_source()
            self.ae(source.source_of(0x25cf), 'font')
            self.ae(source.source_of(0x1f313), 'stand-in')

    def test_a_mask_comes_from_the_cells_file_when_it_holds_it(self) -> None:
        import ctypes

        dot, moon = 0x25cf, 0x1f313

        class Library:
            """What this module uses of libneuronte, with or without the cells calls."""

            def __init__(self, with_cells: bool = True, refuse: tuple[str, ...] = ()) -> None:
                self.log: list[tuple[object, ...]] = []
                self.opened: dict[int, str] = {}
                self.kept: dict[int, str] = {}
                self.refuse = refuse
                # ctypes functions take attributes (argtypes, restype); bound methods do not.
                calls = {'neuronte_open_file': self.open_file, 'neuronte_untrained': self.untrained,
                         'neuronte_coverage': self.coverage, 'neuronte_close': self.close}
                if with_cells:
                    calls.update(neuronte_cells_open=self.cells_open, neuronte_cells_find=self.cells_find)
                for name, method in calls.items():
                    setattr(self, name, (lambda method: lambda *args: method(*args))(method))

            def open_file(self, path: bytes, out: object) -> int:
                number = 100 + len(self.opened)
                self.opened[number] = os.fsdecode(path)
                out._obj.value = number  # type: ignore[attr-defined]
                return 0

            def untrained(self, handle: ctypes.c_void_p, cp: int) -> int:
                return 0 if cp in (dot, moon) else 1

            def coverage(self, handle: ctypes.c_void_p, cp: int, width: int, height: int, samples: int, buf: object) -> int:
                self.log.append(('net', os.path.basename(self.opened[handle.value or 0]), cp, width, height, samples))
                buf[0] = 1.0  # type: ignore[index]
                return 0

            def close(self, handle: ctypes.c_void_p) -> None:
                pass

            def cells_open(self, cells: bytes, weights: bytes, flags: int, out: object) -> int:
                name = os.path.basename(os.fsdecode(cells))
                self.log.append(('open', os.fsdecode(cells), os.fsdecode(weights), flags))
                if name in self.refuse:
                    return -4
                number = 500 + len(self.kept)
                self.kept[number] = name
                out._obj.value = number  # type: ignore[attr-defined]
                return 0

            def cells_find(self, cells: ctypes.c_void_p, cp: int, width: int, height: int, samples: int, style: int, snap: int) -> object:
                self.log.append(('find', self.kept[cells.value or 0], cp, width, height, samples, style, snap))
                # The file holds the dot at 9x18 and nothing else: 0.5 in the last pixel.
                if (cp, width, height) != (dot, 9, 18):
                    return None
                kept = (ctypes.c_float * (width * height + 3))()   # the file goes on after the entry
                kept[width * height - 1] = 0.5
                kept[width * height] = 1.0
                return kept

        def nets(library: Library, cells: str, *names: str) -> neuronte.Nets:
            with patch.object(neuronte.ctypes, 'CDLL', lambda path: library):
                return neuronte.Nets(lib_path, [os.path.join(root, 'weights', name) for name in names], cells_dir=cells)

        with tempfile.TemporaryDirectory() as root:
            lib_path = os.path.join(root, 'libneuronte.so')
            kept_dir = os.path.join(root, 'cells')
            os.makedirs(os.path.join(root, 'weights'))
            os.makedirs(kept_dir)
            for path in (lib_path, os.path.join(root, 'weights', 'a.nrt'), os.path.join(root, 'weights', 'b.nrt'),
                         os.path.join(kept_dir, 'a.cells')):
                open(path, 'wb').close()
            from_net = bytes((255,)) + bytes(9 * 18 - 1)
            from_file = bytes(9 * 18 - 1) + bytes((128,))

            # The file is looked for under the weight file's name, and opened against that weight file.
            self.ae(neuronte.cells_path('/c', '/w/neuronte-split-v9.nrt'), '/c/neuronte-split-v9.cells')
            library = Library()
            n = nets(library, kept_dir, 'a.nrt', 'b.nrt')
            self.ae(library.log, [('open', os.path.join(kept_dir, 'a.cells'), os.path.join(root, 'weights', 'a.nrt'), 0)])
            self.ae(n.problems, [])
            a, b = (handle for _, handle in n.handles)
            del library.log[:]
            # Held by the file: the file's floats, as bytes, and the net is not asked.
            self.ae(n.coverage_from(a, dot, 9, 18), from_file)
            self.ae(library.log, [('find', 'a.cells', dot, 9, 18, 0, 0, -1)])
            del library.log[:]
            # Not held (another size, another character): the file is asked first, then the net.
            self.ae(n.coverage_from(a, dot, 10, 20), bytes((255,)) + bytes(10 * 20 - 1))
            self.ae(n.coverage_from(a, moon, 9, 18), from_net)
            self.ae(library.log, [
                ('find', 'a.cells', dot, 10, 20, 0, 0, -1), ('net', 'a.nrt', dot, 10, 20, 0),
                ('find', 'a.cells', moon, 9, 18, 0, 0, -1), ('net', 'a.nrt', moon, 9, 18, 0)])
            self.ae((n.cells_asked, n.cells_found), (3, 1))
            del library.log[:]
            # A weight file with no cells file: its net, and no other file's cells.
            self.ae(n.coverage_from(b, dot, 9, 18), from_net)
            self.ae(library.log, [('net', 'b.nrt', dot, 9, 18, 0)])
            # A size the library would refuse is refused before either is asked.
            del library.log[:]
            self.assertIsNone(n.coverage_from(a, dot, 3, 18))
            self.ae(library.log, [])

            # No directory given (the setting is off): no file is opened and the net draws.
            library = Library()
            n = nets(library, '', 'a.nrt')
            self.ae(n.coverage_from(n.handles[0][1], dot, 9, 18), from_net)
            self.ae(library.log, [('net', 'a.nrt', dot, 9, 18, 0)])
            # A library without the cells calls: the same.
            library = Library(with_cells=False)
            n = nets(library, kept_dir, 'a.nrt')
            self.ae((n.problems, n.cells), ([], {}))
            self.ae(n.coverage_from(n.handles[0][1], dot, 9, 18), from_net)
            self.ae(library.log, [('net', 'a.nrt', dot, 9, 18, 0)])
            # A cells file the library refuses (made from other weights, or damaged): said, and the net draws.
            library = Library(refuse=('a.cells',))
            n = nets(library, kept_dir, 'a.nrt')
            self.ae(n.problems, [f'cells file {os.path.join(kept_dir, "a.cells")} is not used: error -4'])
            self.ae(n.coverage_from(n.handles[0][1], dot, 9, 18), from_net)

            # Where the files are looked for, and the switch.
            self.ae(neuronte.cells_directory({}), '/var/lib/neuronte/cells')
            self.ae(neuronte.cells_directory({'NEURONTE_CELLS_DIR': '/x'}, 'auto'), '/x')
            self.ae([neuronte.cells_directory({'NEURONTE_CELLS_DIR': '/x'}, word) for word in ('off', ' OFF ', '0', 'no')], [''] * 4)
            # build_source() hands the directory on.
            seen: list[tuple[object, ...]] = []

            class Seen:
                handles: list[object] = []
                colour_handles: list[object] = []

                def __init__(self, *args: object) -> None:
                    seen.append(args)

            for text, expected in (('', kept_dir), ('KILIX_NEURONTE_CELLS=off\n', '')):
                settings = os.path.join(root, 'settings.conf')
                with open(settings, 'w') as f:
                    f.write(text)
                with patch.dict(os.environ, {'NEURONTE_HOME': root, 'NEURONTE_CELLS_DIR': kept_dir, 'GPU_TERMINAL_SETTINGS_FILE': settings}), \
                        patch.object(neuronte, 'Nets', Seen):
                    neuronte.build_source()
                self.ae(seen.pop()[3], expected)


class TestGpuPrototype(BaseTest):
    # KILIX_NEURONTE_GPU=auto: Neuronte's backend loader draws the bulk sizes; off unless asked for.

    def nets(self, held: set[int], cpu_calls: list[int]) -> neuronte.Nets:
        class Library:
            def neuronte_untrained(self, handle: object, codepoint: int) -> int:
                return 0 if codepoint in held else 1

            def neuronte_coverage(self, handle: object, codepoint: int, width: int, height: int, samples: int, buf: object) -> int:
                cpu_calls.append(codepoint)
                return 0

        nets = neuronte.Nets('', [])
        nets.lib = Library()  # type: ignore[assignment]
        nets.path_of[7] = '/weights/x.nrt'
        return nets

    def test_off_unless_asked_and_absent_library_is_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {'GPU_TERMINAL_SETTINGS_FILE': os.path.join(root, 'none')}):
                os.environ.pop(neuronte.GPU_KEY, None)
                self.ae(neuronte.chrome_value(neuronte.GPU_KEY, 'off'), 'off')
            drawer = neuronte.BackendDrawer(os.path.join(root, neuronte.BACKEND_LIBRARY), os.path.join(root, 'log'))
            self.assertIsNone(drawer.lib)
            self.assertIn('cannot load the backend loader', drawer.problem)
            self.assertIsNone(drawer.coverage(os.path.join(root, 'x.nrt'), [0x25cf], 10, 20))
            self.ae(drawer.choice(), (False, drawer.problem))
            drawer.close()

    def test_one_launch_draws_the_listed_symbols_the_file_holds(self) -> None:
        import ctypes
        cpu_calls: list[int] = []
        launches: list[tuple[str, tuple[int, ...], int, int]] = []

        class Drawer:
            def coverage(self, path: str, codepoints: list[int], width: int, height: int) -> list[bytes] | None:
                launches.append((path, tuple(codepoints), width, height))
                return [bytes([cp & 0xff]) * (width * height) for cp in codepoints]

        nets = self.nets({0xf028, 0xf073, 0xf0e7}, cpu_calls)
        nets.gpu = Drawer()  # type: ignore[assignment]
        handle = ctypes.c_void_p(7)
        listed = (0xeab9, 0xf028, 0xf073, 0xf0e7)
        self.ae(nets.bulk_coverage(handle, 0xf073, 4, 8, listed), bytes([0x73]) * 32)
        # the character asked for first, then the listed ones the file holds; not 0xeab9
        self.ae(launches, [('/weights/x.nrt', (0xf073, 0xf028, 0xf0e7), 4, 8)])
        self.ae(nets.bulk_coverage(handle, 0xf028, 4, 8, listed), bytes([0x28]) * 32)
        self.ae(len(launches), 1)                 # kept from the first launch
        nets.bulk_coverage(handle, 0xf028, 5, 8, listed)
        self.ae(len(launches), 2)                 # another size is another launch
        self.ae(cpu_calls, [])
        # a size asked for once stays on the CPU
        nets.coverage_from(handle, 0xf028, 6, 8)
        self.ae((len(launches), cpu_calls), (2, [0xf028]))

    def test_the_cells_file_is_asked_before_the_gpu(self) -> None:
        import ctypes
        cpu_calls: list[int] = []
        launches: list[tuple[int, ...]] = []

        class Drawer:
            def coverage(self, path: str, codepoints: list[int], width: int, height: int) -> list[bytes] | None:
                launches.append(tuple(codepoints))
                return [b'\x01' * (width * height) for _ in codepoints]

        nets = self.nets({0xf028, 0xf073}, cpu_calls)
        nets.gpu = Drawer()  # type: ignore[assignment]
        held = {0xf028: b'\x07' * 32}
        nets.from_cells = lambda handle, codepoint, width, height: held.get(codepoint)  # type: ignore[method-assign]
        handle = ctypes.c_void_p(7)
        self.ae(nets.bulk_coverage(handle, 0xf028, 4, 8, (0xf028, 0xf073)), b'\x07' * 32)
        self.ae((launches, cpu_calls), ([], []))          # kept in the file: nothing is drawn anywhere
        self.ae(nets.bulk_coverage(handle, 0xf073, 4, 8, (0xf028, 0xf073)), b'\x01' * 32)
        self.ae(launches, [(0xf073, 0xf028)])             # not in the file: the prototype's path, as before

    def fake_drawer(self, root: str, state: dict[str, object]) -> neuronte.BackendDrawer:
        # a drawer whose loader is a stand-in that reports `state` (gpu, ready, reason, code) as it stands
        class Loader:
            def __init__(self) -> None:
                self.opened = self.closed = 0
                self.asked: list[int] = []
                self.expected: list[int] = []

            def neuronte_backend_open(self, path: bytes, width: int, height: int, handle: object) -> int:
                self.opened += 1
                if state.get('open', 0) == 0:
                    handle._obj.value = 40 + self.opened  # type: ignore[attr-defined]
                return int(state.get('open', 0))  # type: ignore[call-overload]

            def neuronte_backend_coverage(self, handle: object, scalars: object, count: int, buf: object) -> int:
                self.asked.append(count)
                for k in range(len(buf)):  # type: ignore[arg-type]
                    buf[k] = 1.0  # type: ignore[index]
                state['gpu_glyphs' if state['ready'] else 'cpu_glyphs'] = (
                    int(state.get('gpu_glyphs' if state['ready'] else 'cpu_glyphs', 0)) + count)  # type: ignore[call-overload]
                return int(state.get('code', 0))  # type: ignore[call-overload]

            def neuronte_backend_expect(self, handle: object, count: int) -> None:
                self.expected.append(count)

            def neuronte_backend_info(self, handle: object, info: object) -> None:
                i = info._obj  # type: ignore[attr-defined]
                i.gpu, i.ready = int(bool(state['gpu'])), int(bool(state['ready']))
                i.min_glyphs = int(state.get('min_glyphs', 1))  # type: ignore[call-overload]
                i.reason = str(state.get('reason', '')).encode()
                i.backend = b'gpu-test' if state['gpu'] else b'cpu'
                i.from_cpu, i.from_gpu = int(state.get('cpu_glyphs', 0)), int(state.get('gpu_glyphs', 0))  # type: ignore[call-overload]

            def neuronte_backend_choice(self, info: object) -> int:
                self.neuronte_backend_info(None, info)
                return int(bool(state['gpu']))

            def neuronte_backend_close(self, handle: object) -> None:
                self.closed += 1

        drawer = neuronte.BackendDrawer('', os.path.join(root, 'log'))
        drawer.problem = ''
        drawer.lib = Loader()  # type: ignore[assignment]
        return drawer

    def events(self, root: str) -> list[dict[str, object]]:
        with open(os.path.join(root, 'log')) as f:
            return [json.loads(line) for line in f]

    def test_the_cpu_draws_whenever_the_loader_would(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state: dict[str, object] = {'gpu': False, 'ready': False, 'reason': 'on battery'}
            drawer = self.fake_drawer(root, state)
            # on battery, warming, no backend.conf, a lost device: the loader says so, and nothing is asked of it
            self.assertIsNone(drawer.coverage('/w/x.nrt', [65, 66], 2, 2))
            self.assertIsNone(drawer.coverage('/w/x.nrt', [67], 2, 2))
            self.ae(drawer.lib.asked, [])                               # type: ignore[union-attr]
            self.ae(drawer.choice(), (False, 'on battery'))
            # back on mains: the GPU is wanted but not open yet, so only what is needed now is asked
            state.update(gpu=True, reason='a GPU was measured faster')
            self.ae(drawer.coverage('/w/x.nrt', [65, 66, 67], 2, 2), [b'\xff' * 4])
            self.ae(drawer.lib.asked, [1])                              # type: ignore[union-attr]
            self.ae(drawer.lib.expected, [3])                           # type: ignore[union-attr]  # as a part of a job of three
            # open: everything listed goes in one call
            state.update(ready=True)
            self.ae(drawer.coverage('/w/x.nrt', [65, 66, 67], 2, 2), [b'\xff' * 4] * 3)
            self.ae(drawer.lib.asked, [1, 3])                           # type: ignore[union-attr]
            self.ae(drawer.lib.expected, [3])                           # type: ignore[union-attr]
            # a GPU that is only worth it for larger jobs than this one is not asked at all
            state.update(min_glyphs=4)
            self.assertIsNone(drawer.coverage('/w/x.nrt', [65, 66, 67], 2, 2))
            self.ae(drawer.coverage('/w/x.nrt', [65, 66, 67, 68], 2, 2), [b'\xff' * 4] * 4)
            self.ae(drawer.lib.asked, [1, 3, 4])                        # type: ignore[union-attr]
            state.update(min_glyphs=1)
            # the device fails: the loader answers that call from the CPU and reports the CPU from then on
            state.update(gpu=False, ready=False, reason='device lost')
            self.assertIsNone(drawer.coverage('/w/x.nrt', [65], 2, 2))
            self.ae(drawer.lib.asked, [1, 3, 4])                        # type: ignore[union-attr]
            self.ae(drawer.lib.opened, 1)                               # type: ignore[union-attr]
            drawer.close()
            self.ae(drawer.lib.closed, 1)                               # type: ignore[union-attr]
            log = self.events(root)
            self.ae([e['event'] for e in log], ['state', 'state', 'draw', 'state', 'draw', 'draw', 'state'])
            self.ae([e['reason'] for e in log if e['event'] == 'state'],
                    ['on battery', 'a GPU was measured faster', 'a GPU was measured faster', 'device lost'])
            self.ae([(e['glyphs'], e['cpu'], e['gpu']) for e in log if e['event'] == 'draw'], [(1, 1, 0), (3, 0, 3), (4, 0, 4)])

    def test_a_refused_file_and_a_failed_call_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state: dict[str, object] = {'gpu': True, 'ready': True, 'open': -2}
            drawer = self.fake_drawer(root, state)
            self.assertIsNone(drawer.coverage('/w/x.nrt', [65], 2, 2))      # the loader cannot open the file
            self.assertIsNone(drawer.coverage('/w/x.nrt', [65], 2, 2))
            self.ae(drawer.lib.opened, 1)                                   # type: ignore[union-attr]  # asked once
            state.update(open=0, code=-1)
            self.assertIsNone(drawer.coverage('/w/y.nrt', [65], 2, 2))      # a call the loader fails
            state.update(code=0)
            self.ae(drawer.coverage('/w/y.nrt', [65], 2, 2), [b'\xff' * 4])
            self.ae([e['event'] for e in self.events(root)], ['refused', 'state', 'failed', 'draw'])

    def test_handles_are_kept_a_few_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            drawer = self.fake_drawer(root, {'gpu': True, 'ready': True})
            for size in range(2, 2 + drawer.MAX_HANDLES + 3):
                self.ae(drawer.coverage('/w/x.nrt', [65], size, size), [b'\xff' * (size * size)])
            self.ae(len(drawer.handles), drawer.MAX_HANDLES)
            self.ae(drawer.lib.closed, 3)                                   # type: ignore[union-attr]

    def test_the_real_loader_with_a_stand_in_gpu(self) -> None:
        # the loader itself, a stand-in for the GPU library and a weights file, when the environment names them
        loader, fake, weights = (os.environ.get(k, '') for k in ('NEURONTE_BACKEND_LIB', 'NEURONTE_FAKE_GPU_LIB', 'NEURONTE_TEST_NRT'))
        library = os.environ.get('NEURONTE_LIB', '')
        if not all(os.path.isfile(p) for p in (loader, fake, weights, library)):
            self.skipTest('NEURONTE_BACKEND_LIB, NEURONTE_FAKE_GPU_LIB, NEURONTE_TEST_NRT and NEURONTE_LIB are not all set')

        def identity(path: str) -> str:
            st = os.stat(path)
            return f'{st.st_size}:{st.st_mtime_ns}'

        with tempfile.TemporaryDirectory() as root:
            def supply(name: str, **files: str) -> None:
                os.makedirs(os.path.join(root, 'power', name), exist_ok=True)
                for key, value in files.items():
                    with open(os.path.join(root, 'power', name, key), 'w') as f:
                        f.write(value + '\n')
            # what Neuronte's setup would have written for a machine whose GPU won
            common = f'gpu.backend=cuda\ngpu.library={fake}\ngpu.library.id={identity(fake)}\ngpu.driver=test driver\n'
            with open(os.path.join(root, 'backend.conf'), 'w') as f:
                f.write('version=1\n' + common + 'gpu.identical=yes\nmin_glyphs=1\nbattery=cpu\nwake_ms=0\nreason=a test\n')
            with open(os.path.join(root, 'warm.stamp'), 'w') as f:
                f.write('version=1\n' + common + f'warm.{os.path.basename(weights)}={identity(weights)}\n')
            supply('BAT0', type='Battery', status='Discharging', capacity='97')
            supply('mouse', type='Battery', scope='Device', status='Discharging', capacity='12')
            env = {
                'NEURONTE_BACKEND_CONF': os.path.join(root, 'backend.conf'),
                'NEURONTE_BACKEND_WARM_STAMP': os.path.join(root, 'warm.stamp'),
                'NEURONTE_BACKEND_TEST_DRIVER': 'test driver', 'NEURONTE_BACKEND_POWER_DIR': os.path.join(root, 'power'),
                'NEURONTE_SETUP_CONF': os.path.join(root, 'none'), 'XDG_CONFIG_HOME': root, 'NEURONTE_BACKEND_KICK': '0',
                'FAKE_GPU_LOG': os.path.join(root, 'gpu.log'),
            }
            with patch.dict(os.environ, env):
                os.environ.pop('NEURONTE_BACKEND', None)
                nets = neuronte.Nets(library, [weights])
                self.ae(nets.problems, [])
                handle = nets.handles[0][1]
                held = [cp for cp in range(0x20, 0x10000) if nets.lib.neuronte_untrained(handle, cp) == 0][:40]  # type: ignore[union-attr]
                on_cpu = [nets.from_cpu(handle, cp, 9, 18) for cp in held]
                drawer = neuronte.BackendDrawer(loader, os.path.join(root, 'log'))
                self.ae(drawer.problem, '')
                # discharging: the CPU, whatever the charge, and the GPU library is not even loaded
                self.ae(drawer.choice(), (False, 'on battery'))
                self.assertIsNone(drawer.coverage(weights, held, 9, 18))
                self.assertFalse(os.path.exists(env['FAKE_GPU_LOG']))
                drawer.close()
                # on mains: the same bytes as the CPU library, from the GPU once it is open
                supply('BAT0', status='Charging')
                drawer = neuronte.BackendDrawer(loader, os.path.join(root, 'log'))
                self.ae(drawer.choice(), (True, 'a test'))
                deadline = time.monotonic() + 20
                while True:
                    masks = drawer.coverage(weights, held, 9, 18)
                    assert masks is not None
                    self.ae(masks, on_cpu[:len(masks)])
                    if len(masks) == len(held) or time.monotonic() > deadline:
                        break
                    time.sleep(0.02)
                self.ae(len(masks), len(held))                  # the GPU opened, and the whole list went in one call
                with open(env['FAKE_GPU_LOG']) as f:
                    said = f.read()
                self.assertIn('open 9x18', said)
                self.assertIn(f'coverage {len(held)} glyphs', said)
                drawer.close()
                draws = [e for e in self.events(root) if e['event'] == 'draw']
                self.ae((draws[-1]['glyphs'], draws[-1]['gpu'], draws[-1]['cpu']), (len(held), len(held), 0))


class TestGlyphHook(FontBaseTest):

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(set_kilix_glyph_source, (), None, None)

    def render(self, text: str) -> list[bytes]:
        s = self.create_screen(cols=8, lines=1, scrollback=0)
        before = set(self.sprites)
        s.draw(text)
        test_render_line(s.line(0))
        return [self.sprites[k] for k in sorted(set(self.sprites) - before)]

    def test_listed_character_is_drawn_by_the_source(self) -> None:
        asked: list[int] = []
        drawn: list[tuple[int, int, int]] = []

        def can_draw(cp: int) -> bool:
            asked.append(cp)
            return cp != 0x1f312

        def draw(cp: int, width: int, height: int, cells: int) -> bytes:
            self.ae(cells, 1)
            drawn.append((cp, width, height))
            if cp == 0x1f313:
                return bytes(width * height)
            if cp == 0x1f314:
                return bytes([255]) * (width * height)
            return bytes(255 if x < width // 2 else 0 for _ in range(height) for x in range(width))

        set_kilix_glyph_source((0x25cf, 0x1f311, 0x1f312, 0x1f313, 0x1f314), can_draw, draw)
        empty = self.render('\U0001f313\ufe0e')[0]
        full = self.render('\U0001f314\ufe0e')[0]
        self.assertNotEqual(empty, full)
        del drawn[:]
        sprites = self.render('\U0001f311\ufe0e')
        self.ae(drawn, [(0x1f311, self.cell_width, self.cell_height)])
        self.ae(len(sprites), 1)
        # Left half fully covered, right half empty: exactly the mask handed over.
        pixel = len(full) // (self.cell_width * self.cell_height)
        row = self.cell_width * pixel
        half = (self.cell_width // 2) * pixel
        for y in range(self.cell_height):
            line = sprites[0][y * row:(y + 1) * row]
            self.ae(line[:half], full[y * row:y * row + half])
            self.ae(line[half:], empty[y * row + half:(y + 1) * row])
        # Asked once per character, however often it is drawn.
        self.render('\U0001f311\ufe0e\U0001f311\ufe0e')
        self.ae(asked.count(0x1f311), 1)
        self.ae(len(drawn), 1)
        # A listed character the source declines goes to the fonts as before.
        self.render('\U0001f312\ufe0e')
        self.ae(asked.count(0x1f312), 1)
        self.ae(len(drawn), 1)
        # An unlisted character is never asked about.
        self.render('ab\u25cb')
        self.ae(sorted(set(asked)), [0x1f311, 0x1f312, 0x1f313, 0x1f314])

    def test_colour_emoji_keeps_its_font(self) -> None:
        drawn: list[int] = []
        set_kilix_glyph_source((0x1f311,), lambda cp: True, lambda cp, w, h, cells: drawn.append(cp) or bytes(w * h))
        self.render('\U0001f311')          # emoji presentation: two cells, the emoji font
        self.ae(drawn, [])
        self.render('\U0001f311\ufe0e')    # text presentation: ours
        self.ae(drawn, [0x1f311])

    def test_bad_answers_fall_back_to_the_engine(self) -> None:
        set_kilix_glyph_source((0x25cf,), lambda cp: True, lambda cp, w, h, cells: b'short')
        engine = self.render('\u25cf')
        set_kilix_glyph_source((), None, None)
        self.ae(self.render('\u25cf \u25cf')[:0], [])    # still renders with the source removed
        self.assertTrue(engine and any(engine[0]))        # the engine's own dot, not an empty cell
        with self.assertRaises(ValueError):
            set_kilix_glyph_source((5, 5), None, None)
        with self.assertRaises(ValueError):
            set_kilix_glyph_source((9, 3), None, None)
        with self.assertRaises(ValueError):
            set_kilix_glyph_source((0x110000,), None, None)


    def test_two_cell_character_takes_the_blank_after_it(self) -> None:
        both, wide_only, narrow, nothing = 0xf0219, 0xf073, 0xf0156, 0xf0300
        asked: list[int] = []
        drawn: list[tuple[int, int, int, int]] = []
        answers = {both: 3, wide_only: 2, narrow: 1, nothing: 1}
        cw, ch = self.cell_width, self.cell_height

        def can_draw(cp: int) -> int:
            asked.append(cp)
            return answers[cp]

        def draw(cp: int, width: int, height: int, cells: int) -> bytes:
            drawn.append((cp, width, height, cells))
            if cells == 2:   # the first cell and two columns of the second
                return bytes(255 if x < cw + 2 else 0 for _ in range(height) for x in range(width))
            return bytes([0 if cp == nothing else 255]) * (width * height)

        def render(text: str) -> tuple[list[bytes], list[int]]:
            s = self.create_screen(cols=8, lines=1, scrollback=0)
            before = set(self.sprites)
            parse_bytes(s, ('\x1b[38;2;10;20;30m' + text[:1] + '\x1b[39m' + text[1:]).encode())
            test_render_line(s.line(0))
            line = s.line(0)
            return ([self.sprites[k] for k in sorted(set(self.sprites) - before)],
                    [line.cursor_from(x).fg for x in range(len(text))])

        set_kilix_glyph_source(tuple(sorted(answers)), can_draw, draw)
        full = render(chr(narrow))[0][0]
        self.ae(drawn, [(narrow, cw, ch, 1)])
        empty = render(chr(nothing))[0][0]
        self.assertNotEqual(full, empty)
        del drawn[:]
        # A blank follows: one drawing, twice the cell wide, cut into two cells.
        sprites, colours = render(chr(both) + ' x')
        self.ae(drawn, [(both, 2 * cw, ch, 2)])
        self.ae(len(sprites), 3)    # two halves and the x
        self.ae(sprites[0], full)
        pixel = len(full) // (cw * ch)
        for y in range(ch):
            row = sprites[1][y * cw * pixel:(y + 1) * cw * pixel]
            self.ae(row[:2 * pixel], full[:2 * pixel])
            self.ae(row[2 * pixel:], empty[2 * pixel:cw * pixel])
        # The blank cell is painted in the symbol's colour, like the cell after a wide font glyph.
        self.ae(colours[1], colours[0])
        self.assertNotEqual(colours[2], colours[0])
        # Drawn once, however often it is shown.
        render(chr(both) + ' ' + chr(both) + ' ')
        self.ae(len(drawn), 1)
        # No blank after it: the one-cell drawing.
        del drawn[:]
        sprites, colours = render(chr(both) + 'x')
        self.ae(drawn, [(both, cw, ch, 1)])       # a drawing of its own, not half of the wide one
        self.ae(sprites[0], full)
        self.assertNotEqual(colours[1], colours[0])
        for text in (chr(both), 'aaaaaaa' + chr(both), chr(both) + chr(both), chr(both) + '\u2600 '):
            self.ae(render(text)[0][:0], [])
        self.ae(drawn, [(both, cw, ch, 1)])       # nothing new was asked for: no blank followed
        self.ae(asked.count(both), 1)
        # An en-space counts as a blank, as it does for a wide font glyph.
        self.ae(len(render(chr(both) + '\u2002')[0]), 0)
        # A character with only a two-cell drawing: drawn where a blank follows, left to the fonts elsewhere.
        del drawn[:]
        render(chr(wide_only) + ' ')
        self.ae(drawn, [(wide_only, 2 * cw, ch, 2)])
        render(chr(wide_only) + 'x')
        self.ae(drawn, [(wide_only, 2 * cw, ch, 2)])
        # A character with only a one-cell drawing stays in its cell before a blank.
        del drawn[:]
        self.ae(render(chr(narrow) + ' ')[1][1:], render('a ')[1][1:])
        self.ae(drawn, [])
        self.ae(asked.count(narrow), 1)

    def test_a_refused_two_cell_drawing_falls_back_to_one_cell(self) -> None:
        drawn: list[int] = []

        def draw(cp: int, width: int, height: int, cells: int) -> bytes:
            drawn.append(cells)
            return b'short' if cells == 2 else bytes([255]) * (width * height)

        set_kilix_glyph_source((0xf0219,), lambda cp: 3, draw)
        s = self.create_screen(cols=8, lines=1, scrollback=0)
        s.draw('\U000f0219 ')
        test_render_line(s.line(0))
        self.ae(drawn, [2, 1])
        # Answers that are not the four bits mean "not drawn".
        # A longer list each time, and its last character: a drawing is kept by
        # its place in the list, and one already made is not asked for again.
        for count, answer in enumerate((16, 17, 19, 31, 255, -1, 2 ** 70, None, 0, 'yes', True), 2):
            del drawn[:]
            chars = tuple(range(0xf0300, 0xf0300 + count))
            cp = chars[-1]
            set_kilix_glyph_source(chars, lambda cp: answer, draw)
            s = self.create_screen(cols=8, lines=1, scrollback=0)
            s.draw(chr(cp) + ' ')
            test_render_line(s.line(0))
            self.ae(drawn, [1] if answer in ('yes', True) else [], repr(answer))

    def test_colour_emoji_is_not_drawn_two_cells_wide(self) -> None:
        drawn: list[int] = []
        set_kilix_glyph_source((0x2600, 0x26c5), lambda cp: 3, lambda cp, w, h, cells: drawn.append(cells) or bytes(w * h))
        s = self.create_screen(cols=8, lines=1, scrollback=0)
        s.draw('\u2600\ufe0f ')     # emoji presentation: the emoji font, two cells of its own
        test_render_line(s.line(0))
        self.ae(drawn, [])
        s = self.create_screen(cols=8, lines=1, scrollback=0)
        s.draw('\u2600\ufe0e ')     # text presentation and a blank: ours, two cells
        test_render_line(s.line(0))
        self.ae(drawn, [2])
        # An emoji that is wide by default and made narrow by the text selector: ours, two cells.
        del drawn[:]
        s = self.create_screen(cols=8, lines=1, scrollback=0)
        s.draw('\u26c5 ')            # as it comes: the emoji font
        test_render_line(s.line(0))
        self.ae(drawn, [])
        s = self.create_screen(cols=8, lines=1, scrollback=0)
        s.draw('\u26c5\ufe0e x')
        test_render_line(s.line(0))
        self.ae(drawn, [2])
        self.ae(str(s.line(0)), '\u26c5\ufe0e x')   # one cell, then the blank it is drawn into

    def test_some_two_cell_drawings_are_for_the_bars_only(self) -> None:
        drawn: list[tuple[int, int]] = []
        # One cell and two; the same, two cells on the bars only; only the bars' two cells; the bit by itself.
        anywhere, bars, bars_wide_only, nothing, narrow = 0xf0219, 0x2600, 0x2601, 0x26c8, 0x26c5
        answers = {anywhere: 3, bars: 7, bars_wide_only: 6, nothing: 4, narrow: 5}
        set_kilix_glyph_source(tuple(sorted(answers)), lambda cp: answers[cp],
                               lambda cp, w, h, cells: drawn.append((cp, cells)) or bytes([255]) * (w * h))

        def render(cp: int, on_a_bar: bool) -> tuple[list[tuple[int, int]], bool]:
            del drawn[:]
            s = self.create_screen(cols=8, lines=1, scrollback=0)
            parse_bytes(s, ('\x1b[38;2;10;20;30m' + chr(cp) + '\ufe0e\x1b[39m x').encode())
            test_render_line(s.line(0), on_a_bar)
            line = s.line(0)
            return list(drawn), line.cursor_from(1).fg == line.cursor_from(0).fg

        # In a window's text the character stays in its cell, and the blank after it is left alone.
        self.ae(render(bars, False), ([(bars, 1)], False))
        # On a bar it takes the blank.
        self.ae(render(bars, True), ([(bars, 2)], True))
        self.ae(render(bars, False), ([], False))            # both drawings are kept, each for its place
        self.ae(render(bars, True), ([], True))
        # Only a two-cell drawing: the fonts in a window, the drawing on a bar.
        # (What the fonts then do with the blank is theirs to decide: a wide font glyph may take it.)
        self.ae(render(bars_wide_only, False)[0], [])
        self.ae(render(bars_wide_only, True), ([(bars_wide_only, 2)], True))
        # The bars-only bit without a two-cell drawing means nothing.
        self.ae(render(nothing, True)[0], [])
        self.ae(render(narrow, True), ([(narrow, 1)], False))
        # A character that is two cells anywhere is two cells in both.
        self.ae(render(anywhere, False), ([(anywhere, 2)], True))
        self.ae(render(anywhere, True), ([], True))

    def test_colour_emoji_on_a_bar_is_drawn_in_colour_by_the_source(self) -> None:
        cloud, sun, rain, snow = 0x2601, 0x2600, 0x1f327, 0x1f328
        asked: list[tuple[int, int, int]] = []
        one_colour: list[int] = []
        cw, ch = self.cell_width, self.cell_height
        left, right = (200, 100, 50, 255), (10, 20, 30, 128)

        def draw_colour(cp: int, width: int, height: int) -> bytes:
            asked.append((cp, width, height))
            if cp == rain:
                return bytes(4 * width * height - 1)     # the wrong size
            if cp == snow:
                return None
            # Its first cell and two columns of the second in one colour, the rest in another.
            return b''.join(bytes(left if x < cw + 2 else right) for _ in range(height) for x in range(width))

        def render(text: str, on_a_bar: bool = True) -> tuple[list[bytes], list[int]]:
            s = self.create_screen(cols=8, lines=2, scrollback=0)
            before = set(self.sprites)
            parse_bytes(s, text.encode())
            test_render_line(s.line(0), on_a_bar)
            line = s.line(0)
            return [self.sprites[k] for k in sorted(set(self.sprites) - before)], [line.sprite_at(x) for x in range(3)]

        both = 0x26c5       # every form: one cell, two cells on the bars, and colour
        large = 0x26c8      # shown only at twice the size
        answers = {cloud: 8, sun: 7, rain: 8, snow: 8, both: 15, large: 8}
        set_kilix_glyph_source(tuple(sorted(answers)), lambda cp: answers[cp],
                               lambda cp, w, h, cells: one_colour.append(cp) or bytes(w * h), draw_colour)
        # In a window's text the emoji font draws it.
        font_sprites, font_cells = render('\u2601\ufe0fx', on_a_bar=False)
        self.ae(asked, [])
        self.ae(len(font_sprites), 3)
        # On a bar: one colour drawing, two cells wide, cut into the emoji's two cells.
        sprites, cells = render('\u2601\ufe0fx')
        self.ae(asked, [(cloud, 2 * cw, ch)])
        self.ae(len(sprites), 2)                                    # the x is already drawn
        self.assertNotIn(sprites[0], font_sprites)
        # A sprite pixel is red, green, blue, alpha from its high byte down, exactly as handed over.
        first, second = (struct.pack('=I', (r << 24) | (g << 16) | (b << 8) | a) for r, g, b, a in (left, right))
        self.ae(sprites[0][:4 * cw * ch], first * (cw * ch))
        self.ae(sprites[1][:4 * cw * ch], (first * 2 + second * (cw - 2)) * ch)
        # Both cells are marked as colour, so the text colour does not paint them; the x is not.
        self.ae([bool(c & 0x80000000) for c in cells], [True, True, False])
        self.assertNotEqual(cells[0], cells[1])
        self.ae(cells[2], font_cells[2])
        # Drawn once, however often it is shown; a window still gets the font's.
        self.ae(render('\u2601\ufe0f\u2601\ufe0f')[1][:2], cells[:2])
        self.ae(render('\u2601\ufe0fx', on_a_bar=False)[1], font_cells)
        self.ae(len(asked), 1)
        # The text form of the same character is not a colour emoji: the fonts' business.
        render('\u2601\ufe0e x')
        self.ae((len(asked), one_colour), (1, []))
        # A character with no colour drawing keeps the emoji font on a bar too.
        render('\u2600\ufe0fx')
        self.ae(len(asked), 1)
        # A drawing of the wrong size, or none: the emoji font, as in a window.
        for cp in (rain, snow):
            del asked[:]
            refused = render(chr(cp) + '\ufe0fx')
            self.ae(asked, [(cp, 2 * cw, ch)])
            self.ae(refused[1], render(chr(cp) + '\ufe0fx', on_a_bar=False)[1])
            self.assertTrue(refused[1][0] and refused[1][1])
        # A character with a one-colour two-cell drawing as well: each is asked for and kept apart.
        del asked[:]
        mono = render('\u26c5\ufe0e x')
        self.ae((asked, one_colour), ([], [both]))
        coloured = render('\u26c5\ufe0fx')
        self.ae((asked, one_colour), ([(both, 2 * cw, ch)], [both]))
        self.ae([bool(c & 0x80000000) for c in mono[1][:2] + coloured[1][:2]], [False, False, True, True])
        self.ae(render('\u26c5\ufe0e x')[1], mono[1])
        # An emoji of another size keeps its font. (One not drawn before: a drawing made is kept.)
        del asked[:]
        render('\x1b]66;s=2;\u26c8\ufe0f\x07')
        self.ae(asked, [])
        render('\u26c8\ufe0fx')
        self.ae(asked, [(large, 2 * cw, ch)])
        del asked[:]
        # Without the third callable nothing is drawn in colour.
        set_kilix_glyph_source(tuple(range(0x2601, 0x2609)), lambda cp: 8, lambda cp, w, h, cells: bytes(w * h))
        self.ae(render('\u2608\ufe0fx')[1][:0], [])
        self.ae(asked, [])

    def test_text_of_another_size_is_not_drawn_two_cells_wide(self) -> None:
        drawn: list[tuple[int, int, int]] = []
        set_kilix_glyph_source((0xf0219,), lambda cp: 3, lambda cp, w, h, cells: drawn.append((w, h, cells)) or bytes(w * h))
        s = self.create_screen(cols=8, lines=2, scrollback=0)
        # Half size in one cell, then a blank: the drawing for a two-cell box is not asked for.
        parse_bytes(s, '\x1b]66;n=1:d=2;\U000f0219\x07 x'.encode())
        test_render_line(s.line(0))
        self.assertTrue(drawn)
        self.ae({cells for _, _, cells in drawn}, {1})


class TestOutputClock(BaseTest):

    def test_output_time_and_the_report_after_quiet(self) -> None:
        s = self.create_screen()
        calls: list[int] = []
        answer = [True]

        def on_output_after_quiet() -> bool:
            calls.append(1)
            return answer[0]

        s.callbacks.on_output_after_quiet = on_output_after_quiet
        self.ae(s.kilix_last_output_at(), 0.0)
        parse_bytes(s, b'a')
        self.ae(len(calls), 1)
        first = s.kilix_last_output_at()
        self.assertGreater(first, 0.0)
        parse_bytes(s, b'b')            # inside the quiet second: stamped, not reported
        self.ae(len(calls), 1)
        second = s.kilix_last_output_at()
        self.assertGreaterEqual(second, first)
        parse_bytes(s, b'\x1b[H\x1b[2J')  # moving the cursor and erasing print nothing
        self.ae(s.kilix_last_output_at(), second)
        time.sleep(1.1)
        # False means "not the program working" (a redraw after a resize):
        # the time is put back and the next output is reported again.
        answer[0] = False
        parse_bytes(s, b'c')
        self.ae(len(calls), 2)
        self.ae(s.kilix_last_output_at(), second)
        answer[0] = True
        parse_bytes(s, b'd')
        self.ae(len(calls), 3)
        self.assertGreater(s.kilix_last_output_at(), second + 1.0)

    def test_a_redraw_after_a_resize_is_not_work(self) -> None:
        # The pane's side of the hook: the program repainting because its
        # window changed size has not started working, and says so (False),
        # which makes the screen put the time back.
        from kitty.fast_data_types import monotonic
        from kitty.window import Window

        class Pane:
            last_resized_at = 0.0

        pane = Pane()
        seen: list[object] = []
        with patch.object(status, 'note_output', seen.append):
            pane.last_resized_at = monotonic()
            self.assertIs(Window.on_output_after_quiet(pane), False)  # type: ignore[arg-type]
            self.ae(seen, [])
            pane.last_resized_at = monotonic() - 2.0
            self.assertIs(Window.on_output_after_quiet(pane), True)  # type: ignore[arg-type]
            self.ae(seen, [pane])

    def test_a_screen_without_the_hook_still_draws(self) -> None:
        s = self.create_screen()
        self.assertFalse(hasattr(s.callbacks, 'on_output_after_quiet'))
        parse_bytes(s, b'abc')
        self.ae(str(s.line(0)), 'abc')
        self.assertGreater(s.kilix_last_output_at(), 0.0)

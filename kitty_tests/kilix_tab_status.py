#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kilix

import json
import os
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
        self.ae(native_window_layout(one, 14, 3, 60), NativeWindowLayout(14, 16, 1, 0, 16))
        # A short title is not padded out to the configured width.
        self.ae(native_window_layout(self.entries(('xterm',)), 14, 3, 60), NativeWindowLayout(14, 7, 1, 0, 7))
        three = self.entries(('alpha window', 'beta', 'gamma window title'))
        self.ae(native_window_layout(three, 14, 3, 60), NativeWindowLayout(14, 16, 3, 0, 54))
        # Too narrow for all three at 14: the labels shorten two cells at a time.
        self.ae(native_window_layout(three, 14, 3, 50), NativeWindowLayout(12, 14, 3, 0, 48))
        # Too narrow even at six: show what fits and count the rest.
        self.ae(native_window_layout(three, 14, 3, 20), NativeWindowLayout(6, 8, 2, 1, 19))
        self.ae(native_window_layout(three, 14, 3, 22), NativeWindowLayout(6, 8, 2, 1, 22))
        self.ae(native_window_layout(three, 14, 3, 12), NativeWindowLayout(6, 8, 1, 2, 11))
        self.ae(native_window_layout(three, 14, 3, 5), NativeWindowLayout(6, 8, 0, 3, 0))
        self.ae(native_window_layout(lambda width=18: (), 14, 3, 60), NativeWindowLayout(0, 0, 0, 0, 0))

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

        def draw(cp: int, width: int, height: int) -> bytes:
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
        set_kilix_glyph_source((0x1f311,), lambda cp: True, lambda cp, w, h: drawn.append(cp) or bytes(w * h))
        self.render('\U0001f311')          # emoji presentation: two cells, the emoji font
        self.ae(drawn, [])
        self.render('\U0001f311\ufe0e')    # text presentation: ours
        self.ae(drawn, [0x1f311])

    def test_bad_answers_fall_back_to_the_engine(self) -> None:
        set_kilix_glyph_source((0x25cf,), lambda cp: True, lambda cp, w, h: b'short')
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

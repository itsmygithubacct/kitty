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
                    ('KILIX_NEURONTE_WEATHER=on\n', None, sun),              # no nets: nothing to switch to
                    ('KILIX_NEURONTE_WEATHER=on\n', Whole(None), '\u2600\ufe0e'),
                    ('KILIX_NEURONTE_WEATHER=on\n', Wide(None), '\u2600\ufe0e '),  # with the blank it is drawn into
                ):
                    with open(settings + '.new', 'w') as f:
                        f.write(text)
                    os.replace(settings + '.new', settings)
                    with patch.object(neuronte, '_source', source):
                        self.ae(neuronte.weather_symbol(sun), expected, (text, source))

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
        # Answers that are not the two bits mean "not drawn".
        # A longer list each time, and its last character: a drawing is kept by
        # its place in the list, and one already made is not asked for again.
        for count, answer in enumerate((4, 5, 6, 7, -1, 2 ** 70, None, 0, 'yes', True), 2):
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

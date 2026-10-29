import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kitty import kilix_system_load as system
from kitty.kilix_memory import GIB, MEMORY_GLYPH, MEMORY_WIDGET_ACTION
from kitty.tab_bar import MAX_TABS_PER_ROW, TabBarData, split_tab_bar_rows
from kitty.tabs import TabManager

from . import BaseTest


def write_proc(root: Path, user: int, idle: int, cpus: int = 4,
               total_kib: int = 16 * 1024 * 1024, available_kib: int = 4 * 1024 * 1024,
               iowait: int = 0) -> None:
    lines = [f'cpu  {user} 0 0 {idle} {iowait} 0 0 0 0 0']
    lines += [f'cpu{i} 1 0 0 1 0 0 0 0 0 0' for i in range(cpus)]
    lines.append('intr 0')
    (root / 'stat').write_text('\n'.join(lines) + '\n')
    (root / 'meminfo').write_text(
        f'MemTotal:       {total_kib} kB\nMemFree:          1024 kB\n'
        f'MemAvailable:   {available_kib} kB\n')


class TestSystemLoad(BaseTest):

    def setUp(self) -> None:
        super().setUp()
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            'KILIX_SYSTEM_PROC_ROOT': self.tmp.name,
            'GPU_TERMINAL_SETTINGS_FILE': os.path.join(self.tmp.name, 'missing.conf'),
        })
        self.env.start()
        system._PREVIOUS_TIMES = None
        system._CACHE_UNTIL = 0.0
        system._DRAWN_TEXT.clear()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()
        system._PREVIOUS_TIMES = None
        system._CACHE_UNTIL = 0.0
        system._DRAWN_TEXT.clear()
        super().tearDown()

    def test_cpu_cores_come_from_the_delta_between_samples(self) -> None:
        write_proc(self.root, user=100, idle=800, iowait=100)
        first = system.system_load(force=True)
        self.assertIsNone(first.cpu_cores)
        self.ae(first.memory_used_bytes, 12 * GIB)
        # 300 busy jiffies out of 400 across 4 logical CPUs is 3 cores; time
        # waiting on I/O is idle, not load.
        write_proc(self.root, user=400, idle=850, iowait=150)
        second = system.system_load(force=True)
        self.assertAlmostEqual(second.cpu_cores, 3.0)

    def test_steal_is_load_and_guest_is_not_counted_twice(self) -> None:
        # user nice system idle iowait irq softirq steal guest guest_nice
        (self.root / 'meminfo').write_text('MemTotal: 1024 kB\nMemAvailable: 512 kB\n')
        cpus = ''.join(f'cpu{i} 1 0 0 1 0 0 0 0 0 0\n' for i in range(2))
        (self.root / 'stat').write_text('cpu  100 0 0 900 0 0 0 0 0 0\n' + cpus)
        system.system_load(force=True)
        # 100 user (of which 100 guest, already inside user), 100 steal and
        # 200 idle: 200 busy of 400 is half of 2 CPUs.
        (self.root / 'stat').write_text('cpu  200 0 0 1100 0 0 0 100 100 0\n' + cpus)
        self.assertAlmostEqual(system.system_load(force=True).cpu_cores, 1.0)

    def test_counter_reset_reads_as_idle_not_negative(self) -> None:
        write_proc(self.root, user=400, idle=100)
        system.system_load(force=True)
        # user went backwards while idle advanced past the total delta.
        write_proc(self.root, user=100, idle=500)
        self.ae(system.system_load(force=True).cpu_cores, 0.0)

    def test_cache_holds_between_refreshes(self) -> None:
        write_proc(self.root, user=100, idle=900)
        system.system_load(force=True)
        write_proc(self.root, user=400, idle=1000, available_kib=8 * 1024 * 1024)
        self.ae(system.system_load().memory_used_bytes, 12 * GIB)
        self.ae(system.system_load(force=True).memory_used_bytes, 8 * GIB)

    def test_unreadable_proc_reports_nothing(self) -> None:
        load = system.system_load(force=True)
        self.ae(load, system.SystemLoad(None, None))
        self.ae(system.format_system_load(load, 'always'), '')
        (self.root / 'meminfo').write_text('MemTotal: 1024 kB\n')
        self.assertIsNone(system.system_load(force=True).memory_used_bytes)

    def test_modes_match_the_pane_chip(self) -> None:
        busy = system.SystemLoad(6.44, 12 * GIB + GIB // 10 * 3)
        self.ae(system.format_system_load(busy, 'always'), f' 6.4 {MEMORY_GLYPH} 12.3 ')
        self.ae(system.format_system_load(busy, 'auto'), f' 6.4 {MEMORY_GLYPH} 12.3 ')
        self.ae(system.format_system_load(busy, 'off'), '')
        quiet = system.SystemLoad(0.5, 512 * 1024 * 1024)
        self.ae(system.format_system_load(quiet, 'always'), f' 0.5 {MEMORY_GLYPH} 512M ')
        self.ae(system.format_system_load(quiet, 'auto'), '')
        # Before the second CPU sample, RAM shows on its own.
        self.ae(system.format_system_load(system.SystemLoad(None, 2 * GIB), 'always'),
                f' {MEMORY_GLYPH} 2.0 ')
        self.ae(system.format_system_load(busy, 'bogus'), f' 6.4 {MEMORY_GLYPH} 12.3 ')

    def test_mode_setting_defaults_to_always(self) -> None:
        self.ae(system.system_load_mode(), 'always')
        for value, expected in (('off', 'off'), ('AUTO', 'auto'), ('nonsense', 'always')):
            with patch.dict(os.environ, {system.SYSTEM_LOAD_MODE_KEY: value}):
                self.ae(system.system_load_mode(), expected)

    def test_segment_is_the_clickable_memory_chip(self) -> None:
        write_proc(self.root, user=100, idle=900)
        text, action, _ = system.system_load_segment()
        self.ae(text, f' {MEMORY_GLYPH} 12.0 ')
        self.ae(action, MEMORY_WIDGET_ACTION)
        with patch.dict(os.environ, {system.SYSTEM_LOAD_MODE_KEY: 'off'}):
            self.assertIsNone(system.system_load_segment())

    def test_timer_redraws_only_tab_bars_and_only_on_change(self) -> None:
        tab = MagicMock()
        manager = MagicMock()
        manager.os_window_id = 7
        manager.__iter__.side_effect = lambda: iter([tab])
        boss = MagicMock(all_tab_managers=[manager])
        write_proc(self.root, user=100, idle=900)
        system.system_load_segment(7)
        with patch('kitty.fast_data_types.get_boss', return_value=boss), \
                patch('kitty.fast_data_types.mark_os_window_dirty') as dirty:
            write_proc(self.root, user=400, idle=1000)
            system._system_load_timer()
            manager.mark_tab_bar_dirty.assert_called_once_with()
            dirty.assert_called_once_with(7)
            # Pane title bars do not show the system readout; redrawing them
            # every refresh would be wasted work.
            tab.update_window_title_bars.assert_not_called()
            system.system_load_segment(7)
            manager.mark_tab_bar_dirty.reset_mock()
            system._CACHE_UNTIL = 0.0
            write_proc(self.root, user=700, idle=1100)
            system._system_load_timer()
            manager.mark_tab_bar_dirty.assert_not_called()

    def test_each_os_window_is_refreshed_from_what_it_drew(self) -> None:
        # 9 comes first so waking it cannot hide behind the others being done.
        managers = {i: MagicMock(os_window_id=i) for i in (9, 7, 8)}
        boss = MagicMock(all_tab_managers=list(managers.values()))
        write_proc(self.root, user=100, idle=900)
        system.system_load_segment(7)
        system.system_load_segment(8)
        with patch('kitty.fast_data_types.get_boss', return_value=boss), \
                patch('kitty.fast_data_types.mark_os_window_dirty') as dirty:
            write_proc(self.root, user=400, idle=1000)
            system._system_load_timer()
            # 9 never drew the segment (hidden, vertical or fullscreen bar).
            self.ae(sorted(c.args[0] for c in dirty.call_args_list), [7, 8])
            managers[9].mark_tab_bar_dirty.assert_not_called()
            # Only 7 redraws; 8 stays hidden. Another OS window having drawn
            # the new text must not stop 7 seeing the next change, and 8 is not
            # woken again until it draws.
            system.system_load_segment(7)
            dirty.reset_mock()
            system._CACHE_UNTIL = 0.0
            write_proc(self.root, user=700, idle=1050)
            system._system_load_timer()
            self.ae([c.args[0] for c in dirty.call_args_list], [7])
            # 8 draws stale-free text, and a later unchanged refresh leaves both alone.
            system.system_load_segment(7)
            system.system_load_segment(8)
            dirty.reset_mock()
            with patch.object(system, 'format_system_load', return_value=system._DRAWN_TEXT[7]):
                system._system_load_timer()
            dirty.assert_not_called()

    def test_turning_the_mode_off_clears_drawn_readouts(self) -> None:
        manager = MagicMock(os_window_id=7)
        boss = MagicMock(all_tab_managers=[manager])
        write_proc(self.root, user=100, idle=900)
        system.system_load_segment(7)
        with patch('kitty.fast_data_types.get_boss', return_value=boss), \
                patch('kitty.fast_data_types.mark_os_window_dirty') as dirty, \
                patch.dict(os.environ, {system.SYSTEM_LOAD_MODE_KEY: 'off'}):
            system._system_load_timer()
        dirty.assert_called_once_with(7)

    def test_windows_hides_the_linux_proc_readout(self) -> None:
        with patch('sys.platform', 'win32'):
            self.ae(system.system_load_mode(), 'off')
            self.assertIsNone(system.system_load_segment())

    def test_chrome_lifecycle_starts_the_timer(self) -> None:
        from kitty.kilix_chrome import lifecycle
        with patch('kitty.kilix_system_load.ensure_system_load_timer') as start, \
                patch('kitty.kilix_battery.ensure_battery_timer'), \
                patch('kitty.kilix_battery.ensure_thermal_timer'), \
                patch('kitty.kilix_memory.ensure_pane_memory_timer'), \
                patch('kitty.kilix_windows.ensure_windows_timer'), \
                patch('kitty.kilix_chrome.weather.ensure_weather_timer'), \
                patch.object(lifecycle, 'ensure_timer'), \
                patch.object(lifecycle, 'ensure_clock_timer'):
            lifecycle.ensure_chrome_timers()
        start.assert_called_once_with()

    def test_off_never_starts_the_timer(self) -> None:
        with patch.dict(os.environ, {system.SYSTEM_LOAD_MODE_KEY: 'off'}), \
                patch.object(system, '_TIMER_STARTED', False), \
                patch('kitty.fast_data_types.add_timer') as add_timer:
            system.ensure_system_load_timer()
            add_timer.assert_not_called()


class TestTabRows(BaseTest):

    def tabs(self, count: int, new_tab_button: bool = True) -> tuple[TabBarData, ...]:
        ans = tuple(TabBarData(title=str(i), tab_id=i + 1) for i in range(count))
        return ans + ((TabBarData(title='+'),) if new_tab_button else ())

    def shape(self, rows: tuple[tuple[TabBarData, ...], ...]) -> list[int]:
        return [sum(1 for t in row if t.tab_id >= 0) for row in rows]

    def test_ten_tabs_per_row(self) -> None:
        self.ae(MAX_TABS_PER_ROW, 10)
        for count, expected in ((1, [1]), (10, [10]), (11, [10, 1]), (20, [10, 10]),
                                (21, [10, 10, 1]), (35, [10, 10, 10, 5])):
            for button in (True, False):
                with self.subTest(count=count, button=button):
                    rows = split_tab_bar_rows(self.tabs(count, button))
                    self.ae(self.shape(rows), expected)

    def test_new_tab_button_ends_the_last_row_without_starting_one(self) -> None:
        for count in (9, 10, 20):
            rows = split_tab_bar_rows(self.tabs(count))
            self.ae(rows[-1][-1].title, '+')
            self.ae(len(rows), (count + 9) // 10)
        self.ae(split_tab_bar_rows((TabBarData(title='+'),)), ((TabBarData(title='+'),),))
        self.ae(split_tab_bar_rows(()), ((),))

    def manager(self, data: tuple[TabBarData, ...], vertical: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            os_window_id=5, tab_bar_data=data, tabs=(),
            tab_bar=MagicMock(is_vertical=vertical, **{'update.return_value': False}),
            resize=MagicMock())

    def test_bar_height_follows_the_rows_actually_drawn(self) -> None:
        # state.c sizes the bar from this report, so a tab dragged out of an
        # eleven-tab window (ten drawn) or a drop placeholder in a ten-tab
        # window (eleven drawn) gets the height of the rows on screen.
        for count, rows in ((10, 1), (11, 2), (20, 2), (21, 3)):
            with self.subTest(count=count), patch('kitty.tabs.set_tab_bar_rows', return_value=True) as report:
                tm = self.manager(self.tabs(count))
                TabManager.update_tab_bar_data(tm)
                report.assert_called_once_with(5, rows)
                tm.tab_bar.layout.assert_called_once_with()
                tm.resize.assert_called_once_with(only_tabs=True)
                tm.tab_bar.update.assert_called_once_with(tm.tab_bar_data)

    def test_unchanged_rows_do_not_relayout(self) -> None:
        with patch('kitty.tabs.set_tab_bar_rows', return_value=False) as report:
            tm = self.manager(self.tabs(11))
            TabManager.update_tab_bar_data(tm)
        report.assert_called_once_with(5, 2)
        tm.tab_bar.layout.assert_not_called()
        tm.resize.assert_not_called()
        with patch('kitty.tabs.set_tab_bar_rows') as report:
            tm = self.manager(self.tabs(11), vertical=True)
            TabManager.update_tab_bar_data(tm)
        report.assert_not_called()

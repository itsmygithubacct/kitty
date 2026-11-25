#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kilix
"""The tab status mark: a moon that turns while a tab works, a dot while it rests.

Each tab shows one mark before its title. While a program in the tab is
working the mark is a moon that steps through its eight phases; when the tab
has gone quiet it is a still dot. Its colour is the tab's load: the CPU its
panes' process trees are using, in cores.

"Working" is either of two signals, because neither covers the other:

* a pane printed within the last ``KILIX_TAB_STATUS_IDLE_AFTER_SECONDS``
  (a build that prints, an agent that streams); or
* the tab's process trees used at least ``KILIX_TAB_STATUS_ACTIVE_CORES`` of a
  core in the last sample (a build that prints nothing for a minute).

Keyboard focus is not a signal. The time a tab has been idle is counted from
the later of those two.

Costs. Nothing here polls an idle terminal. The engine reports the first
output after a quiet second (``Window.on_output_after_quiet``); the load comes
from the pane sampler that the title-bar chips already run, one pass over
/proc every four seconds for every pane together. The step timer exists only
while at least one tab is working, and is removed when the last one rests.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from .kilix_chrome.popup import TAB_STATUS_ACTION
from .kilix_chrome.settings import chrome_value, signature

if TYPE_CHECKING:
    from .tabs import Tab, TabManager
    from .window import Window

ENABLED_KEY = 'KILIX_TAB_STATUS'
MODERATE_KEY = 'KILIX_TAB_STATUS_MODERATE_CORES'
HIGH_KEY = 'KILIX_TAB_STATUS_HIGH_CORES'
ACTIVE_KEY = 'KILIX_TAB_STATUS_ACTIVE_CORES'
IDLE_AFTER_KEY = 'KILIX_TAB_STATUS_IDLE_AFTER_SECONDS'
STEP_KEY = 'KILIX_TAB_STATUS_STEP_SECONDS'
SOFTWARE_STEP_KEY = 'KILIX_TAB_STATUS_SOFTWARE_STEP_SECONDS'
LOW_COLOR_KEY = 'KILIX_TAB_STATUS_LOW_COLOR'
MODERATE_COLOR_KEY = 'KILIX_TAB_STATUS_MODERATE_COLOR'
HIGH_COLOR_KEY = 'KILIX_TAB_STATUS_HIGH_COLOR'

MODERATE_DEFAULT = '1.0'      # cores: where a pane's own CPU chip appears
HIGH_DEFAULT = '50%'          # of the machine's logical CPUs, never below 2 cores
HIGH_FLOOR_CORES = 2.0
ACTIVE_DEFAULT = '0.10'       # cores
IDLE_AFTER_DEFAULT = '5'      # seconds
STEP_DEFAULT = '1'            # seconds between moon phases
STEP_MINIMUM = 0.25
# Each step draws one frame. With no graphics card (most virtual machines) a
# frame is CPU time, about a quarter of a second of it for a full-screen
# window, so there the moon steps no faster than this.
SOFTWARE_STEP_DEFAULT = '4'
LOAD_SAMPLE_GRACE = 6.0       # seconds: one and a half pane samples
LOW_COLOR_DEFAULT = '#8ae234'       # Tango chameleon
MODERATE_COLOR_DEFAULT = '#fce94f'  # Tango butter
HIGH_COLOR_DEFAULT = '#ef2929'      # Tango scarlet red

LEVELS = ('low', 'moderate', 'high')
TEXT_PRESENTATION = '\ufe0e'  # one cell, one colour: not the colour emoji
MOON_PHASES = tuple(chr(0x1f311 + i) + TEXT_PRESENTATION for i in range(8))
IDLE_DOT = '\u25cf'  # drawn smaller than a moon's disc, so a still frame tells them apart
# A bell is a different shape from both marks, so it cannot be read as load.
BELL_MARK = '\U0001f56d'



class Settings(NamedTuple):
    enabled: bool
    moderate_cores: float
    high_cores: float
    active_cores: float
    idle_after: float
    step: float
    software_step: float
    colors: tuple[str, str, str]  # low, moderate, high as #rrggbb


def _number(text: str, default: str, minimum: float = 0.0) -> float:
    for candidate in (text, default):
        try:
            value = float(candidate.strip())
        except ValueError:
            continue
        if value == value and value not in (float('inf'), float('-inf')) and value >= minimum:
            return value
    return float(default)


def parse_high_cores(text: str, moderate_cores: float, cpu_count: int) -> float:
    """A number of cores, or NN% of the logical CPUs (never below two cores)."""
    cpus = max(1, int(cpu_count))
    text = text.strip()
    value: float | None = None
    if text.endswith('%'):
        try:
            percent = float(text[:-1].strip())
        except ValueError:
            percent = -1.0
        if 0.0 < percent <= 100.0:
            value = max(HIGH_FLOOR_CORES, cpus * percent / 100.0)
    else:
        try:
            value = float(text)
        except ValueError:
            value = None
        if value is not None and not (0.0 < value < float('inf')):
            value = None
    if value is None:
        value = max(HIGH_FLOOR_CORES, cpus * float(HIGH_DEFAULT[:-1]) / 100.0)
    # high is never below moderate: an inverted pair would hide "moderate".
    return max(value, moderate_cores)


def _color(text: str, default: str) -> str:
    text = text.strip().lower()
    if len(text) == 7 and text[0] == '#' and all(c in '0123456789abcdef' for c in text[1:]):
        return text
    return default


def parse_settings(get: Any = chrome_value, cpu_count: int | None = None) -> Settings:
    moderate = _number(get(MODERATE_KEY, MODERATE_DEFAULT), MODERATE_DEFAULT, 0.01)
    return Settings(
        enabled=get(ENABLED_KEY, '1').strip().lower() not in ('', '0', 'no', 'false', 'off', 'disabled'),
        moderate_cores=moderate,
        high_cores=parse_high_cores(
            get(HIGH_KEY, HIGH_DEFAULT), moderate, os.cpu_count() or 1 if cpu_count is None else cpu_count),
        active_cores=_number(get(ACTIVE_KEY, ACTIVE_DEFAULT), ACTIVE_DEFAULT, 0.0),
        idle_after=_number(get(IDLE_AFTER_KEY, IDLE_AFTER_DEFAULT), IDLE_AFTER_DEFAULT, 1.0),
        step=_number(get(STEP_KEY, STEP_DEFAULT), STEP_DEFAULT, STEP_MINIMUM),
        software_step=_number(get(SOFTWARE_STEP_KEY, SOFTWARE_STEP_DEFAULT), SOFTWARE_STEP_DEFAULT, STEP_MINIMUM),
        colors=(
            _color(get(LOW_COLOR_KEY, LOW_COLOR_DEFAULT), LOW_COLOR_DEFAULT),
            _color(get(MODERATE_COLOR_KEY, MODERATE_COLOR_DEFAULT), MODERATE_COLOR_DEFAULT),
            _color(get(HIGH_COLOR_KEY, HIGH_COLOR_DEFAULT), HIGH_COLOR_DEFAULT),
        ),
    )


_settings_cache: tuple[tuple[object, ...], Settings] | None = None


def settings() -> Settings:
    """The parsed settings, re-read only when the settings file changes."""
    global _settings_cache
    current = signature()
    if _settings_cache is None or _settings_cache[0] != current:
        _settings_cache = (current, parse_settings())
    return _settings_cache[1]


def classify_load(cores: float, moderate_cores: float, high_cores: float) -> str:
    """low below ``moderate_cores``, high from ``high_cores``, moderate between."""
    if not cores == cores or cores < moderate_cores:  # NaN reads as no load
        return 'low'
    return 'high' if cores >= high_cores else 'moderate'


def moon_phase(step: int) -> str:
    return MOON_PHASES[step % len(MOON_PHASES)]


def step_interval(conf: Settings, software_rendering: bool) -> float:
    """Seconds between moon phases: never faster than software_step when the CPU draws the screen."""
    return max(conf.step, conf.software_step) if software_rendering else conf.step


def format_duration(seconds: float) -> str:
    """Whole units, two at most: 42 s, 12 min 5 s, 3 h 12 min, 2 d 3 h."""
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f'{days} d {hours} h'
    if hours:
        return f'{hours} h {minutes} min'
    if minutes:
        return f'{minutes} min {secs} s'
    return f'{secs} s'


@dataclass
class TabState:
    busy_at: float            # monotonic seconds: last output, or last sample at or above the floor
    load_at: float = float('-inf')  # last sample at or above the floor
    working: bool = False
    cores: float = 0.0
    level: str = 'low'


class TabClock:
    """Which tabs are working, how loaded they are and since when they rest.

    Holds no reference to the terminal: the caller passes times in, which is
    what lets the tests drive it with a hand-written clock.
    """

    def __init__(self) -> None:
        self.tabs: dict[int, TabState] = {}
        self.step = 0

    def state(self, tab_id: int, now: float) -> TabState:
        state = self.tabs.get(tab_id)
        if state is None:
            # A new tab has done nothing yet: it has been idle since it opened.
            state = self.tabs[tab_id] = TabState(busy_at=now)
        return state

    def note_output(self, tab_id: int, now: float) -> bool:
        """A pane printed. True when the tab went from resting to working."""
        state = self.state(tab_id, now)
        state.busy_at = max(state.busy_at, now)
        started = not state.working
        state.working = True
        return started

    def note_load(self, tab_id: int, cores: float, now: float, conf: Settings) -> bool:
        """A fresh CPU sample. True when the mark must be redrawn."""
        state = self.state(tab_id, now)
        level = classify_load(cores, conf.moderate_cores, conf.high_cores)
        changed = level != state.level
        state.cores, state.level = cores, level
        if cores >= conf.active_cores and cores > 0.0:
            state.busy_at = max(state.busy_at, now)
            state.load_at = now
            if not state.working:
                state.working = changed = True
        return changed

    def settle(self, tab_id: int, last_output_at: float, now: float, conf: Settings) -> bool:
        """Rest a working tab once it has been quiet long enough. True if it rested."""
        state = self.state(tab_id, now)
        state.busy_at = max(state.busy_at, min(last_output_at, now))
        # Load arrives once per sample: a tab kept working by its CPU alone
        # must outlast the wait for the next sample, however short idle_after is.
        if state.working and now - state.busy_at >= conf.idle_after and (
                now - state.load_at >= max(conf.idle_after, LOAD_SAMPLE_GRACE)):
            state.working = False
            return True
        return False

    def idle_seconds(self, tab_id: int, now: float) -> float | None:
        """How long the tab has rested; None while it is working."""
        state = self.state(tab_id, now)
        return None if state.working else max(0.0, now - state.busy_at)

    def any_working(self) -> bool:
        return any(state.working for state in self.tabs.values())

    def forget_all_but(self, tab_ids: set[int]) -> None:
        for tab_id in tuple(self.tabs):
            if tab_id not in tab_ids:
                del self.tabs[tab_id]


def template_fields(clock: TabClock, tab_id: int, now: float, conf: Settings) -> dict[str, Any]:
    """What the tab title template may use. Empty strings when turned off."""
    if not conf.enabled or tab_id < 0:
        return {'status': '', 'status_symbol': '', 'status_color': '', 'load_level': '', 'tab_working': False,
                'tab_idle_seconds': 0, 'tab_load_cores': 0.0}
    state = clock.state(tab_id, now)
    symbol = moon_phase(clock.step) if state.working else IDLE_DOT
    red, green, blue = (int(conf.colors[LEVELS.index(state.level)][i:i + 2], 16) for i in (1, 3, 5))
    color = f'\x1b[38:2:{red}:{green}:{blue}m'
    idle = clock.idle_seconds(tab_id, now)
    return {
        'status': f'{color}{symbol} ',
        'status_symbol': symbol,
        'status_color': color,
        'load_level': state.level,
        'tab_working': state.working,
        'tab_idle_seconds': 0 if idle is None else int(idle),
        'tab_load_cores': state.cores,
    }


def card_payload(clock: TabClock, tab_id: int, index: int, title: str, now: float, wall_now: float,
                 conf: Settings, cpu_count: int | None = None) -> dict[str, Any]:
    """What the right-click card shows, as plain data for the card's process."""
    state = clock.state(tab_id, now)
    idle = clock.idle_seconds(tab_id, now)
    return {
        'index': index, 'title': title, 'working': state.working,
        # Wall-clock time the tab went quiet, so the card can keep counting.
        'idle_since': None if idle is None else wall_now - idle,
        'cores': round(state.cores, 2), 'level': state.level,
        'cpus': os.cpu_count() or 1 if cpu_count is None else cpu_count,
    }


def card_lines(payload: dict[str, Any], wall_now: float) -> tuple[str, ...]:
    """The card's text. Pure, so the kitten and the tests share it."""
    title = str(payload.get('title', ''))
    head = f"Tab {payload.get('index', '?')}: {title}" if title else f"Tab {payload.get('index', '?')}"
    cores = float(payload.get('cores', 0.0))
    load = f"Load {cores:.1f} of {payload.get('cpus', 1)} cores \u00b7 {payload.get('level', 'low')}"
    since = payload.get('idle_since')
    if payload.get('working') or since is None:
        return (head, '', 'Working', load, '', 'Esc close')
    stamp = time.strftime('%H:%M:%S', time.localtime(float(since)))
    return (head, '', f'Idle for {format_duration(wall_now - float(since))}', f'since {stamp}', load, '', 'Esc close')


# Wiring into the running terminal {{{

clock = TabClock()
_timer_id: int | None = None
_timer_interval = 0.0


def _is_pane(window: Window) -> bool:
    # A chrome card is Kilix's own surface: its output is not the tab working.
    return getattr(window, 'kilix_popup', None) is None


def _redraw(managers: Any) -> None:
    from .fast_data_types import mark_os_window_dirty, wakeup_main_loop
    seen = False
    for manager in managers:
        manager.mark_tab_bar_dirty()
        mark_os_window_dirty(manager.os_window_id)
        seen = True
    if seen:
        # Timers fire inside the event wait, which does not draw by itself.
        wakeup_main_loop()


def _stop_timer() -> None:
    global _timer_id
    if _timer_id is not None:
        from .fast_data_types import remove_timer
        try:
            remove_timer(_timer_id)
        except Exception:
            pass
        _timer_id = None


def _ensure_timer(conf: Settings) -> None:
    global _timer_id, _timer_interval
    try:
        from .fast_data_types import kilix_software_rendering
        interval = step_interval(conf, kilix_software_rendering())
    except Exception:
        interval = conf.step
    if _timer_id is not None and _timer_interval == interval:
        return
    _stop_timer()
    try:
        from .fast_data_types import add_timer
        _timer_id = add_timer(_tick, interval, True)
        _timer_interval = interval
    except Exception as error:
        from .utils import log_error
        log_error(f'Failed to start the kilix tab status timer: {error}')


def _tick(timer_id: int | None = None) -> None:
    """Step the moon and rest the tabs that went quiet. Runs only while one works."""
    del timer_id
    from .fast_data_types import get_boss, monotonic
    conf = settings()
    try:
        boss = get_boss()
    except Exception:
        return
    now = monotonic()
    dirty = []
    live: set[int] = set()
    for manager in boss.all_tab_managers:
        manager_dirty = False
        for tab in manager:
            live.add(tab.id)
            state = clock.state(tab.id, now)
            if not state.working:
                continue
            last = max((w.screen.kilix_last_output_at() for w in tab if _is_pane(w)), default=0.0)
            clock.settle(tab.id, last, now, conf)
            manager_dirty = True  # it stepped, or it has just come to rest
        if manager_dirty:
            dirty.append(manager)
    clock.forget_all_but(live)
    if not conf.enabled or not clock.any_working():
        _stop_timer()
    else:
        clock.step += 1
        _ensure_timer(conf)  # picks up a changed step interval
    _redraw(dirty)


def card_open_for(tab: Tab, tab_id: int) -> bool:
    """Is the idle card for ``tab_id`` showing in ``tab`` (the tab in view)?"""
    from .kilix_chrome.popup import current_popup
    window = current_popup(tab)
    popup = None if window is None else window.kilix_popup
    return popup is not None and popup.action == f'{TAB_STATUS_ACTION}:{tab_id}'


def _close_card_for(tab_id: int) -> None:
    """A tab that starts working takes down a card that still says it is idle."""
    from .fast_data_types import get_boss
    from .kilix_chrome.popup import dismiss_popup
    try:
        managers = tuple(get_boss().all_tab_managers)
    except Exception:
        return
    for manager in managers:
        active = manager.active_tab
        if active is not None and card_open_for(active, tab_id):
            dismiss_popup(active)


def note_output(window: Window) -> None:
    """The engine's report of the first output after a quiet second."""
    if not _is_pane(window):
        return
    conf = settings()
    if not conf.enabled:
        return
    tab = window.tabref()
    if tab is None:
        return
    from .fast_data_types import monotonic
    if clock.note_output(tab.id, monotonic()):
        tab.mark_tab_bar_dirty()
        _close_card_for(tab.id)
    _ensure_timer(conf)


def note_loads(pane_cores: Any) -> None:
    """Called by the pane sampler after its pass. pane_cores(pid) -> cores or None."""
    conf = settings()
    if not conf.enabled:
        return
    from .fast_data_types import get_boss, monotonic
    try:
        boss = get_boss()
    except Exception:
        return
    now = monotonic()
    dirty = []
    for manager in boss.all_tab_managers:
        manager_dirty = False
        for tab in manager:
            cores = 0.0
            for window in tab:
                if not _is_pane(window):
                    continue
                pid = getattr(getattr(window, 'child', None), 'process_tree_root_pid', 0)
                value = pane_cores(pid) if pid else None
                if value:
                    cores += float(value)
            was_working = clock.state(tab.id, now).working
            if clock.note_load(tab.id, cores, now, conf):
                manager_dirty = True
                if not was_working and clock.state(tab.id, now).working:
                    _close_card_for(tab.id)
        if manager_dirty:
            dirty.append(manager)
    if clock.any_working():
        _ensure_timer(conf)
    _redraw(dirty)


def fields_for_tab(tab_id: int) -> dict[str, Any]:
    from .fast_data_types import monotonic
    return template_fields(clock, tab_id, monotonic(), settings())


def show_card(tab_manager: TabManager, tab: Tab) -> bool:
    """Right click on a tab: a card under it saying how long it has been idle."""
    conf = settings()
    if not conf.enabled:
        return False
    from .fast_data_types import get_boss, monotonic
    from .kilix_chrome.popup import ChromePopup, popup_target
    if clock.state(tab.id, monotonic()).working:
        # Settle first: a tab whose step timer has not fired since it went
        # quiet must not be reported as working.
        last = max((w.screen.kilix_last_output_at() for w in tab if _is_pane(w)), default=0.0)
        if clock.settle(tab.id, last, monotonic(), conf):
            tab_manager.mark_tab_bar_dirty()
    active = tab_manager.active_tab
    target = active.active_window if active is not None else None
    action = f'{TAB_STATUS_ACTION}:{tab.id}'
    if target is None or (target := popup_target(target, action)) is None:
        return True  # the same card was open: the click closed it
    index = next((i for i, t in enumerate(tab_manager, 1) if t is tab), 0)
    payload = card_payload(clock, tab.id, index, str(tab.name or tab.title or ''), monotonic(), time.time(), conf)
    get_boss().run_kitten_with_metadata(
        'kilix_chrome_widget', ('tab', json.dumps(payload)), window=target,
        kilix_popup=ChromePopup(action, 46, 9))
    return True

# }}}

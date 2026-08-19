"""Refresh and invalidation lifecycle shared by all chrome widgets."""

from .providers import clock_segment
from .settings import chrome_enabled, ensure_timer

_clock_timer_started = False
_clock_last_text = ''
CLOCK_REFRESH_SECONDS = 15.0


def invalidate_all() -> None:
    from ..fast_data_types import get_boss, mark_os_window_dirty
    synchronized = chrome_enabled('KILIX_CHROME_BUTTON_SYNCHRONIZE_INPUT')
    for manager in get_boss().all_tab_managers:
        manager.mark_tab_bar_dirty()
        for tab in manager:
            tab.kilix_apply_synchronized_input_setting(synchronized)
            tab.update_window_title_bars()
        mark_os_window_dirty(manager.os_window_id)


def _clock_timer(timer_id: int | None = None) -> None:
    global _clock_last_text
    text = clock_segment() or ''
    if text != _clock_last_text:
        _clock_last_text = text
        invalidate_all()


def ensure_clock_timer() -> None:
    global _clock_last_text, _clock_timer_started
    if _clock_timer_started or not chrome_enabled('KILIX_CHROME_CLOCK'):
        return
    _clock_timer_started = True
    _clock_last_text = clock_segment() or ''
    try:
        from ..fast_data_types import add_timer
        add_timer(_clock_timer, CLOCK_REFRESH_SECONDS, True)
    except Exception as e:
        from ..utils import log_error
        log_error(f'Failed to start kilix clock chrome timer: {e}')


def ensure_chrome_timers() -> None:
    from ..kilix_battery import ensure_battery_timer, ensure_thermal_timer
    from ..kilix_memory import ensure_pane_memory_timer
    from ..kilix_windows import ensure_windows_timer
    ensure_timer(invalidate_all)
    ensure_thermal_timer()
    ensure_clock_timer()
    ensure_battery_timer()
    ensure_pane_memory_timer()
    ensure_windows_timer()

#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kilix

"""Native X11 windows as tab-bar entries — the tab bar IS the taskbar.

In a Pleb session Kilix is a fullscreen terminal running under a minimal
Openbox. Applications launched from a Kilix shell open as native X11 windows
that Openbox manages, which means they can be focused, raised and reached with
Alt-Tab — but nothing on screen *lists* them, and a minimised window has no
visible representation at all.

This module makes Kilix's tab bar that list. Every window Openbox manages
(other than Kilix itself) becomes an entry beside the real tabs; minimised ones
stay listed and are marked, so "minimise" has somewhere to minimise *to*.
Clicking an entry sends `_NET_ACTIVE_WINDOW`, which restores and focuses the
window in one step — exactly what a pager or taskbar does.

Deliberately inert unless it is useful: it needs X11, a running EWMH window
manager, and python-xlib (a Pleb runtime dependency). Missing any of those, it
returns nothing and Kilix's tab bar is unchanged.
"""

import os
import time

from .kilix_battery import chrome_enabled, chrome_value
from .utils import log_error

# Clicking an entry activates that window. The window id is appended, so the
# dispatcher in tabs.py can recover it without a side table that could go stale
# between the render that produced the extent and the click that consumes it.
WINDOW_ACTIVATE_ACTION_PREFIX = 'kilix_activate_window:'

MINIMISED_GLYPH = '_'

_REFRESH_SECONDS = 1.0
_TIMER_STARTED = False
_LAST_SIGNATURE: tuple[object, ...] | None = None

_CACHE: tuple[tuple[int, str, bool], ...] = ()
_CACHE_UNTIL = 0.0
_CACHE_SECONDS = 0.75

_XLIB_UNAVAILABLE_LOGGED = False
_display = None
_display_name: str | None = None


class _NoDisplay(Exception):
    pass


def _get_display():          # type: ignore[no-untyped-def]
    """One long-lived X connection, reopened if the display changes or dies."""
    global _display, _display_name, _XLIB_UNAVAILABLE_LOGGED
    name = os.environ.get('DISPLAY') or ''
    if not name:
        raise _NoDisplay
    if _display is not None and _display_name == name:
        return _display
    try:
        from Xlib import display as xdisplay
    except Exception:
        # python-xlib is a Pleb dependency; outside a Pleb install it may be
        # absent. Say so once, then stay quiet — this runs on a timer.
        if not _XLIB_UNAVAILABLE_LOGGED:
            _XLIB_UNAVAILABLE_LOGGED = True
            log_error(
                'kilix: python-xlib unavailable; native windows will not be '
                'listed in the tab bar')
        raise _NoDisplay
    try:
        _display = xdisplay.Display(name)
        _display_name = name
    except Exception:
        _display = None
        _display_name = None
        raise _NoDisplay
    return _display


def _drop_display() -> None:
    global _display, _display_name
    try:
        if _display is not None:
            _display.close()
    except Exception:
        pass
    _display = None
    _display_name = None


def _window_ids(d, root, atom_name: str) -> list[int]:   # type: ignore[no-untyped-def]
    from Xlib import Xatom
    prop = root.get_full_property(d.intern_atom(atom_name), Xatom.WINDOW)
    return list(prop.value) if prop else []


def _own_window_ids(d, root) -> set[int]:                # type: ignore[no-untyped-def]
    """Kilix's own top-level windows, which must never appear as entries."""
    ids: set[int] = set()
    # Deliberately NOT keyed on $WINDOWID: kitty sets that in the environment it
    # hands to *child* processes (see tabs.py), so this process never has its
    # own id there — and when Kilix was started from another terminal, WINDOWID
    # is that terminal's window, which would hide an unrelated window from the
    # taskbar. WM_CLASS is authoritative here: the kilix launcher execs the
    # engine with `--class kilix` precisely so it groups as itself.
    own_class = 'kilix'
    for wid in _window_ids(d, root, '_NET_CLIENT_LIST'):
        try:
            cls = d.create_resource_object('window', wid).get_wm_class()
        except Exception:
            continue
        if cls and any(c and c.lower() == own_class for c in cls):
            ids.add(wid)
    return ids


def _window_title(d, win) -> str:                        # type: ignore[no-untyped-def]
    # _NET_WM_NAME (UTF-8) is the modern property; fall back to WM_NAME, which
    # is all some clients set (xterm, for one).
    try:
        prop = win.get_full_property(d.intern_atom('_NET_WM_NAME'), 0)
        if prop and prop.value:
            value = prop.value
            if isinstance(value, bytes):
                return value.decode('utf-8', 'replace')
            return str(value)
    except Exception:
        pass
    try:
        name = win.get_wm_name()
        if name:
            return name if isinstance(name, str) else name.decode('utf-8', 'replace')
    except Exception:
        pass
    return ''


def _is_minimised(d, win) -> bool:                       # type: ignore[no-untyped-def]
    from Xlib import Xatom
    try:
        prop = win.get_full_property(d.intern_atom('_NET_WM_STATE'), Xatom.ATOM)
    except Exception:
        return False
    if not prop:
        return False
    hidden = d.intern_atom('_NET_WM_STATE_HIDDEN')
    return hidden in set(prop.value)


def _has_window_manager(d, root) -> bool:                # type: ignore[no-untyped-def]
    """The same validated handshake pleb-session uses.

    A stale _NET_SUPPORTING_WM_CHECK left by a window manager that died would
    otherwise make us advertise a taskbar for windows nobody can manage.
    """
    from Xlib import Xatom
    try:
        prop = root.get_full_property(
            d.intern_atom('_NET_SUPPORTING_WM_CHECK'), Xatom.WINDOW)
        if not prop or not prop.value:
            return False
        child_id = prop.value[0]
        child = d.create_resource_object('window', child_id)
        back = child.get_full_property(
            d.intern_atom('_NET_SUPPORTING_WM_CHECK'), Xatom.WINDOW)
        return bool(back and back.value and back.value[0] == child_id)
    except Exception:
        return False


def in_pleb_session() -> bool:
    """Only Pleb makes its tab bar the desktop's taskbar.

    On an ordinary desktop the user already has a panel, and listing every
    window of their session inside a terminal's tab bar would be noise. These
    are the same markers pleb-session exports for the GUI-alias decision.
    """
    return (os.environ.get('XDG_SESSION_DESKTOP', '').lower() == 'pleb'
            or os.environ.get('XDG_CURRENT_DESKTOP', '') == 'Pleb')


def native_windows() -> tuple[tuple[int, str, bool], ...]:
    """(window id, title, minimised) for every managed window but our own."""
    global _CACHE, _CACHE_UNTIL
    if not in_pleb_session() or not chrome_enabled('KILIX_CHROME_WINDOWS'):
        return ()
    now = time.monotonic()
    if now < _CACHE_UNTIL:
        return _CACHE
    ans: list[tuple[int, str, bool]] = []
    try:
        d = _get_display()
        root = d.screen().root
        if _has_window_manager(d, root):
            skip = _own_window_ids(d, root)
            for wid in _window_ids(d, root, '_NET_CLIENT_LIST'):
                if wid in skip:
                    continue
                win = d.create_resource_object('window', wid)
                try:
                    ans.append((wid, _window_title(d, win), _is_minimised(d, win)))
                except Exception:
                    continue
    except _NoDisplay:
        ans = []
    except Exception as e:
        # A dead connection must not wedge the tab bar permanently.
        _drop_display()
        log_error(f'kilix: failed to list native windows: {e}')
        ans = []
    _CACHE = tuple(ans)
    _CACHE_UNTIL = now + _CACHE_SECONDS
    return _CACHE


def window_entries() -> tuple[tuple[str, str], ...]:
    """(label, action) per native window, in the tab bar's own text style."""
    try:
        limit = int(chrome_value('KILIX_CHROME_WINDOWS_MAX_TITLE', '18') or 18)
    except Exception:
        limit = 18
    limit = max(4, limit)
    ans: list[tuple[str, str]] = []
    for wid, title, minimised in native_windows():
        text = title.strip() or f'0x{wid:x}'
        if len(text) > limit:
            text = text[:limit - 1] + '…'
        if minimised:
            text = f'{MINIMISED_GLYPH}{text}'
        ans.append((f' {text} ', f'{WINDOW_ACTIVATE_ACTION_PREFIX}{wid}'))
    return tuple(ans)


def activate_window(window_id: int) -> bool:
    """Restore (if minimised) and focus a native window, as a pager does."""
    try:
        from Xlib import X
        from Xlib.protocol import event as xevent
        d = _get_display()
        root = d.screen().root
        win = d.create_resource_object('window', window_id)
        # source indication 2 == a pager/taskbar acting on the user's behalf;
        # window managers honour it even when focus stealing prevention is on.
        ev = xevent.ClientMessage(
            window=win, client_type=d.intern_atom('_NET_ACTIVE_WINDOW'),
            data=(32, [2, X.CurrentTime, 0, 0, 0]))
        root.send_event(
            ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        d.flush()
        return True
    except _NoDisplay:
        return False
    except Exception as e:
        _drop_display()
        log_error(f'kilix: failed to activate window 0x{window_id:x}: {e}')
        return False


def action_window_id(action: str) -> int | None:
    if not action.startswith(WINDOW_ACTIVATE_ACTION_PREFIX):
        return None
    try:
        return int(action[len(WINDOW_ACTIVATE_ACTION_PREFIX):])
    except ValueError:
        return None


def _signature() -> tuple[object, ...]:
    return tuple(native_windows())


def _windows_timer(timer_id: int | None = None) -> None:
    global _CACHE_UNTIL, _LAST_SIGNATURE
    if not in_pleb_session() or not chrome_enabled('KILIX_CHROME_WINDOWS'):
        return
    _CACHE_UNTIL = 0.0
    sig = _signature()
    if sig != _LAST_SIGNATURE:
        _LAST_SIGNATURE = sig
        from .kilix_battery import _invalidate_all_chrome
        _invalidate_all_chrome()


def ensure_windows_timer() -> None:
    global _TIMER_STARTED
    if _TIMER_STARTED or not in_pleb_session() or not chrome_enabled('KILIX_CHROME_WINDOWS'):
        return
    _TIMER_STARTED = True
    try:
        from .fast_data_types import add_timer
        add_timer(_windows_timer, _REFRESH_SECONDS, True)
    except Exception as e:
        log_error(f'Failed to start kilix native window chrome timer: {e}')

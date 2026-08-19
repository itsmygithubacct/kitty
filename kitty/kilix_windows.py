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
import re
import subprocess
import time

from .kilix_chrome.settings import chrome_enabled, chrome_value
from .utils import log_error, which

# Clicking an entry activates that window. The window id is appended, so the
# dispatcher in tabs.py can recover it without a side table that could go stale
# between the render that produced the extent and the click that consumes it.
WINDOW_ACTIVATE_ACTION_PREFIX = 'kilix_activate_window:'

MINIMISED_GLYPH = '_'

_REFRESH_SECONDS = 5.0
_TIMER_STARTED = False
_LAST_SIGNATURE: tuple[object, ...] | None = None

_CACHE: tuple[tuple[int, str, bool], ...] = ()
_CACHE_UNTIL = 0.0
# Longer than _REFRESH_SECONDS on purpose. Each refresh spawns an xprop per
# window, and the tab bar asks for entries while it is drawing -- if the cache
# expired before the timer next fired, that work would land on the render path
# and stall a frame. The timer invalidates explicitly, so it stays the only
# thing that pays for a refresh; this is just the ceiling if it ever stops.
_CACHE_SECONDS = 10.0

_XLIB_UNAVAILABLE_LOGGED = False
_display = None
_display_name: str | None = None


SKIP_STATES = ('_NET_WM_STATE_SKIP_TASKBAR', '_NET_WM_STATE_SKIP_PAGER')
SKIP_TYPES = ('_NET_WM_WINDOW_TYPE_DOCK', '_NET_WM_WINDOW_TYPE_DESKTOP',
              '_NET_WM_WINDOW_TYPE_SPLASH')


def _xprop(*args: str) -> str:
    """Query X properties with xprop.

    Deliberately not python-xlib: the terminal embeds its own interpreter whose
    sys.path does not include the system dist-packages, so `import Xlib` fails
    inside Kilix even when python3-xlib is installed. xprop ships in x11-utils,
    which Pleb already requires for the session's own EWMH readiness check.
    """
    if not os.environ.get('DISPLAY'):
        return ''
    try:
        out = subprocess.run(
            ('xprop', *args), capture_output=True, text=True, timeout=5,
            env=os.environ.copy())
    except Exception:
        return ''
    return out.stdout if out.returncode == 0 else ''


def _prop_ids(text: str) -> list[int]:
    """Window ids from an xprop window-list property."""
    if '#' not in text:
        return []
    ans: list[int] = []
    for token in text.split('#', 1)[1].split(','):
        token = token.strip()
        if token.startswith('0x'):
            try:
                ans.append(int(token, 16))
            except ValueError:
                pass
    return ans


def _has_window_manager() -> bool:
    """The same validated handshake pleb-session uses.

    A stale _NET_SUPPORTING_WM_CHECK left by a window manager that died would
    otherwise make us advertise a taskbar for windows nobody can manage.
    """
    root = _prop_ids(_xprop('-root', '-notype', '_NET_SUPPORTING_WM_CHECK'))
    if not root:
        return False
    child = _prop_ids(_xprop('-id', str(root[0]), '-notype', '_NET_SUPPORTING_WM_CHECK'))
    return bool(child) and child[0] == root[0]


def _quoted(line: str) -> list[str]:
    return re.findall(r'"((?:[^"\\]|\\.)*)"', line)


def _window_info(wid: int) -> tuple[str, bool] | None:
    """(title, minimised) for a window, or None if it must not be listed."""
    text = _xprop('-id', str(wid), '-notype', '_NET_WM_NAME', 'WM_NAME',
                  'WM_CLASS', '_NET_WM_STATE', '_NET_WM_WINDOW_TYPE')
    if not text:
        return None
    net_name = wm_name = ''
    classes: list[str] = []
    state = types = ''
    for line in text.splitlines():
        if line.startswith('_NET_WM_NAME'):
            got = _quoted(line)
            net_name = got[0] if got else ''
        elif line.startswith('WM_NAME'):
            got = _quoted(line)
            wm_name = got[0] if got else ''
        elif line.startswith('WM_CLASS'):
            classes = _quoted(line)
        elif line.startswith('_NET_WM_STATE'):
            state = line
        elif line.startswith('_NET_WM_WINDOW_TYPE'):
            types = line
    # Kilix must never list itself. WM_CLASS is authoritative: the launcher
    # execs the engine with `--class kilix`.
    if any(c.lower() == 'kilix' for c in classes):
        return None
    # Panels, docks and anything asking to be skipped are not tasks. The
    # desktop's own panel sets SKIP_TASKBAR precisely so lists like this one
    # leave it alone.
    if any(flag in state for flag in SKIP_STATES):
        return None
    if any(flag in types for flag in SKIP_TYPES):
        return None
    return (net_name or wm_name), ('_NET_WM_STATE_HIDDEN' in state)


def _activate_command(window_id: int) -> tuple[str, ...] | None:
    hexid = f'0x{window_id:x}'
    if which('wmctrl'):
        return ('wmctrl', '-i', '-a', hexid)
    if which('xdotool'):
        return ('xdotool', 'windowactivate', hexid)
    # Fall back to the system interpreter, which -- unlike the one embedded in
    # the terminal -- can import python-xlib. Sending _NET_ACTIVE_WINDOW with
    # source indication 2 (a pager acting for the user) is what restores a
    # minimised window and focuses it in one step.
    if which('python3'):
        return ('python3', '-c', _ACTIVATE_SNIPPET, hexid)
    return None


_ACTIVATE_SNIPPET = """
import sys
from Xlib import X, display
from Xlib.protocol import event
d = display.Display()
root = d.screen().root
win = d.create_resource_object('window', int(sys.argv[1], 16))
root.send_event(event.ClientMessage(
    window=win, client_type=d.intern_atom('_NET_ACTIVE_WINDOW'),
    data=(32, [2, X.CurrentTime, 0, 0, 0])),
    event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
d.flush()
"""


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
        if _has_window_manager():
            for wid in _prop_ids(_xprop('-root', '-notype', '_NET_CLIENT_LIST')):
                info = _window_info(wid)
                if info is None:
                    continue
                ans.append((wid, info[0], info[1]))
    except Exception as e:
        log_error(f'kilix: failed to list native windows: {e}')
        ans = []
    _CACHE = tuple(ans)
    _CACHE_UNTIL = now + _CACHE_SECONDS
    return _CACHE


def window_entries(max_title: int | None = None) -> tuple[tuple[str, str], ...]:
    """(label, action) per native window, in the tab bar's own text style.

    max_title lets the caller fit the run into whatever space is left beside
    the pages; without it the configured default applies.
    """
    if max_title is None:
        try:
            max_title = int(chrome_value('KILIX_CHROME_WINDOWS_MAX_TITLE', '18') or 18)
        except Exception:
            max_title = 18
    limit = max(3, max_title)
    ans: list[tuple[str, str]] = []
    for wid, title, minimised in native_windows():
        text = title.strip() or f'0x{wid:x}'
        if minimised:
            text = f'{MINIMISED_GLYPH}{text}'
        if len(text) > limit:
            text = text[:limit - 1] + '…'
        ans.append((f' {text} ', f'{WINDOW_ACTIVATE_ACTION_PREFIX}{wid}'))
    return tuple(ans)


def activate_window(window_id: int) -> bool:
    """Restore (if minimised) and focus a native window, as a pager does."""
    cmd = _activate_command(window_id)
    if cmd is None:
        log_error('kilix: no way to activate a window (install wmctrl or xdotool)')
        return False
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5,
                             env=os.environ.copy())
    except Exception as e:
        log_error(f'kilix: failed to activate window 0x{window_id:x}: {e}')
        return False
    if res.returncode != 0:
        log_error(f'kilix: failed to activate window 0x{window_id:x}: '
                  f'{res.stderr.strip()}')
        return False
    return True


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
        from .kilix_chrome.lifecycle import invalidate_all
        invalidate_all()


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

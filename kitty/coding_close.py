"""Coding pane close choices; prepare recovery before changing pane ownership."""
from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Queue
import shutil
import stat
import subprocess
import sys
from threading import Thread
import time

_busy: set[int] = set()


def client_registry():
    """Read bounded declarative host data; never run a helper in the UI thread."""
    home = os.environ.get('KILIX_HOME')
    if not sys.platform.startswith('linux') or not home:
        return ()
    try:
        fd = os.open(Path(home) / 'config/coding_clients.json',
                     os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                return ()
            data = stream.read(16385)
        if len(data) > 16384:
            return ()
        registry = json.loads(data)
        if not isinstance(registry, dict) or registry.get('schema') != 'kilix.coding-clients/v1':
            return ()
        clients = registry.get('clients')
        if not isinstance(clients, list) or not 1 <= len(clients) <= 8:
            return ()
        def component(value):
            return (isinstance(value, str) and 0 < len(value) <= 128
                    and value not in ('.', '..') and '/' not in value and '\0' not in value)
        for client in clients:
            if not isinstance(client, dict):
                return ()
            names, entries = client.get('executable_names'), client.get('entrypoints')
            if (not isinstance(names, list) or not 1 <= len(names) <= 8
                    or not all(component(name) for name in names)
                    or not isinstance(entries, list) or len(entries) > 8):
                return ()
            for entry in entries:
                if not isinstance(entry, dict) or not component(entry.get('interpreter')):
                    return ()
                suffix = entry.get('path_suffix')
                if (not isinstance(suffix, list) or not 2 <= len(suffix) <= 16
                        or not all(component(part) for part in suffix)):
                    return ()
        return clients
    except (OSError, ValueError, UnicodeError, RecursionError):
        return ()


def is_coding(window):
    clients = client_registry()
    for process in window.child.foreground_processes[:64]:
        argv = process.get('cmdline') or []
        if not argv or not isinstance(argv[0], str) or len(argv[0]) > 4096:
            continue
        executable = Path(argv[0]).name
        for client in clients:
            if executable in client['executable_names']:
                return True
            if len(argv) < 2 or not isinstance(argv[1], str) or len(argv[1]) > 4096:
                continue
            entrypoint = Path(argv[1]).parts
            for entry in client['entrypoints']:
                suffix = tuple(entry['path_suffix'])
                if (executable == entry['interpreter'] and '..' not in entrypoint
                        and entrypoint[-len(suffix):] == suffix):
                    return True
    return False


def show(boss, window):
    if not is_coding(window):
        return False
    if window.id in _busy:
        return True
    from .fast_data_types import add_timer
    from .tabs import SpecialWindow

    if not window.child.is_pty_brokered:
        boss.show_error('Cannot transfer coding pane',
                        'A broker with descendant cessation support is required. The original pane remains open.')
        return True
    snapshot = window.as_dict()
    broker = snapshot.get('env', {}).get('KITTY_PTY_BROKER_SESSION', '')
    foreground = snapshot['foreground_processes']

    def same_window():
        current = boss.window_id_map.get(window.id)
        if current is not window:
            return False
        now = current.as_dict()
        return (now.get('env', {}).get('KITTY_PTY_BROKER_SESSION', '') == broker
                and now['foreground_processes'] == foreground)

    def chosen(choice):
        if choice not in ('l', 'b') or not same_window() or window.id in _busy:
            return
        home = os.environ.get('KILIX_HOME', '')
        helper = Path(home) / 'config/coding_handoff.py'
        python = shutil.which('python3')
        if not home or not helper.is_file() or not python:
            boss.show_error('Cannot close coding pane', 'The installed coding handoff helper is unavailable. The original pane remains open.')
            return
        command = [python, str(helper)]
        _busy.add(window.id)
        results = Queue()

        def prepare():
            try:
                result = subprocess.run(command + ['prepare'],
                    input=json.dumps({'window': snapshot, 'background': choice == 'b'}),
                    text=True, capture_output=True, timeout=30, check=True)
                results.put((json.loads(result.stdout), None))
            except Exception as error:
                results.put((None, getattr(error, 'stderr', None) or str(error)))

        def error(message, receipt=None):
            _busy.discard(window.id)
            if receipt:
                # Cancel only a prepared waiting replacement; never an active one.
                Thread(target=lambda: subprocess.run(command + ['cancel', receipt],
                    capture_output=True, timeout=15), daemon=True).start()
            boss.show_error('Cannot close coding pane', str(message) + '\nThe original pane remains open.'
                            + ('\nRecovery receipt: ' + receipt if receipt else ''))

        def completed(timer_id):
            if results.empty():
                add_timer(completed, .1, False); return
            result, failure = results.get_nowait()
            if failure:
                error(failure); return
            receipt = result['receipt']
            if not same_window():
                error('Pane or process identity changed during preparation.', receipt); return
            try:
                tab = boss._new_tab(SpecialWindow(command + ['display', receipt], override_title='Coding session location'))
                display = tab.active_window if tab is not None else None
                if display is None:
                    raise ValueError('could not open a surviving receipt terminal')
            except Exception as exception:
                error(exception, receipt); return
            deadline = time.monotonic() + 10
            releases = Queue()

            def released(timer_id):
                if releases.empty():
                    add_timer(released, .1, False); return
                failure = releases.get_nowait()
                _busy.discard(window.id)
                if failure or not same_window() or boss.window_id_map.get(display.id) is not display:
                    error(failure or 'Original pane or receipt terminal changed after preparation.', receipt)
                    return
                commits = Queue()

                def commit():
                    try:
                        subprocess.run(command + ['commit', receipt], text=True,
                                       capture_output=True, timeout=15, check=True)
                        commits.put(None)
                    except Exception:
                        # DRAIN may have reached the broker even when its reply
                        # was lost. Never promise the original is still alive.
                        (Path(receipt).parent / 'commit-failure.txt').write_text(
                            'Cessation/commit unconfirmed. No automatic retry; inspect the recovery receipt.\n')
                        commits.put('Cessation/commit unconfirmed. The original may have stopped. Inspect the recovery receipt.')

                def committed(timer_id):
                    if commits.empty():
                        add_timer(committed, .1, False); return
                    failure = commits.get_nowait()
                    _busy.discard(window.id)
                    if failure:
                        boss.show_error('Coding handoff unconfirmed', failure + '\nRecovery receipt: ' + receipt)
                        return
                    # Only retire the same frontend. The broker domain has
                    # already ceased; do not send a second legacy termination.
                    current = boss.window_id_map.get(window.id)
                    if (current is window and current.as_dict().get('env', {}).get(
                            'KITTY_PTY_BROKER_SESSION', '') == broker):
                        boss.mark_window_for_close(window)

                _busy.add(window.id)
                Thread(target=commit, daemon=True).start()
                add_timer(committed, .1, False)

            def ready(timer_id):
                if not same_window() or boss.window_id_map.get(display.id) is not display:
                    error('Original pane changed or receipt terminal was closed.', receipt); return
                if not (Path(receipt).parent / 'display-ready.json').is_file():
                    if time.monotonic() >= deadline:
                        error('Receipt terminal did not become ready.', receipt); return
                    add_timer(ready, .1, False); return

                def release():
                    try:
                        subprocess.run(command + ['release', receipt], text=True,
                                       capture_output=True, timeout=15, check=True)
                        releases.put(None)
                    except Exception as exception:
                        releases.put(getattr(exception, 'stderr', None) or str(exception))
                Thread(target=release, daemon=True).start()
                add_timer(released, .1, False)

            add_timer(ready, .1, False)

        Thread(target=prepare, daemon=True).start()
        add_timer(completed, .1, False)

    boss.choose('Close this coding conversation?\n'
                'Close and give path to rollout log keeps a recovery receipt in another terminal.\n'
                'Background to tmux preserves captured launch settings and resumes only after the original client exits.',
                chosen, 'l:Close and give path to rollout log', 'b:Background to tmux', 'c:Cancel',
                window=window, default='c', title='Close coding pane?')
    return True

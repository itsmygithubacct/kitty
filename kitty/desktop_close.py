# License: GPL v3
"""Desktop close choices; system actions never imply permission to force close."""
import os
import re
import subprocess
from queue import Queue
from threading import Thread

from .fast_data_types import add_timer


MARKER = 'kilix_desktop_provider'


def action_command(choice, environment):
    if choice in ('s', 'r'):
        return ['systemctl', '--no-ask-password', 'poweroff' if choice == 's' else 'reboot']
    if choice == 'l':
        session = environment.get('XDG_SESSION_ID', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', session):
            raise ValueError('The current login session could not be identified.')
        return ['loginctl', 'terminate-session', '--', session]
    raise ValueError('Unknown desktop system action.')


def request_action(boss, choice):
    # Use the frontend's login session, never metadata supplied by a child pane.
    environment = os.environ.copy()
    results = Queue()

    def run():
        try:
            result = subprocess.run(action_command(choice, environment), env=environment,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or f'System action failed ({result.returncode}).')
        except Exception as error:
            results.put(str(error))
        else:
            results.put('')

    def completed(timer_id):
        if results.empty():
            add_timer(completed, 0.1, False)
            return
        error = results.get_nowait()
        if error:
            boss.show_error('Could not complete system action', error + '\nThe desktop tab remains open.')

    Thread(target=run, daemon=True).start()
    add_timer(completed, 0.1, False)


def show(boss, window=None, tab=None):
    providers = [w for w in tab if w.user_vars.get(MARKER)] if tab is not None else (
        [window] if window is not None and window.user_vars.get(MARKER) else [])
    if not providers:
        return False
    provider = providers[0]
    generation = provider.user_vars[MARKER]
    if tab is not None:
        existing = boss.window_id_map.get(tab.confirm_close_window_id)
        if existing is not None and existing in tab:
            tab.set_active_window(existing)
            return True

    def chosen(choice):
        if tab is not None:
            if not any(t is tab for t in boss.all_tabs):
                return
            tab.confirm_close_window_id = 0
        if boss.window_id_map.get(provider.id) is not provider or provider.user_vars.get(MARKER) != generation:
            return
        if tab is not None and provider not in tab:
            return
        if choice == 'y':
            if tab is not None:
                boss.close_tab_no_confirm(tab)
            else:
                boss.handle_kilix_persistent_close_confirmation(True, provider.id)
        elif choice in ('s', 'r', 'l'):
            request_action(boss, choice)

    overlay = boss.choose(
        'Are you sure you want to close this desktop tab?\n'
        'Yes closes the tab. Choose a system action to shut down, reboot, or log out of Pleb.',
        chosen, 'y:Yes', 'n:No', 's:System shut down', 'r:System reboot', 'l:Log out of Pleb',
        window=provider, default='n', title='Close desktop tab?')
    if tab is not None and overlay is not None:
        tab.confirm_close_window_id = overlay.id
    return True

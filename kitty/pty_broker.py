#!/usr/bin/env python
# License: GPL v3

"""Kilix integration for the external kitty-pty-broker library.

The broker executable is intentionally configured by the launcher rather than
discovered on PATH. This keeps ordinary kitty builds unchanged and makes the
process-lifetime boundary explicit.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

_SESSION_ID = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


def configuration(environment: Mapping[str, str] | None = None) -> tuple[str, str]:
    env = os.environ if environment is None else environment
    executable = env.get('KITTY_PTY_BROKER_EXECUTABLE', '')
    runtime = env.get('KITTY_PTY_BROKER_RUNTIME', '')
    if not (os.path.isabs(executable) and os.access(executable, os.X_OK)):
        return '', ''
    if not os.path.isabs(runtime):
        return '', ''
    return executable, runtime


def valid_session_id(value: str) -> bool:
    return bool(_SESSION_ID.fullmatch(value)) and value not in {'.', '..'}


def new_session_id() -> str:
    return secrets.token_hex(16)


def journal_limit(environment: Mapping[str, str] | None = None) -> int:
    env = os.environ if environment is None else environment
    raw = env.get('KITTY_PTY_BROKER_JOURNAL_LIMIT', '67108864')
    try:
        value = int(raw)
    except ValueError:
        return 67108864
    return value if 0 <= value <= 1024 * 1024 * 1024 else 67108864


def transcript_limit(environment: Mapping[str, str] | None = None) -> int:
    env = os.environ if environment is None else environment
    raw = env.get('KITTY_PTY_BROKER_TRANSCRIPT_LIMIT', '8388608')
    try:
        value = int(raw)
    except ValueError:
        return 8388608
    return value if 0 <= value <= 1024 * 1024 * 1024 else 8388608


def transcript_options(
    session_id: str,
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    """Return the broker flags that record this pane's output, if enabled.

    Session logging is opt-out rather than opt-in, so an unset directory means
    the host did not configure it and no transcript is written.  The session ID
    is already constrained to a safe single path component.
    """
    env = os.environ if environment is None else environment
    directory = env.get('KITTY_PTY_BROKER_TRANSCRIPT_DIR', '')
    if not directory or not os.path.isabs(directory) or not os.path.isdir(directory):
        return []
    graphics = env.get('KITTY_PTY_BROKER_TRANSCRIPT_GRAPHICS', 'elide')
    if graphics not in {'elide', 'keep'}:
        graphics = 'elide'
    return [
        '--transcript', os.path.join(directory, f'{session_id}.log'),
        '--transcript-limit', str(transcript_limit(env)),
        '--transcript-graphics', graphics,
    ]


def wrap_command(
    executable: str,
    runtime: str,
    session_id: str,
    command: Sequence[str],
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    if not valid_session_id(session_id) or not command:
        raise ValueError('invalid PTY broker command')
    answer = [
        executable, '--runtime-dir', runtime, 'run', '--id', session_id,
        '--journal-limit', str(journal_limit(environment)),
    ]
    answer.extend(transcript_options(session_id, environment))
    answer.append('--')
    answer.extend(command)
    return answer


def attach_command(executable: str, runtime: str, session_id: str) -> list[str]:
    if not valid_session_id(session_id):
        raise ValueError('invalid PTY broker session ID')
    return [executable, '--runtime-dir', runtime, 'attach', session_id]


def query_status(executable: str, runtime: str, session_id: str) -> dict[str, Any]:
    if not valid_session_id(session_id):
        return {}
    try:
        completed = subprocess.run(
            [executable, '--runtime-dir', runtime, 'status', session_id, '--json'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=.25,
            check=False,
        )
        if completed.returncode != 0:
            return {}
        answer = json.loads(completed.stdout)
        return answer if isinstance(answer, dict) and answer.get('id') == session_id else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def detached_sessions(executable: str, runtime: str) -> tuple[dict[str, Any], ...]:
    try:
        completed = subprocess.run(
            [executable, '--runtime-dir', runtime, 'list', '--json'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        if completed.returncode != 0:
            return ()
        decoded = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ()
    if not isinstance(decoded, list):
        return ()
    answer = []
    for item in decoded:
        if not isinstance(item, dict) or item.get('attached'):
            continue
        session_id = item.get('id')
        if isinstance(session_id, str) and valid_session_id(session_id):
            answer.append(item)
    return tuple(answer)


def terminate(executable: str, runtime: str, session_id: str) -> bool:
    if not valid_session_id(session_id):
        return False
    try:
        completed = subprocess.run(
            [executable, '--runtime-dir', runtime, 'kill', session_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

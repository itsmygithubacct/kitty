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
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

_SESSION_ID = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
_STARTUP_TOKEN = re.compile(r'^[0-9a-f]{32}$')


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


def valid_startup_token(value: str) -> bool:
    return bool(_STARTUP_TOKEN.fullmatch(value))


def _startup_token_of_child(pid: int) -> str:
    """Read the association from a live same-user broker child, never log env."""
    directory = f'/proc/{pid}'
    try:
        if os.stat(directory).st_uid != os.getuid():
            return ''
        with open(f'{directory}/stat') as stream:
            before = stream.read().rsplit(')', 1)[1].split()
        if before[0] in {'Z', 'X'}:
            return ''
        with open(f'{directory}/environ', 'rb') as stream:
            data = stream.read(262145)
        if len(data) > 262144:
            return ''
        with open(f'{directory}/stat') as stream:
            after = stream.read().rsplit(')', 1)[1].split()
        if after[0] in {'Z', 'X'} or before[19] != after[19]:
            return ''
        values = [item.partition(b'=')[2] for item in data.split(b'\0')
                  if item.startswith(b'KITTY_PTY_BROKER_STARTUP_SESSION=')]
        if len(values) == 1:
            token = values[0].decode('ascii')
            if valid_startup_token(token):
                return token
    except (OSError, UnicodeError, IndexError):
        pass
    return ''


def startup_session(sessions: Sequence[dict[str, Any]], token: str) -> dict[str, Any] | None:
    """Select one detached initial child for this login; ambiguity starts fresh."""
    if not valid_startup_token(token):
        return None
    found = []
    for status in sessions:
        session_id, pid = status.get('id'), status.get('child_pid')
        if (status.get('attached') or not isinstance(session_id, str)
                or not valid_session_id(session_id) or type(pid) is not int or pid <= 0):
            continue
        if _startup_token_of_child(pid) == token:
            found.append(status)
    return found[0] if len(found) == 1 else None


def new_session_id() -> str:
    # The ID becomes a path component of the broker's control socket:
    #   <runtime-dir>/sessions/<id>/control.sock
    # A Unix socket address is limited to sizeof(sun_path), 108 bytes on Linux,
    # and the broker refuses anything longer. With Kilix's default runtime
    # directory (~/.local/gpu_terminal/kilix/session/pty-broker, 55 bytes) a
    # 32-character ID produced a 110-byte path, so every pane failed to start
    # and the terminal exited with no windows. 16 characters leaves ~14 bytes of
    # headroom for a relocated KILIX_STORAGE_HOME while keeping a 2**64 space,
    # which is ample for short-lived per-pane sessions.
    return secrets.token_hex(8)


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


def transcript_metadata_path(
    session_id: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Return this pane's transcript sidecar path, or '' if not recording."""
    env = os.environ if environment is None else environment
    directory = env.get('KITTY_PTY_BROKER_TRANSCRIPT_DIR', '')
    if not directory or not os.path.isabs(directory) or not os.path.isdir(directory):
        return ''
    if not valid_session_id(session_id):
        return ''
    return os.path.join(directory, f'{session_id}.meta')


def _one_line(value: str, limit: int = 2048) -> str:
    """Collapse a value onto one line so the sidecar stays line-oriented."""
    flattened = value.replace('\r', ' ').replace('\n', ' ').strip()
    return flattened[:limit]


def write_transcript_metadata(
    session_id: str,
    cwd: str,
    command: Sequence[str],
    environment: Mapping[str, str] | None = None,
) -> str:
    """Record which pane a transcript belongs to, for `kilix transcript list`.

    Session IDs are random, so the index can otherwise only offer a hash --
    no help at all when what you are looking for is "the tab I had open on
    that directory".  This is advisory: a sidecar that cannot be written must
    never keep a pane from starting, so every failure here is swallowed.
    """
    path = transcript_metadata_path(session_id, environment)
    if not path:
        return ''
    created = False
    fd = -1
    try:
        payload = (
            f'cwd={_one_line(cwd)}\n'
            f'cmd={_one_line(shlex.join(command))}\n'
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        created = True
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8',
                       errors='surrogateescape') as sidecar:
            fd = -1
            sidecar.write(payload)
    except (OSError, UnicodeError, ValueError):
        if fd >= 0:
            os.close(fd)
        if created:
            try:
                os.unlink(path)
            except OSError:
                pass
        return ''
    return path


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


def live_session_ids(executable: str, runtime: str) -> frozenset[str] | None:
    """Every session the broker still runs, attached or not; None if unknown."""
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
            return None
        decoded = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(decoded, list):
        return None
    return frozenset(
        item['id'] for item in decoded
        if isinstance(item, dict) and isinstance(item.get('id'), str)
        and valid_session_id(item['id']))


# A brokered pane outlives the frontend that drew it, but its tab name and
# window title live only in that frontend.  They are kept beside the broker's
# runtime so a recovered frontend can show the pane under its own name again
# instead of an anonymous session id.
TITLE_KEYS = ('tab', 'window', 'override')
_written_titles: dict[str, str] = {}


def titles_path(runtime: str, session_id: str) -> str:
    if not runtime or not os.path.isabs(runtime) or not valid_session_id(session_id):
        return ''
    return os.path.join(runtime, 'titles', f'{session_id}.json')


def write_titles(runtime: str, session_id: str, titles: Mapping[str, str | None]) -> bool:
    """Record a pane's titles; advisory, so every failure is swallowed."""
    path = titles_path(runtime, session_id)
    if not path:
        return False
    payload = json.dumps({
        key: _one_line(value, 512) for key, value in titles.items()
        if key in TITLE_KEYS and isinstance(value, str) and value.strip()
    }, sort_keys=True)
    if _written_titles.get(path) == payload:
        return True
    temporary = f'{path}.{os.getpid()}.tmp'
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', errors='surrogateescape') as sidecar:
            sidecar.write(payload)
        os.replace(temporary, path)
    except (OSError, UnicodeError, ValueError):
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False
    _written_titles[path] = payload
    return True


def read_titles(runtime: str, session_id: str) -> dict[str, str]:
    path = titles_path(runtime, session_id)
    if not path:
        return {}
    flags = os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, 'O_NOFOLLOW') else 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as sidecar:
            data = json.loads(sidecar.read(16384).decode('utf-8', 'surrogateescape'))
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: _one_line(value, 512) for key, value in data.items()
            if key in TITLE_KEYS and isinstance(value, str) and value.strip()}


def forget_titles(runtime: str, session_id: str) -> None:
    path = titles_path(runtime, session_id)
    if path:
        _written_titles.pop(path, None)
        try:
            os.unlink(path)
        except OSError:
            pass


def prune_titles(runtime: str, live: frozenset[str]) -> None:
    """Drop the titles of sessions the broker no longer runs."""
    directory = os.path.join(runtime, 'titles') if runtime and os.path.isabs(runtime) else ''
    try:
        names = os.listdir(directory) if directory else []
    except OSError:
        return
    for name in names:
        session_id = name[:-5] if name.endswith('.json') else ''
        if valid_session_id(session_id) and session_id not in live:
            forget_titles(runtime, session_id)


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

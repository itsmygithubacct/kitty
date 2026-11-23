#!/usr/bin/env python
# License: GPL v3

"""Verified submission of one command at a fresh, empty shell prompt.

A prompt mark, a pane title, the foreground process or the screen text can
each describe a shell prompt, but none of them can say what the line editor
holds.  Only the editor can.  This module is the engine half of a protocol in
which the shell integration proves its own state at the instant it accepts a
command, and the engine contributes the facts only it can see.

1. Before each primary prompt the shell integration sends
   ``DCS @kitty-input|ready;v=1;pid=PID;token=TOKEN;pending=0|1 ST``.  It does
   so only when its line editor can run the guarded check below.  TOKEN is a
   fresh random value for that prompt; ``pending=1`` means unread type-ahead
   was already waiting.  The engine records the prompt together with the
   number of bytes it had queued for the child so far.

2. A submission (``kitten @ shell-submit``) is refused unless, at that moment
   and in one step of the engine's main loop: the prompt is the one the caller
   planned against, no byte has been queued for the child since the prompt was
   reported (nor shortly before it), no command has started, the reporting
   shell leads the terminal's foreground process group and the pane is not in
   the alternate screen.  The engine then writes the command to a private
   request file named after TOKEN and queues one inert key sequence.

3. That key sequence is bound only by the shell integration.  Its handler runs
   inside the line editor and accepts only when the buffer is empty, the
   editor is in an insert keymap and reading a primary (not continuation)
   line, and TOKEN is still its current prompt.  It claims the request
   atomically (``O_EXCL``), puts the command in the buffer, accepts it and
   reports ``DCS @kitty-input|result;token=TOKEN;status=accepted ST``.  On any
   other state it reports ``status=refused;reason=...`` and leaves the buffer
   as it was.

The engine never sees the contents of the editor buffer: the shell reports
only whether it was empty.  A request that is neither accepted nor refused in
time is cancelled by the engine claiming it first, so it can never run late.
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .constants import is_windows, runtime_dir
from .fast_data_types import add_timer, monotonic, remove_timer

if TYPE_CHECKING:
    from .window import Window

PROTOCOL_VERSION = 1
# The key sequence the integration binds. It names a Private Use Area key in
# the kitty keyboard protocol that no keyboard produces, and it carries none of
# the command, so a program that reads it by mistake sees one unknown key.
TRIGGER = b'\x1b[63171u'
# Input queued this long before a prompt was reported may still have been in
# flight when the shell looked for unread type-ahead.
QUIET_BEFORE_READY = 0.25
RESULT_TIMEOUT = 3.0
MAX_COMMAND_BYTES = 4096
DIRECTORY_ENV = 'KITTY_SHELL_INPUT_DIR'
_TOKEN = re.compile(r'[0-9a-f]{16,64}')
_FIELD = re.compile(r'[a-z_]{1,32}')
_REASON = re.compile(r'[a-z_]{1,40}')
_UNSAFE = re.compile(r'[\x00-\x1f\x7f-\x9f]')
_MAX_MESSAGE = 512


class ShellInputRefused(Exception):
    # An expected answer, reported to remote control as CODE: REASON alone.
    hide_traceback = True

    def __init__(self, code: str, reason: str):
        super().__init__(f'{code}: {reason}')
        self.code = code
        self.reason = reason


@dataclass
class Prompt:
    pid: int
    token: str
    prompt_id: str
    baseline: int
    quiet: bool
    pending: bool


@dataclass
class Request:
    token: str
    prompt_id: str
    path: str
    claim: str
    respond: Callable[[dict[str, Any] | None, ShellInputRefused | None], None]
    timer: int = 0
    done: bool = False


@dataclass
class ShellInputState:
    """What the engine knows about one pane's shell editor. Never its text."""

    prompt: Prompt | None = None
    request: Request | None = None
    supported: bool = False
    # The byte count right after the engine queued its own key sequence; the
    # next prompt is still quiet when nothing else was queued since.
    engine_mark: int = -1
    # The byte count when the shell last marked a command start (OSC 133;C).
    # Bytes queued before it were read by the line editor as that command's
    # line; only bytes queued after it can be type-ahead at the next prompt.
    command_mark: int = -1
    last_change: str = 'no prompt reported'


def directory() -> str:
    """The private directory request files live in; created 0700 on demand."""
    path = os.path.join(runtime_dir(), 'kilix-shell-input')
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise ShellInputRefused('unavailable', 'the shell input directory is not private')
    return path


def export_directory(env: dict[str, str]) -> None:
    if is_windows:
        return
    try:
        env[DIRECTORY_ENV] = directory()
    except (OSError, ShellInputRefused):
        env.pop(DIRECTORY_ENV, None)


def parse_message(raw: memoryview | bytes | str) -> tuple[str, dict[str, str]]:
    text = raw if isinstance(raw, str) else str(raw, 'utf-8', 'replace')
    if len(text) > _MAX_MESSAGE:
        raise ValueError('message too long')
    kind, _, rest = text.partition(';')
    fields: dict[str, str] = {}
    for item in rest.split(';') if rest else ():
        key, sep, value = item.partition('=')
        if not sep or not _FIELD.fullmatch(key) or key in fields:
            raise ValueError('malformed field')
        fields[key] = value
    return kind, fields


def state_of(window: Window) -> ShellInputState:
    ans: ShellInputState | None = getattr(window, 'shell_input_state', None)
    if ans is None:
        ans = window.shell_input_state = ShellInputState()
    return ans


def _queue_state(window: Window) -> tuple[int, float, int]:
    """Bytes ever queued to the child, when the last was queued, and how many
    of them the engine has not yet written to the pane."""
    count, at, unwritten = window.screen.input_queue_state()
    return count, at / 1e9, unwritten


def invalidate(window: Window, why: str) -> None:
    st = state_of(window)
    if st.prompt is not None:
        st.prompt = None
        st.last_change = why


def command_started(window: Window) -> None:
    st = state_of(window)
    st.command_mark = _queue_state(window)[0]
    invalidate(window, 'a command started')


def handle_message(window: Window, raw: memoryview | bytes | str) -> None:
    try:
        kind, fields = parse_message(raw)
    except ValueError:
        return
    st = state_of(window)
    if kind == 'ready':
        _on_ready(window, st, fields)
    elif kind == 'result':
        _on_result(window, st, fields)


def _on_ready(window: Window, st: ShellInputState, fields: dict[str, str]) -> None:
    try:
        version = int(fields.get('v', ''))
        pid = int(fields.get('pid', ''))
    except ValueError:
        return
    token = fields.get('token', '')
    if version != PROTOCOL_VERSION or pid <= 0 or not _TOKEN.fullmatch(token):
        return
    count, last_at, unwritten = _queue_state(window)
    st.supported = True
    # Engine-chosen and never reused, so a plan cannot match a later prompt,
    # another pane or another engine instance by accident.
    st.prompt = Prompt(
        pid=pid, token=token, prompt_id=secrets.token_hex(12), baseline=count,
        # Input the engine still holds, or queued so recently that it may still
        # be on its way, could reach the line editor after the shell looked
        # for unread input; the shell would then never see it.
        quiet=not unwritten and (count in (0, st.engine_mark, st.command_mark)
                                 or monotonic() - last_at >= QUIET_BEFORE_READY),
        pending=fields.get('pending') != '0')
    st.last_change = 'prompt reported'


def _on_result(window: Window, st: ShellInputState, fields: dict[str, str]) -> None:
    req = st.request
    if req is None or req.done or fields.get('token') != req.token:
        return
    status = fields.get('status')
    if status == 'accepted':
        _finish(st, req, {'status': 'submitted', 'prompt_id': req.prompt_id}, None)
    elif status == 'refused':
        reason = fields.get('reason', '')
        if not _REASON.fullmatch(reason):
            reason = 'unknown'
        if _cancel(req):
            _finish(st, req, None, ShellInputRefused(reason, _SHELL_REASONS.get(reason, 'the shell refused the command')))
        else:
            # The shell claimed the request after all; it is running it.
            _finish(st, req, {'status': 'submitted', 'prompt_id': req.prompt_id}, None)


_SHELL_REASONS = {
    'draft': 'the shell editor already holds input; it was left untouched',
    'continuation': 'the shell is reading a continuation line',
    'vi_command': 'the shell editor is in vi command mode',
    'pending_key': 'a key sequence typed ahead was still incomplete in the shell editor',
    'not_prompt': 'the shell is not at the prompt this request was planned against',
    'no_request': 'the shell found no request for its current prompt',
    'claimed': 'the request was already claimed or cancelled',
    'invalid_command': 'the shell rejected the command text',
    'unverifiable': 'the shell cannot verify its prompt state',
}


def _cancel(req: Request) -> bool:
    """Claim the request for the engine. False when the shell claimed it first."""
    try:
        fd = os.open(req.claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return False
    except OSError:
        # Without a claim the shell could still take the request; remove it.
        _remove(req.path)
        return not os.path.exists(req.claim)
    os.close(fd)
    return True


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _finish(st: ShellInputState, req: Request, result: dict[str, Any] | None, error: ShellInputRefused | None) -> None:
    if req.done:
        return
    req.done = True
    if req.timer:
        remove_timer(req.timer)
        req.timer = 0
    _remove(req.path)
    _remove(req.claim)
    if st.request is req:
        st.request = None
    req.respond(result, error)


def _timed_out(window_ref: Callable[[], Window | None], req: Request, timer_id: int | None = None) -> None:
    req.timer = 0
    window = window_ref()
    st = state_of(window) if window is not None else None
    if req.done:
        return
    if _cancel(req):
        err = ShellInputRefused('timeout', 'the shell did not confirm the command; nothing was submitted')
        if st is not None:
            _finish(st, req, None, err)
        else:
            req.done = True
            _remove(req.path)
            _remove(req.claim)
            req.respond(None, err)
    else:
        err = ShellInputRefused('unconfirmed', 'the shell claimed the command but did not confirm it; do not resubmit')
        if st is not None:
            _finish(st, req, None, err)
        else:
            req.done = True
            req.respond(None, err)


def _process_group(pid: int) -> tuple[int, int] | None:
    """(pgrp, tpgid) of a live process from /proc, or None."""
    try:
        with open(f'/proc/{pid}/stat', 'rb') as f:
            data = f.read()
        fields = data.rsplit(b')', 1)[1].split()
        if fields[0] in (b'Z', b'X'):
            return None
        return int(fields[2]), int(fields[5])
    except (OSError, IndexError, ValueError):
        return None


def shell_in_foreground(window: Window, pid: int, *, fresh: bool) -> bool:
    groups = _process_group(pid)
    if groups is None or groups != (pid, pid):
        return False
    child = window.child
    if child.is_pty_brokered:
        if fresh:
            from .pty_broker import query_status
            status = query_status(child.pty_broker_executable, child.pty_broker_runtime, child.pty_broker_session_id)
        else:
            status = child.pty_broker_status()
        return status.get('foreground_pgrp') == pid
    if child.child_fd is None:
        return False
    try:
        return os.tcgetpgrp(child.child_fd) == pid
    except OSError:
        return False


def _check(window: Window, st: ShellInputState, *, fresh: bool, expect_prompt: str = '') -> Prompt:
    if is_windows:
        raise ShellInputRefused('unavailable', 'shell input verification is not available on this platform')
    p = st.prompt
    if st.request is not None and not st.request.done:
        raise ShellInputRefused('in_flight', 'another command is being submitted to this pane')
    if p is None:
        if not st.supported:
            raise ShellInputRefused('unavailable', 'the shell in this pane has not reported a verifiable prompt '
                                    '(no Kilix shell integration, or its line editor cannot prove its state)')
        raise ShellInputRefused('busy', f'the shell is not at a fresh prompt ({st.last_change})')
    if expect_prompt and expect_prompt != p.prompt_id:
        raise ShellInputRefused('stale', 'the prompt changed after the request was planned')
    count, _, _ = _queue_state(window)
    if count != p.baseline:
        raise ShellInputRefused('input', 'input reached the pane after its prompt appeared')
    if p.pending or not p.quiet:
        raise ShellInputRefused('input', 'type-ahead may be waiting at this prompt')
    if window.screen.is_using_alternate_linebuf():
        raise ShellInputRefused('busy', 'the pane is showing a full-screen program')
    if not shell_in_foreground(window, p.pid, fresh=fresh):
        raise ShellInputRefused('busy', 'the shell that reported the prompt is not in the foreground')
    return p


def describe(window: Window) -> dict[str, Any]:
    """The pane's shell input state for listings. Never the editor contents."""
    st = state_of(window)
    try:
        p = _check(window, st, fresh=False)
    except ShellInputRefused as e:
        ans: dict[str, Any] = {'verifiable': False, 'code': e.code, 'reason': e.reason}
        return ans
    return {'verifiable': True, 'prompt_id': p.prompt_id, 'protocol': PROTOCOL_VERSION}


def abandon(window: Window) -> None:
    """The pane is closing: cancel any request so it can never run later."""
    st = getattr(window, 'shell_input_state', None)
    if st is None:
        return
    st.prompt = None
    req = st.request
    if req is not None and not req.done:
        if _cancel(req):
            _finish(st, req, None, ShellInputRefused('closed', 'the pane closed before the shell confirmed the command'))
        else:
            _finish(st, req, None, ShellInputRefused('unconfirmed', 'the pane closed after the shell claimed the command; do not resubmit'))


def validate_command(command: str) -> bytes:
    if not command or not command.strip():
        raise ShellInputRefused('invalid_command', 'the command is empty')
    if _UNSAFE.search(command):
        raise ShellInputRefused('invalid_command', 'the command contains a newline or control character')
    data = command.encode('utf-8')
    if len(data) > MAX_COMMAND_BYTES:
        raise ShellInputRefused('invalid_command', f'the command is longer than {MAX_COMMAND_BYTES} bytes')
    return data


def submit(
    window: Window, command: str, expect_prompt: str,
    respond: Callable[[dict[str, Any] | None, ShellInputRefused | None], None],
) -> None:
    """Check, then hand the command to the shell's guarded handler. Raises ShellInputRefused."""
    import weakref
    data = validate_command(command)
    st = state_of(window)
    p = _check(window, st, fresh=True, expect_prompt=expect_prompt)
    folder = directory()
    path = os.path.join(folder, f'request-{p.token}')
    claim = os.path.join(folder, f'claim-{p.token}')
    if os.path.lexists(claim):
        raise ShellInputRefused('stale', 'this prompt was already used for a request')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise ShellInputRefused('stale' if e.errno == errno.EEXIST else 'unavailable',
                                'the request for this prompt could not be prepared') from e
    try:
        written = os.write(fd, data)
    finally:
        os.close(fd)
    if written != len(data):
        _remove(path)
        raise ShellInputRefused('unavailable', 'the request for this prompt could not be prepared')
    req = Request(token=p.token, prompt_id=p.prompt_id, path=path, claim=claim, respond=respond)
    # One prompt, one attempt: whatever the shell answers, a new prompt is needed.
    st.prompt = None
    st.last_change = 'a command was submitted at this prompt'
    st.request = req
    window.write_to_child(TRIGGER)
    st.engine_mark = _queue_state(window)[0]
    ref = weakref.ref(window)
    req.timer = add_timer(lambda timer_id: _timed_out(ref, req, timer_id), RESULT_TIMEOUT, False)


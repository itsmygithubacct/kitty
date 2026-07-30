#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kilix

"""Read-aloud and dictation as clickable tab-bar chrome.

Two widgets: a speaking head that reads the active pane, and a microphone that
dictates into it. Everything audio-shaped happens in `kilix-voiced`, a separate
process — synthesising a screenful takes hundreds of milliseconds and
recognition takes seconds, and this code runs on kitty's UI thread. The fork
keeps only the two jobs that are impossible outside the process: reading a
pane's text, and writing to a pane's PTY.

The microphone is click-to-talk. Nothing opens the audio input device until a
click, there is no pre-roll buffer, and the daemon is not even started until a
widget is used. A terminal that listens by default would be indefensible.

Dictated text is never submitted. It is written without a trailing newline and
the sanitiser below removes every control character, so "never submits" is a
property of the code path rather than a policy someone has to remember.
`KILIX_VOICE_STT_SUBMIT=confirm` is strictly more permissive than that and its
dialog belongs to a later phase, so this fork treats it as `never` — the safe
direction to degrade in.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import termios
import time
from contextlib import suppress
from dataclasses import dataclass
from itertools import count
from typing import TYPE_CHECKING

from .kilix_battery import chrome_enabled, chrome_value
from .rgb import to_color
from .utils import color_as_int, log_error, which

if TYPE_CHECKING:
    from .window import Window


SPEAK_ACTION = 'kilix_speak_active_pane'
DICTATE_ACTION = 'kilix_dictate_active_pane'
# Verified against the pinned Symbols Nerd Font Mono by glyph name, not merely
# by "the codepoint is mapped": an unmapped codepoint renders as tofu in the tab
# bar with no other symptom. Keep these in kitty.conf's symbol_map.
SPEAK_GLYPH = chr(0xf05cb)            # Material Design "account_voice"
MICROPHONE_GLYPH = chr(0xf036c)       # Material Design "microphone"
MICROPHONE_OFF_GLYPH = chr(0xf036d)   # Material Design "microphone_off"

# One vocabulary with the Kilix SDK. These defaults and choice tuples are the
# same ones config/kilix_sdk/settings.py declares; the fork re-reads rather than
# imports them, so a divergence here is a silent bug and not a crash.
TTS_ENGINES = ('espeak', 'mbrola', 'off')
TTS_RATES = ('120', '150', '170', '200', '240')
TTS_EXTENTS = ('screen', 'scrollback', 'selection')
STT_ENGINES = ('vosk', 'vibevoice', 'off')
STT_MODELS = ('small-en-us', 'lgraph-en-us', 'vibevoice-asr-bitnet')
STT_MAX_SECONDS = ('15', '30', '60', '120')
_VOICE_TOKEN = re.compile(r'[A-Za-z0-9_+-]{1,32}')

_ACTIVE_COLOR = (color_as_int(to_color('#8ae234')) << 8) | 2
_UNAVAILABLE_COLOR = (color_as_int(to_color('#888a85')) << 8) | 2
_ERROR_COLOR = (color_as_int(to_color('#ef2929')) << 8) | 2
_ERROR_SECONDS = 5.0

_AVAILABILITY_CACHE: dict[str, bool] = {}
_AVAILABILITY_SIGNATURE: tuple[object, ...] | None = None
_AVAILABILITY_UNTIL = 0.0
_AVAILABILITY_SECONDS = 5.0

_CONTROL_TIMEOUT = 0.25
_CONTROL_MAX_BYTES = 1 << 16
_SPAWN_SETTLE_SECONDS = 0.25
_SPAWN_POLL_SECONDS = 0.02
# A SOCK_SEQPACKET send has to fit one datagram, and the pane behind an
# `unlimited` read extent can be megabytes of scrollback. This is a transport
# bound, not the user-facing KILIX_VOICE_TTS_MAX_CHARS cap: that one applies to
# *conditioned* text, and counting escape sequences here would cut in the wrong
# place. Every preset except `unlimited` is far below it.
_MAX_REQUEST_CHARS = 32768
_DICTATION_MAX_BYTES = 1 << 16

_VOICE_REFRESH_SECONDS = 0.1
_VOICE_TIMER_ID: int | None = None
_VOICE_LAST_SIGNATURE: tuple[object, ...] | None = None

_control: socket.socket | None = None
_ids = count(1)


@dataclass
class VoiceState:
    """What the widgets render and what the click dispatcher toggles."""

    speaking: bool = False
    listening: bool = False
    # Recorded when the microphone is clicked and never re-read, so a transcript
    # cannot land in a pane the user switched to mid-sentence.
    target_window_id: int = 0
    last_error_until: float = 0.0
    partial: str = ''
    dictation_socket: socket.socket | None = None
    dictation_path: str = ''
    speech_id: int = 0
    dictation_id: int = 0


voice_state = VoiceState()


def _choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    """Read a preset choice, falling back rather than coercing.

    Matches the SDK's rule: an unrecognised value reads back as the default
    instead of being turned into something no interface ever offered.
    """
    value = chrome_value(name, default).strip().lower()
    return value if value in choices else default


def tts_engine() -> str:
    return _choice('KILIX_VOICE_TTS_ENGINE', 'espeak', TTS_ENGINES)


def tts_voice() -> str:
    # Not a preset list: the voice name is whatever the installed engine offers,
    # so it is shape-validated instead and passed through untouched.
    value = chrome_value('KILIX_VOICE_TTS_VOICE', 'en-us').strip()
    return value if _VOICE_TOKEN.fullmatch(value) else 'en-us'


def tts_rate() -> int:
    return int(_choice('KILIX_VOICE_TTS_RATE', '170', TTS_RATES))


def read_extent() -> str:
    return _choice('KILIX_VOICE_TTS_EXTENT', 'screen', TTS_EXTENTS)


def stt_engine() -> str:
    return _choice('KILIX_VOICE_STT_ENGINE', 'vosk', STT_ENGINES)


def stt_model() -> str:
    return _choice('KILIX_VOICE_STT_MODEL', 'small-en-us', STT_MODELS)


def stt_max_seconds() -> int:
    return int(_choice('KILIX_VOICE_STT_MAX_SECONDS', '30', STT_MAX_SECONDS))


def _storage_home() -> str:
    return os.environ.get('KILIX_STORAGE_HOME') or os.path.join(
        os.path.expanduser('~'), '.local', 'gpu_terminal', 'kilix')


def session_voice_dir() -> str:
    root = os.environ.get('KILIX_SESSION_HOME') or os.path.join(
        _storage_home(), 'session')
    return os.path.abspath(os.path.join(os.path.expanduser(root), 'voice'))


def data_voice_dir() -> str:
    root = os.environ.get('KILIX_DATA_HOME') or os.path.join(
        _storage_home(), 'data')
    return os.path.abspath(os.path.join(os.path.expanduser(root), 'voice'))


def control_socket_path() -> str:
    return os.path.join(session_voice_dir(), 'control.sock')


def _ensure_session_voice_dir() -> str:
    path = session_voice_dir()
    os.makedirs(path, mode=0o700, exist_ok=True)
    # makedirs honours the umask, and an existing directory keeps whatever mode
    # it already had, so neither call alone guarantees 0700.
    os.chmod(path, 0o700)
    return path


def voice_daemon_target() -> tuple[list[str], str | None] | None:
    """Resolve kilix-voiced without relying on the caller's cwd."""
    # An explicitly installed command is the most reliable target. In
    # particular, do not let an incomplete development checkout shadow it.
    if executable := which('kilix-voiced'):
        return [executable], None
    source_home = os.environ.get('GPU_TERMINAL_SOURCE_HOME') or os.path.join(
        os.path.expanduser('~'), 'gpu_terminal')
    project = os.path.join(
        os.path.abspath(os.path.expanduser(source_home)),
        'kilix-apps', 'kilix-voice')
    candidate = os.path.join(project, 'kilix-voiced')
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return [candidate], project
    # Every Kilix launch exports KILIX_HOME. Its `voice` command installs the
    # exact kilix-voice closure pinned by this Kilix checkout before starting
    # the daemon, which also makes a fresh standalone checkout useful.
    if kilix_home := os.environ.get('KILIX_HOME'):
        kilix = os.path.join(kilix_home, 'kilix')
        if os.path.isfile(kilix) and os.access(kilix, os.X_OK):
            return [kilix, 'voice', 'daemon'], None
    return None


def _tts_available(engine: str) -> bool:
    if engine == 'off':
        return False
    # mbrola is driven through espeak-ng, so one synthesiser answers for both,
    # and something has to be able to play the result.
    if not (which('espeak-ng') or which('espeak')):
        return False
    return bool(which('pacat') or which('paplay') or which('aplay'))


def _stt_available(engine: str, model: str) -> bool:
    if engine == 'off':
        return False
    if not (which('parec') or which('arecord')):
        return False
    data = data_voice_dir()
    if not os.path.isdir(os.path.join(data, 'models', model)):
        return False
    if engine == 'vosk':
        return os.path.isfile(os.path.join(data, 'lib', 'current', 'libvosk.so'))
    return True


def _availability() -> dict[str, bool]:
    """Which widgets have a working engine behind them, cheaply.

    This probes PATH and the data directory, so it is cached: the signature
    catches a settings change immediately and the deadline catches an install
    that happened without one.
    """
    global _AVAILABILITY_CACHE, _AVAILABILITY_SIGNATURE, _AVAILABILITY_UNTIL
    signature = (tts_engine(), stt_engine(), stt_model())
    now = time.monotonic()
    if signature != _AVAILABILITY_SIGNATURE or now >= _AVAILABILITY_UNTIL:
        _AVAILABILITY_CACHE = {
            'speak': _tts_available(signature[0]),
            'dictate': _stt_available(signature[1], signature[2]),
        }
        _AVAILABILITY_SIGNATURE = signature
        _AVAILABILITY_UNTIL = now + _AVAILABILITY_SECONDS
    return _AVAILABILITY_CACHE


def _idle_color() -> int:
    from .fast_data_types import get_options
    # The default inactive-tab foreground is too dim for a persistent control;
    # the volume and network widgets use the terminal foreground for the same
    # reason, and these sit between them.
    return (color_as_int(get_options().foreground) << 8) | 2


def _widget_color(active: bool, available: bool) -> int:
    # The error tint outranks the rest for its five seconds: whatever the user
    # clicked is what they are looking at the widget to explain.
    if time.monotonic() < voice_state.last_error_until:
        return _ERROR_COLOR
    if active:
        return _ACTIVE_COLOR
    return _idle_color() if available else _UNAVAILABLE_COLOR


def speak_segment() -> tuple[str, str, int] | None:
    if not chrome_enabled('KILIX_CHROME_SPEAK'):
        return None
    return (f' {SPEAK_GLYPH} ', SPEAK_ACTION,
            _widget_color(voice_state.speaking, _availability()['speak']))


def dictate_segment() -> tuple[str, str, int] | None:
    if not chrome_enabled('KILIX_CHROME_DICTATE'):
        return None
    available = _availability()['dictate']
    glyph = MICROPHONE_GLYPH if available else MICROPHONE_OFF_GLYPH
    return (f' {glyph} ', DICTATE_ACTION,
            _widget_color(voice_state.listening, available))


def _invalidate() -> None:
    from .kilix_battery import _invalidate_all_chrome
    with suppress(Exception):
        _invalidate_all_chrome()


def flag_error(message: str) -> str:
    """Tint the widgets for five seconds and hand the message back to report."""
    voice_state.last_error_until = time.monotonic() + _ERROR_SECONDS
    ensure_voice_timer()
    _invalidate()
    return message


def _spawn_daemon() -> bool:
    target = voice_daemon_target()
    if target is None:
        return False
    cmd, cwd = target
    try:
        subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError as e:
        log_error(f'kilix voice: failed to start {cmd[0]}: {e}')
        return False
    return True


def _connect_control() -> socket.socket | None:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except OSError as e:
        log_error(f'kilix voice: SOCK_SEQPACKET unavailable: {e}')
        return None
    sock.settimeout(_CONTROL_TIMEOUT)
    try:
        sock.connect(control_socket_path())
    except OSError:
        sock.close()
        return None
    return sock


def control_connection(spawn: bool = True) -> socket.socket | None:
    """One connection for the session, opened on first use.

    The daemon reports `speech-done` asynchronously, so the connection has to
    outlive the request that started the speech; a per-click connect would also
    pay the handshake on every button press.
    """
    global _control
    if _control is not None:
        return _control
    sock = _connect_control()
    if sock is None and spawn and _spawn_daemon():
        # The lazy spawn costs this wait once per session — every later click
        # finds the socket already there. Bounded, because a daemon that cannot
        # start must degrade the widget, not freeze the tab bar.
        deadline = time.monotonic() + _SPAWN_SETTLE_SECONDS
        while sock is None and time.monotonic() < deadline:
            time.sleep(_SPAWN_POLL_SECONDS)
            sock = _connect_control()
    _control = sock
    return _control


def _close_control() -> None:
    global _control
    if _control is not None:
        with suppress(OSError):
            _control.close()
        _control = None


def _lost_daemon(detail: str) -> None:
    """Forget a daemon that died, rather than rendering state it no longer has."""
    log_error(f'kilix voice: lost the voice daemon: {detail}')
    _close_control()
    voice_state.speaking = False
    if voice_state.listening:
        _close_dictation_socket()
        voice_state.listening = False
        voice_state.target_window_id = 0
    flag_error('The voice daemon stopped unexpectedly.')


def _recv_message(sock: socket.socket) -> dict[str, object] | None:
    """One SEQPACKET message; None when the peer closed or sent nonsense."""
    raw = sock.recv(_CONTROL_MAX_BYTES)
    if not raw:
        return None
    try:
        message = json.loads(raw.decode('utf-8', 'replace'))
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def _apply_event(message: dict[str, object]) -> None:
    if message.get('event') == 'speech-done':
        voice_state.speaking = False


def send_control(request: dict[str, object], spawn: bool = True) -> dict[str, object] | None:
    """Send one request, return its reply, or None when unreachable.

    This runs on kitty's UI thread, so every wait is bounded by
    _CONTROL_TIMEOUT. Async events that arrive while the reply is outstanding
    are applied in passing instead of being discarded. `spawn=False` is for the
    stop requests: a daemon that is not running has nothing to stop, and
    starting one to say so would be the wrong answer to a click.
    """
    sock = control_connection(spawn)
    if sock is None:
        return None
    try:
        sock.settimeout(_CONTROL_TIMEOUT)
        sock.send(json.dumps(request).encode('utf-8') + b'\n')
        # The budget covers the whole exchange, not each recv, so an event
        # arriving ahead of the reply cannot double the time spent here.
        deadline = time.monotonic() + _CONTROL_TIMEOUT
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            message = _recv_message(sock)
            if message is None:
                _lost_daemon('control connection closed')
                return None
            if 'event' in message:
                _apply_event(message)
                continue
            return message
    except TimeoutError:
        return None
    except OSError as e:
        _lost_daemon(str(e))
    return None


def _drain_control_events() -> None:
    """Apply whatever the daemon has said since the last poll, without waiting."""
    sock = _control
    if sock is None:
        return
    sock.settimeout(0.0)
    while True:
        try:
            message = _recv_message(sock)
        except (BlockingIOError, TimeoutError):
            return
        except OSError as e:
            _lost_daemon(str(e))
            return
        if message is None:
            _lost_daemon('control connection closed')
            return
        _apply_event(message)


def speak(text: str) -> str | None:
    """Ask the daemon to read `text`. Returns an error message, or None."""
    text = text.strip()
    if not text:
        return flag_error('This pane has no text to read.')
    if not _availability()['speak']:
        return flag_error(
            'No speech engine is available. Install espeak-ng, then run '
            '`kilix voice doctor`.')
    voice_state.speech_id = next(_ids)
    reply = send_control({
        'op': 'speak',
        'text': text[:_MAX_REQUEST_CHARS],
        # The daemon reads the same shared settings file, but carrying the voice
        # and rate in the request means a settings rewrite landing between this
        # click and the synthesis cannot split one read across two settings.
        'voice': tts_voice(),
        'rate': tts_rate(),
        'id': voice_state.speech_id,
    })
    if reply is None:
        return flag_error(
            'The voice daemon could not be reached. Run `kilix voice doctor`.')
    if not reply.get('ok'):
        return flag_error(
            str(reply.get('error') or 'The voice daemon refused the request.'))
    voice_state.speaking = True
    ensure_voice_timer()
    _invalidate()
    return None


def stop_speech() -> None:
    send_control({'op': 'stop-speech', 'id': voice_state.speech_id}, spawn=False)
    voice_state.speaking = False
    _invalidate()


def is_pixel_pane(window: Window) -> bool:
    """True for a pane presenting a framebuffer rather than text.

    `kilix desktop` and `kilix run` paint kitty graphics placements over an
    otherwise empty screen, so as_text() yields blank cells and reading it aloud
    would be forty pauses. Requiring *both* live images and no text keeps an
    ordinary pane readable when it happens to contain an icat image.
    """
    # grman is a C-level member of Screen with no entry in the type stub.
    grman = getattr(window.screen, 'grman', None)
    if not getattr(grman, 'image_count', 0):
        return False
    return not window.as_text().strip()


def pane_echo_disabled(window: Window) -> bool:
    """True when the pane's tty has echo off, which means a password prompt.

    Kilix already refuses to record hidden prompts in session transcripts.
    Speaking a password at a recogniser and typing it into the pane would defeat
    that from the other direction.
    """
    fd = getattr(getattr(window, 'child', None), 'child_fd', None)
    if fd is None:
        return False
    try:
        return not (termios.tcgetattr(fd)[3] & termios.ECHO)
    except (OSError, termios.error):
        return False


def sanitize_for_injection(text: str) -> str:
    """Make recognised text safe to hand to a PTY.

    The single place injected text is filtered, and deliberately so — the rule
    is only reliable if there is one copy of it. A recogniser should never emit
    control characters, but this is where speech becomes bytes on a terminal's
    input, so: nothing below 0x20 except space, no DEL, and nothing in the C1
    range that a pane in a non-UTF-8 mode would read as an eight-bit CSI, OSC or
    DCS introducer. Newlines go with the rest, which is what makes "dictation
    never submits" structural rather than a rule callers have to remember.
    """
    return ''.join(
        ch for ch in text
        if ch == ' ' or 0x20 < ord(ch) < 0x7f or ord(ch) > 0x9f
    ).strip()


def deliver_dictation(text: str) -> None:
    """Inject a finished transcript into the window recorded at click time."""
    text = sanitize_for_injection(text)
    if not text:
        return
    from .fast_data_types import get_boss
    try:
        window = get_boss().window_id_map.get(voice_state.target_window_id)
    except Exception:
        return
    if window is None or window.destroyed:
        # A pane that closed mid-dictation discards its text rather than letting
        # it land in whatever pane inherited the window id.
        return
    if pane_echo_disabled(window):
        # Checked again here and not only at click time: a password prompt can
        # appear while the user is still speaking.
        flag_error('The pane is at a hidden prompt; the transcript was discarded.')
        return
    if window.screen.in_bracketed_paste_mode:
        # One literal insert rather than a stream of keystrokes, so a shell
        # cannot act on anything inside the transcript.
        text = f'\x1b[200~{text}\x1b[201~'
    window.write_to_child(text)


def _close_dictation_socket() -> None:
    sock, path = voice_state.dictation_socket, voice_state.dictation_path
    voice_state.dictation_socket = None
    voice_state.dictation_path = ''
    if sock is not None:
        with suppress(OSError):
            sock.close()
    if path:
        with suppress(OSError):
            os.unlink(path)


def begin_dictation(window_id: int) -> str | None:
    """Open the fork-owned return socket and ask the daemon to listen.

    Returns an error message, or None. The return socket lives in Kilix's own
    0700 session directory and is created here rather than by the daemon, which
    is what keeps the dictation path out of the remote-control allowlist: text
    comes back to a socket only this process owns and is injected in-process
    into the recorded window.
    """
    if voice_state.listening:
        return None
    if not _availability()['dictate']:
        return flag_error(
            'No speech recogniser is available. Run `kilix voice install`, '
            'then `kilix voice doctor`.')
    try:
        directory = _ensure_session_voice_dir()
    except OSError as e:
        return flag_error(f'Could not prepare the voice session directory: {e}')
    path = os.path.join(directory, f'dictate-{window_id}.sock')
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        # A leftover from a killed Kilix would fail the bind. It can only be
        # ours: the directory it sits in is private and was just re-checked.
        with suppress(FileNotFoundError):
            os.unlink(path)
        sock.bind(path)
        os.chmod(path, 0o600)
        sock.setblocking(False)
    except OSError as e:
        sock.close()
        return flag_error(f'Could not open the dictation socket: {e}')
    voice_state.dictation_socket = sock
    voice_state.dictation_path = path
    voice_state.dictation_id = next(_ids)
    reply = send_control({
        'op': 'dictate',
        'sock': path,
        'max_seconds': stt_max_seconds(),
        'model': stt_model(),
        'id': voice_state.dictation_id,
    })
    if reply is None or not reply.get('ok'):
        _close_dictation_socket()
        if reply is None:
            return flag_error(
                'The voice daemon could not be reached. Run `kilix voice doctor`.')
        return flag_error(
            str(reply.get('error') or 'The voice daemon refused to listen.'))
    voice_state.listening = True
    voice_state.target_window_id = window_id
    voice_state.partial = ''
    ensure_voice_timer()
    _invalidate()
    return None


def poll_dictation() -> None:
    """Drain the dictation socket without blocking and deliver a final result."""
    sock = voice_state.dictation_socket
    if sock is None:
        return
    while True:
        try:
            raw = sock.recv(_DICTATION_MAX_BYTES)
        except BlockingIOError:
            return
        except OSError as e:
            end_dictation()
            flag_error(f'Dictation failed: {e}')
            return
        try:
            message = json.loads(raw.decode('utf-8', 'replace'))
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if (partial := message.get('partial')) is not None:
            # Kept for the live hint text and as the flush fallback below, but
            # deliberately not rendered in the tab bar: a variable-width segment
            # would shove the clock and battery cluster sideways per syllable.
            voice_state.partial = str(partial)
        elif (error := message.get('error')) is not None:
            end_dictation()
            flag_error(str(error))
            return
        elif (final := message.get('final')) is not None:
            deliver_dictation(str(final))
            end_dictation()
            return


def end_dictation(flush: bool = False) -> None:
    """Stop listening and take the return socket down with it.

    `flush` delivers whatever utterance the daemon still had in hand, which is
    what a second click on the microphone is asking for.
    """
    if voice_state.listening:
        reply = send_control(
            {'op': 'stop-dictation', 'id': voice_state.dictation_id}, spawn=False)
        if flush and reply is not None and reply.get('ok'):
            deliver_dictation(str(reply.get('text') or voice_state.partial))
    _close_dictation_socket()
    voice_state.listening = False
    voice_state.target_window_id = 0
    voice_state.partial = ''
    _invalidate()


def _voice_signature() -> tuple[object, ...]:
    return (
        voice_state.speaking,
        voice_state.listening,
        time.monotonic() < voice_state.last_error_until,
    )


def _voice_timer(timer_id: int | None = None) -> None:
    global _VOICE_LAST_SIGNATURE
    del timer_id
    _drain_control_events()
    if voice_state.listening:
        poll_dictation()
    signature = _voice_signature()
    if signature != _VOICE_LAST_SIGNATURE:
        _VOICE_LAST_SIGNATURE = signature
        _invalidate()
    if not any(signature):
        # Idle: nothing speaking, nothing listening, error tint expired. A
        # dictation poll that outlives dictation is the one thing this widget
        # must never do, so the timer stops instead of idling at 100 ms.
        cancel_voice_timer()


def ensure_voice_timer() -> None:
    global _VOICE_LAST_SIGNATURE, _VOICE_TIMER_ID
    if _VOICE_TIMER_ID is not None:
        return
    _VOICE_LAST_SIGNATURE = _voice_signature()
    try:
        from .fast_data_types import add_timer
        _VOICE_TIMER_ID = add_timer(_voice_timer, _VOICE_REFRESH_SECONDS, True)
    except Exception as e:
        log_error(f'Failed to start kilix voice chrome timer: {e}')


def cancel_voice_timer() -> None:
    global _VOICE_TIMER_ID
    if _VOICE_TIMER_ID is None:
        return
    try:
        from .fast_data_types import remove_timer
        remove_timer(_VOICE_TIMER_ID)
    except Exception as e:
        log_error(f'Failed to stop kilix voice chrome timer: {e}')
    _VOICE_TIMER_ID = None

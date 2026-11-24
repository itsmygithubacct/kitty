"""Pane screenshots and recordings for the pane title bar's camera button.

A left click captures the pane's content at full resolution and opens a Save
As dialog. A right click opens a menu: take a screenshot, record the pane,
and choose the image and video formats. While a pane is being recorded its
button turns into a red timer, and clicking it stops the recording and asks
where to save it.

Frames come from the engine's own GPU readback, request_callback_with_thumbnail,
which pane drag thumbnails also use. A capture is therefore exactly what the
pane shows, without the title bar, and needs no screenshot tool or compositor
support. PNG is encoded by the engine; the other formats and every recording
use ffmpeg. A recording is first written as a lossless FFV1 master in the
user's cache directory, then encoded into the chosen format once the user
picks a file name, so changing the format never costs quality.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .kilix_chrome.settings import chrome_value, set_chrome_value
from .utils import log_error

if TYPE_CHECKING:
    from .boss import Boss
    from .window import Window

SCREENSHOT_ACTION = 'kilix_pane_screenshot'
RECORD_ACTION = 'kilix_pane_record'
MENU_ACTION = 'kilix_pane_capture_menu'
# A right click on the button, in either state, opens the capture menu.
BUTTON_ACTIONS = frozenset((SCREENSHOT_ACTION, RECORD_ACTION))
BUTTON_KEY = 'KILIX_CHROME_BUTTON_SCREENSHOT'
IMAGE_FORMAT_KEY = 'KILIX_CAPTURE_IMAGE_FORMAT'
VIDEO_FORMAT_KEY = 'KILIX_CAPTURE_VIDEO_FORMAT'

CAMERA_GLYPH = chr(0xf0100)   # md-camera: idle
RECORD_GLYPH = chr(0xf044a)   # md-record: recording, followed by the time
BUSY_GLYPH = chr(0xf051f)     # md-timer_sand: encoding a capture
SAVED_GLYPH = chr(0xf012c)    # md-check: the last capture was saved
RECORD_RGB = 0xef2929         # Tango scarlet red

FPS = 15
MAX_BACKLOG = 2 * FPS         # frames queued for ffmpeg before they are merged
FRAME_RETRY = 0.5             # seconds before a lost capture request is reissued
FRAME_TIMEOUT = 5.0           # seconds before a capture is reported as failed
SAVED_SECONDS = 3.0
MAX_WIDTH = 1 << 30           # no downscaling
EVEN = 'pad=ceil(iw/2)*2:ceil(ih/2)*2'   # 4:2:0 video needs even dimensions


@dataclass(frozen=True)
class Format:
    key: str
    label: str
    ext: str
    encoder: str              # ffmpeg encoder; empty when the engine encodes it
    args: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    @property
    def extensions(self) -> tuple[str, ...]:
        return (self.ext,) + self.aliases


IMAGE_FORMATS = (
    Format('png', 'PNG (lossless)', 'png', ''),
    Format('jpeg', 'JPEG', 'jpg', 'mjpeg',
           ('-frames:v', '1', '-c:v', 'mjpeg', '-q:v', '2', '-pix_fmt', 'yuvj444p', '-update', '1'),
           ('jpeg',)),
    Format('webp', 'WebP (lossless)', 'webp', 'libwebp',
           ('-frames:v', '1', '-c:v', 'libwebp', '-lossless', '1')),
    Format('tiff', 'TIFF', 'tiff', 'tiff',
           ('-frames:v', '1', '-c:v', 'tiff', '-compression_algo', 'deflate', '-update', '1'), ('tif',)),
    Format('bmp', 'BMP', 'bmp', 'bmp', ('-frames:v', '1', '-c:v', 'bmp', '-update', '1')),
)
VIDEO_FORMATS = (
    Format('mp4', 'MP4 (H.264)', 'mp4', 'libx264',
           ('-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p',
            '-vf', EVEN, '-movflags', '+faststart')),
    Format('webm', 'WebM (VP9)', 'webm', 'libvpx-vp9',
           ('-c:v', 'libvpx-vp9', '-crf', '32', '-b:v', '0', '-row-mt', '1', '-deadline', 'good',
            '-cpu-used', '4', '-pix_fmt', 'yuv420p', '-vf', EVEN)),
    Format('mkv', 'MKV (H.264)', 'mkv', 'libx264',
           ('-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p', '-vf', EVEN)),
    Format('gif', 'GIF (animated)', 'gif', 'gif',
           ('-vf', 'split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle',
            '-loop', '0')),
    Format('apng', 'APNG (animated PNG)', 'apng', 'apng', ('-c:v', 'apng', '-plays', '0', '-f', 'apng'), ('png',)),
    Format('webp', 'WebP (animated)', 'webp', 'libwebp_anim',
           ('-c:v', 'libwebp_anim', '-lossless', '0', '-quality', '80', '-loop', '0')),
)
KINDS: dict[str, tuple[tuple[Format, ...], str, str]] = {
    'image': (IMAGE_FORMATS, IMAGE_FORMAT_KEY, 'png'),
    'video': (VIDEO_FORMATS, VIDEO_FORMAT_KEY, 'mp4'),
}


def formats(kind: str) -> tuple[Format, ...]:
    return KINDS[kind][0]


def selected_format(kind: str) -> Format:
    choices, key, default = KINDS[kind]
    value = chrome_value(key, default).strip().lower()
    return next((f for f in choices if f.key == value), next(f for f in choices if f.key == default))


def select_format(kind: str, fmt: Format) -> None:
    set_chrome_value(KINDS[kind][1], fmt.key)


# ffmpeg ---------------------------------------------------------------------

_encoder_cache: tuple[str, frozenset[str]] | None = None


def ffmpeg() -> str:
    return shutil.which('ffmpeg') or ''


def ffmpeg_encoders() -> frozenset[str]:
    global _encoder_cache
    exe = ffmpeg()
    if not exe:
        return frozenset()
    if _encoder_cache is None or _encoder_cache[0] != exe:
        names: set[str] = set()
        try:
            out = subprocess.run(
                [exe, '-hide_banner', '-encoders'], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = ''
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] == 'V':
                names.add(parts[1])
        _encoder_cache = exe, frozenset(names)
    return _encoder_cache[1]


def unavailable_reason(fmt: Format, master: bool = False) -> str:
    """Why this format cannot be written here, or '' when it can."""
    if not fmt.encoder and not master:
        return ''
    if not ffmpeg():
        return 'needs ffmpeg'
    needed = {fmt.encoder} if fmt.encoder else set()
    if master:
        needed.add('ffv1')
    missing = sorted(needed - ffmpeg_encoders())
    return f'needs the ffmpeg {missing[0]} encoder' if missing else ''


def _ffmpeg_cmd(*args: str) -> list[str]:
    return [ffmpeg(), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', *args]


def _raw_input(width: int, height: int) -> list[str]:
    return ['-f', 'rawvideo', '-pix_fmt', 'rgba', '-video_size', f'{width}x{height}',
            '-framerate', str(FPS), '-i', 'pipe:0']


def _run_ffmpeg(cmd: list[str], data: bytes | None = None) -> None:
    proc = subprocess.run(cmd, input=data, stdin=None if data is not None else subprocess.DEVNULL,
                          capture_output=True, timeout=3600)
    if proc.returncode != 0:
        detail = proc.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise OSError(detail[-1] if detail else f'ffmpeg exited with status {proc.returncode}')


# pixels -----------------------------------------------------------------------

def opaque(pixels: bytes, width: int, height: int) -> bytes:
    """The readback keeps the window's alpha; a screenshot should not."""
    buf = bytearray(pixels)
    buf[3::4] = b'\xff' * (width * height)
    return bytes(buf)


def fit_frame(pixels: bytes, width: int, height: int, to_width: int, to_height: int) -> bytes:
    """Crop or pad (with black) a frame to the recording's size."""
    if (width, height) == (to_width, to_height):
        return pixels
    row, copy = to_width * 4, min(width, to_width) * 4
    out = bytearray(row * to_height)
    for y in range(min(height, to_height)):
        start = y * width * 4
        out[y * row:y * row + copy] = pixels[start:start + copy]
    return bytes(out)


def encode_image(pixels: bytes, width: int, height: int, fmt: Format, dest: str) -> None:
    if not fmt.encoder:
        from .fast_data_types import png_from_32bit_rgba_data
        with open(dest, 'wb') as stream:
            stream.write(png_from_32bit_rgba_data(pixels, width, height))
        return
    _run_ffmpeg(_ffmpeg_cmd(*_raw_input(width, height), *fmt.args, dest), pixels)


def encode_video(master: str, fmt: Format, dest: str) -> None:
    _run_ffmpeg(_ffmpeg_cmd('-i', master, *fmt.args, dest))


def _umask() -> int:
    try:
        with open('/proc/self/status') as stream:
            for line in stream:
                if line.startswith('Umask:'):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    return 0o022


def write_atomically(dest: str, fmt: Format, write: Callable[[str], None]) -> None:
    """Write next to dest, then rename, so a failure never leaves half a file."""
    directory = os.path.dirname(dest) or '.'
    fd, temporary = tempfile.mkstemp(
        prefix=f'.{os.path.basename(dest)}.', suffix=f'.{fmt.ext}', dir=directory)
    os.close(fd)
    try:
        write(temporary)
        os.chmod(temporary, 0o666 & ~_umask())
        os.replace(temporary, dest)
        temporary = ''
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


# names and places ---------------------------------------------------------------

_last_directory: dict[str, str] = {}


def _xdg_user_dir(name: str) -> str:
    home = os.path.expanduser('~')
    config = os.environ.get('XDG_CONFIG_HOME') or os.path.join(home, '.config')
    try:
        with open(os.path.join(config, 'user-dirs.dirs'), encoding='utf-8') as stream:
            for line in stream:
                key, sep, value = line.strip().partition('=')
                if sep and key == f'XDG_{name}_DIR':
                    value = value.strip().strip('"').replace('$HOME', home)
                    return os.path.normpath(value) if os.path.isabs(value) else ''
    except OSError:
        pass
    return ''


def default_directory(kind: str) -> str:
    remembered = _last_directory.get(kind, '')
    if remembered and os.path.isdir(remembered):
        return remembered
    home = os.path.expanduser('~')
    name, fallback = ('PICTURES', 'Pictures') if kind == 'image' else ('VIDEOS', 'Videos')
    for candidate in (_xdg_user_dir(name), os.path.join(home, fallback)):
        if candidate and os.path.isdir(candidate):
            return candidate
    return home


def suggested_stem(title: str, when: float | None = None) -> str:
    stamp = time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime(time.time() if when is None else when))
    slug = re.sub(r'[^A-Za-z0-9]+', '-', title).strip('-').lower()[:32].strip('-')
    return f'kilix-{slug}-{stamp}' if slug else f'kilix-{stamp}'


def resolve_destination(path: str, kind: str, fallback: Format, current_filter: str = '') -> tuple[str, Format]:
    """The file to write and its format.

    A known extension the user typed decides the format. Otherwise the filter
    shown when they accepted decides it, and its extension is added; with the
    catch-all filter the selected format is used.
    """
    path = os.path.abspath(os.path.expanduser(path))
    ext = os.path.splitext(path)[1].lower().lstrip('.')
    choices = formats(kind)
    for fmt in choices:
        if ext and ext in fmt.extensions:
            return path, fmt
    fmt = next((f for f in choices if f.label == current_filter), fallback)
    return f'{path}.{fmt.ext}', fmt


# capture state shown on the button --------------------------------------------

_busy: dict[int, int] = {}
_saved: dict[int, float] = {}


def _refresh(window_id: int) -> None:
    from .fast_data_types import get_boss
    boss = get_boss()
    if boss is None or (w := boss.window_id_map.get(window_id)) is None:
        return
    tab = w.tabref()
    w.update_title_bar(is_active=tab is not None and tab.active_window is w)


def title_state(window_id: int) -> str:
    """'' (idle), 'rec:SECONDS', 'busy' or 'saved' for the title bar."""
    if (rec := _recordings.get(window_id)) is not None and not rec.stopping:
        return f'rec:{int(rec.elapsed())}'
    if _busy.get(window_id):
        return 'busy'
    if _saved.get(window_id, 0) > time.monotonic():
        return 'saved'
    return ''


def button(state: str) -> tuple[str, str, int | None, bool]:
    """(text, action, RGB colour, always shown) for the title-bar button."""
    if state.startswith('rec:'):
        seconds = int(state[4:] or 0)
        return f' {RECORD_GLYPH} {seconds // 60}:{seconds % 60:02d} ', RECORD_ACTION, RECORD_RGB, True
    glyph = {'busy': BUSY_GLYPH, 'saved': SAVED_GLYPH}.get(state, CAMERA_GLYPH)
    return f' {glyph} ', SCREENSHOT_ACTION, None, False


def _mark_saved(window_id: int) -> None:
    from .fast_data_types import add_timer
    _saved[window_id] = time.monotonic() + SAVED_SECONDS
    _refresh(window_id)

    def clear(timer_id: int | None = None) -> None:
        if _saved.get(window_id, 0) <= time.monotonic():
            _saved.pop(window_id, None)
            _refresh(window_id)
    add_timer(clear, SAVED_SECONDS + 0.05, False)


def yield_gil() -> None:
    """Let worker threads run for a moment.

    The engine's event loop keeps the GIL while it waits for events, so a
    Python thread only runs when the UI thread gives the GIL up. Each poll
    tick sleeps briefly for that reason; the worker then runs until it blocks
    in a system call (writing to ffmpeg, waiting for it), which releases the
    GIL again.
    """
    time.sleep(0.002)


def run_job(window_id: int, job: Callable[[], None], done: Callable[[str], None]) -> None:
    """Run job off the UI thread, then call done(error) on the UI thread."""
    from .fast_data_types import add_timer, remove_timer
    result: list[str] = []
    _busy[window_id] = _busy.get(window_id, 0) + 1
    _refresh(window_id)

    def work() -> None:
        try:
            job()
        except Exception as error:
            result.append(str(error) or type(error).__name__)
        else:
            result.append('')

    def poll(timer_id: int | None = None) -> None:
        if not result:
            yield_gil()
            return
        remove_timer(timer)
        if _busy.get(window_id, 0) <= 1:
            _busy.pop(window_id, None)
        else:
            _busy[window_id] -= 1
        _refresh(window_id)
        done(result[0])

    threading.Thread(target=work, name='kilix-capture', daemon=True).start()
    # One repeating timer: a timer armed from inside its own callback is not
    # guaranteed to wake an idle event loop.
    timer = add_timer(poll, 0.05, True)


# frames -----------------------------------------------------------------------

FrameConsumer = Callable[[bytes | None, int, int], None]


class FrameRequests:
    """Share the engine's single capture slot between panes.

    The engine keeps one pending capture request; a pane drag can replace
    ours, so a request that is not answered in FRAME_RETRY is issued again,
    and one that is never answered fails after FRAME_TIMEOUT.
    """

    def __init__(self) -> None:
        self.pending: dict[int, list[FrameConsumer]] = {}
        self.since: dict[int, float] = {}
        self.inflight: tuple[int, float] | None = None
        self.timer: int | None = None

    def request(self, window_id: int, consumer: FrameConsumer) -> None:
        self.pending.setdefault(window_id, []).append(consumer)
        self.since.setdefault(window_id, time.monotonic())
        if self.inflight is None:
            self.issue()
        if self.timer is None:
            from .fast_data_types import add_timer
            self.timer = add_timer(self.watchdog, FRAME_RETRY / 2, True)

    def has_pending(self, window_id: int) -> bool:
        return window_id in self.pending

    def _fail(self, window_id: int) -> None:
        self.since.pop(window_id, None)
        for consumer in self.pending.pop(window_id, ()):
            self._deliver(consumer, None, 0, 0)

    @staticmethod
    def _deliver(consumer: FrameConsumer, pixels: bytes | None, width: int, height: int) -> None:
        try:
            consumer(pixels, width, height)
        except Exception:
            import traceback
            log_error('Pane capture failed:\n' + traceback.format_exc())

    def issue(self) -> None:
        from .fast_data_types import get_boss, get_tab_being_dragged, get_window_being_dragged, request_callback_with_thumbnail
        self.inflight = None
        boss = get_boss()
        if get_window_being_dragged()[0] or get_tab_being_dragged()[0]:
            return  # never take the slot a drag thumbnail is waiting for
        for window_id in list(self.pending):
            w = boss.window_id_map.get(window_id) if boss is not None else None
            if w is None:
                self._fail(window_id)
                continue
            request_callback_with_thumbnail('kilix_capture_frame', w.os_window_id, window_id, False, 1.0, MAX_WIDTH)
            self.inflight = window_id, time.monotonic()
            return

    def on_frame(self, window_id: int, pixels: bytes, width: int, height: int) -> None:
        self.inflight = None
        self.since.pop(window_id, None)
        for consumer in self.pending.pop(window_id, ()):
            self._deliver(consumer, pixels if width and height else None, width, height)
        self.issue()

    def watchdog(self, timer_id: int | None = None) -> None:
        now = time.monotonic()
        for window_id, since in list(self.since.items()):
            if now - since > FRAME_TIMEOUT:
                self._fail(window_id)
        if not self.pending:
            from .fast_data_types import remove_timer
            if self.timer is not None:
                remove_timer(self.timer)
            self.timer, self.inflight = None, None
            return
        if self.inflight is None or now - self.inflight[1] > FRAME_RETRY:
            self.issue()


frame_requests = FrameRequests()


def on_frame(os_window_id: int, window_id: int, pixels: bytes, width: int, height: int) -> None:
    frame_requests.on_frame(window_id, pixels, width, height)


# recording ----------------------------------------------------------------------

def _cache_directory() -> str:
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    directory = os.path.join(base, 'kilix', 'capture')
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def pane_is_showing(w: 'Window') -> bool:
    tab = w.tabref()
    if tab is None or (manager := tab.tab_manager_ref()) is None or manager.active_tab is not tab:
        return False
    group = tab.windows.group_for_window(w)
    return group is not None and group.is_visible_in_layout


class Recording:
    """One pane recording: frames on the UI thread, ffmpeg on a writer thread."""

    def __init__(self, window: 'Window') -> None:
        from .fast_data_types import add_timer
        self.window_id = window.id
        self.title = window.title or ''
        self.started = time.monotonic()
        self.wall_started = time.time()
        self.stopping = False
        self.paused = 0
        self.size: tuple[int, int] | None = None
        self.last: tuple[bytes, int, int] | None = None
        self.submitted = 0
        self.queue: deque[list[Any]] = deque()
        self.cond = threading.Condition()
        self.closed = False
        self.error = ''
        self.finished = threading.Event()
        self.proc: subprocess.Popen[bytes] | None = None
        self.directory = tempfile.mkdtemp(prefix='recording-', dir=_cache_directory())
        self.master = os.path.join(self.directory, 'master.mkv')
        self.last_second = -1
        self.timer = add_timer(self.tick, 1 / FPS, True)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def frame_index(self) -> int:
        return int(self.elapsed() * FPS)

    def tick(self, timer_id: int | None = None) -> None:
        from .fast_data_types import get_boss
        w = get_boss().window_id_map.get(self.window_id)
        if w is None:
            stop_recording(self.window_id)
            return
        if (second := int(self.elapsed())) != self.last_second:
            self.last_second = second
            _refresh(self.window_id)
        if self.error:
            stop_recording(self.window_id)
            return
        if self.queue:
            yield_gil()  # let the writer hand queued frames to ffmpeg
        if self.paused or not pane_is_showing(w):
            self.extend(self.frame_index())  # hold the last picture
        elif not frame_requests.has_pending(self.window_id):
            frame_requests.request(self.window_id, self.on_frame)

    def on_frame(self, pixels: bytes | None, width: int, height: int) -> None:
        if self.stopping or pixels is None:
            return
        if self.size is None:
            self.start(width, height)
            if self.error:
                return
        index = self.frame_index()
        self.extend(index)
        self.push(pixels, width, height, index + 1 - self.submitted)

    def start(self, width: int, height: int) -> None:
        self.size = width, height
        try:
            self.log = open(os.path.join(self.directory, 'ffmpeg.log'), 'wb')
            self.proc = subprocess.Popen(
                _ffmpeg_cmd(*_raw_input(width, height), '-c:v', 'ffv1', '-level', '3',
                            '-pix_fmt', 'bgr0', self.master),
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.log)
        except OSError as error:
            self.error = f'Could not start ffmpeg: {error}'
            return
        threading.Thread(target=self.write, name='kilix-recording', daemon=True).start()

    def extend(self, index: int) -> None:
        """Repeat the last frame up to (not including) frame index."""
        if self.last is not None and index > self.submitted:
            self.push(*self.last, index - self.submitted, remember=False)

    def push(self, pixels: bytes, width: int, height: int, count: int, remember: bool = True) -> None:
        if count <= 0 or self.proc is None:
            return
        if remember:
            self.last = pixels, width, height
        self.submitted += count
        with self.cond:
            if len(self.queue) >= MAX_BACKLOG:
                # ffmpeg is behind: show the newest picture for the whole gap.
                item = self.queue[-1]
                item[0], item[1], item[2] = pixels, width, height
                item[3] += count
            else:
                self.queue.append([pixels, width, height, count])
            self.cond.notify()

    def write(self) -> None:
        assert self.proc is not None and self.proc.stdin is not None and self.size is not None
        to_width, to_height = self.size
        try:
            while True:
                with self.cond:
                    while not self.queue and not self.closed:
                        self.cond.wait()
                    if not self.queue:
                        break
                    pixels, width, height, count = self.queue.popleft()
                frame = fit_frame(pixels, width, height, to_width, to_height)
                for _ in range(count):
                    self.proc.stdin.write(frame)
        except OSError as error:
            self.error = f'ffmpeg stopped accepting frames: {error}'
        finally:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            if self.proc.wait() != 0 and not self.error:
                self.error = f'ffmpeg exited with status {self.proc.returncode}'
            self.log.close()
            if self.error:
                try:
                    with open(os.path.join(self.directory, 'ffmpeg.log'), 'rb') as stream:
                        tail = stream.read().decode('utf-8', 'replace').strip().splitlines()
                    if tail:
                        self.error += f'\n{tail[-1]}'
                except OSError:
                    pass
            self.finished.set()

    def finish(self) -> None:
        """Stop taking frames; the writer thread drains and closes ffmpeg."""
        from .fast_data_types import remove_timer
        self.stopping = True
        remove_timer(self.timer)
        self.extend(self.frame_index() + 1)
        with self.cond:
            self.closed = True
            self.cond.notify()
        if self.proc is None:
            self.finished.set()

    def discard(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


_recordings: dict[int, Recording] = {}


def is_recording(window_id: int) -> bool:
    return window_id in _recordings and not _recordings[window_id].stopping


def hold(window_id: int) -> Callable[[], None]:
    """Freeze a recording of this pane while one of these dialogs covers it."""
    rec = _recordings.get(window_id)
    if rec is None:
        return lambda: None
    rec.paused += 1
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            rec.paused -= 1
    return release


def start_recording(boss: 'Boss', window: 'Window') -> None:
    if is_recording(window.id):
        return
    if reason := unavailable_reason(selected_format('video'), master=True):
        boss.show_error('Recording unavailable', f'Recording a pane {reason}. Install ffmpeg and try again.')
        return
    try:
        _recordings[window.id] = Recording(window)
    except OSError as error:
        boss.show_error('Recording unavailable', f'Could not prepare the recording: {error}')
        return
    _refresh(window.id)


def stop_recording(window_id: int) -> None:
    from .fast_data_types import add_timer, get_boss, remove_timer
    rec = _recordings.get(window_id)
    if rec is None or rec.stopping:
        return
    rec.finish()
    _busy[window_id] = _busy.get(window_id, 0) + 1
    _refresh(window_id)

    def wait(timer_id: int | None = None) -> None:
        if not rec.finished.is_set():
            yield_gil()
            return
        remove_timer(timer)
        _recordings.pop(window_id, None)
        if _busy.get(window_id, 0) <= 1:
            _busy.pop(window_id, None)
        else:
            _busy[window_id] -= 1
        _refresh(window_id)
        boss = get_boss()
        if rec.error or rec.size is None:
            rec.discard()
            boss.show_error('Recording failed', rec.error or 'No frame of the pane was captured.')
            return
        stem = suggested_stem(rec.title, rec.wall_started)
        save_as(boss, window_id, 'video', stem,
                lambda dest, fmt: encode_video(rec.master, fmt, dest), rec.discard)
    timer = add_timer(wait, 0.05, True)


def toggle_recording(boss: 'Boss', window: 'Window') -> None:
    if is_recording(window.id):
        stop_recording(window.id)
    else:
        start_recording(boss, window)


# screenshots ------------------------------------------------------------------

def take_screenshot(boss: 'Boss', window: 'Window') -> None:
    if not pane_is_showing(window):
        # The capture reads what is drawn where the pane sits; a pane on
        # another page, or behind a maximized one, is not drawn there.
        boss.show_error('Screenshot unavailable', 'Show the pane first: it is not visible on screen.')
        return
    window_id, title = window.id, window.title or ''
    when = time.time()

    def captured(pixels: bytes | None, width: int, height: int) -> None:
        if pixels is None:
            get_boss_().show_error('Screenshot failed', 'The pane could not be captured.')
            return
        image = opaque(pixels, width, height)
        save_as(get_boss_(), window_id, 'image', suggested_stem(title, when),
                lambda dest, fmt: encode_image(image, width, height, fmt, dest))

    frame_requests.request(window_id, captured)


def get_boss_() -> 'Boss':
    from .fast_data_types import get_boss
    return get_boss()


# Save As --------------------------------------------------------------------------

def save_as(
    boss: 'Boss', window_id: int, kind: str, stem: str,
    write: Callable[[str, Format], None], cleanup: Callable[[], None] = lambda: None,
) -> None:
    """Ask for a file name, then write the capture there in its format."""
    selected = selected_format(kind)
    if unavailable_reason(selected):
        selected = formats(kind)[0]
    usable = [f for f in formats(kind) if not unavailable_reason(f)]
    ordered = [selected] + [f for f in usable if f is not selected]
    directory = default_directory(kind)
    noun = 'screenshot' if kind == 'image' else 'recording'

    def chosen(path: str, current_filter: str) -> None:
        dest, fmt = resolve_destination(path, kind, selected, current_filter)
        if reason := unavailable_reason(fmt):
            boss.show_error(f'Cannot save {noun}', f'{fmt.label} {reason}. Choose another format.')
            ask()
            return
        if os.path.isdir(dest):
            boss.show_error(f'Cannot save {noun}', f'{dest} is a folder.')
            ask()
            return

        def go(confirmed: bool = True) -> None:
            if not confirmed:
                ask()
                return
            _last_directory[kind] = os.path.dirname(dest)

            def done(error: str) -> None:
                if error:
                    boss.show_error(f'Could not save {noun}', f'{dest}\n{error}')
                    ask()
                    return
                cleanup()
                _mark_saved(window_id)
            run_job(window_id, lambda: write_atomically(dest, fmt, lambda tmp: write(tmp, fmt)), done)

        if os.path.exists(dest):
            target = boss.window_id_map.get(window_id) or boss.active_window
            boss.confirm(f'{os.path.basename(dest)} already exists. Replace it?', go,
                         window=target, title=f'Save {noun}')
        else:
            go()

    def ask() -> None:
        target = boss.window_id_map.get(window_id) or boss.active_window
        if target is None:
            # The pane and every other window are gone: keep the capture.
            chosen(os.path.join(directory, f'{stem}.{selected.ext}'), '')
            return
        release = hold(window_id)
        result: dict[str, Any] = {}

        def on_result(data: dict[str, Any], target_window_id: int, boss: 'Boss') -> None:
            result.update(data)

        def on_removal(wid: int, boss: 'Boss') -> None:
            release()
            paths = result.get('paths') or []
            if paths:
                chosen(str(paths[0]), str(result.get('current_filter') or ''))
            else:
                cleanup()

        others = ', '.join(f'.{f.ext}' for f in ordered[1:])
        hint = f'Save {noun} as {selected.label}' + (
            f' (end the name with {others} for another format)' if others else '')
        args = [
            '--mode=save-file', '--start-with-name', f'--title={hint}', '--display-title',
            f'--suggested-save-file-name={stem}.{selected.ext}',
            *(f'--file-filter=glob:*.{f.ext}:{f.label}' for f in ordered),
            '--file-filter=glob:*:All files', directory,
        ]
        overlay = boss.run_kitten_with_metadata(
            'choose-files', args, window=target, custom_callback=on_result,
            action_on_removal=on_removal, default_data={})
        if overlay is None:
            release()
            chosen(os.path.join(directory, f'{stem}.{selected.ext}'), '')

    ask()


# menu -------------------------------------------------------------------------

def show_menu(boss: 'Boss', window: 'Window') -> None:
    window_id = window.id
    recording = is_recording(window_id)
    image, video = selected_format('image'), selected_format('video')
    choices = (
        's:Take screenshot',
        'r:Stop recording and save…' if recording else 'r:Record screen',
        f'f:Screenshot format: {image.label} ▸',
        f'c:Recording format: {video.label} ▸',
    )
    release = hold(window_id)

    def picked(choice: str) -> None:
        release()
        target = boss.window_id_map.get(window_id)
        if not choice or target is None:
            return
        if choice == 's':
            take_screenshot(boss, target)
        elif choice == 'r':
            toggle_recording(boss, target)
        elif choice in ('f', 'c'):
            show_format_menu(boss, target, 'image' if choice == 'f' else 'video')

    boss.choose('Screenshot and recording', picked, *choices, window=window, title='Screenshot')


def show_format_menu(boss: 'Boss', window: 'Window', kind: str) -> None:
    window_id = window.id
    current = selected_format(kind)
    keys: dict[str, Format] = {}
    choices = []
    for index, fmt in enumerate(formats(kind), 1):
        key = str(index)
        keys[key] = fmt
        mark = '●' if fmt is current else ' '
        reason = unavailable_reason(fmt, master=kind == 'video')
        choices.append(f'{key}:{key}. {mark} {fmt.label}' + (f' ({reason})' if reason else ''))
    choices.append('k:◂ Back')
    noun = 'Screenshot' if kind == 'image' else 'Recording'
    release = hold(window_id)

    def picked(choice: str) -> None:
        release()
        target = boss.window_id_map.get(window_id)
        if not choice or target is None:
            return
        if choice == 'k':
            show_menu(boss, target)
            return
        if (fmt := keys.get(choice)) is None:
            return
        if reason := unavailable_reason(fmt, master=kind == 'video'):
            boss.show_error(f'{fmt.label} unavailable', f'{fmt.label} {reason}.')
            return
        try:
            select_format(kind, fmt)
        except OSError as error:
            boss.show_error('Could not save the setting', str(error))

    boss.choose(f'{noun} format', picked, *choices, window=window, title=f'{noun} format')

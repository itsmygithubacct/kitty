#!/usr/bin/env python
# License: GPL v3

import os
import stat
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kitty import kilix_capture as capture
from kitty.fast_data_types import GLFW_MOUSE_BUTTON_LEFT, GLFW_MOUSE_BUTTON_RIGHT, GLFW_PRESS, GLFW_RELEASE
from kitty.kilix_chrome import settings
from kitty.tabs import MouseEvents, TabManager
from kitty.types import WindowGeometry
from kitty.window_title_bar import WindowTitleBarScreen, WindowTitleData

from . import BaseTest


class TestCaptureButton(BaseTest):

    def render(self, state: str = '', enabled: bool = True) -> WindowTitleBarScreen:
        self.set_options()
        title_bar = WindowTitleBarScreen(1, 10, 20)
        title_bar.layout(WindowGeometry(0, 0, 1600, 20, 160, 1))

        def chrome_enabled(key: str, default: str = '1') -> bool:
            return enabled or key != capture.BUTTON_KEY
        with patch('kitty.window_title_bar.chrome_enabled', side_effect=chrome_enabled):
            title_bar.render(WindowTitleData('pane', True, 1, 1, capture_state=state), '')
        return title_bar

    def columns(self, title_bar: WindowTitleBarScreen, action: str) -> list[int]:
        return sorted(c for c, a in title_bar.button_cols.items() if a == action)

    def test_button_text_and_action_follow_the_capture_state(self) -> None:
        self.ae(capture.button(''), (f' {capture.CAMERA_GLYPH} ', capture.SCREENSHOT_ACTION, None, False))
        self.ae(capture.button('busy')[:2], (f' {capture.BUSY_GLYPH} ', capture.SCREENSHOT_ACTION))
        self.ae(capture.button('saved')[:2], (f' {capture.SAVED_GLYPH} ', capture.SCREENSHOT_ACTION))
        text, action, rgb, always = capture.button('rec:75')
        self.ae(text, f' {capture.RECORD_GLYPH} 1:15 ')
        self.ae(action, capture.RECORD_ACTION)
        self.ae(rgb, capture.RECORD_RGB)
        self.assertTrue(always)

    def test_camera_follows_the_log_button_with_an_exact_hit_area(self) -> None:
        title_bar = self.render()
        camera = self.columns(title_bar, capture.SCREENSHOT_ACTION)
        log = self.columns(title_bar, 'kilix_show_pane_log')
        self.ae(len(camera), 3)
        self.ae(camera, list(range(log[-1] + 1, log[-1] + 4)))
        self.ae(title_bar.button_cols.get(camera[-1] + 1), 'kilix_toggle_synchronized_input')
        line = str(title_bar.screen.line(0))
        self.ae(line[camera[0]:camera[-1] + 1], f' {capture.CAMERA_GLYPH} ')

    def test_turning_the_button_off_hides_it_but_never_hides_a_recording(self) -> None:
        self.assertFalse(self.columns(self.render(enabled=False), capture.SCREENSHOT_ACTION))
        title_bar = self.render('rec:3', enabled=False)
        recording = self.columns(title_bar, capture.RECORD_ACTION)
        self.ae(len(recording), len(f' {capture.RECORD_GLYPH} 0:03 '))
        line = str(title_bar.screen.line(0))
        self.ae(line[recording[0]:recording[-1] + 1], f' {capture.RECORD_GLYPH} 0:03 ')

    def click(self, action: str | None, button: int) -> Mock:
        title_bar = SimpleNamespace(
            cell_width=10, geometry=SimpleNamespace(left=0), button_cols={1: action} if action else {})
        window = SimpleNamespace(id=7, _title_bar_screen=title_bar, tabref=lambda: None)
        boss = SimpleNamespace(
            window_id_map={window.id: window}, set_active_window=Mock(), combine=Mock(),
            show_window_title_menu=Mock())
        manager = object.__new__(TabManager)
        manager.recent_title_bar_mouse_events = MouseEvents()
        with (
            patch('kitty.tabs.get_boss', return_value=boss),
            patch('kitty.tabs.get_options', return_value=SimpleNamespace(drag_threshold=0)),
            patch('kitty.tabs.get_click_interval', return_value=0.5),
            patch('kitty.tabs.get_window_being_dragged', return_value=(0, False, 0, 0)),
            patch('kitty.tabs.set_window_being_dragged'),
        ):
            manager.handle_window_title_bar_mouse(window.id, 15, 5, button, 0, GLFW_PRESS)
            manager.handle_window_title_bar_mouse(window.id, 15, 5, button, 0, GLFW_RELEASE)
        boss.window = window
        return boss

    def test_left_click_dispatches_and_right_click_opens_the_menu(self) -> None:
        boss = self.click(capture.SCREENSHOT_ACTION, GLFW_MOUSE_BUTTON_LEFT)
        boss.combine.assert_called_once_with(capture.SCREENSHOT_ACTION, window_for_dispatch=boss.window)
        for action in (capture.SCREENSHOT_ACTION, capture.RECORD_ACTION):
            boss = self.click(action, GLFW_MOUSE_BUTTON_RIGHT)
            boss.combine.assert_called_once_with(capture.MENU_ACTION, window_for_dispatch=boss.window)
            boss.set_active_window.assert_called_once()

    def test_right_click_elsewhere_on_the_title_bar_does_nothing(self) -> None:
        for action in ('kilix_show_pane_log', None):
            boss = self.click(action, GLFW_MOUSE_BUTTON_RIGHT)
            boss.combine.assert_not_called()
            boss.show_window_title_menu.assert_not_called()


class TestCaptureFiles(BaseTest):

    def test_destination_and_format_from_the_typed_name(self) -> None:
        png, jpeg = capture.IMAGE_FORMATS[0], capture.IMAGE_FORMATS[1]
        resolve = capture.resolve_destination
        self.ae(resolve('/p/a.PNG', 'image', jpeg), ('/p/a.PNG', png))
        self.ae(resolve('/p/a.jpeg', 'image', png), ('/p/a.jpeg', jpeg))
        self.ae(resolve('/p/a.tif', 'image', png)[1].key, 'tiff')
        self.ae(resolve('/p/a', 'image', jpeg), ('/p/a.jpg', jpeg))
        # The filter in view decides when the name has no known extension.
        self.ae(resolve('/p/a.v2', 'image', png, 'WebP (lossless)'), ('/p/a.v2.webp', capture.IMAGE_FORMATS[2]))
        self.ae(resolve('/p/a', 'image', png, 'All files'), ('/p/a.png', png))
        mp4 = capture.VIDEO_FORMATS[0]
        self.ae(resolve('/v/clip.png', 'video', mp4)[1].key, 'apng')
        self.ae(resolve('/v/clip.webp', 'video', mp4)[1].label, 'WebP (animated)')
        self.ae(resolve('/v/clip.jpg', 'video', mp4), ('/v/clip.jpg.mp4', mp4))
        home = os.path.expanduser('~')
        self.ae(resolve('~/x.gif', 'video', mp4)[0], os.path.join(home, 'x.gif'))

    def test_suggested_names(self) -> None:
        when = 1_800_000_000
        stamp = capture.time.strftime('%Y-%m-%d_%H-%M-%S', capture.time.localtime(when))
        self.ae(capture.suggested_stem('~/src: vim main.c', when), f'kilix-src-vim-main-c-{stamp}')
        self.ae(capture.suggested_stem('', when), f'kilix-{stamp}')
        self.ae(capture.suggested_stem('!!!', when), f'kilix-{stamp}')
        self.assertLessEqual(len(capture.suggested_stem('x' * 200, when)), len(f'kilix--{stamp}') + 32)

    def test_pixels_are_made_opaque_and_fitted(self) -> None:
        pixels = bytes([1, 2, 3, 0, 4, 5, 6, 128])
        self.ae(capture.opaque(pixels, 2, 1), bytes([1, 2, 3, 255, 4, 5, 6, 255]))
        frame = bytes(range(2 * 2 * 4))  # 2x2
        self.ae(capture.fit_frame(frame, 2, 2, 2, 2), frame)
        cropped = capture.fit_frame(frame, 2, 2, 1, 1)
        self.ae(cropped, frame[:4])
        padded = capture.fit_frame(frame, 2, 2, 3, 3)
        self.ae(len(padded), 3 * 3 * 4)
        self.ae(padded[:8], frame[:8])
        self.ae(padded[8:12], bytes(4))
        self.ae(padded[12:20], frame[8:16])
        self.ae(padded[24:], bytes(12))

    def test_atomic_write_leaves_nothing_behind_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dest = os.path.join(directory, 'shot.png')
            fmt = capture.IMAGE_FORMATS[0]

            def fail(path: str) -> None:
                with open(path, 'wb') as stream:
                    stream.write(b'partial')
                raise OSError('encoder failed')
            with self.assertRaises(OSError):
                capture.write_atomically(dest, fmt, fail)
            self.ae(os.listdir(directory), [])

            def ok(path: str) -> None:
                with open(path, 'wb') as stream:
                    stream.write(b'image')
            capture.write_atomically(dest, fmt, ok)
            self.ae(os.listdir(directory), ['shot.png'])
            self.ae(stat.S_IMODE(os.stat(dest).st_mode), 0o666 & ~capture._umask())

    def test_formats_need_ffmpeg_except_png(self) -> None:
        with patch('kitty.kilix_capture.shutil.which', return_value=None):
            self.ae(capture.unavailable_reason(capture.IMAGE_FORMATS[0]), '')
            for fmt in capture.IMAGE_FORMATS[1:] + capture.VIDEO_FORMATS:
                self.ae(capture.unavailable_reason(fmt), 'needs ffmpeg')
        with (
            patch('kitty.kilix_capture.shutil.which', return_value='/usr/bin/ffmpeg'),
            patch('kitty.kilix_capture.ffmpeg_encoders', return_value=frozenset({'libx264', 'gif'})),
        ):
            self.ae(capture.unavailable_reason(capture.VIDEO_FORMATS[0]), '')
            self.ae(capture.unavailable_reason(capture.VIDEO_FORMATS[0], master=True), 'needs the ffmpeg ffv1 encoder')
            self.ae(capture.unavailable_reason(capture.VIDEO_FORMATS[1]), 'needs the ffmpeg libvpx-vp9 encoder')

    def test_selected_formats_are_read_and_saved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, 'settings.conf')
            with patch.dict(os.environ, {'GPU_TERMINAL_SETTINGS_FILE': target}):
                self.ae(capture.selected_format('image').key, 'png')
                self.ae(capture.selected_format('video').key, 'mp4')
                capture.select_format('video', capture.VIDEO_FORMATS[3])
                self.ae(capture.selected_format('video').key, 'gif')
                with open(target, 'a') as stream:
                    stream.write('KILIX_CAPTURE_IMAGE_FORMAT=nonsense\n')
                self.ae(capture.selected_format('image').key, 'png')


class TestChromeSettingsWriter(BaseTest):

    def write(self, text: str | None, name: str, value: str) -> str:
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, 'gpu', 'settings.conf')
            with patch.dict(os.environ, {'GPU_TERMINAL_SETTINGS_FILE': target}):
                if text is not None:
                    os.makedirs(os.path.dirname(target))
                    with open(target, 'w') as stream:
                        stream.write(text)
                settings.set_chrome_value(name, value)
                self.ae(stat.S_IMODE(os.stat(target).st_mode), 0o600)
                self.ae(settings.chrome_value(name, ''), value.replace('\n', ' ').strip())
                self.ae(sorted(os.listdir(os.path.dirname(target))), ['settings.conf', 'settings.conf.lock'])
                with open(target) as stream:
                    return stream.read()

    def test_replaces_only_the_last_assignment_and_keeps_everything_else(self) -> None:
        text = '# mine\nA=1\nKILIX_X=old\nB=2\n  KILIX_X=older\nC=3\n'
        self.ae(self.write(text, 'KILIX_X', 'new'), '# mine\nA=1\nKILIX_X=old\nB=2\nKILIX_X=new\nC=3\n')

    def test_new_keys_go_under_the_chrome_section_or_at_the_end(self) -> None:
        marker = settings.SETTINGS_MARKER
        self.ae(self.write(f'A=1\n{marker}\nB=2\n', 'KILIX_X', 'v'), f'A=1\n{marker}\nKILIX_X=v\nB=2\n')
        self.ae(self.write('A=1', 'KILIX_X', 'v'), f'A=1\n{marker}\nKILIX_X=v\n')
        self.ae(self.write(None, 'KILIX_X', ' v\nINJECTED=1 '), f'{marker}\nKILIX_X=v INJECTED=1\n')

    def test_rejects_bad_names_and_symlinked_files(self) -> None:
        with self.assertRaises(ValueError):
            settings.set_chrome_value('bad name', '1')
        with tempfile.TemporaryDirectory() as directory:
            real = os.path.join(directory, 'real.conf')
            with open(real, 'w') as stream:
                stream.write('A=1\n')
            target = os.path.join(directory, 'settings.conf')
            os.symlink(real, target)
            with patch.dict(os.environ, {'GPU_TERMINAL_SETTINGS_FILE': target}), self.assertRaises(OSError):
                settings.set_chrome_value('KILIX_X', '1')
            with open(real) as stream:
                self.ae(stream.read(), 'A=1\n')


class TestRecordingTimeline(BaseTest):

    def recording(self) -> capture.Recording:
        rec = object.__new__(capture.Recording)
        rec.queue = capture.deque()
        rec.cond = capture.threading.Condition()
        rec.submitted = 0
        rec.last = None
        rec.proc = Mock()
        rec.started = 100.0
        return rec

    def test_frames_cover_wall_clock_time_and_gaps_repeat_the_last_frame(self) -> None:
        rec = self.recording()
        rec.push(b'A', 1, 1, 1)
        rec.extend(4)
        rec.push(b'B', 1, 1, 1)
        self.ae([(item[0], item[3]) for item in rec.queue], [(b'A', 1), (b'A', 3), (b'B', 1)])
        self.ae(rec.submitted, 5)
        rec.extend(3)  # never goes backwards
        self.ae(rec.submitted, 5)
        rec.push(b'C', 1, 1, 0)
        self.ae(len(rec.queue), 3)

    def test_a_backlog_is_merged_into_the_newest_frame(self) -> None:
        rec = self.recording()
        for index in range(capture.MAX_BACKLOG + 5):
            rec.push(bytes([index % 256]), 1, 1, 1)
        self.ae(len(rec.queue), capture.MAX_BACKLOG)
        self.ae(rec.queue[-1][0], bytes([(capture.MAX_BACKLOG + 4) % 256]))
        self.ae(rec.queue[-1][3], 6)
        self.ae(sum(item[3] for item in rec.queue), capture.MAX_BACKLOG + 5)

    def test_hold_pauses_once_per_dialog(self) -> None:
        rec = self.recording()
        rec.paused = 0
        with patch.dict(capture._recordings, {5: rec}):
            release = capture.hold(5)
            self.ae(rec.paused, 1)
            release()
            release()
            self.ae(rec.paused, 0)
        capture.hold(6)()  # a pane that is not recording


class TestFrameRequests(BaseTest):

    def test_requests_share_the_slot_and_are_delivered_by_pane(self) -> None:
        requests = capture.FrameRequests()
        boss = SimpleNamespace(window_id_map={1: SimpleNamespace(os_window_id=9), 2: SimpleNamespace(os_window_id=9)})
        issue = Mock()
        got: list[tuple[int, object]] = []
        with (
            patch('kitty.fast_data_types.get_boss', return_value=boss),
            patch('kitty.fast_data_types.request_callback_with_thumbnail', issue),
            patch('kitty.fast_data_types.get_window_being_dragged', return_value=(0, False, 0, 0)),
            patch('kitty.fast_data_types.get_tab_being_dragged', return_value=(0, False, 0, 0)),
            patch('kitty.fast_data_types.add_timer', return_value=1),
            patch('kitty.fast_data_types.remove_timer'),
        ):
            requests.request(1, lambda p, w, h: got.append((1, p)))
            requests.request(2, lambda p, w, h: got.append((2, p)))
            requests.request(1, lambda p, w, h: got.append((11, p)))
            issue.assert_called_once_with('kilix_capture_frame', 9, 1, False, 1.0, capture.MAX_WIDTH)
            requests.on_frame(1, b'x', 1, 1)
            self.ae(got, [(1, b'x'), (11, b'x')])
            self.ae(issue.call_args[0][2], 2)
            requests.on_frame(2, b'y', 1, 1)
            self.ae(got[-1], (2, b'y'))
            requests.watchdog()
            self.assertIsNone(requests.timer)

    def test_drags_keep_the_slot_and_lost_requests_time_out(self) -> None:
        requests = capture.FrameRequests()
        boss = SimpleNamespace(window_id_map={1: SimpleNamespace(os_window_id=9)})
        issue = Mock()
        got: list[object] = []
        clock = [1000.0]
        with (
            patch('kitty.fast_data_types.get_boss', return_value=boss),
            patch('kitty.fast_data_types.request_callback_with_thumbnail', issue),
            patch('kitty.fast_data_types.get_window_being_dragged', return_value=(4, True, 0, 0)),
            patch('kitty.fast_data_types.get_tab_being_dragged', return_value=(0, False, 0, 0)),
            patch('kitty.fast_data_types.add_timer', return_value=1),
            patch('kitty.fast_data_types.remove_timer'),
            patch('kitty.kilix_capture.time.monotonic', side_effect=lambda: clock[0]),
        ):
            requests.request(1, lambda p, w, h: got.append(p))
            issue.assert_not_called()
            clock[0] += capture.FRAME_TIMEOUT + 1
            requests.watchdog()
            self.ae(got, [None])
            self.assertFalse(requests.pending)

    def test_a_closed_pane_fails_its_request(self) -> None:
        requests = capture.FrameRequests()
        got: list[object] = []
        with (
            patch('kitty.fast_data_types.get_boss', return_value=SimpleNamespace(window_id_map={})),
            patch('kitty.fast_data_types.request_callback_with_thumbnail') as issue,
            patch('kitty.fast_data_types.get_window_being_dragged', return_value=(0, False, 0, 0)),
            patch('kitty.fast_data_types.get_tab_being_dragged', return_value=(0, False, 0, 0)),
            patch('kitty.fast_data_types.add_timer', return_value=1),
        ):
            requests.request(3, lambda p, w, h: got.append(p))
        issue.assert_not_called()
        self.ae(got, [None])


class TestScreenshotVisibility(BaseTest):

    def test_a_pane_that_is_not_drawn_is_refused(self) -> None:
        boss = SimpleNamespace(show_error=Mock())
        window = SimpleNamespace(id=4, title='t')
        with (
            patch('kitty.kilix_capture.pane_is_showing', return_value=False),
            patch.object(capture.frame_requests, 'request') as request,
        ):
            capture.take_screenshot(boss, window)
        request.assert_not_called()
        boss.show_error.assert_called_once()
        with (
            patch('kitty.kilix_capture.pane_is_showing', return_value=True),
            patch.object(capture.frame_requests, 'request') as request,
        ):
            capture.take_screenshot(boss, window)
        self.ae(request.call_args[0][0], 4)

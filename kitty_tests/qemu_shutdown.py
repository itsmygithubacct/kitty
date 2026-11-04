import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from kitty.boss import Boss
from kitty.qemu_shutdown import request_shutdown
from . import BaseTest

class TestQemuShutdown(BaseTest):
    def fixture(self):
        window = SimpleNamespace(id=12, user_vars={'kilix_qemu_monitor': '/private/monitor'})
        boss = SimpleNamespace(window_id_map={12: window}, choose=Mock(),
                               handle_kilix_persistent_close_confirmation=Mock(), show_error=Mock())
        self.assertTrue(Boss.confirm_qemu_close(boss, window))
        return boss, window, boss.choose.call_args.args[1]

    def test_choices_and_cancel_default(self):
        boss, window, chosen = self.fixture()
        self.assertEqual(boss.choose.call_args.args[2:], ('s:Shut down VM', 'f:Force close', 'c:Cancel'))
        self.assertEqual(boss.choose.call_args.kwargs['default'], 'c')
        for choice in ('', 'c'):
            chosen(choice)
        boss.handle_kilix_persistent_close_confirmation.assert_not_called()
        chosen('f')
        boss.handle_kilix_persistent_close_confirmation.assert_called_once_with(True, 12)

    def test_stale_dialog_does_not_act_on_replaced_vm(self):
        boss, window, chosen = self.fixture()
        window.user_vars['kilix_qemu_monitor'] = '/different/monitor'
        chosen('f')
        boss.handle_kilix_persistent_close_confirmation.assert_not_called()

    def test_shutdown_ack_and_failure_never_force_close(self):
        import threading
        for error in (None, OSError('monitor unavailable')):
            boss, window, chosen = self.fixture()
            done = threading.Event()
            callbacks = []
            def request(path):
                done.set()
                if error:
                    raise error
            with patch('kitty.qemu_shutdown.request_shutdown', side_effect=request), \
                    patch('kitty.boss.add_timer', side_effect=lambda cb, *args: callbacks.append(cb)):
                chosen('s')
                self.assertTrue(done.wait(2))
                deadline = time.monotonic() + 2
                while callbacks and time.monotonic() < deadline:
                    callbacks.pop(0)(0)
                    time.sleep(.01)
            boss.handle_kilix_persistent_close_confirmation.assert_not_called()
            self.assertEqual(boss.show_error.call_count, int(error is not None))

    def test_ordinary_pane_is_not_intercepted(self):
        boss = SimpleNamespace(choose=Mock())
        self.assertFalse(Boss.confirm_qemu_close(boss, SimpleNamespace(user_vars={})))
        boss.choose.assert_not_called()

    def test_monitor_refuses_non_qemu_peer_and_public_directory(self):
        import socket
        with tempfile.TemporaryDirectory(prefix='kilix-qmp-test-') as directory:
            monitor = str(Path(directory)/'monitor')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(monitor)
                server.listen(1)
                with self.assertRaisesRegex(ValueError, 'peer is not QEMU'):
                    request_shutdown(monitor)
                os.chmod(directory, 0o755)
                with self.assertRaisesRegex(ValueError, 'private directory'):
                    request_shutdown(monitor)

    def test_single_vm_tab_uses_vm_confirmation(self):
        boss = SimpleNamespace(confirm_desktop_close=Mock(return_value=False),
                               confirm_coding_close=Mock(return_value=False),
                               confirm_qemu_close=Mock(return_value=True),
                               close_windows_with_confirmation_msg=Mock())
        class Tab:
            active_window = object()
            def __len__(self):
                return 1
        tab = Tab()
        Boss.confirm_tab_close(boss, tab)
        boss.confirm_desktop_close.assert_called_once_with(tab=tab)
        boss.confirm_coding_close.assert_called_once_with(tab.active_window)
        boss.confirm_qemu_close.assert_called_once_with(tab.active_window)
        boss.close_windows_with_confirmation_msg.assert_not_called()

    def test_real_qemu_power_button_ack_does_not_kill_process(self):
        qemu = shutil.which('qemu-system-x86_64')
        if not qemu:
            self.skipTest('QEMU not installed')
        with tempfile.TemporaryDirectory(prefix='kilix-qmp-test-') as directory:
            monitor = str(Path(directory)/'monitor')
            process = subprocess.Popen([qemu, '-machine', 'none,accel=tcg', '-m', '32',
                                        '-nodefaults', '-display', 'none', '-S',
                                        '-qmp', f'unix:{monitor},server=on,wait=off'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(monitor) and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(os.path.exists(monitor))
                request_shutdown(monitor)
                self.assertIsNone(process.poll())
            finally:
                process.terminate()
                process.communicate(timeout=5)

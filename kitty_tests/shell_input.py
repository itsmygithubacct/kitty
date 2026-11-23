#!/usr/bin/env python
# License: GPL v3

import os
import shutil
import tempfile
from unittest import mock

from kitty import shell_input
from kitty.shell_input import TRIGGER, ShellInputRefused

from . import BaseTest


class FakeScreen:

    def __init__(self) -> None:
        self.count = 0
        self.at = 0
        self.alternate = False
        self.unwritten = 0

    def input_queue_state(self):
        return self.count, self.at, self.unwritten

    def is_using_alternate_linebuf(self):
        return self.alternate


class FakeChild:
    is_pty_brokered = False
    child_fd = 99


class FakeWindow:

    def __init__(self) -> None:
        self.screen = FakeScreen()
        self.child = FakeChild()
        self.written: list[bytes] = []

    def write_to_child(self, data):
        self.written.append(bytes(data))
        self.screen.count += len(data)


class ShellInputEngine(BaseTest):

    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp()
        os.chmod(self.folder, 0o700)
        self.now = 100.0
        self.timers: dict[int, object] = {}
        self.fg = 4242
        patches = [
            mock.patch.object(shell_input, 'directory', lambda: self.folder),
            mock.patch.object(shell_input, 'monotonic', lambda: self.now),
            mock.patch.object(shell_input, 'add_timer', self.add_timer),
            mock.patch.object(shell_input, 'remove_timer', lambda t: self.timers.pop(t, None)),
            mock.patch.object(shell_input, '_process_group', lambda pid: (self.fg, self.fg) if pid == self.fg else (pid, self.fg)),
            mock.patch.object(shell_input.os, 'tcgetpgrp', lambda fd: self.fg),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)
        super().tearDown()

    def add_timer(self, callback, delay, repeats):
        tid = len(self.timers) + 1
        self.timers[tid] = callback
        return tid

    def fire(self):
        (tid, callback), = self.timers.items()
        del self.timers[tid]  # a timer that does not repeat is gone once it fires
        callback(tid)

    def ready(self, w, token='ab' * 16, pid=4242, pending='0', **extra):
        fields = {'v': '1', 'pid': str(pid), 'token': token, 'pending': pending}
        fields.update(extra)
        shell_input.handle_message(w, 'ready;' + ';'.join(f'{k}={v}' for k, v in fields.items()))
        return shell_input.describe(w)

    def submit(self, w, command='echo hi', expect=''):
        answers = []
        shell_input.submit(w, command, expect, lambda r, e: answers.append((r, e)))
        return answers

    def refused(self, w, code, command='echo hi', expect=''):
        with self.assertRaises(ShellInputRefused) as cm:
            self.submit(w, command, expect)
        self.ae(cm.exception.code, code)
        self.ae(w.written, [])

    def test_unavailable_without_a_reported_prompt(self):
        w = FakeWindow()
        d = shell_input.describe(w)
        self.ae((d['verifiable'], d['code']), (False, 'unavailable'))
        self.refused(w, 'unavailable')
        # Malformed and wrong-version reports are ignored
        for bad in ('ready;v=2;pid=1;token=' + 'a' * 32 + ';pending=0', 'ready;v=1;pid=x;token=' + 'a' * 32,
                    'ready;v=1;pid=1;token=../../etc', 'ready;v=1;v=1', 'x' * 600):
            shell_input.handle_message(w, bad)
        self.refused(w, 'unavailable')

    def test_success_and_single_use(self):
        w = FakeWindow()
        d = self.ready(w)
        self.assertTrue(d['verifiable'])
        answers = self.submit(w, 'echo hi', d['prompt_id'])
        self.ae(w.written, [TRIGGER])
        req = os.path.join(self.folder, 'request-' + 'ab' * 16)
        with open(req) as f:
            self.ae(f.read(), 'echo hi')
        self.ae(os.stat(req).st_mode & 0o777, 0o600)
        self.ae(answers, [])
        # Nothing else may be submitted while the shell answers, nor after.
        self.refused_after_trigger(w, 'in_flight')
        shell_input.handle_message(w, 'result;token=' + 'cd' * 16 + ';status=accepted')  # another prompt's token
        self.ae(answers, [])
        shell_input.handle_message(w, 'result;token=' + 'ab' * 16 + ';status=accepted')
        self.ae(answers, [({'status': 'submitted', 'prompt_id': d['prompt_id']}, None)])
        self.assertFalse(os.path.exists(req))
        self.ae(self.timers, {})
        with self.assertRaises(ShellInputRefused) as cm:
            self.submit(w)
        self.ae(cm.exception.code, 'busy')
        # The prompt after the command is quiet although the trigger was just queued.
        d = self.ready(w, token='ef' * 16)
        self.assertTrue(d['verifiable'], d)

    def refused_after_trigger(self, w, code):
        with self.assertRaises(ShellInputRefused) as cm:
            self.submit(w)
        self.ae(cm.exception.code, code)

    def test_engine_refusals_send_nothing(self):
        w = FakeWindow()
        d = self.ready(w)
        self.refused(w, 'stale', expect='0' * 24)
        w.screen.count += 1
        w.screen.at = 100 * 1e9
        self.refused(w, 'input')
        self.ae(shell_input.describe(w)['code'], 'input')
        # A prompt reported right after a key was queued may race type-ahead
        self.now = 100.1
        self.ready(w)
        self.refused(w, 'input')
        self.now = 101
        d = self.ready(w)
        self.assertTrue(d['verifiable'])
        self.ready(w, pending='1')
        self.refused(w, 'input')
        # Input the engine has not yet written may arrive after the shell looked.
        w.screen.unwritten = 3
        self.ready(w)
        self.refused(w, 'input')
        w.screen.unwritten = 0
        self.ready(w)
        w.screen.alternate = True
        self.refused(w, 'busy')
        w.screen.alternate = False
        self.fg = 777
        self.refused(w, 'busy')
        self.fg = 4242
        self.ready(w)
        shell_input.command_started(w)
        self.refused(w, 'busy')
        # The Enter that ran a quick command was read before the command
        # started, so it does not make the next prompt unverifiable...
        w.screen.count += 1
        w.screen.at = 101 * 1e9
        shell_input.command_started(w)
        self.ae(self.ready(w)['verifiable'], True)
        # ...but a key queued while the command ran, just before the prompt, does.
        shell_input.command_started(w)
        w.screen.count += 1
        self.ae(self.ready(w)['code'], 'input')
        self.now = 102
        self.ready(w)
        for bad in ('', '   ', 'echo a\nrm -rf x', 'echo \x1b[31m', 'x' * 5000):
            self.refused(w, 'invalid_command', command=bad)
        self.assertTrue(shell_input.describe(w)['verifiable'])
        self.ae(os.listdir(self.folder), [])

    def test_shell_refusal_and_timeout_cancel(self):
        w = FakeWindow()
        self.ready(w)
        answers = self.submit(w)
        shell_input.handle_message(w, 'result;token=' + 'ab' * 16 + ';status=refused;reason=draft')
        (result, error), = answers
        self.ae((result, error.code), (None, 'draft'))
        self.ae(os.listdir(self.folder), [])
        # No answer: the engine claims the request, so it can never run late
        w.screen.count = 0
        self.ready(w, token='cd' * 16)
        answers = self.submit(w)
        self.fire()
        (result, error), = answers
        self.ae(error.code, 'timeout')
        self.ae(os.listdir(self.folder), [])
        # A shell that claimed it before the timeout is reported, never retried
        self.ready(w, token='ef' * 16)
        answers = self.submit(w)
        open(os.path.join(self.folder, 'claim-' + 'ef' * 16), 'w').close()
        self.fire()
        (result, error), = answers
        self.ae(error.code, 'unconfirmed')
        # Closing the pane cancels an outstanding request
        self.ready(w, token='aa' * 16)
        answers = self.submit(w)
        shell_input.abandon(w)
        self.ae(answers[0][1].code, 'closed')
        self.ae(os.listdir(self.folder), [])

    def test_a_used_prompt_is_not_reused(self):
        w = FakeWindow()
        self.ready(w)
        open(os.path.join(self.folder, 'claim-' + 'ab' * 16), 'w').close()
        with self.assertRaises(ShellInputRefused) as cm:
            self.submit(w)
        self.ae(cm.exception.code, 'stale')

    def test_parse_message(self):
        self.ae(shell_input.parse_message(b'ready;v=1;pid=2'), ('ready', {'v': '1', 'pid': '2'}))
        for bad in ('ready;V=1', 'ready;v', 'ready;v=1;v=2'):
            with self.assertRaises(ValueError):
                shell_input.parse_message(bad)

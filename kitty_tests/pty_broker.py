#!/usr/bin/env python
# License: GPL v3

import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from unittest.mock import patch

from kitty.child import Child
from kitty.pty_broker import (
    configuration,
    journal_limit,
    valid_session_id,
    wrap_command,
)

from . import BaseTest


class TestPtyBrokerIntegration(BaseTest):

    def test_configuration_requires_explicit_absolute_paths(self) -> None:
        self.ae(configuration({}), ('', ''))
        self.ae(configuration({
            'KITTY_PTY_BROKER_EXECUTABLE': 'relative',
            'KITTY_PTY_BROKER_RUNTIME': '/tmp/runtime',
        }), ('', ''))
        with TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'broker'
            executable.write_text('#!/bin/sh\nexit 0\n')
            executable.chmod(
                stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            runtime = Path(temporary) / 'runtime'
            runtime.mkdir()
            self.ae(configuration({
                'KITTY_PTY_BROKER_EXECUTABLE': str(executable),
                'KITTY_PTY_BROKER_RUNTIME': str(runtime),
            }), (str(executable), str(runtime)))

    def test_session_ids_and_wrapped_command_are_bounded(self) -> None:
        self.assertTrue(valid_session_id('0123456789abcdef'))
        self.assertFalse(valid_session_id('../escape'))
        self.assertFalse(valid_session_id('a' * 65))
        with patch.dict(os.environ, {
            'KITTY_PTY_BROKER_JOURNAL_LIMIT': '4096',
        }):
            command = wrap_command(
                '/opt/kpb', '/run/user/1/kpb', 'pane-1',
                ['/bin/bash', '-l'])
        self.ae(command, [
            '/opt/kpb', '--runtime-dir', '/run/user/1/kpb',
            'run', '--id', 'pane-1', '--journal-limit', '4096',
            '--', '/bin/bash', '-l',
        ])
        self.ae(journal_limit({
            'KITTY_PTY_BROKER_JOURNAL_LIMIT': '999999999999999',
        }), 67108864)

    def test_process_metadata_uses_the_durable_child_pid(self) -> None:
        child = Child(['/bin/sh'], '/')
        child.pid = 123
        self.ae(child.process_tree_root_pid, 123)
        child.pty_broker_executable = '/opt/kpb'
        child.pty_broker_runtime = '/run/user/1/kpb'
        child.pty_broker_session_id = 'pane-1'
        with patch.object(child, '_pty_broker_child_pid', return_value=456):
            self.ae(child.process_tree_root_pid, 456)
            with patch.object(
                    child, 'cmdline_of_pid',
                    return_value=['/bin/bash']) as cmdline_of_pid:
                self.ae(child.cmdline, ['/bin/bash'])
                cmdline_of_pid.assert_called_once_with(456)
        with patch.object(child, '_pty_broker_child_pid', return_value=None):
            self.ae(child.process_tree_root_pid, 123)

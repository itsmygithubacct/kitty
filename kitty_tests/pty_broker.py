#!/usr/bin/env python
# License: GPL v3

import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from kitty.boss import Boss
from kitty.child import Child
from kitty.options.types import defaults
from kitty.pty_broker import (
    configuration,
    journal_limit,
    new_session_id,
    transcript_limit,
    transcript_options,
    valid_session_id,
    wrap_command,
)
from kitty.session import Session
from kitty.tabs import SpecialWindow

from . import BaseTest


class TestPtyBrokerIntegration(BaseTest):

    def startup(self):
        session = Session()
        session.add_tab(defaults, 'Desktop')
        session.add_special_window(SpecialWindow(['/opt/kilix', 'desktop'], env={'EXISTING': 'value'}))
        return session

    def test_initial_child_gets_login_association_without_recovery(self) -> None:
        boss = object.__new__(Boss)
        session = self.startup()
        with patch.dict(os.environ, {'KITTY_PTY_BROKER_STARTUP_TOKEN': 'a'*32,
                                    'KITTY_PTY_BROKER_RECOVER_STARTUP': '0'}), \
                patch('kitty.pty_broker.detached_sessions') as scan:
            boss.prepare_pty_broker_startup([session])
        spec = session.tabs[0].windows[0].launch_spec
        self.ae(spec.cmd, ['/opt/kilix', 'desktop'])
        self.ae(spec.env, {'EXISTING': 'value', 'KITTY_PTY_BROKER_STARTUP_SESSION': 'a'*32,
                           'KITTY_PTY_BROKER_STARTUP_SPEC': '1'})
        scan.assert_not_called()

    def test_recovered_initial_child_replaces_startup_before_it_is_spawned(self) -> None:
        boss = object.__new__(Boss)
        session = self.startup()
        with patch.dict(os.environ, {'KITTY_PTY_BROKER_STARTUP_TOKEN': 'a'*32,
                                    'KITTY_PTY_BROKER_RECOVER_STARTUP': '1',
                                    'KITTY_PTY_BROKER_AUTO_RECOVER': '1'}), \
                patch('kitty.pty_broker.configuration', return_value=('/opt/broker', '/run/user/1/broker')), \
                patch('kitty.pty_broker.detached_sessions', return_value=({'id': 'original'},)), \
                patch('kitty.pty_broker.startup_session', return_value={'id': 'original'}) as select:
            boss.prepare_pty_broker_startup([session])
        select.assert_called_once_with(({'id': 'original'},), 'a'*32)
        spec = session.tabs[0].windows[0].launch_spec
        self.ae(spec.cmd, ['/opt/broker', '--runtime-dir', '/run/user/1/broker', 'attach', 'original'])
        self.ae(spec.env['KITTY_PTY_BROKER_BYPASS'], '1')
        self.ae(spec.env['KITTY_PTY_BROKER_SESSION'], 'original')
        self.ae(spec.env['EXISTING'], 'value')
        self.ae(boss._pty_broker_startup_session_id, 'original')
        self.ae(session.tabs[0].name, 'Desktop')

    def test_missing_match_or_disabled_recovery_keeps_default_command(self) -> None:
        for enabled in ('0', '1'):
            session = self.startup()
            boss = object.__new__(Boss)
            with patch.dict(os.environ, {'KITTY_PTY_BROKER_STARTUP_TOKEN': 'a'*32,
                                        'KITTY_PTY_BROKER_RECOVER_STARTUP': '1',
                                        'KITTY_PTY_BROKER_AUTO_RECOVER': enabled}), \
                    patch('kitty.pty_broker.configuration', return_value=('/opt/broker', '/run/user/1/broker')), \
                    patch('kitty.pty_broker.detached_sessions', return_value=()), \
                    patch('kitty.pty_broker.startup_session', return_value=None) as select:
                boss.prepare_pty_broker_startup([session])
            self.ae(session.tabs[0].windows[0].launch_spec.cmd, ['/opt/kilix', 'desktop'])
            self.assertFalse(hasattr(boss, '_pty_broker_startup_session_id'))
            if enabled == '0':
                select.assert_not_called()

    def test_custom_startup_sessions_are_preserved(self) -> None:
        from kitty.launch import parse_launch_args
        multiple_windows = self.startup()
        multiple_windows.add_special_window(SpecialWindow(['/bin/sh']))
        multiple_tabs = self.startup()
        multiple_tabs.add_tab(defaults)
        multiple_tabs.add_special_window(SpecialWindow(['/bin/sh']))
        explicit = self.startup()
        explicit.tabs[0].windows[0].launch_spec = parse_launch_args(['/bin/sh'])
        for sessions in ([self.startup(), self.startup()], [multiple_windows], [multiple_tabs], [explicit], []):
            before = [w.launch_spec for s in sessions for t in s.tabs for w in t.windows]
            with patch.dict(os.environ, {'KITTY_PTY_BROKER_STARTUP_TOKEN': 'a'*32}), \
                    patch('kitty.pty_broker.detached_sessions') as scan:
                object.__new__(Boss).prepare_pty_broker_startup(sessions)
            self.ae([w.launch_spec for s in sessions for t in s.tabs for w in t.windows], before)
            scan.assert_not_called()

    def test_initial_attach_is_not_recovered_again_and_stays_active(self) -> None:
        boss = object.__new__(Boss)
        boss._pty_broker_startup_session_id = 'original'
        original_tab = object()
        class Manager(list):
            active_tab = original_tab
            def new_tab(self, special_window):
                self.append(special_window)
            def set_active_tab(self, tab):
                self.active_tab = tab
        manager = Manager([original_tab])
        with patch.dict(os.environ, {'KITTY_PTY_BROKER_AUTO_RECOVER': '1'}), \
                patch('kitty.pty_broker.configuration', return_value=('/opt/broker', '/run/user/1/broker')), \
                patch('kitty.pty_broker.detached_sessions', return_value=({'id': 'original'}, {'id': 'job'})), \
                patch.object(Boss, 'active_tab_manager', new_callable=PropertyMock, return_value=manager):
            boss.recover_pty_broker_sessions()
            boss.recover_pty_broker_sessions()
        self.ae(len(manager), 2)
        self.ae(manager[1].env['KITTY_PTY_BROKER_SESSION'], 'job')
        self.assertIs(manager.active_tab, original_tab)

    def test_initial_child_role_is_not_inherited_by_later_panes(self) -> None:
        inherited = {'KITTY_PTY_BROKER_STARTUP_SESSION': 'b'*32,
                     'KITTY_PTY_BROKER_STARTUP_TOKEN': 'c'*32,
                     'KITTY_PTY_BROKER_RECOVER_STARTUP': '1', 'UNCHANGED': 'value'}
        opts = SimpleNamespace(term='xterm-kitty', terminfo_type='none', shell_integration={'disabled'})
        boss = SimpleNamespace(encryption_public_key='test-key', listening_on='')
        startup_spec = {'KITTY_PTY_BROKER_STARTUP_SESSION': 'a'*32, 'KITTY_PTY_BROKER_STARTUP_SPEC': '1'}
        for explicit, expected in (({}, None), (startup_spec, 'a'*32)):
            child = Child(['/bin/sh'], '/', env=explicit)
            with patch('kitty.child.default_env', return_value=inherited), \
                    patch('kitty.child.fast_data_types.get_options', return_value=opts), \
                    patch('kitty.child.fast_data_types.get_boss', return_value=boss):
                env, _ = child.get_final_env()
            self.ae(env.get('KITTY_PTY_BROKER_STARTUP_SESSION'), expected)
            self.assertNotIn('KITTY_PTY_BROKER_STARTUP_SPEC', env)
            self.assertNotIn('KITTY_PTY_BROKER_STARTUP_TOKEN', env)
            self.assertNotIn('KITTY_PTY_BROKER_RECOVER_STARTUP', env)
            self.ae(env['UNCHANGED'], 'value')

    def test_copy_env_from_the_initial_pane_does_not_copy_its_role(self) -> None:
        from kitty.launch import get_env, parse_launch_args
        opts = SimpleNamespace(term='xterm-kitty', terminfo_type='none', shell_integration={'disabled'})
        boss = SimpleNamespace(encryption_public_key='test-key', listening_on='')
        with patch('kitty.child.default_env', return_value={}), \
                patch('kitty.child.fast_data_types.get_options', return_value=opts), \
                patch('kitty.child.fast_data_types.get_boss', return_value=boss):
            initial = Child(['/bin/sh'], '/', env={'KITTY_PTY_BROKER_STARTUP_SESSION': 'a'*32,
                                                   'KITTY_PTY_BROKER_STARTUP_SPEC': '1'})
            initial_env, _ = initial.get_final_env()
            self.ae(initial_env['KITTY_PTY_BROKER_STARTUP_SESSION'], 'a'*32)
            # The initial pane's shell exports exactly what it was started with.
            active = SimpleNamespace(foreground_environ=dict(initial_env, COPIED='yes'))
            copied = get_env(parse_launch_args(['--copy-env', '/bin/sh']).opts, active)
            self.ae(copied['KITTY_PTY_BROKER_STARTUP_SESSION'], 'a'*32)
            env, _ = Child(['/bin/sh'], '/', env=copied).get_final_env()
        self.assertNotIn('KITTY_PTY_BROKER_STARTUP_SESSION', env)
        self.assertNotIn('KITTY_PTY_BROKER_STARTUP_SPEC', env)
        self.ae(env['COPIED'], 'yes')

    def test_log_button_uses_clicked_pane_and_opens_a_separate_tab(self) -> None:
        boss = object.__new__(Boss)
        child = type('Child', (), {
            'pty_broker_session_id': 'clicked-pane',
            'final_env': {'KITTY_PTY_BROKER_TRANSCRIPT_DIR': '/private/pane logs'},
        })()
        window = type('Window', (), {
            'child': child, 'os_window_id': 7, 'title': 'source pane',
        })()
        manager = Mock()
        boss.os_window_map = {7: manager}
        boss.window_for_dispatch = window
        with patch('kitty.boss.os.access', return_value=False), patch('kitty.boss.which', return_value='/usr/bin/kilix'):
            boss.kilix_show_pane_log()
        opened = manager.new_tab.call_args.kwargs['special_window']
        self.ae(opened.cmd, ['/usr/bin/kilix', 'transcript', 'view', 'clicked-pane'])
        self.ae(opened.override_title, 'Log: source pane')
        self.ae(opened.env['KILIX_TRANSCRIPT_DIR'], '/private/pane logs')
        self.ae(opened.env['KITTY_PTY_BROKER_BYPASS'], '1')
        manager.reset_mock()
        child.pty_broker_session_id = ''
        with patch.object(boss, 'show_error') as error:
            boss.kilix_show_pane_log()
            error.assert_called_once()
        manager.new_tab.assert_not_called()

    def test_explicit_close_terminates_broker_before_frontend(self) -> None:
        boss = object.__new__(Boss)
        brokered = type('Child', (), {
            'is_pty_brokered': True,
            'terminate_pty_broker': lambda self: True,
        })()
        window = type('Window', (), {'id': 42, 'child': brokered})()
        with patch.object(boss, 'mark_window_for_close') as mark:
            self.assertTrue(boss.close_window_explicitly(window))
            mark.assert_called_once_with(window)

        refusing = type('Child', (), {
            'is_pty_brokered': True,
            'terminate_pty_broker': lambda self: False,
        })()
        window.child = refusing
        with patch.object(boss, 'mark_window_for_close') as mark:
            self.assertFalse(boss.close_window_explicitly(window))
            mark.assert_not_called()

        ordinary = type('Child', (), {'is_pty_brokered': False})()
        window.child = ordinary
        with patch.object(boss, 'mark_window_for_close') as mark:
            self.assertTrue(boss.close_window_explicitly(window))
            mark.assert_called_once_with(window)

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
        # The transcript directory is cleared explicitly: a live Kilix session
        # exports it, and this assertion is about the un-configured shape.
        with patch.dict(os.environ, {
            'KITTY_PTY_BROKER_JOURNAL_LIMIT': '4096',
            'KITTY_PTY_BROKER_TRANSCRIPT_DIR': '',
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

    def test_transcript_options_follow_the_configured_directory(self) -> None:
        # No configured directory means the host did not enable session
        # logging, and no transcript flags are produced.
        self.ae(transcript_options('pane-1', {}), [])
        self.ae(transcript_options('pane-1', {
            'KITTY_PTY_BROKER_TRANSCRIPT_DIR': 'relative/dir',
        }), [])
        self.ae(transcript_options('pane-1', {
            'KITTY_PTY_BROKER_TRANSCRIPT_DIR': '/does/not/exist',
        }), [])
        with TemporaryDirectory() as temporary:
            self.ae(transcript_options('pane-1', {
                'KITTY_PTY_BROKER_TRANSCRIPT_DIR': temporary,
            }), [
                '--transcript', os.path.join(temporary, 'pane-1.log'),
                '--transcript-limit', '8388608',
                '--transcript-graphics', 'elide',
            ])
            # An unrecognised graphics policy falls back to eliding rather
            # than letting a pixel desktop flood the log.
            self.ae(transcript_options('pane-1', {
                'KITTY_PTY_BROKER_TRANSCRIPT_DIR': temporary,
                'KITTY_PTY_BROKER_TRANSCRIPT_GRAPHICS': 'bogus',
                'KITTY_PTY_BROKER_TRANSCRIPT_LIMIT': '65536',
            }), [
                '--transcript', os.path.join(temporary, 'pane-1.log'),
                '--transcript-limit', '65536',
                '--transcript-graphics', 'elide',
            ])
            with patch.dict(os.environ, {
                'KITTY_PTY_BROKER_JOURNAL_LIMIT': '4096',
                'KITTY_PTY_BROKER_TRANSCRIPT_DIR': temporary,
                'KITTY_PTY_BROKER_TRANSCRIPT_GRAPHICS': 'keep',
            }):
                command = wrap_command(
                    '/opt/kpb', '/run/user/1/kpb', 'pane-1',
                    ['/bin/bash', '-l'])
            self.ae(command, [
                '/opt/kpb', '--runtime-dir', '/run/user/1/kpb',
                'run', '--id', 'pane-1', '--journal-limit', '4096',
                '--transcript', os.path.join(temporary, 'pane-1.log'),
                '--transcript-limit', '8388608',
                '--transcript-graphics', 'keep',
                '--', '/bin/bash', '-l',
            ])
        self.ae(transcript_limit({
            'KITTY_PTY_BROKER_TRANSCRIPT_LIMIT': '999999999999999',
        }), 8388608)

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

    def test_session_id_leaves_room_for_the_control_socket_path(self) -> None:
        # The broker builds <runtime>/sessions/<id>/control.sock and refuses a
        # path that will not fit a Unix socket address. A 32-character ID
        # overflowed that with Kilix's own default runtime directory, so every
        # pane failed to start and the terminal exited with no windows.
        session_id = new_session_id()
        self.ae(len(session_id), 16)
        # The test runner supplies an isolated HOME under the platform's
        # temporary directory, which can itself exceed the socket limit on
        # macOS. Exercise representative default homes rather than that
        # unrelated temporary path. Unix socket limits count bytes.
        for home, limit in ((os.path.join('/home', 'kilix'), 108), ('/Users/kilix', 104)):
            with self.subTest(home=home):
                runtime = os.path.join(home, '.local/gpu_terminal/kilix/session/pty-broker')
                projected = os.path.join(runtime, 'sessions', session_id, 'control.sock')
                self.assertLess(len(os.fsencode(projected)), limit)
                old_projected = os.path.join(runtime, 'sessions', 'a' * 32, 'control.sock')
                self.assertGreaterEqual(len(os.fsencode(old_projected)), limit)

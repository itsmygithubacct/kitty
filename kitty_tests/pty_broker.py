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

    def test_pane_titles_survive_beside_the_broker(self) -> None:
        from kitty import pty_broker
        with TemporaryDirectory() as runtime:
            pty_broker._written_titles.clear()
            sid = new_session_id()
            previous_umask = os.umask(0o022)      # permissive, so only the code keeps it private
            self.addCleanup(os.umask, previous_umask)
            self.assertTrue(pty_broker.write_titles(runtime, sid, {
                'tab': 'R4 live work', 'window': 'python3\nsecond line', 'override': None,
                'unexpected': 'dropped'}))
            path = pty_broker.titles_path(runtime, sid)
            self.ae(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.ae(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
            self.ae(pty_broker.read_titles(runtime, sid),
                    {'tab': 'R4 live work', 'window': 'python3 second line'})
            # unchanged titles are not rewritten
            before = os.stat(path).st_ino        # a rewrite replaces the inode
            pty_broker.write_titles(runtime, sid, {'tab': 'R4 live work', 'window': 'python3\nsecond line'})
            self.ae(os.stat(path).st_ino, before)
            # a symlinked sidecar is never followed
            other = new_session_id()
            target = Path(runtime, 'elsewhere.json')
            target.write_text('{"tab": "planted"}')
            os.symlink(target, pty_broker.titles_path(runtime, other))
            self.ae(pty_broker.read_titles(runtime, other), {})
            # junk and invalid ids yield nothing
            Path(pty_broker.titles_path(runtime, other)).unlink()
            Path(pty_broker.titles_path(runtime, other)).write_text('[1, 2]')
            self.ae(pty_broker.read_titles(runtime, other), {})
            self.ae(pty_broker.titles_path(runtime, '../escape'), '')
            self.ae(pty_broker.titles_path('relative', sid), '')
            # pruning keeps live sessions only
            pty_broker.prune_titles(runtime, frozenset({sid}))
            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(pty_broker.titles_path(runtime, other)))
            pty_broker.prune_titles(runtime, frozenset())
            self.assertFalse(os.path.exists(path))

    def test_a_brokered_window_records_its_names_but_not_the_placeholder(self) -> None:
        from kitty import pty_broker
        from kitty.window import Window
        with TemporaryDirectory() as runtime:
            pty_broker._written_titles.clear()
            sid = new_session_id()
            tab = SimpleNamespace(name='R4 live work')
            window = SimpleNamespace(
                child=SimpleNamespace(is_pty_brokered=True, pty_broker_runtime=runtime,
                                      pty_broker_session_id=sid),
                child_title='python3', override_title='recovered:abcdef12', tabref=lambda: tab)
            Window.remember_broker_titles(window)
            self.ae(pty_broker.read_titles(runtime, sid), {'tab': 'R4 live work', 'window': 'python3'})
            window.override_title = 'my notes'
            Window.remember_broker_titles(window)
            self.ae(pty_broker.read_titles(runtime, sid)['override'], 'my notes')
            unbrokered = SimpleNamespace(child=SimpleNamespace(is_pty_brokered=False))
            Window.remember_broker_titles(unbrokered)          # no-op, no error
            # A recovered pane whose program never set a title shows the broker's
            # executable as its default title: that is not a name to bring back.
            for override, env in (('recovered:abcdef12', {}), (None, {'KITTY_PTY_BROKER_BYPASS': '1'})):
                pty_broker._written_titles.clear()
                rsid = new_session_id()
                attach = SimpleNamespace(
                    child=SimpleNamespace(is_pty_brokered=True, pty_broker_runtime=runtime,
                                          pty_broker_session_id=rsid, final_env=env),
                    child_title='kitty-pty-broker', default_title='kitty-pty-broker',
                    override_title=override, tabref=lambda: None)
                Window.remember_broker_titles(attach)
                self.ae(pty_broker.read_titles(runtime, rsid), {})
                attach.child_title = 'vim'                       # ... until its program names it
                Window.remember_broker_titles(attach)
                self.ae(pty_broker.read_titles(runtime, rsid), {'window': 'vim'})
            # An ordinary pane's default title (its shell) is still its name.
            pty_broker._written_titles.clear()
            psid = new_session_id()
            plain = SimpleNamespace(
                child=SimpleNamespace(is_pty_brokered=True, pty_broker_runtime=runtime,
                                      pty_broker_session_id=psid, final_env={}),
                child_title='bash', default_title='bash', override_title=None, tabref=lambda: None)
            Window.remember_broker_titles(plain)
            self.ae(pty_broker.read_titles(runtime, psid), {'window': 'bash'})

    def test_every_naming_path_records_and_ending_paths_forget(self) -> None:
        import inspect
        from kitty import pty_broker
        from kitty.tabs import Tab
        from kitty.window import Window
        # a title change records
        fake = SimpleNamespace(os_window_id=1, tab_id=2, id=3, title='t', tabref=lambda: None,
                               remember_broker_titles=Mock())
        with patch('kitty.window.update_window_title'):
            Window.title_updated(fake)
        fake.remember_broker_titles.assert_called_once_with()
        # renaming a tab records every pane in it
        panes = [Mock(), Mock()]
        class FakeTab(list):
            name = ''
            mark_tab_bar_dirty = Mock()
        tab = FakeTab(panes)
        Tab.set_title(tab, 'R4 live work')
        self.ae(tab.name, 'R4 live work')
        for pane in panes:
            pane.remember_broker_titles.assert_called_once_with()
        # a window joining its tab records (a tab named at launch, or a silent program)
        joining = Mock()
        host = SimpleNamespace(current_layout=Mock(), windows=Mock(), active_window=None,
                               mark_tab_bar_dirty=Mock(), relayout=Mock())
        Tab._add_window(host, joining)
        joining.remember_broker_titles.assert_called_once_with()
        # an explicit close forgets, but only once the broker really ended the session
        for terminated in (True, False):
            boss = object.__new__(Boss)
            boss.mark_window_for_close = Mock()
            child = SimpleNamespace(is_pty_brokered=True, terminate_pty_broker=lambda: terminated,
                                    pty_broker_runtime='/run/user/1/broker', pty_broker_session_id='sess')
            with patch('kitty.pty_broker.forget_titles') as forget:
                Boss.close_window_explicitly(boss, SimpleNamespace(child=child))
            if terminated:
                forget.assert_called_once_with('/run/user/1/broker', 'sess')
            else:
                forget.assert_not_called()
        # child death never blocks on the broker
        self.assertNotIn('query_status', inspect.getsource(Boss.on_child_death))
        # recovery prunes against the live list, timed before the listing
        boss = object.__new__(Boss)
        boss._pty_broker_startup_session_id = ''
        class Manager(list):
            active_tab = None
            def new_tab(self, special_window):
                return None
        events: list[str] = []
        def clock() -> float:
            events.append('clock')
            return 1234.5
        def listing(executable: str, runtime: str) -> frozenset[str]:
            events.append('list')
            return frozenset({'job'})
        with patch.dict(os.environ, {'KITTY_PTY_BROKER_AUTO_RECOVER': '1'}), \
                patch('kitty.pty_broker.configuration', return_value=('/opt/broker', '/run/user/1/broker')), \
                patch('time.time', side_effect=clock), \
                patch('kitty.pty_broker.live_session_ids', side_effect=listing), \
                patch('kitty.pty_broker.prune_titles') as prune, \
                patch('kitty.pty_broker.detached_sessions', return_value=()), \
                patch.object(Boss, 'active_tab_manager', new_callable=PropertyMock, return_value=Manager()):
            boss.recover_pty_broker_sessions()
        prune.assert_called_once()
        self.ae(prune.call_args.args, ('/run/user/1/broker', frozenset({'job'}), 1234.5))
        # the clock is read before the listing: a sidecar written while it ran is kept
        self.assertLess(events.index('clock'), events.index('list'), events)
        # a sidecar written after the listing survives the prune
        with TemporaryDirectory() as runtime:
            pty_broker._written_titles.clear()
            old, new = new_session_id(), new_session_id()
            pty_broker.write_titles(runtime, old, {'window': 'gone'})
            os.utime(pty_broker.titles_path(runtime, old), (1000, 1000))
            pty_broker.write_titles(runtime, new, {'window': 'started meanwhile'})
            pty_broker.prune_titles(runtime, frozenset(), listed_at=2000.0)
            self.assertFalse(os.path.exists(pty_broker.titles_path(runtime, old)))
            self.assertTrue(os.path.exists(pty_broker.titles_path(runtime, new)))

    def test_recovered_panes_get_their_saved_names_back(self) -> None:
        boss = object.__new__(Boss)
        boss._pty_broker_startup_session_id = 'original'
        original_tab = object()
        made = []
        class FakeTab:
            def __init__(self, special_window):
                self.special_window = special_window
                self.name = ''
                self.active_window = SimpleNamespace(child_title='', titled=0)
                self.active_window.title_updated = lambda w=self.active_window: setattr(w, 'titled', w.titled + 1)
            def set_title(self, title):
                self.name = title
        class Manager(list):
            active_tab = original_tab
            def new_tab(self, special_window):
                tab = FakeTab(special_window)
                made.append(tab)
                self.append(tab)
                return tab
            def set_active_tab(self, tab):
                self.active_tab = tab
        saved = {
            'job': {'tab': 'R4 live work', 'window': 'python3'},
            'renamed': {'window': 'shell', 'override': 'my notes'},
        }
        manager = Manager([original_tab])
        with patch.dict(os.environ, {'KITTY_PTY_BROKER_AUTO_RECOVER': '1'}), \
                patch('kitty.pty_broker.configuration', return_value=('/opt/broker', '/run/user/1/broker')), \
                patch('kitty.pty_broker.live_session_ids', return_value=None), \
                patch('kitty.pty_broker.read_titles', side_effect=lambda runtime, sid: dict(saved.get(sid, {}))), \
                patch('kitty.pty_broker.detached_sessions',
                      return_value=({'id': 'original'}, {'id': 'job'}, {'id': 'renamed'}, {'id': 'anonymous'})), \
                patch.object(Boss, 'active_tab_manager', new_callable=PropertyMock, return_value=manager):
            boss.recover_pty_broker_sessions()
        job, renamed, anonymous = made
        self.ae(job.name, 'R4 live work')
        self.assertIsNone(job.special_window.override_title)
        self.ae(job.active_window.child_title, 'python3')
        self.ae(job.active_window.titled, 1)
        self.ae(renamed.special_window.override_title, 'my notes')
        self.ae(renamed.name, '')
        self.ae(anonymous.special_window.override_title, 'recovered:anonymou')
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

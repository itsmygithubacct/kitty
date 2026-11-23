#!/usr/bin/env python
# License: GPLv3 Copyright: 2022, Kovid Goyal <kovid at kovidgoyal.net>


import errno
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from functools import lru_cache, partial

from kitty.bash import decode_ansi_c_quoted_string
from kitty.constants import is_macos, kitten_exe, kitty_base_dir, shell_integration_dir, terminfo_dir
from kitty.fast_data_types import CURSOR_BEAM, CURSOR_BLOCK, CURSOR_UNDERLINE
from kitty.shell_integration import setup_bash_env, setup_fish_env, setup_zsh_env

from . import BaseTest, skip_on_windows


@lru_cache
def bash_ok():
    v = shutil.which('bash')
    if not v:
        return False
    o = subprocess.check_output([v, '-c', 'echo "${BASH_VERSINFO[0]}\n${BASH_VERSINFO[4]}"']).decode('utf-8').splitlines()
    if not o:
        return False
    major_ver, relstatus = o[0], o[-1]
    return int(major_ver) >= 5 and relstatus == 'release'


def extract_sudo_function(
    content: str, opening='sudo() {', closing='}',
    witht='command sudo TERMINFO="$TERMINFO" "$@";', without='command sudo "$@";'
) -> str:
    """Extract the sudo() function from bash/zsh shell integration content using indentation."""
    lines = content.split('\n')
    start_idx = None
    indent_len = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped == opening:
            start_idx = i
            indent_len = len(line) - len(stripped)
            break
    if start_idx is None:
        return None
    for i in range(start_idx + 1, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if stripped == closing and (len(line) - len(line.lstrip())) == indent_len:
            func = '\n'.join(lines[start_idx:i + 1])
            func = func.replace(witht, 'echo "with_terminfo";')
            func = func.replace(without, 'echo "no_terminfo";')
            return func
    return None


def basic_shell_env(home_dir):
    ans = {
        'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'),
        'HOME': home_dir,
        'TERM': 'xterm-kitty',
        'TERMINFO': terminfo_dir,
        'KITTY_SHELL_INTEGRATION': 'enabled',
        'KITTY_INSTALLATION_DIR': kitty_base_dir,
        'BASH_SILENCE_DEPRECATION_WARNING': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
        'WEZTERM_SHELL_SKIP_ALL': '1',  # dont fail if WezTerm's system wide, default on (why?) shell integration is installed
    }
    for x in ('USER', 'LANG'):
        if os.environ.get(x):
            ans[x] = os.environ[x]
    return ans


def safe_env_for_running_shell(argv, home_dir, rc='', shell='zsh', with_kitten=False):
    ans = basic_shell_env(home_dir)
    if shell == 'zsh':
        argv.insert(1, '--noglobalrcs')
        with open(os.path.join(home_dir, '.zshrc'), 'w') as f:
            print(rc + '\nZLE_RPROMPT_INDENT=0', file=f)
        setup_zsh_env(ans, argv)
    elif shell == 'fish':
        conf_dir = os.path.join(home_dir, '.config', 'fish')
        os.makedirs(conf_dir, exist_ok=True)
        # Avoid generating unneeded completion scripts
        os.makedirs(os.path.join(home_dir, '.local', 'share', 'fish', 'generated_completions'), exist_ok=True)
        with open(os.path.join(conf_dir, 'config.fish'), 'w') as f:
            print(rc + '\n', file=f)
        setup_fish_env(ans, argv)
    elif shell == 'bash':
        bashrc = os.path.join(home_dir, '.bashrc')
        if with_kitten:
            ans['KITTY_RUNNING_BASH_INTEGRATION_TEST'] = bashrc
        else:
            setup_bash_env(ans, argv)
            ans['KITTY_BASH_INJECT'] += ' posix'
            ans['KITTY_BASH_POSIX_ENV'] = bashrc
        with open(bashrc, 'w') as f:
            # ensure LINES and COLUMNS are kept up to date
            print('shopt -s checkwinsize', file=f)
            if rc:
                print(rc, file=f)
    return ans


@skip_on_windows('drives a POSIX shell over a pty, and MSYS2 supplying bash is not the same as the platform supporting it')
class ShellIntegration(BaseTest):

    with_kitten = False

    @contextmanager
    def run_shell(self, shell='zsh', rc='', cmd='', setup_env=None, extra_env=None):
        home_dir = self.home_dir = os.path.realpath(tempfile.mkdtemp())
        needs_da1 = os.path.basename(shell) == 'fish'
        cmd = cmd or shell
        cmd = shlex.split(cmd.format(**locals()))
        env = (setup_env or safe_env_for_running_shell)(cmd, home_dir, rc=rc, shell=shell, with_kitten=self.with_kitten)
        env['KITTY_RUNNING_SHELL_INTEGRATION_TEST'] = '1'
        if extra_env:
            env.update(extra_env)
        try:
            if self.with_kitten:
                cmd = [kitten_exe(), 'run-shell', '--shell', shlex.join(cmd)]
            pty = self.create_pty(cmd, cwd=home_dir, env=env, cols=180, needs_da1=needs_da1)
            i = 10
            while i > 0 and not pty.screen_contents().strip():
                pty.process_input_from_child()
                i -= 1
            yield pty
        finally:
            while os.path.exists(home_dir):
                try:
                    shutil.rmtree(home_dir)
                except OSError as e:
                    # As of fish 4 fish runs a background daemon generating
                    # completions.
                    if e.errno == errno.ENOTEMPTY:
                        continue
                    raise

    @unittest.skipUnless(shutil.which('zsh'), 'zsh not installed')
    def test_zsh_sudo_parser(self):
        if self.with_kitten:
            return
        src = os.path.join(shell_integration_dir, 'zsh', 'kitty-integration')
        with open(src) as f:
            func = extract_sudo_function(f.read())
        self.assertIsNotNone(func)
        self.sudo_parser_tests(['zsh', '--no-rcs', '-c'], func)

    @unittest.skipUnless(bash_ok(), 'bash not installed, too old, or debug build')
    def test_bash_sudo_parser(self):
        if self.with_kitten:
            return
        src = os.path.join(shell_integration_dir, 'bash', 'kitty.bash')
        with open(src) as f:
            func = extract_sudo_function(f.read())
        self.assertIsNotNone(func)
        self.sudo_parser_tests(['bash', '--noprofile', '--norc', '-c'], func)

    @unittest.skipUnless(shutil.which('fish'), 'fish not installed')
    def test_fish_sudo_parser(self):
        if self.with_kitten:
            return
        src = os.path.join(shell_integration_dir, 'fish', 'vendor_conf.d', 'kitty-shell-integration.fish')
        with open(src) as f:
            func = extract_sudo_function(
                f.read(), opening='function sudo', closing='end', witht='sudo TERMINFO="$TERMINFO" $argv',
                without='sudo $argv')
        self.assertIsNotNone(func)
        self.sudo_parser_tests(['fish', '--no-config', '-c'], func)

    def sudo_parser_tests(self, shell: list[str], func: str) -> None:
        test_cases = [
            (['ls'],                               'with_terminfo', 'simple command'),
            (['-e', 'file.txt'],                   'no_terminfo',   '-e flag'),
            (['--edit', 'file.txt'],               'no_terminfo',   '--edit flag'),
            (['-v'],                               'no_terminfo',   '-v validate flag'),
            (['--validate'],                       'no_terminfo',   '--validate flag'),
            (['-nv'],                              'no_terminfo',   '-nv grouped flags'),
            (['-ne', 'file.txt'],                  'no_terminfo',   '-ne grouped flags'),
            (['-u', 'root', 'ls'],                 'with_terminfo', '-u takes argument'),
            (['-u', 'root', '-v'],                 'no_terminfo',   '-u arg then -v'),
            (['--', '-v'],                         'with_terminfo', '-- ends option processing'),
            (['TERM=xterm-kitty', 'ls'],           'with_terminfo', 'env var before command'),
            (['TERM=xterm-kitty', '-v', 'ls'],     'no_terminfo',   'env var before -v'),
            (['-g', 'wheel', '-v'],                'no_terminfo', '-g takes argument'),
            (['-D', '/tmp', '-e'],                 'no_terminfo', '-D takes argument'),
            (['-R', '/chroot', '-e'],              'no_terminfo', '-R takes argument'),
            (['-h', 'localhost', '-v'],            'no_terminfo', '-h takes argument'),
            (['-uroot', '-v', 'ls'],               'with_terminfo', '-u with embedded argument'),
            (['--user', '-v', 'ls'],               'with_terminfo', '--user takes argument'),
            (['--user=root', '-v', 'ls'],          'no_terminfo', '--user=root self-contained'),
        ]
        for cmd, expected, _ in test_cases:
            func += f'\n\nsudo {" ".join(cmd)}'
        cp = subprocess.run(shell + [func], capture_output=True)
        self.assertEqual(cp.returncode, 0, f'{shell[0]} failed with stderr: {cp.stderr.decode()}')
        for i, line in enumerate(cp.stdout.decode().splitlines()):
            line = line.strip()
            cmd, expected, desc = test_cases[i]
            self.assertEqual(expected, line, str(cmd))

    @unittest.skipUnless(shutil.which('zsh'), 'zsh not installed')
    def test_zsh_integration(self):
        ps1, rps1 = 'left>', '<right'
        with self.run_shell(
            rc=f'''
PS1="{ps1}"
RPS1="{rps1}"
''') as pty:
            q = ps1 + ' ' * (pty.screen.columns - len(ps1) - len(rps1)) + rps1
            try:
                pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_BEAM)
            except TimeoutError as e:
                raise AssertionError(f'Cursor was not changed to beam. Screen contents: {repr(pty.screen_contents())}') from e
            pty.wait_till(lambda: pty.screen_contents() == q)
            self.ae(pty.callbacks.titlebuf[-1], '~')
            pty.callbacks.clear()
            pty.send_cmd_to_child('mkdir test && ls -a')
            self.assert_command(pty)
            pty.wait_till(lambda: pty.screen_contents().count(rps1) == 2)
            self.ae(pty.callbacks.titlebuf[-2:], ['mkdir test && ls -a', '~'])
            q = '\n'.join(str(pty.screen.line(i)) for i in range(1, pty.screen.cursor.y))
            self.ae(pty.last_cmd_output(), q)
            # shrink the screen
            pty.write_to_child(r'echo $COLUMNS')
            pty.set_window_size(rows=20, columns=40)
            q = ps1 + 'echo $COLUMNS' + ' ' * (40 - len(ps1) - len(rps1) - len('echo $COLUMNS')) + rps1
            pty.process_input_from_child()

            def redrawn():
                q = pty.screen_contents()
                return '$COLUMNS' in q and q.count(rps1) == 2 and q.count(ps1) == 2

            pty.wait_till(redrawn)
            self.ae(q, str(pty.screen.line(pty.screen.cursor.y)))
            pty.write_to_child('\r')
            self.assert_command(pty, 'echo $COLUMNS')
            pty.wait_till(lambda: pty.screen_contents().count(rps1) == 3)
            self.ae('40', str(pty.screen.line(pty.screen.cursor.y - 1)))
            self.ae(q, str(pty.screen.line(pty.screen.cursor.y - 2)))
            pty.send_cmd_to_child('clear')
            self.assert_command(pty)
            q = ps1 + ' ' * (pty.screen.columns - len(ps1) - len(rps1)) + rps1
            pty.wait_till(lambda: pty.screen_contents() == q)
            pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_BEAM)
            pty.send_cmd_to_child('cat')
            pty.wait_till(lambda: pty.screen.cursor.shape == 0)
            pty.write_to_child('\x04')
            pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_BEAM)
            self.assert_command(pty)
            # Check escaping of inputs
            pty.send_cmd_to_child("-f-this-command-must-not-exist")
            self.assert_command(pty, exit_status=127)
        with self.run_shell(rc=f'''PS1="{ps1}"''') as pty:
            pty.callbacks.clear()
            pty.send_cmd_to_child('printf "%s\x16\a%s" "a" "b"')
            pty.wait_till(lambda: 'ab' in pty.screen_contents())
            self.assertTrue(pty.screen.last_reported_cwd.decode().endswith(self.home_dir))
            self.assertIn('%s^G%s', pty.screen_contents())
            q = os.path.join(self.home_dir, 'testing-cwd-notification-🐱')
            os.mkdir(q)
            pty.send_cmd_to_child(f'cd {q}')
            pty.wait_till(lambda: pty.screen.last_reported_cwd.decode().endswith(q))
            if not is_macos:  # Fails on older macOS like the one used to build kitty binary because of unicode encoding issues
                self.assert_command(pty)
        with self.run_shell(rc=f'''PS1="{ps1}"\nexport ES="a\n b c\nd"''') as pty:
            pty.callbacks.clear()
            pty.send_cmd_to_child('clone-in-kitty')
            pty.wait_till(lambda: len(pty.callbacks.clone_cmds) == 1)
            self.assert_command(pty)
            env = pty.callbacks.clone_cmds[0].env
            self.ae(env.get('ES'), 'a\n b c\nd')
        with self.run_shell(rc='PS1=XXX', extra_env={'KITTY_SI_RUN_COMMAND_AT_STARTUP': 'echo pre-start'}) as pty:
            pty.wait_till(lambda: 'XXX' in pty.screen_contents())
            self.assertIn('pre-start', pty.screen_contents())
            self.assertTrue(pty.screen_contents().startswith('pre-start'))

    @unittest.skipUnless(shutil.which('fish'), 'fish not installed')
    def test_fish_integration(self):
        fish_prompt, right_prompt = 'left>', '<right'
        completions_dir = os.path.join(kitty_base_dir, 'shell-integration', 'fish', 'vendor_completions.d')
        with self.run_shell(
            shell='fish',
            extra_env={'KITTY_SI_RUN_COMMAND_AT_STARTUP': 'echo XXX'},
            rc=f'''
set -g fish_greeting
function fish_prompt; echo -n "{fish_prompt}"; end
function fish_right_prompt; echo -n "{right_prompt}"; end
function _test_comp_path; contains "{completions_dir}" $fish_complete_path; and echo ok; end
function _set_key; set -g fish_key_bindings fish_$argv[1]_key_bindings; end
function _set_status_prompt; function fish_prompt; echo -n "$pipestatus $status {fish_prompt}"; end; end
''') as pty:
            q = 'XXX\n' + fish_prompt + ' ' * (pty.screen.columns - len(fish_prompt) - len(right_prompt)) + right_prompt
            pty.wait_till(lambda: pty.screen_contents().count(right_prompt) == 1)
            self.ae(pty.screen_contents(), q)

            # shell integration dir must not be in XDG_DATA_DIRS
            cmd = f'string match -q -- "*{shell_integration_dir}*" "$XDG_DATA_DIRS" || echo "XDD_OK"'
            pty.send_cmd_to_child(cmd)
            pty.wait_till(lambda: 'XDD_OK' in pty.screen_contents())
            # self.assert_command(pty, cmd)

            # CWD reporting
            self.assertTrue(pty.screen.last_reported_cwd.decode().endswith(self.home_dir))
            q = os.path.join(self.home_dir, 'testing-cwd-notification-🐱')
            os.mkdir(q)
            pty.send_cmd_to_child(f'cd {q}')
            # self.assert_command(pty)
            pty.wait_till(lambda: pty.screen.last_reported_cwd.decode().endswith(q))
            pty.send_cmd_to_child('cd -')
            pty.wait_till(lambda: pty.screen.last_reported_cwd.decode().endswith(self.home_dir))

            # completion and prompt marking
            pty.wait_till(lambda: 'cd -' not in pty.screen_contents().splitlines()[-1])
            pty.send_cmd_to_child('clear')
            pty.wait_till(lambda: pty.screen_contents().count(right_prompt) == 1)
            pty.send_cmd_to_child('_test_comp_path')
            # self.assert_command(pty)
            pty.wait_till(lambda: pty.screen_contents().count(right_prompt) == 2)
            q = '\n'.join(str(pty.screen.line(i)) for i in range(1, pty.screen.cursor.y))
            self.ae(q, 'ok')
            self.ae(pty.last_cmd_output(), q)

            # resize and redraw (fish_handle_reflow)
            pty.write_to_child(r'echo $COLUMNS')
            pty.set_window_size(rows=20, columns=40)
            q = fish_prompt + 'echo $COLUMNS' + ' ' * (40 - len(fish_prompt) - len(right_prompt) - len('echo $COLUMNS')) + right_prompt
            pty.process_input_from_child()

            def redrawn():
                q = pty.screen_contents()
                return '$COLUMNS' in q and q.count(right_prompt) == 2 and q.count(fish_prompt) == 2

            pty.wait_till(redrawn)
            self.ae(q, str(pty.screen.line(pty.screen.cursor.y)))
            pty.write_to_child('\r')
            pty.wait_till(lambda: pty.screen_contents().count(right_prompt) == 3)
            # self.assert_command(pty, 'echo $COLUMNS')
            self.ae('40', str(pty.screen.line(pty.screen.cursor.y - 1)))
            self.ae(q, str(pty.screen.line(pty.screen.cursor.y - 2)))

            # cursor shapes
            pty.send_cmd_to_child('clear')
            q = fish_prompt + ' ' * (pty.screen.columns - len(fish_prompt) - len(right_prompt)) + right_prompt
            pty.wait_till(lambda: pty.screen_contents() == q)
            pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_BEAM)
            pty.send_cmd_to_child('echo; cat')
            pty.wait_till(lambda: pty.screen.cursor.shape == 0 and pty.screen.cursor.y > 1)
            pty.write_to_child('\x04')
            pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_BEAM)
            pty.send_cmd_to_child('_set_key vi')
            pty.wait_till(lambda: pty.screen_contents().count(right_prompt) == 3)
            pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_BEAM)
            pty.write_to_child('\x1b')
            pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_BLOCK)
            pty.write_to_child('r')
            pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_UNDERLINE)
            pty.write_to_child('\x1b')
            pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_BLOCK)
            pty.write_to_child('i')
            pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_BEAM)
            pty.send_cmd_to_child('_set_key default')
            # self.assert_command(pty)
            pty.wait_till(lambda: pty.screen_contents().count(right_prompt) == 4)
            pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_BEAM)

            pty.send_cmd_to_child('exit')

    def assert_command(self, pty, cmd='', exit_status=0):
        cmd = cmd or pty.last_cmd
        pty.wait_till(lambda: pty.callbacks.last_cmd_exit_status == exit_status, timeout_msg=lambda: f'{pty.callbacks.last_cmd_exit_status=} != {exit_status}')
        pty.wait_till(lambda: pty.callbacks.last_cmd_cmdline == cmd, timeout_msg=lambda: f'{pty.callbacks.last_cmd_cmdline=!r} != {cmd!r}')

    @unittest.skipUnless(bash_ok(), 'bash not installed, too old, or debug build')
    def test_bash_integration(self):
        ps1 = 'prompt> '
        with self.run_shell(
            shell='bash', rc=f'''
PS1="{ps1}"
''') as pty:
            try:
                pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_BEAM)
            except TimeoutError as e:
                raise AssertionError(f'Cursor was not changed to beam. Screen contents: {repr(pty.screen_contents())}') from e
            pty.wait_till(lambda: pty.screen_contents().count(ps1) == 1)
            self.ae(pty.screen_contents(), ps1)
            pty.wait_till(lambda: pty.callbacks.titlebuf[-1:] == ['~'])
            self.ae(pty.callbacks.titlebuf[-1], '~')
            pty.callbacks.clear()
            cmd = 'mkdir test && ls -a'
            pty.send_cmd_to_child(cmd)
            pty.wait_till(lambda: pty.callbacks.titlebuf[-2:] == [cmd, '~'])
            pty.wait_till(lambda: pty.screen_contents().count(ps1) == 2)
            self.assert_command(pty, cmd)
            q = '\n'.join(str(pty.screen.line(i)) for i in range(1, pty.screen.cursor.y))
            self.ae(pty.last_cmd_output(), q)
            # shrink the screen
            pty.write_to_child(r'echo $COLUMNS')
            pty.set_window_size(rows=20, columns=40)
            pty.process_input_from_child()

            def redrawn():
                q = pty.screen_contents()
                return '$COLUMNS' in q and q.count(ps1) == 2

            pty.wait_till(redrawn)
            self.ae(ps1 + 'echo $COLUMNS', str(pty.screen.line(pty.screen.cursor.y)))
            pty.write_to_child('\r')
            pty.wait_till(lambda: pty.screen_contents().count(ps1) == 3)
            self.ae('40', str(pty.screen.line(pty.screen.cursor.y - 1)))
            self.ae(ps1 + 'echo $COLUMNS', str(pty.screen.line(pty.screen.cursor.y - 2)))
            pty.send_cmd_to_child('clear')
            pty.wait_till(lambda: pty.screen_contents() == ps1)
            pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_BEAM)
            pty.send_cmd_to_child('cat')
            pty.wait_till(lambda: pty.screen.cursor.shape == 0)
            pty.write_to_child('\x04')
            pty.wait_till(lambda: pty.screen.cursor.shape == CURSOR_BEAM)
            pty.write_to_child('\x04')
            pty.send_cmd_to_child('clear')
            pty.wait_till(lambda: pty.callbacks.titlebuf)
        with self.run_shell(shell='bash', rc=f'''PS1="{ps1}"\ndeclare LOCAL_KSI_VAR=1''') as pty:
            pty.callbacks.clear()
            pty.send_cmd_to_child('declare')
            pty.wait_till(lambda: 'LOCAL_KSI_VAR' in pty.screen_contents())
            self.assert_command(pty, 'declare')
        with self.run_shell(shell='bash', rc=f'''PS1="{ps1}"''') as pty:
            pty.callbacks.clear()
            pty.send_cmd_to_child('printf "%s\x16\a%s" "a" "b"')
            pty.wait_till(lambda: pty.screen_contents().count(ps1) == 2)
            self.ae(pty.screen_contents(), f'{ps1}printf "%s^G%s" "a" "b"\nab{ps1}')
            self.assertTrue(pty.screen.last_reported_cwd.decode().endswith(self.home_dir))
            pty.send_cmd_to_child('echo $HISTFILE')
            pty.wait_till(lambda: '.bash_history' in pty.screen_contents().replace('\n', ''))
            q = os.path.join(self.home_dir, 'testing-cwd-notification-🐱')
            os.mkdir(q)
            pty.send_cmd_to_child(f'cd {q}')
            pty.wait_till(lambda: pty.screen.last_reported_cwd.decode().endswith(q))

        for ps1 in ('line1\\nline\\2\\prompt> ', 'line1\nprompt> ', 'line1\\nprompt> ',):
            with self.subTest(ps1=ps1), self.run_shell(
                shell='bash', rc=f'''
    PS1="{ps1}"
    ''') as pty:
                ps1 = ps1.replace('\\n', '\n')
                pty.wait_till(lambda: pty.screen_contents().count(ps1) == 1)
                pty.send_cmd_to_child('echo test')
                pty.wait_till(lambda: pty.screen_contents().count(ps1) == 2)
                self.ae(pty.screen_contents(), f'{ps1}echo test\ntest\n{ps1}')
                pty.write_to_child(r'echo $COLUMNS')
                pty.set_window_size(rows=20, columns=40)
                pty.process_input_from_child()
                pty.wait_till(redrawn)
                self.ae(ps1.splitlines()[-1] + 'echo $COLUMNS', str(pty.screen.line(pty.screen.cursor.y)))
                pty.write_to_child('\r')
                pty.wait_till(lambda: pty.screen_contents().count(ps1) == 3)
                self.ae('40', str(pty.screen.line(pty.screen.cursor.y - len(ps1.splitlines()))))
                self.ae(ps1.splitlines()[-1] + 'echo $COLUMNS', str(pty.screen.line(pty.screen.cursor.y - 1 - len(ps1.splitlines()))))
                self.assert_command(pty, 'echo $COLUMNS')

        with self.run_shell(shell='bash', rc='PS1=XXX', extra_env={'KITTY_SI_RUN_COMMAND_AT_STARTUP': 'echo pre-start'}) as pty:
            pty.wait_till(lambda: 'XXX' in pty.screen_contents())
            self.assertIn('pre-start', pty.screen_contents())
            self.assertTrue(pty.screen_contents().startswith('pre-start'))
        # test startup file sourcing

        def setup_env(excluded, argv, home_dir, rc='', shell='bash', with_kitten=self.with_kitten):
            ans = basic_shell_env(home_dir)
            if not with_kitten:
                setup_bash_env(ans, argv)
            for x in {'profile', 'bash.bashrc', '.bash_profile', '.bash_login', '.profile', '.bashrc', 'rcfile'} - excluded:
                with open(os.path.join(home_dir, x), 'w') as f:
                    if x == '.bashrc' and rc:
                        print(rc, file=f)
                    else:
                        print(f'echo [{x}]', file=f)
            ans['KITTY_BASH_ETC_LOCATION'] = home_dir
            ans['PS1'] = 'PROMPT $ '
            return ans

        def run_test(argv, *expected, excluded=(), rc='', wait_string='PROMPT $', assert_not_in=False):
            with self.subTest(argv=argv), self.run_shell(shell='bash', setup_env=partial(setup_env, set(excluded)), cmd=argv, rc=rc) as pty:
                pty.wait_till(lambda: wait_string in pty.screen_contents())
                q = pty.screen_contents()
                for x in expected:
                    if assert_not_in:
                        self.assertNotIn(f'[{x}]', q)
                    else:
                        self.assertIn(f'[{x}]', q)

        run_test('bash', 'bash.bashrc', '.bashrc')
        run_test('bash --rcfile rcfile', 'bash.bashrc', 'rcfile')
        run_test('bash --init-file rcfile', 'bash.bashrc', 'rcfile')
        run_test('bash --norc')
        run_test('bash -l', 'profile', '.bash_profile')
        run_test('bash --noprofile -l')
        run_test('bash -l', 'profile', '.bash_login', excluded=('.bash_profile',))
        run_test('bash -l', 'profile', '.profile', excluded=('.bash_profile', '.bash_login'))

        # test argument parsing and non-interactive shell
        run_test('bash -s arg1 --rcfile rcfile', 'rcfile', rc='echo ok;read', wait_string='ok', assert_not_in=True)
        run_test('bash +O login_shell -ic "echo ok;read"', 'bash.bashrc', excluded=('.bash_profile'), wait_string='ok', assert_not_in=True)
        run_test('bash -l .bashrc', 'profile', rc='echo ok;read', wait_string='ok', assert_not_in=True)
        run_test('bash -il -- .bashrc', 'profile', rc='echo ok;read', wait_string='ok')

        with self.run_shell(shell='bash', setup_env=partial(setup_env, set()), cmd='bash',
                            rc=f'''PS1="{ps1}"\nexport ES=$'a\n `b` c\n$d'\nexport ES2="XXX" ''') as pty:
            pty.callbacks.clear()
            pty.send_cmd_to_child('clone-in-kitty')
            pty.wait_till(lambda: len(pty.callbacks.clone_cmds) == 1)
            env = pty.callbacks.clone_cmds[0].env
            self.ae(env.get('ES'), 'a\n `b` c\n$d', f'Screen contents: {pty.screen_contents()!r}')
            self.ae(env.get('ES2'), 'XXX', f'Screen contents: {pty.screen_contents()!r}')

        for q, e in {
            'a': 'a',
            r'a\ab': 'a\ab',
            r'a\x7z': 'a\x07z',
            r'a\7b': 'a\007b',
            r'a\U1f345x': 'a🍅x',
            r'a\c b': 'a\0b',
        }.items():
            self.ae(decode_ansi_c_quoted_string(f"$'{q}'"), e, f'Failed to decode: {q!r}')

    @unittest.skipUnless(bash_ok(), 'bash not installed, too old, or debug build')
    def test_bash_verified_input(self):
        # The test plays the engine: it writes the request file and queues the
        # key sequence itself, without the engine's own checks, so every
        # verdict below is the shell's.
        from kitty.shell_input import TRIGGER
        if self.with_kitten:
            return
        folder = tempfile.mkdtemp()
        os.chmod(folder, 0o700)
        self.addCleanup(shutil.rmtree, folder, True)
        ps1, ps2 = 'prompt> ', 'more> '
        rc = f"""PS1='{ps1}'\nPS2='{ps2}'\nHISTFILE=/dev/null\nhistory -s 'echo seed_history'"""

        def readies(pty):
            return [f for k, f in pty.callbacks.shell_input if k == 'ready']

        def results(pty):
            return [f for k, f in pty.callbacks.shell_input if k == 'result']

        def wait_ready(pty, count):
            pty.wait_till(lambda: len(readies(pty)) >= count, timeout_msg=lambda: f'{pty.callbacks.shell_input} {pty.screen_contents()!r}')
            return readies(pty)[-1]

        def submit(pty, ready, command, expected, reason='', answered_by=None):
            before = len(results(pty))
            marker = os.path.join(self.home_dir, 'marker')
            with open(os.path.join(folder, f'request-{ready["token"]}'), 'w') as f:
                f.write(command.format(marker=marker))
            pty.write_to_child(TRIGGER)
            pty.wait_till(lambda: len(results(pty)) > before, timeout_msg=lambda: f'no result: {pty.screen_contents()!r}')
            r = results(pty)[-1]
            self.ae((r['token'], r['status'], r.get('reason', '')), ((answered_by or ready)['token'], expected, reason))
            return marker

        def settle(pty, text):
            pty.wait_till(lambda: text in pty.screen_contents(), timeout_msg=lambda: f'{text!r} not in {pty.screen_contents()!r}')
            # vi mode switches need readline's keyseq-timeout to pass
            for i in range(8):
                pty.process_input_from_child(0.1)
            return pty.screen_contents()

        def assert_refused(pty, keys, shown, reason, vi=False, after=None):
            ready = wait_ready(pty, len(readies(pty)))
            if vi:
                pty.send_cmd_to_child('set -o vi')
                ready = wait_ready(pty, len(readies(pty)) + 1)
            # Keys sent before readline takes the terminal are echoed by the tty.
            settle(pty, ps1)
            pty.write_to_child(keys)
            before = settle(pty, shown)
            marker = submit(pty, ready, 'touch {marker}', 'refused', reason)
            for i in range(5):
                pty.process_input_from_child(0.1)
            self.ae(pty.screen_contents(), after or before)
            self.assertFalse(os.path.exists(marker))
            self.assertTrue(os.path.exists(os.path.join(folder, f'request-{ready["token"]}')))
            self.assertFalse(os.path.exists(os.path.join(folder, f'claim-{ready["token"]}')))

        env = {'KITTY_SHELL_INPUT_DIR': folder}
        with self.run_shell(shell='bash', rc=rc, extra_env=env) as pty:
            ready = wait_ready(pty, 1)
            self.ae((ready['v'], ready['pid'], ready['pending']), ('1', str(pty.child_pid), '0'))
            pty.callbacks.clear()
            marker = submit(pty, ready, 'echo ran >> {marker}', 'accepted')
            nxt = wait_ready(pty, 1)
            self.assertNotEqual(nxt['token'], ready['token'])
            with open(marker) as f:
                self.ae(f.read(), 'ran\n')
            self.assert_command(pty, f'echo ran >> {marker}')
            # The used prompt cannot be used again: the shell answers for its
            # current prompt, which has no request.
            submit(pty, ready, 'touch {marker}.again', 'refused', 'no_request', answered_by=nxt)
            self.assertFalse(os.path.exists(marker + '.again'))
        for keys, shown, reason, vi, after in (
            ('echo keep_draft', 'keep_draft', 'draft', False, None),
            (' ', ps1 + ' ', 'draft', False, None),
            ('echo keep_suffix\x1b[D', 'keep_suffix', 'draft', True, None),
            ('echo keep_normal\x1b', 'keep_normal', 'vi_command', True, None),
            ('\x1b', ps1, 'vi_command', True, None),
            ("echo 'open\r", ps2, 'continuation', False, None),
            ('echo keep_prefix\x1b', 'keep_prefix', 'pending_key', False, None),
            ('echo keep_ctrl_x\x18', 'keep_ctrl_x', 'pending_key', False, None),
            # Readline ends a search before any bound key runs, so the shell
            # sees the match as a draft and keeps it, but the search itself is
            # over. Only the engine, which sends nothing once a key has been
            # typed at the prompt, keeps a search open.
            ('\x12seed_history', 'seed_history', 'draft', False, ps1 + 'echo seed_history'),
        ):
            with self.subTest(keys=keys), self.run_shell(shell='bash', rc=rc, extra_env=env) as pty:
                assert_refused(pty, keys, shown, reason, vi, after)
        with self.run_shell(shell='bash', rc=rc, extra_env=env) as pty:
            # The engine cancels by claiming first; the shell then runs nothing.
            ready = wait_ready(pty, 1)
            open(os.path.join(folder, f'claim-{ready["token"]}'), 'w').close()
            marker = submit(pty, ready, 'touch {marker}', 'refused', 'claimed')
            self.assertFalse(os.path.exists(marker))
            # A continuation abandoned with Ctrl-C leaves a primary prompt that works again.
            pty.write_to_child("echo 'open\r")
            settle(pty, ps2)
            pty.write_to_child('\x03')
            ready = wait_ready(pty, len(readies(pty)) + 1)
            marker = submit(pty, ready, 'echo ok >> {marker}', 'accepted')
            pty.wait_till(lambda: os.path.exists(marker))
            # Type-ahead waiting when the prompt is drawn is reported.
            pty.send_cmd_to_child('sleep 0.5')
            pty.write_to_child('echo typeahead')
            ready = wait_ready(pty, len(readies(pty)) + 1)
            self.ae(ready['pending'], '1')
        with self.run_shell(shell='bash', rc=rc + '\nshopt -u promptvars', extra_env=env) as pty:
            pty.wait_till(lambda: ps1 in pty.screen_contents())
            for i in range(5):
                pty.process_input_from_child(0.1)
            self.ae(readies(pty), [])
        with self.run_shell(shell='bash', rc=rc + '\nset +o emacs', extra_env=env) as pty:
            pty.wait_till(lambda: ps1 in pty.screen_contents())
            for i in range(5):
                pty.process_input_from_child(0.1)
            self.ae(readies(pty), [])
        with self.run_shell(shell='bash', rc=rc) as pty:
            pty.wait_till(lambda: ps1 in pty.screen_contents())
            for i in range(5):
                pty.process_input_from_child(0.1)
            self.ae(readies(pty), [])

    def test_bash_verified_input_with_the_engine(self):
        # Real bash and the engine's own checks (kitty.shell_input): the test
        # decides exactly when each key reaches the terminal relative to the
        # shell's prompt report, so the windows in which a key could reach
        # readline before the engine's key are reproduced deterministically.
        from unittest import mock

        from kitty import shell_input
        from kitty.shell_input import ShellInputRefused
        if self.with_kitten:
            return
        folder = tempfile.mkdtemp()
        os.chmod(folder, 0o700)
        self.addCleanup(shutil.rmtree, folder, True)
        ps1 = 'prompt> '
        # With flow control and reprint off, Ctrl-S, Ctrl-Q and Ctrl-R typed
        # ahead are kept by the terminal for readline: a search, a quoted insert.
        rc = f"""PS1='{ps1}'\nHISTFILE=/dev/null\nhistory -s 'echo seed_history'\nstty -ixon rprnt undef"""
        import time
        for name, value in (('directory', lambda: folder), ('shell_in_foreground', lambda *a, **k: True),
                            ('add_timer', lambda *a: 1), ('remove_timer', lambda *a: None)):
            patcher = mock.patch.object(shell_input, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        class Counter:
            def __init__(self) -> None:
                self.count, self.at = 0, 0

            def input_queue_state(self):
                return self.count, self.at, 0

            def is_using_alternate_linebuf(self):
                return False

        class Pane:
            # What the engine sees of a window: every byte sent to the shell is counted.
            def __init__(self, pty) -> None:
                self.pty, self.screen = pty, Counter()

            def write_to_child(self, data):
                self.pty.write_to_child(data)
                self.screen.count += len(data)
                self.screen.at = int(time.monotonic() * 1e9)

        def readies(pty):
            return [f for k, f in pty.callbacks.shell_input if k == 'ready']

        def report(pane, ready):
            shell_input.handle_message(pane, 'ready;' + ';'.join(f'{k}={v}' for k, v in ready.items()))

        def drain(pty, n=6):
            for i in range(n):
                pty.process_input_from_child(0.1)
            return pty.screen_contents()

        def shows(pty, *texts):
            # The last screen line, once it holds every one of texts.
            last = lambda: (pty.screen_contents().splitlines() or [''])[-1]
            pty.wait_till(lambda: all(t in last() for t in texts), timeout_msg=lambda: f'{texts} not in {last()!r}')
            drain(pty, 2)
            return last()

        def next_ready(pty, count):
            pty.wait_till(lambda: len(readies(pty)) >= count, timeout_msg=lambda: f'{pty.callbacks.shell_input} {pty.screen_contents()!r}')
            return readies(pty)[-1]

        def refused(pane, code, command='touch {marker}'):
            marker = os.path.join(self.home_dir, 'marker')
            before = pane.screen.count
            with self.assertRaises(ShellInputRefused) as cm:
                shell_input.submit(pane, command.format(marker=marker), '', lambda *a: None)
            self.ae(cm.exception.code, code)
            self.ae(pane.screen.count, before)  # nothing was sent
            self.ae(os.listdir(folder), [f for f in os.listdir(folder) if f.startswith('tty-')])
            return marker

        env = {'KITTY_SHELL_INPUT_DIR': folder}
        # 1. Keys typed ahead while a command runs wait in the terminal's
        # partial line, where read -t 0 cannot see them. The shell's report
        # still says input is pending, so nothing is sent and readline then
        # gets exactly what was typed.
        for keys, shown in (('echo ec', 'echo ec'), ('\x13', '(i-search)'), ('\x12', '(reverse-i-search)'), ('\x11', None)):
            with self.subTest(type_ahead=keys), self.run_shell(shell='bash', rc=rc, extra_env=env) as pty:
                pane = Pane(pty)
                report(pane, next_ready(pty, 1))
                pane.write_to_child('sleep 0.8\r')
                drain(pty, 3)
                pane.write_to_child(keys)
                drain(pty, 2)
                pane.screen.at -= int(10e9)  # long ago: only the shell's own look can catch it
                ready = next_ready(pty, 2)
                self.ae(ready['pending'], '1')
                report(pane, ready)
                self.ae(shell_input.describe(pane)['code'], 'input')
                marker = refused(pane, 'input')
                drain(pty)
                if shown:
                    shows(pty, shown)
                # The typed-ahead state is intact: a quoted insert still takes the
                # next key literally; the search and the draft go on as typed.
                pane.write_to_child('x' if shown is None else 'seed')
                # (the terminal echoed the type-ahead before the prompt)
                if keys == 'echo ec':
                    self.assertTrue(shows(pty, 'ecseed').endswith(ps1 + 'echo ecseed'))
                elif shown:
                    shows(pty, shown[1:], "`seed'")  # still searching, now for the new text
                else:
                    self.assertTrue(shows(pty, ps1 + 'x').endswith(ps1 + 'x'))
                self.assertFalse(os.path.exists(marker))
        # 2. A key that reaches readline after the shell looked but before the
        # engine read the report (the search race): queued just before the
        # report is read, it makes the prompt unverifiable; queued after, the
        # prompt has changed. Either way the search stays open with its text.
        for when in ('before the report is read', 'after the report is read'):
            for keys, shown in (('\x12', '(reverse-i-search)'), ('\x13', '(i-search)')):
                with self.subTest(race=when, keys=keys), self.run_shell(shell='bash', rc=rc, extra_env=env) as pty:
                    pane = Pane(pty)
                    ready = next_ready(pty, 1)
                    self.ae(ready['pending'], '0')
                    drain(pty, 2)
                    if when == 'after the report is read':
                        report(pane, ready)
                        self.assertTrue(shell_input.describe(pane)['verifiable'])
                    pane.write_to_child(keys)
                    if when == 'before the report is read':
                        report(pane, ready)
                        self.ae(shell_input.describe(pane)['code'], 'input')
                    shows(pty, shown)
                    marker = refused(pane, 'input')
                    pane.write_to_child('seed')
                    shows(pty, shown[1:], "`seed'", *(["echo seed_history"] if keys == '\x12' else []))
                    self.assertFalse(os.path.exists(marker))
        # 3. With nothing typed, the same engine and shell accept exactly once.
        with self.run_shell(shell='bash', rc=rc, extra_env=env) as pty:
            pane = Pane(pty)
            report(pane, next_ready(pty, 1))
            drain(pty, 2)
            answers = []
            marker = os.path.join(self.home_dir, 'marker')
            shell_input.submit(pane, f'echo ran >> {marker}', '', lambda r, e: answers.append((r, e)))
            pty.wait_till(lambda: [k for k, f in pty.callbacks.shell_input if k == 'result'])
            for k, f in pty.callbacks.shell_input:
                if k == 'result':
                    shell_input.handle_message(pane, 'result;' + ';'.join(f'{a}={b}' for a, b in f.items()))
            self.ae([e for r, e in answers], [None])
            pty.wait_till(lambda: os.path.exists(marker))
            drain(pty)
            with open(marker) as f:
                self.ae(f.read(), 'ran\n')


class ShellIntegrationWithKitten(ShellIntegration):
    with_kitten = True

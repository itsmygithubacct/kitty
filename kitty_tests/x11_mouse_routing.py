#!/usr/bin/env python3
"""Compile actual X11 mouse handlers in a headless harness; no X server needed.

Run directly: python3 kitty_tests/x11_mouse_routing.py
Set X11_MOUSE_SOURCE to another source root to exercise the same regression
against its actual handlers (including the pre-fix revision).
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


def body(text, marker):
    start = text.index('{', text.index(marker))
    depth = 1
    end = start + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start + 1:end - 1]


class X11MouseRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='x11-mouse-routing-')
        cls.addClassCleanup(cls.temp.cleanup)
        fixture = Path(__file__).with_name('x11_mouse_routing_fixture.c').read_text()
        root = Path(os.environ.get('X11_MOUSE_SOURCE', Path(__file__).resolve().parent.parent))
        source = (root/'glfw/x11_window.c').read_text()
        platform = (root/'glfw/x11_platform.h').read_text()
        input_source = (root/'glfw/input.c').read_text()
        pieces = {
            'SCROLL_TYPES': platform[platform.index('typedef struct XIScrollValuator'):platform.index('typedef struct XdndSelectionRequest')],
            'INPUT_FUNCTION': 'void _glfwInputCursorPos(_GLFWwindow* window, double xpos, double ypos) {' + body(input_source, 'void _glfwInputCursorPos(') + '}',
            'ROUTING_FUNCTIONS': source[source.index('static void\nhandle_mouse_move_event'):source.index('#define dnd _glfw.x11.xdnd')],
            'XI_DISPATCH_BODY': body(source, 'else if (event->xcookie.evtype == XI_Motion)'),
            'RAW_DISPATCH_BODY': body(source, 'if (event->xcookie.evtype == XI_RawMotion)'),
            'CORE_DISPATCH_BODY': body(source, 'case MotionNotify:'),
        }
        for name, value in pieces.items():
            fixture = fixture.replace('/* ' + name + ' */', value)
        test_source = Path(cls.temp.name)/'routing.c'
        test_source.write_text(fixture)
        cls.binary = test_source.with_suffix('')
        subprocess.run([*shlex.split(os.environ.get('CC', 'cc')), '-std=c11', '-Wall', '-Wextra', '-Werror', '-O0',
                        str(test_source), '-lm', '-o', str(cls.binary)], check=True)

    def check_case(self, name):
        result = subprocess.run([str(self.binary), name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'ok')


for case in ('stale_slave_master', 'slave_no_scroll', 'smooth_scroll', 'highres_momentum', 'v120_scroll',
             'disabled_raw', 'disabled_relative', 'core_fallback_duplicate', 'unknown_window_warp'):
    setattr(X11MouseRouting, 'test_' + case, lambda self, name=case: self.check_case(name))

if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python
# License: GPLv3

import json
import os
import subprocess
import tempfile

from kitty.constants import kitty_exe

from . import BaseTest

# The switch is read once per process, so the cells are rendered by a child
# that starts with it set and with a fontconfig that knows no font at all.
CHILD = r'''
import json
from kitty.fonts.render import render_string
ans = {}
for name, text in (('letters', 'AB'), ('untrained', '\U000F0000\U000F0001'), ('wide', '中日')):
    w, h, cells = render_string(text, size=11.0)
    ans[name] = {'w': w, 'h': h, 'cells': [c.hex() for c in cells]}
print('RESULT' + json.dumps(ans))
'''


def neuronte_home() -> str:
    for key, tail in (('KILIX_NEURONTE_HOME', ''), ('NEURONTE_HOME', ''), ('GPU_TERMINAL_SOURCE_HOME', 'neuronte')):
        base = os.environ.get(key)
        if base:
            return os.path.join(base, tail) if tail else base
    return os.path.expanduser('~/.local/gpu_terminal/sources/neuronte')


class TestNeuronteOnly(BaseTest):

    def render(self) -> dict:
        home = neuronte_home()
        if not os.path.exists(os.path.join(home, 'build', 'libneuronte.so')):
            self.skipTest(f'no built Neuronte tree at {home}')
        with tempfile.TemporaryDirectory() as tdir:
            conf = os.path.join(tdir, 'fonts.conf')
            with open(conf, 'w') as f:
                f.write('<?xml version="1.0"?>\n<fontconfig></fontconfig>\n')
            env = dict(os.environ, KILIX_NEURONTE_ONLY='1', KILIX_NEURONTE_HOME=home, FONTCONFIG_FILE=conf)
            cp = subprocess.run([kitty_exe(), '+runpy', CHILD], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        line = [x for x in cp.stdout.splitlines() if x.startswith('RESULT')]
        self.assertTrue(line, cp.stdout + cp.stderr)
        return json.loads(line[-1][len('RESULT'):])

    def test_neuronte_is_the_only_glyph_source(self):
        r = self.render()
        blank = lambda cell: not cell.strip('0')

        letters = r['letters']
        self.ae(letters['h'], 2 * letters['w'], 'a Neuronte cell is twice as high as it is wide')
        a, b = letters['cells']
        self.assertFalse(blank(a))
        self.assertFalse(blank(b))
        self.assertNotEqual(a, b)

        # A character with no drawing shows the library's mark: the same
        # picture for every such character, never a blank, never a letter.
        m1, m2 = r['untrained']['cells']
        self.assertFalse(blank(m1))
        self.ae(m1, m2)
        self.assertNotIn(m1, (a, b))

        # A character two cells wide is drawn once, in its first cell.
        cells = r['wide']['cells']
        self.ae(len(cells), 3, 'the trailing blank cell of the last character is trimmed')
        self.ae(cells[0], m1)
        self.assertTrue(blank(cells[1]))
        self.ae(cells[2], m1)

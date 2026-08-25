import os
import subprocess

from kitty.fast_data_types import num_users

from . import BaseTest


class UTMPTest(BaseTest):

    def test_num_users(self):
        # who is the control
        try:
            expected = subprocess.check_output(['who']).decode('utf-8').count('\n')
        except FileNotFoundError:
            self.skipTest('No who executable cannot verify num_users')
        else:
            actual = num_users()
            # On systemd-backed hosts, who can report logind/wtmp sessions even
            # when libc's live utmp database is absent. num_users() deliberately
            # uses getutxent(), so that is not a like-for-like control.
            if actual != expected and not any(os.path.exists(path) for path in (
                    '/run/utmp', '/var/run/utmp', '/var/adm/utmpx')):
                self.skipTest('libc utmp database is absent')
            self.ae(actual, expected)

#!/usr/bin/env python
# License: GPL v3
"""Run the real PowerShell startup command under Windows execution policies."""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from . import BaseTest, is_windows


@unittest.skipUnless(is_windows, 'Native Windows PowerShell startup')
class WindowsShellIntegrationTest(BaseTest):
    def test_powershell_execution_policy(self):
        from kitty import shell_integration
        shell = os.path.join(os.environ['SystemRoot'], 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        env = dict(os.environ)
        env.pop('PSExecutionPolicyPreference', None)
        # Include an apostrophe to cover user/installation paths needing PS quoting.
        with tempfile.TemporaryDirectory(prefix="kilix's integration ") as root:
            os.mkdir(os.path.join(root, 'powershell'))
            shutil.copyfile(os.path.join(shell_integration.shell_integration_dir, 'powershell', 'kitty.ps1'),
                            os.path.join(root, 'powershell', 'kitty.ps1'))
            for policy, installed in (('AllSigned', False), ('Restricted', False), ('RemoteSigned', True)):
                with self.subTest(policy=policy), patch.object(shell_integration, 'shell_integration_dir', root):
                    argv = [shell, '-NoLogo', '-NoProfile', '-ExecutionPolicy', policy]
                    shell_integration.setup_powershell_env(env, argv)
                    argv.remove('-NoExit')
                    argv[-1] += "; Write-Output ('POLICY=' + (Get-ExecutionPolicy)); Write-Output ('INTEGRATION=' + [bool]$global:__kitty_integration_installed)"
                    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=20)
                    self.ae(result.returncode, 0, result.stderr)
                    self.ae(result.stderr, '')
                    self.assertIn('POLICY=' + policy, result.stdout)
                    self.assertIn('INTEGRATION=' + str(installed), result.stdout)

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


class AuditKernelTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix' and hasattr(os, 'geteuid') and os.geteuid() == 0,
                         'Real conntrack validation requires POSIX root and a disposable network namespace')
    def test_real_snapshot_events_database_export_and_sigterm(self):
        for command in ['unshare','nsenter','ip','nft','conntrack']:
            if not shutil.which(command):self.skipTest(command + ' unavailable')
        probe = subprocess.run(['unshare','--net','--','nft','add','table','bridge','onepve_audit_probe'], capture_output=True, text=True, timeout=10)
        if probe.returncode and any(reason in probe.stderr for reason in ['Operation not supported','Operation not permitted']):
            self.skipTest('Kernel lacks nft bridge/namespace support')
        self.assertEqual(probe.returncode, 0, probe.stderr)
        answer = subprocess.run(['unshare','--net','--',sys.executable,str(Path(__file__).with_name('audit_namespace.py'))],
                                env=dict(os.environ,ONEPVE_TEST_NETNS='1'), capture_output=True,text=True,timeout=60)
        self.assertEqual(answer.returncode,0,answer.stderr)
        result = json.loads(answer.stdout)
        self.assertGreaterEqual(result['flows'],4)
        self.assertTrue(result['events'] and result['snapshot'] and result['sigterm_clean'])


if __name__ == '__main__':unittest.main()

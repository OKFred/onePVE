import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import onepve_menu as menu

spec = importlib.util.spec_from_file_location('menu_cli', Path(__file__).resolve().parents[1] / 'onepve.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class MenuSummaryTests(unittest.TestCase):
    def summary(self, data, kind='agent', action='status', args=(), failed=False):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = menu.summarize(data, kind, action, args, failed)
        return code, output.getvalue()

    def test_agent_plan_summary_uses_fingerprint_and_selected_resources(self):
        key = 'ssh-ed25519 YWJj comment-never-print'
        value = dict(status='PLAN_ONLY', config=dict(vmid=219, name='ai-dev', storage='fast', bridge='vmbr0',
                     cores=4, memory=8192, disk=32, ssh_key=key, address='192.0.2.7/24', gateway='192.0.2.1'),
                     selected_gpu={'bdf':'0000:00:02.1'}, image={'version':'20261103-2651'}, mode='wayland')
        code, output = self.summary(value, action='plan')
        self.assertEqual(code, 0)
        for expected in ['219', 'ai-dev', 'fast', 'vmbr0', '192.0.2.7/24', '0000:00:02.1', 'SHA256:', '只读预检']:
            self.assertIn(expected, output)
        self.assertNotIn(key, output); self.assertNotIn('comment-never-print', output)

    def test_missing_or_unknown_preview_status_cannot_authorize_apply(self):
        for value in [{}, {'status':None}, {'status':'UNKNOWN'}]:
            for kind, action, args in [('gpu','prepare-sriov',['--dry-run']), ('agent','plan',[])]:
                with self.subTest(value=value, kind=kind):
                    code, output = self.summary(value, kind, action, args)
                    self.assertEqual(code, 1); self.assertIn('未确认', output)

    def test_agent_summary_shows_mirror_name_and_package_security_urls(self):
        for mirror,label,host in [('tuna','清华 TUNA','mirrors.tuna.tsinghua.edu.cn'),
                                  ('ustc','中科大 USTC','mirrors.ustc.edu.cn')]:
            with self.subTest(mirror=mirror):
                code,output=self.summary({'phase':'PLANNED','config':{'apt_mirror':mirror}})
                self.assertEqual(code,0)
                self.assertIn('客机 APT 源: '+label+' ('+mirror+')',output)
                self.assertIn('https://'+host+'/debian',output)
                self.assertIn('https://'+host+'/debian-security',output)
        code,output=self.summary({'phase':'BASE_READY','config':{'vmid':103}})
        self.assertIn('Debian 官方 (official)',output)
        self.assertIn('https://security.debian.org/debian-security',output)

    def test_gpu_inventory_shows_pf_vf_driver_and_occupancy(self):
        value = [dict(bdf='0000:00:02.1', vendor='0x8086', device='0x7d67', driver='vfio-pci',
                      physfn='0000:00:02.0', vfs=[], consumers=[{'kind':'device_fd','vmid':'103','pid':'42'}])]
        code, output = self.summary(value, 'gpu', 'inventory')
        self.assertEqual(code, 0)
        for expected in ['0000:00:02.1', '0000:00:02.0', 'vfio-pci', 'VM=103', 'PID=42']:
            self.assertIn(expected, output)

    def test_rebuild_summary_highlights_exact_destructive_vmid(self):
        code, output = self.summary({'status':'PLAN_ONLY','destructive_vmid':219}, action='rebuild', args=['--dry-run'])
        self.assertEqual(code, 0)
        self.assertIn('无备份删除并重建 VMID: 219', output)

    def test_previous_record_summary_explains_pending_consent_and_completed_cleanup(self):
        code, output = self.summary({'status': 'PLAN_ONLY', 'previous_record': {
            'path': '/var/lib/onepve/agent/219/state.json', 'phase': 'READY_GPU'}}, action='plan')
        self.assertEqual(code, 0)
        for expected in ['旧记录待确认', '/var/lib/onepve/agent/219/state.json', 'READY_GPU', '缓存保留']:
            self.assertIn(expected, output)
        code, output = self.summary({'phase': 'READY_SOFTWARE', 'previous_record_cleared': {'phase': 'READY_GPU'}})
        self.assertEqual(code, 0)
        self.assertIn('旧记录清理: 已确认并完成', output)

    def test_agent_state_shows_connection_logs_and_driver_cache(self):
        value = dict(vmid=219, phase='READY_GPU', config={'viewer_port':16680}, mode='wayland',
                     driver_spec={'kernel':'6.12.113+deb13-amd64','version':'2025.12.10.2'}, guest_arch='x86_64',
                     connection={'ipv4':['192.0.2.7'], 'host_key_fingerprint':'SHA256:abc'},
                     jobs={'driver':{'path':'/guest/jobs/driver.json'}},
                     driver_cache={'status':'HIT','reason':'verified','path':'/cache/driver.tar.gz','bytes':1048576,
                                   'export_error':'archive unavailable'})
        code, output = self.summary(value)
        self.assertEqual(code, 0)
        for expected in ['ssh agent@192.0.2.7', '16680/vnc.html', '18931/mcp', 'SHA256:abc',
                         '/guest/jobs/driver.log', 'HIT', '/cache/driver.tar.gz', '1.0 MiB',
                         '6.12.113+deb13-amd64','2025.12.10.2','x86_64','archive unavailable']:
            self.assertIn(expected, output)

    def test_verify_does_not_invent_a_viewer_port(self):
        code, output = self.summary({'status':'VERIFIED','connection':{'ipv4':['192.0.2.7']}}, action='verify')
        self.assertEqual(code, 0)
        self.assertNotIn(':16080', output)
        self.assertIn('连接说明', output)

    def test_swap_summary_uses_actual_runtime_in_verify_and_saved_state(self):
        runtime = {'swap': {'total_mib': 4095, 'active_paths': ['/var/lib/onepve-agent/swapfile']}}
        for data in [{'status': 'VERIFIED', 'runtime': runtime},
                     {'phase': 'READY_SOFTWARE', 'verification': {'base': {'runtime': runtime}}}]:
            with self.subTest(data=data):
                code, output = self.summary(data)
                self.assertEqual(code, 0)
                self.assertIn('Swap 总量: 4.0 GiB', output)
                self.assertIn('/var/lib/onepve-agent/swapfile', output)
        code, output = self.summary({'status': 'VERIFIED', 'runtime': {'swap': {'total_mib': 0, 'active_paths': []}}})
        self.assertEqual(code, 0)
        self.assertIn('Swap 总量: 0.0 GiB', output)
        self.assertIn('Swap 路径: 未启用', output)

    def test_nonzero_backend_cannot_show_gpu_ready(self):
        code, output = self.summary({'status':'READY'}, 'gpu', 'prepare-sriov', failed=True)
        self.assertEqual(code, 1)
        self.assertIn('执行失败', output); self.assertNotIn('已就绪', output)

    def test_cli_default_error_stays_json_stderr_and_menu_mode_preserves_json(self):
        for mode in [None, '1']:
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=False), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                if mode is None:
                    os.environ.pop('ONEPVE_MENU_ERRORS', None)
                else:
                    os.environ['ONEPVE_MENU_ERRORS'] = mode
                code = cli.main(['invalid-group'])
            self.assertEqual(code, 2)
            document = json.loads(stdout.getvalue() if mode else stderr.getvalue())
            self.assertEqual(document['status'], 'ERROR')
            if mode:
                self.assertIn('onePVE:', stderr.getvalue()); self.assertNotIn('"status"', stderr.getvalue())
            else:
                self.assertEqual(stdout.getvalue(), '')


if __name__ == '__main__':
    unittest.main()

"""Exercise the interactive boundaries with command substitutes, never a PVE host."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
BASH = (r'C:\Program Files\Git\bin\bash.exe' if os.name == 'nt'
        and Path(r'C:\Program Files\Git\bin\bash.exe').exists() else shutil.which('bash'))
RESOURCE = {
    'nextid': 219,
    'storages': [{'storage': 'test-images', 'type': 'dir', 'avail': 90 * 1024**3}],
    'bridges': [{'iface': 'br-test', 'type': 'bridge', 'active': 1}],
}


@unittest.skipUnless(BASH, 'Bash is required for menu checks')
class MenuTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        binary = self.root / 'bin'
        binary.mkdir()
        python = binary / 'python3'
        python.write_text('#!/usr/bin/env bash\nset -o pipefail\n' + shlex.quote(sys.executable.replace('\\', '/'))
                          + ' "$@" | tr -d "\\r"\n', encoding='utf-8')
        python.chmod(0o755)
        self.env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'])
        self.env['MSYS_NO_PATHCONV'] = '1'
        self.env['PYTHONUTF8'] = '1'
        self.env['PYTHONIOENCODING'] = 'utf-8'

    def tearDown(self):
        self.tmp.cleanup()

    def shell(self, code, inputs='', check=True):
        result = subprocess.run([BASH, '--noprofile', '--norc', '-c', code], input=inputs.encode(),
                                cwd=self.root, env=self.env,
                                capture_output=True, timeout=30)
        result.stdout = result.stdout.decode('utf-8')
        result.stderr = result.stderr.decode('utf-8')
        if check and result.returncode:
            self.fail(result.stdout + '\n' + result.stderr)
        return result

    def source(self, file):
        return 'source ' + shlex.quote((REPO / file).as_posix()) + '\n'

    def mock_cli(self):
        return 'onepve_cli() { printf "CLI"; printf "\\t%s" "$@"; printf "\\n"; }\n'

    def agent_code(self, action):
        return (self.source('components/the_agent_workstation.sh')
                + 'onepve_agent_resources() { printf "%s\\n" ' + shlex.quote(json.dumps(RESOURCE)) + '; }\n'
                + self.mock_cli() + 'onepve_agent_wizard ' + action)

    @staticmethod
    def calls(output):
        return [line.split('\t')[1:] for line in output.splitlines() if line.startswith('CLI\t')]

    @staticmethod
    def form(vmid='', cores='', key='ssh-ed25519 AAAA demo', confirmation='n',
             network='', address='', gateway='', nameserver='', expected_ip='', disk=''):
        fields = [vmid, '', '', '', cores, '', disk, 'paste', key, '', '', network]
        if network == 'static':
            fields.extend([address, gateway, nameserver])
        fields.extend([expected_ip, '', confirmation])
        return '\n'.join(fields) + '\n'

    def test_shell_syntax(self):
        for name in ['menu.sh', 'index.sh', 'components/the_pass_through.sh', 'components/the_agent_workstation.sh']:
            result = subprocess.run([BASH, '-n', (REPO / name).as_posix()], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_import_has_no_actions_and_loads_from_other_cwd(self):
        result = self.shell(self.source('menu.sh')
                            + 'before="$PWD"; onepve_load_components || exit; '
                              '[[ "$PWD" == "$before" ]] || exit 2; '
                              'declare -F the_pass_through the_agent_workstation; printf "DONE\\n"')
        self.assertIn('the_pass_through', result.stdout)
        self.assertIn('the_agent_workstation', result.stdout)
        self.assertIn('DONE', result.stdout)
        self.assertEqual(result.stderr, '')

    def test_legacy_index_only_routes_to_menu(self):
        result = self.shell(self.source('index.sh')
                            + 'bash() { printf "ROUTE:%s\\n" "$1"; }; onepve_index_main')
        self.assertIn('/menu.sh', result.stdout)
        self.assertNotIn('apt install', result.stdout)

    def test_gpu_decline_only_plans(self):
        result = self.shell(self.source('components/the_pass_through.sh') + self.mock_cli()
                            + 'the_pass_through', '3\n0000:00:02.0\n1\nn\n0\n')
        calls = self.calls(result.stdout)
        self.assertEqual(calls, [['gpu', 'inventory'], ['gpu', 'prepare-sriov', '--pf',
                          '0000:00:02.0', '--num-vfs', '1', '--dry-run']])

    def test_gpu_apply_follows_successful_preview(self):
        result = self.shell(self.source('components/the_pass_through.sh') + self.mock_cli()
                            + 'onepve_gpu_apply prepare-passthrough --bdf 0000:02:00.0', 'y\n')
        self.assertEqual(self.calls(result.stdout), [
            ['gpu', 'prepare-passthrough', '--bdf', '0000:02:00.0', '--dry-run'],
            ['gpu', 'prepare-passthrough', '--bdf', '0000:02:00.0']])

    def test_gpu_failed_preview_does_not_apply(self):
        result = self.shell(self.source('components/the_pass_through.sh')
                            + 'onepve_cli() { printf "CLI\\t%s\\n" "$1"; return 1; }; '
                              'onepve_gpu_apply prepare-sriov --pf 0000:00:02.0', 'y\n', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls(result.stdout)), 1)

    def test_gpu_invalid_address_never_reaches_prepare(self):
        result = self.shell(self.source('components/the_pass_through.sh') + self.mock_cli()
                            + 'the_pass_through', '3\n0000:00:02.0;reboot\n0\n')
        self.assertEqual(self.calls(result.stdout), [['gpu', 'inventory']])

    def test_gpu_restore_rejects_resource_path_traversal(self):
        result = self.shell(self.source('components/the_pass_through.sh') + self.mock_cli()
                            + 'the_pass_through', '5\n../../etc/modules\n0\n')
        self.assertEqual(self.calls(result.stdout), [['gpu', 'status']])

    def test_agent_decline_preserves_public_key_as_one_argument(self):
        result = self.shell(self.agent_code('create'), self.form())
        calls = self.calls(result.stdout)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ['agent', 'plan'])
        self.assertEqual(calls[0][calls[0].index('--ssh-key') + 1], 'ssh-ed25519 AAAA demo')
        self.assertEqual(calls[0][calls[0].index('--vmid') + 1], '219')
        self.assertEqual(calls[0][calls[0].index('--cores') + 1], '4')
        self.assertEqual(calls[0][calls[0].index('--memory') + 1], '8192')
        self.assertEqual(calls[0][calls[0].index('--disk') + 1], '32')
        self.assertIn('test-images', calls[0])
        self.assertIn('br-test', calls[0])
        for option in ['--address', '--gateway', '--nameserver']:
            self.assertNotIn(option, calls[0])

    def test_agent_apply_is_plan_then_create_only(self):
        result = self.shell(self.agent_code('create'), self.form(confirmation='y'))
        calls = self.calls(result.stdout)
        self.assertEqual([x[:2] for x in calls], [['agent', 'plan'], ['agent', 'create']])
        self.assertEqual(calls[0][2:], calls[1][2:])

    def test_agent_disk_minimum_accepts_32_and_rejects_31_before_plan(self):
        for disk, accepted in [('31', False), ('32', True)]:
            with self.subTest(disk=disk):
                result = self.shell(self.agent_code('create'),
                                    self.form(disk=disk, confirmation='y'), check=False)
                calls = self.calls(result.stdout)
                if accepted:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual([x[:2] for x in calls], [['agent', 'plan'], ['agent', 'create']])
                    for call in calls:
                        self.assertEqual(call[call.index('--disk') + 1], '32')
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(calls, [])

    def test_agent_static_network_passes_validated_arguments_to_plan_and_create(self):
        result = self.shell(self.agent_code('create'), self.form(
            network='static', address='192.168.50.30/24', gateway='192.168.50.1',
            nameserver='192.168.50.1', confirmation='y'))
        calls = self.calls(result.stdout)
        self.assertEqual([x[:2] for x in calls], [['agent', 'plan'], ['agent', 'create']])
        self.assertEqual(calls[0][2:], calls[1][2:])
        for option, value in [('--address', '192.168.50.30/24'), ('--gateway', '192.168.50.1'),
                              ('--nameserver', '192.168.50.1'), ('--expected-ip', '192.168.50.30')]:
            self.assertEqual(calls[0][calls[0].index(option) + 1], value)

    def test_agent_rejects_invalid_static_network_before_plan(self):
        invalid = [
            ('192.168.50.30', '192.168.50.1', '192.168.50.1', ''),
            ('192.168.50.30/24', '192.168.51.1', '192.168.50.1', ''),
            ('192.168.50.30/24', '192.168.50.30', '192.168.50.1', ''),
            ('192.168.50.0/24', '192.168.50.1', '192.168.50.1', ''),
            ('192.168.50.30/24', '192.168.50.255', '192.168.50.1', ''),
            ('127.0.0.2/24', '127.0.0.1', '192.168.50.1', ''),
            ('192.168.50.30/24', '192.168.50.1', '224.0.0.1', ''),
            ('192.168.50.30/24', '192.168.50.1', '240.0.0.1', ''),
            ('192.168.50.30/24', '192.168.50.1', '192.168.50.1 8.8.8.8', ''),
            ('192.168.50.30/24', '192.168.50.1', '', ''),
            ('192.168.50.30/24', '192.168.50.1', '192.168.50.1', '192.168.50.31'),
        ]
        for address, gateway, nameserver, expected in invalid:
            with self.subTest(address=address, gateway=gateway, nameserver=nameserver, expected=expected):
                result = self.shell(self.agent_code('create'), self.form(
                    network='static', address=address, gateway=gateway,
                    nameserver=nameserver, expected_ip=expected), check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(result.stdout), [])

    def test_agent_allows_a_valid_point_to_point_static_subnet(self):
        result = self.shell(self.agent_code('create'), self.form(
            network='static', address='192.168.50.30/31', gateway='192.168.50.31',
            nameserver='192.168.50.1'))
        calls = self.calls(result.stdout)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ['agent', 'plan'])
        self.assertEqual(calls[0][calls[0].index('--address') + 1], '192.168.50.30/31')

    def test_agent_network_input_is_not_executed_or_split(self):
        result = self.shell(self.agent_code('create'), self.form(
            network='static', address='192.168.50.30/24;touch NETWORK_EXECUTED',
            gateway='192.168.50.1', nameserver='192.168.50.1'), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(result.stdout), [])
        self.assertFalse((self.root / 'NETWORK_EXECUTED').exists())
        result = self.shell(self.agent_code('create'), self.form(
            expected_ip='192.168.50.30;touch NETWORK_EXECUTED'), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(result.stdout), [])
        self.assertFalse((self.root / 'NETWORK_EXECUTED').exists())

    def test_agent_rejects_insufficient_resources_before_plan(self):
        result = self.shell(self.agent_code('create'), self.form(cores='3'), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(result.stdout), [])

    def test_agent_rejects_private_key_text(self):
        result = self.shell(self.agent_code('create'), self.form(key='-----BEGIN OPENSSH PRIVATE KEY-----'), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(result.stdout), [])
        self.assertNotIn('BEGIN OPENSSH', result.stdout)

    def test_rebuild_mismatched_confirmation_never_deletes(self):
        result = self.shell(self.agent_code('rebuild'), self.form(vmid='227', confirmation='228'), check=False)
        self.assertNotEqual(result.returncode, 0)
        calls = self.calls(result.stdout)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ['agent', 'rebuild'])
        self.assertIn('--dry-run', calls[0])
        self.assertEqual(calls[0][-3:], ['--confirm-vmid', '227', '--dry-run'])

    def test_status_and_resume_do_not_prepare_host_gpu(self):
        result = self.shell(self.source('components/the_agent_workstation.sh') + self.mock_cli()
                            + 'the_agent_workstation', '2\n5\n239\n0\n')
        self.assertEqual(self.calls(result.stdout), [['agent', 'status'], ['agent', 'resume', '--vmid', '239']])

    def test_discovery_only_reads_pve_and_filters_resources(self):
        datasets = {
            '*/storage': [{'storage': 'readonly-backups', 'type': 'dir', 'content': 'backup', 'active': 1},
                          {'storage': 'available-images', 'type': 'dir', 'content': 'images,snippets', 'active': 1, 'avail': 500}],
            '*/network': [{'iface': 'br-off', 'type': 'bridge', 'active': 0}, {'iface': 'br-ok', 'type': 'bridge', 'active': 1}],
            '/cluster/nextid': 241,
        }
        body = ('import fnmatch,json,subprocess\nDATA=' + repr(datasets) + '\n'
                'def output(args,**kwargs):\n'
                ' assert args[0:2]==["pvesh","get"] and args[3:]==["--output-format","json"]\n'
                ' for pattern,value in DATA.items():\n'
                '  if fnmatch.fnmatchcase(args[2],pattern):return json.dumps(value)\n'
                ' raise AssertionError(args)\n'
                'subprocess.check_output=output\n')
        (self.root / 'sitecustomize.py').write_text(body, encoding='utf-8')
        self.env['PYTHONPATH'] = str(self.root)
        result = self.shell(self.source('components/the_agent_workstation.sh') + 'onepve_agent_resources')
        data = json.loads(result.stdout)
        self.assertEqual(data['nextid'], 241)
        self.assertEqual([x['storage'] for x in data['storages']], ['available-images'])
        self.assertEqual([x['iface'] for x in data['bridges']], ['br-ok'])


if __name__ == '__main__':
    unittest.main()

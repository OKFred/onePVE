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
        self.env['TMPDIR'] = self.root.as_posix()
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
        temporary = ('mktemp() { local value; value="$(command mktemp "$@")" || return; cygpath -m "$value"; };\n'
                     if os.name == 'nt' else '')
        return temporary + 'ONEPVE_ROOT=' + shlex.quote(REPO.as_posix()) + '\nsource ' + shlex.quote((REPO / file).as_posix()) + '\n'

    def version_repo(self):
        if not shutil.which('git'):
            self.skipTest('Git is required for version checks')
        directory = self.root / 'version-repo'
        directory.mkdir()
        for arguments in [
            ['init', '--quiet'], ['config', 'user.name', 'onePVE test'],
            ['config', 'user.email', 'onepve-test@example.invalid'],
            ['config', 'commit.gpgsign', 'false'], ['config', 'core.autocrlf', 'false'],
            ['config', 'core.hooksPath', (directory / 'no-hooks').as_posix()],
        ]:
            subprocess.run(['git', '-C', str(directory), *arguments], check=True, capture_output=True)
        (directory / 'tracked.txt').write_text('original\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(directory), 'add', 'tracked.txt'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(directory), 'commit', '--quiet', '-m', 'initial'],
                       check=True, capture_output=True)
        commit = subprocess.check_output(['git', '-C', str(directory), 'rev-parse', 'HEAD'], text=True).strip()
        return directory, commit

    def mock_cli(self):
        return ('onepve_cli() { { printf "CLI"; printf "\\t%s" "$@"; printf "\\n"; } >&2; '
                'case "$1 $2" in '
                '"gpu inventory") printf "[]\\n" ;; '
                '"gpu status") printf \'{"devices":[],"resources":[]}\\n\' ;; '
                '"agent status") printf \'{"vms":[]}\\n\' ;; '
                '"agent plan"|"agent rebuild") printf \'{"status":"PLAN_ONLY"}\\n\' ;; '
                '*) if [[ " $* " == *" --dry-run "* ]]; then printf \'{"status":"DRY_RUN"}\\n\'; '
                'else printf \'{"status":"READY"}\\n\'; fi ;; esac; }\n')

    def mock_gpu_cli(self, payload=None, returncode=0, stderr=''):
        if payload is None:
            payload = {'status': 'READY'}
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False, indent=2)
        return ('onepve_cli() { { printf "CLI"; printf "\\t%s" "$@"; printf "\\n"; } >&2; '
                'if [[ " $* " == *" --dry-run "* ]]; then '
                'printf "%s\\n" \'{"status":"DRY_RUN"}\'; return 0; fi; '
                'printf "%s\\n" ' + shlex.quote(payload) + '; '
                'printf "%s" ' + shlex.quote(stderr) + ' >&2; return ' + str(returncode) + '; }\n')

    def agent_code(self, action, metadata=None):
        metadata = RESOURCE if metadata is None else metadata
        return (self.source('components/the_agent_workstation.sh')
                + 'onepve_agent_resources() { printf "%s\\n" ' + shlex.quote(json.dumps(metadata)) + '; }\n'
                + self.mock_cli() + 'onepve_agent_wizard ' + action)

    def discovery(self, datasets, probes=None, failures=None):
        log = self.root / 'discovery-calls.jsonl'
        body = ('import fnmatch,json,subprocess\nfrom pathlib import Path\n'
                'DATA=' + repr(datasets) + '\nPROBES=' + repr(probes or {})
                + '\nFAILURES=' + repr(failures or {}) + '\nLOG=Path(' + repr(str(log)) + ')\n'
                'def output(args,**kwargs):\n'
                ' with LOG.open("a",encoding="utf-8") as stream:stream.write(json.dumps(args)+"\\n")\n'
                ' if args[0]=="pvesh":\n'
                '  assert args[1]=="get" and args[3:]==["--output-format","json"],args\n'
                '  key=args[2]\n'
                '  if key in DATA:value=DATA[key]\n'
                '  else:value=next(value for pattern,value in DATA.items() if fnmatch.fnmatchcase(key,pattern))\n'
                ' elif args[0]=="findmnt":\n'
                '  assert "--json" in args and "--target" in args,args\n'
                '  key="findmnt:"+args[args.index("--target")+1];value=PROBES[key]\n'
                ' elif args[0]=="lsblk":\n'
                '  assert all(flag in args for flag in ["--json","--inverse","--tree"]),args\n'
                '  key="lsblk:"+args[-1];value=PROBES[key]\n'
                ' elif args[0]=="pvs":\n'
                '  assert "--reportformat" in args and "json" in args,args\n'
                '  key="pvs";value=PROBES[key]\n'
                ' else:raise AssertionError("Unexpected/non-read-only command: "+repr(args))\n'
                ' failure=FAILURES.get(key)\n'
                ' if failure=="missing":raise FileNotFoundError(key)\n'
                ' if failure=="timeout":raise subprocess.TimeoutExpired(args,1)\n'
                ' if failure=="invalid":return "invalid JSON"\n'
                ' return json.dumps(value)\n'
                'def run(args,**kwargs):return subprocess.CompletedProcess(args,0,output(args,**kwargs),"")\n'
                'subprocess.check_output=output\nsubprocess.run=run\n')
        (self.root / 'sitecustomize.py').write_text(body, encoding='utf-8')
        self.env['PYTHONPATH'] = str(self.root)
        result = self.shell(self.source('components/the_agent_workstation.sh') + 'onepve_agent_resources')
        calls = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        return json.loads(result.stdout), calls

    @staticmethod
    def calls(output):
        return [line.split('\t')[1:] for line in output.splitlines() if line.startswith('CLI\t')]

    @staticmethod
    def form(vmid='', cores='', key='ssh-ed25519 AAAA demo', confirmation='n',
             network='', address='', gateway='', nameserver='', expected_ip='', disk='', storage='', bridge=''):
        fields = [vmid, '', storage, bridge, cores, '', disk, 'paste', key, '', '', network]
        if network == 'static':
            fields.extend([address, gateway, nameserver])
        fields.extend([expected_ip, '', '', confirmation, ''])
        return '\n'.join(fields) + '\n'

    def test_shell_syntax(self):
        for name in ['menu.sh', 'index.sh', 'components/the_pass_through.sh', 'components/the_agent_workstation.sh']:
            result = subprocess.run([BASH, '-n', (REPO / name).as_posix()], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_import_has_no_actions_and_loads_from_other_cwd(self):
        result = self.shell('git() { printf "UNEXPECTED_GIT\\n"; return 1; }; '
                            'python3() { printf "UNEXPECTED_PYTHON\\n"; return 1; };\n'
                            + self.source('menu.sh')
                            + 'before="$PWD"; onepve_load_components || exit; '
                              '[[ "$PWD" == "$before" ]] || exit 2; '
                              'declare -F the_pass_through the_agent_workstation; printf "DONE\\n"')
        self.assertIn('the_pass_through', result.stdout)
        self.assertIn('the_agent_workstation', result.stdout)
        self.assertIn('DONE', result.stdout)
        self.assertNotIn('UNEXPECTED_', result.stdout)
        self.assertEqual(result.stderr, '')

    def test_menu_version_keeps_startup_commit_after_head_changes(self):
        directory, commit = self.version_repo()
        quoted = shlex.quote(directory.as_posix())
        result = self.shell(self.source('menu.sh') + 'ONEPVE_ROOT=' + quoted + '\n'
                            'onepve_capture_version; menu_title; '
                            'git -C "$ONEPVE_ROOT" commit --quiet --allow-empty -m newer || exit; menu_title')
        current = subprocess.check_output(['git', '-C', str(directory), 'rev-parse', 'HEAD'], text=True).strip()
        self.assertNotEqual(current, commit)
        self.assertEqual(result.stdout.count('启动版本: ' + commit[:12]), 2)
        self.assertNotIn(current[:12], result.stdout)
        self.assertNotIn('本地修改', result.stdout)
        self.assertIn('更新后需退出并重新启动菜单', result.stdout)

    def test_menu_version_marks_tracked_changes_without_refreshing_index(self):
        directory, commit = self.version_repo()
        (directory / 'tracked.txt').write_text('modified\n', encoding='utf-8')
        index = directory / '.git' / 'index'
        before = index.stat().st_mtime_ns
        result = self.shell(self.source('menu.sh') + 'ONEPVE_ROOT=' + shlex.quote(directory.as_posix())
                            + '\nonepve_capture_version; menu_title')
        self.assertIn('启动版本: ' + commit[:12] + '（本地修改）', result.stdout)
        self.assertEqual(index.stat().st_mtime_ns, before)

    def test_menu_version_uses_distribution_record_instead_of_parent_repository(self):
        parent, parent_commit = self.version_repo()
        directory = parent / 'distributed-copy'
        directory.mkdir()
        commit = 'b' * 40
        (directory / 'onepve-source.json').write_text(json.dumps({'commit': commit}), encoding='utf-8')
        result = self.shell(self.source('menu.sh') + 'ONEPVE_ROOT=' + shlex.quote(directory.as_posix())
                            + '\nonepve_capture_version; menu_title')
        self.assertIn('启动版本: ' + commit[:12] + '（分发记录）', result.stdout)
        self.assertNotIn(parent_commit[:12], result.stdout)

    def test_menu_version_unknown_without_valid_repository_or_record(self):
        path = self.root / 'onepve-source.json'
        for content in [None, '{', '{"commit":"invalid"}', '[]']:
            with self.subTest(content=content):
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(content, encoding='utf-8')
                result = self.shell(self.source('menu.sh') + 'ONEPVE_ROOT=' + shlex.quote(self.root.as_posix())
                                    + '\nonepve_capture_version; menu_title')
                self.assertIn('启动版本: 未知', result.stdout)
                self.assertEqual(result.stderr, '')

    def test_legacy_index_only_routes_to_menu(self):
        result = self.shell(self.source('index.sh')
                            + 'bash() { printf "ROUTE:%s\\n" "$1"; }; onepve_index_main')
        self.assertIn('/menu.sh', result.stdout)
        self.assertNotIn('apt install', result.stdout)

    def test_gpu_decline_only_plans(self):
        result = self.shell(self.source('components/the_pass_through.sh') + self.mock_cli()
                            + 'the_pass_through', '3\n\n0000:00:02.0\n1\n\nn\n0\n')
        calls = self.calls(result.stderr)
        self.assertEqual(calls, [['gpu', 'inventory'], ['gpu', 'prepare-sriov', '--pf',
                          '0000:00:02.0', '--num-vfs', '1', '--dry-run']])
        self.assertIn('【已取消】', result.stdout)
        self.assertNotIn('【已就绪】', result.stdout)

    def test_gpu_apply_follows_successful_preview(self):
        result = self.shell(self.source('components/the_pass_through.sh') + self.mock_gpu_cli()
                            + 'onepve_gpu_apply prepare-passthrough --bdf 0000:02:00.0', '\ny\n\n')
        self.assertEqual(self.calls(result.stderr), [
            ['gpu', 'prepare-passthrough', '--bdf', '0000:02:00.0', '--dry-run'],
            ['gpu', 'prepare-passthrough', '--bdf', '0000:02:00.0']])
        self.assertNotIn(json.dumps({'status': 'READY'}, indent=2), result.stdout)
        self.assertIn('【已就绪】', result.stdout)

    def test_gpu_apply_prints_key_summary_and_hides_raw_json(self):
        for status, label in [('EXTERNAL_READY', '【已就绪】'),
                              ('NEEDS_REBOOT', '【待重启】'),
                              ('RESTORED', '【已撤销】'),
                              ('RESTORED_NEEDS_REBOOT', '【待重启】'),
                              ('EXTERNAL_PRESERVED', '【已保留】')]:
            with self.subTest(status=status):
                payload = {'status': status, 'bdf': '0000:00:02.0', 'detail': '保留原始详情'}
                raw_json = json.dumps(payload, ensure_ascii=False, indent=2)
                result = self.shell(self.source('components/the_pass_through.sh')
                                    + self.mock_gpu_cli(payload)
                                    + 'onepve_gpu_apply prepare-sriov --pf 0000:00:02.0', '\ny\n\n')
                self.assertNotIn(raw_json, result.stdout)
                lines = [line for line in result.stdout.splitlines() if label in line]
                self.assertEqual(len(lines), 1, result.stdout)
                self.assertTrue(lines[0].startswith(label), lines[0])
                self.assertIn('PCI 地址: 0000:00:02.0', result.stdout)
                self.assertNotIn('\x1b[', result.stdout)

    def test_gpu_apply_failure_preserves_details_and_exit_code(self):
        payload = {'status': 'ERROR', 'error': 'driver operation failed'}
        result = self.shell(self.source('components/the_pass_through.sh')
                            + self.mock_gpu_cli(payload, returncode=7, stderr='原始错误详情\n')
                            + 'onepve_gpu_apply prepare-sriov --pf 0000:00:02.0', '\ny\n\n', check=False)
        self.assertEqual(result.returncode, 7)
        self.assertIn('错误: driver operation failed', result.stdout)
        self.assertIn('原始错误详情', result.stderr)
        self.assertIn('【执行失败】', result.stdout)
        self.assertNotIn('【已就绪】', result.stdout)

    def test_gpu_apply_rejects_nonfinal_or_error_status_even_when_cli_returns_zero(self):
        for status, label in [('DRY_RUN', '【仅预览】'), ('ERROR', '【执行失败】'),
                              ('FAILED', '【执行失败】')]:
            with self.subTest(status=status):
                result = self.shell(self.source('components/the_pass_through.sh')
                                    + self.mock_gpu_cli({'status': status})
                                    + 'onepve_gpu_apply prepare-sriov --pf 0000:00:02.0',
                                    '\ny\n\n', check=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn(label, result.stdout)
                self.assertNotIn('【已就绪】', result.stdout)

    def test_gpu_apply_does_not_report_unknown_or_invalid_output_as_success(self):
        for payload in ['not JSON', '[]', 'null', '{}', '{"status":null}',
                        '{"status":"NEW_STATUS"}']:
            with self.subTest(payload=payload):
                result = self.shell(self.source('components/the_pass_through.sh')
                                    + self.mock_gpu_cli(payload)
                                    + 'onepve_gpu_apply prepare-sriov --pf 0000:00:02.0',
                                    '\ny\n\n', check=False)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(payload, result.stdout)
                self.assertIn('【未确认】', result.stdout)
                self.assertNotIn('【已就绪】', result.stdout)

    def test_gpu_failed_preview_does_not_apply(self):
        result = self.shell(self.source('components/the_pass_through.sh')
                            + 'onepve_cli() { printf "CLI\\t%s\\n" "$1" >&2; return 7; }; '
                              'onepve_gpu_apply prepare-sriov --pf 0000:00:02.0', '\ny\n\n', check=False)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(len(self.calls(result.stderr)), 1)
        self.assertIn('【预检失败】', result.stdout)
        self.assertNotIn('【已就绪】', result.stdout)

    def test_raw_json_is_hidden_for_empty_n_and_N_and_temporary_files_are_removed(self):
        for answer in ['\n', 'n\n', 'N\n']:
            with self.subTest(answer=answer):
                result = self.shell(self.source('components/the_pass_through.sh')
                                    + 'nano() { printf "UNEXPECTED_NANO"; return 9; }; '
                                    + self.mock_gpu_cli({'status':'READY','private_marker':'raw-only'})
                                    + 'onepve_menu_run gpu prepare-sriov', answer)
                self.assertIn('【已就绪】', result.stdout)
                self.assertNotIn('raw-only', result.stdout + result.stderr)
                self.assertNotIn('UNEXPECTED_NANO', result.stdout)
                self.assertEqual(list(self.root.glob('onepve-json.*')), [])

    def test_agent_component_loads_menu_helper_without_overwriting_an_existing_cli(self):
        result = self.shell(self.mock_cli() + self.source('components/the_agent_workstation.sh')
                            + 'onepve_menu_run agent status', '\n')
        self.assertIn('工作机状态', result.stdout)
        self.assertEqual(self.calls(result.stderr), [['agent','status']])

    def test_raw_json_Y_opens_readonly_nano_and_cleans_its_private_file(self):
        result = self.shell(self.source('components/the_pass_through.sh')
                            + 'nano() { [[ "$1" == -v && "$2" == -- ]] || return 8; '
                              'printf "NANO_PATH:%s\\n" "$3"; stat -c "NANO_MODE:%a\\n" "$3"; cat -- "$3"; }; '
                            + self.mock_gpu_cli({'status':'READY','private_marker':'raw-only'})
                            + 'onepve_menu_run gpu prepare-sriov', 'Y\n')
        self.assertIn('raw-only', result.stdout)
        self.assertLess(result.stdout.index('【已就绪】'), result.stdout.index('NANO_PATH:'))
        path = Path(next(line.removeprefix('NANO_PATH:') for line in result.stdout.splitlines() if line.startswith('NANO_PATH:')))
        self.assertFalse(path.exists()); self.assertFalse(path.parent.exists())
        if os.name != 'nt':
            self.assertIn('NANO_MODE:600', result.stdout)
        self.assertEqual(list(self.root.glob('onepve-json.*')), [])

    def test_viewer_failure_does_not_change_successful_operation_exit(self):
        result = self.shell(self.source('components/the_pass_through.sh')
                            + 'nano() { return 9; }; ' + self.mock_gpu_cli()
                            + 'onepve_menu_run gpu prepare-sriov', 'y\n')
        self.assertIn('操作结果保持不变', result.stdout)
        self.assertEqual(list(self.root.glob('onepve-json.*')), [])

    def test_successful_viewer_does_not_mask_failed_operation_exit(self):
        result = self.shell(self.source('components/the_pass_through.sh')
                            + 'nano() { return 0; }; '
                            + self.mock_gpu_cli({'status':'ERROR','error':'failure'}, returncode=7)
                            + 'onepve_menu_run gpu prepare-sriov', 'y\n', check=False)
        self.assertEqual(result.returncode, 7)
        self.assertIn('错误: failure', result.stdout)
        self.assertEqual(list(self.root.glob('onepve-json.*')), [])

    def test_missing_nano_skips_view_without_changing_operation_exit(self):
        result = self.shell(self.source('components/the_pass_through.sh')
                            + 'command() { if [[ "$1" == -v && "$2" == nano ]]; then return 1; fi; builtin command "$@"; }; '
                            + self.mock_gpu_cli() + 'onepve_menu_run gpu prepare-sriov', 'y\n')
        self.assertIn('未安装 nano', result.stdout)
        self.assertEqual(list(self.root.glob('onepve-json.*')), [])

    def test_gpu_invalid_address_never_reaches_prepare(self):
        result = self.shell(self.source('components/the_pass_through.sh') + self.mock_cli()
                            + 'the_pass_through', '3\n\n0000:00:02.0;reboot\n0\n')
        self.assertEqual(self.calls(result.stderr), [['gpu', 'inventory']])

    def test_gpu_restore_rejects_resource_path_traversal(self):
        result = self.shell(self.source('components/the_pass_through.sh') + self.mock_cli()
                            + 'the_pass_through', '5\n\n../../etc/modules\n0\n')
        self.assertEqual(self.calls(result.stderr), [['gpu', 'status']])

    def test_agent_decline_preserves_public_key_as_one_argument(self):
        result = self.shell(self.agent_code('create'), self.form())
        calls = self.calls(result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ['agent', 'plan'])
        self.assertEqual(calls[0][calls[0].index('--ssh-key') + 1], 'ssh-ed25519 AAAA demo')
        self.assertEqual(calls[0][calls[0].index('--vmid') + 1], '219')
        self.assertEqual(calls[0][calls[0].index('--name') + 1], 'ai-dev')
        self.assertEqual(calls[0][calls[0].index('--cores') + 1], '4')
        self.assertEqual(calls[0][calls[0].index('--memory') + 1], '8192')
        self.assertEqual(calls[0][calls[0].index('--disk') + 1], '32')
        self.assertIn('test-images', calls[0])
        self.assertIn('br-test', calls[0])
        for option in ['--address', '--gateway', '--nameserver']:
            self.assertNotIn(option, calls[0])

    def test_agent_apply_is_plan_then_create_only(self):
        result = self.shell(self.agent_code('create'), self.form(confirmation='y'))
        calls = self.calls(result.stderr)
        self.assertEqual([x[:2] for x in calls], [['agent', 'plan'], ['agent', 'create']])
        self.assertEqual(calls[0][2:], calls[1][2:])

    def test_agent_selects_one_nondefault_storage_and_bridge_from_multiple_choices(self):
        metadata = dict(RESOURCE,
                        storages=RESOURCE['storages'] + [{'storage': 'second-images', 'type': 'dir', 'avail': 50 * 1024**3}],
                        bridges=RESOURCE['bridges'] + [{'iface': 'vmbr1', 'type': 'bridge', 'active': 1}])
        result = self.shell(self.agent_code('create', metadata),
                            self.form(storage='second-images', bridge='vmbr1', confirmation='y'))
        calls = self.calls(result.stderr)
        self.assertEqual([call[:2] for call in calls], [['agent', 'plan'], ['agent', 'create']])
        for call in calls:
            for option, value in [('--storage', 'second-images'), ('--bridge', 'vmbr1')]:
                self.assertEqual(call.count(option), 1)
                self.assertEqual(call[call.index(option) + 1], value)
        self.assertIn('可选磁盘存储（单选', result.stdout)
        self.assertIn('可选网桥（单选', result.stdout)
        self.assertIn('仅创建1张网卡', result.stdout)
        for bridge in ['br-test vmbr1', 'br-test,vmbr1']:
            with self.subTest(bridge=bridge):
                rejected = self.shell(self.agent_code('create', metadata),
                                      self.form(storage='second-images', bridge=bridge, confirmation='y'), check=False)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(self.calls(rejected.stdout), [])

    def test_agent_disk_minimum_accepts_32_and_rejects_31_before_plan(self):
        for disk, accepted in [('31', False), ('32', True)]:
            with self.subTest(disk=disk):
                result = self.shell(self.agent_code('create'),
                                    self.form(disk=disk, confirmation='y'), check=False)
                calls = self.calls(result.stderr)
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
        calls = self.calls(result.stderr)
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
                self.assertEqual(self.calls(result.stderr), [])

    def test_agent_allows_a_valid_point_to_point_static_subnet(self):
        result = self.shell(self.agent_code('create'), self.form(
            network='static', address='192.168.50.30/31', gateway='192.168.50.31',
            nameserver='192.168.50.1'))
        calls = self.calls(result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ['agent', 'plan'])
        self.assertEqual(calls[0][calls[0].index('--address') + 1], '192.168.50.30/31')

    def test_agent_network_input_is_not_executed_or_split(self):
        result = self.shell(self.agent_code('create'), self.form(
            network='static', address='192.168.50.30/24;touch NETWORK_EXECUTED',
            gateway='192.168.50.1', nameserver='192.168.50.1'), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(result.stderr), [])
        self.assertFalse((self.root / 'NETWORK_EXECUTED').exists())
        result = self.shell(self.agent_code('create'), self.form(
            expected_ip='192.168.50.30;touch NETWORK_EXECUTED'), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(result.stderr), [])
        self.assertFalse((self.root / 'NETWORK_EXECUTED').exists())

    def test_agent_rejects_insufficient_resources_before_plan(self):
        result = self.shell(self.agent_code('create'), self.form(cores='3'), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(result.stderr), [])

    def test_agent_rejects_private_key_text(self):
        result = self.shell(self.agent_code('create'), self.form(key='-----BEGIN OPENSSH PRIVATE KEY-----'), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(result.stderr), [])
        self.assertNotIn('BEGIN OPENSSH', result.stdout)

    def test_rebuild_mismatched_confirmation_never_deletes(self):
        result = self.shell(self.agent_code('rebuild'), self.form(vmid='227', confirmation='228'), check=False)
        self.assertNotEqual(result.returncode, 0)
        calls = self.calls(result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ['agent', 'rebuild'])
        self.assertIn('--dry-run', calls[0])
        self.assertEqual(calls[0][-3:], ['--confirm-vmid', '227', '--dry-run'])

    def test_status_and_resume_do_not_prepare_host_gpu(self):
        result = self.shell(self.source('components/the_agent_workstation.sh') + self.mock_cli()
                            + 'the_agent_workstation', '2\n\n5\n239\n\n0\n')
        self.assertEqual(self.calls(result.stderr), [['agent', 'status'], ['agent', 'resume', '--vmid', '239']])

    def test_discovery_only_reads_pve_and_filters_resources(self):
        datasets = {
            '/storage': [],
            '*/storage': [{'storage': 'readonly-backups', 'type': 'dir', 'content': 'backup', 'active': 1},
                          {'storage': 'available-images', 'type': 'dir', 'content': 'images,snippets', 'active': 1, 'avail': 500}],
            '*/network': [{'iface': 'br-off', 'type': 'bridge', 'active': 0}, {'iface': 'br-ok', 'type': 'bridge', 'active': 1}],
            '/cluster/nextid': 241,
        }
        data, _ = self.discovery(datasets)
        self.assertEqual(data['nextid'], 241)
        self.assertEqual([x['storage'] for x in data['storages']], ['available-images'])
        self.assertEqual([x['iface'] for x in data['bridges']], ['br-ok'])

    @staticmethod
    def storage_fixture():
        disks = [{'path': '/dev/nvme0n1', 'type': 'disk', 'model': 'Disk One', 'size': 500 * 1024**3},
                 {'path': '/dev/nvme1n1', 'type': 'disk', 'model': 'Disk Two', 'size': 1000 * 1024**3}]
        parts = [{'path': disk['path'] + 'p3', 'type': 'part', 'children': [disk]} for disk in disks]
        datasets = {
            '/storage': [{'storage': 'ai-dev', 'path': '/var/lib/ai-dev-storage',
                          'password': 'TEST_SECRET_PASSWORD', 'encryption-key': 'TEST_SECRET_KEY'},
                         {'storage': 'thin-images', 'vgname': 'pve', 'thinpool': 'data'}],
            '*/storage': [{'storage': 'thin-images', 'type': 'lvmthin', 'content': 'images', 'active': 1, 'avail': 500},
                          {'storage': 'ai-dev', 'type': 'dir', 'content': 'images,snippets', 'active': 1, 'avail': 600}],
            '*/network': [{'iface': 'br-test', 'type': 'bridge', 'active': 1}],
            '/cluster/nextid': 219,
        }
        probes = {
            'findmnt:/var/lib/ai-dev-storage': {'filesystems': [
                {'target': '/', 'source': '/dev/mapper/pve-root', 'fstype': 'ext4'}]},
            'lsblk:/dev/mapper/pve-root': {'blockdevices': [
                {'path': '/dev/mapper/pve-root', 'type': 'lvm', 'children': parts}]},
            'pvs': {'report': [{'pv': [{'vg_name': 'pve', 'pv_name': part['path']} for part in parts]
                                   + [{'vg_name': 'other-vg', 'pv_name': '/dev/unrelated'}]}]},
        }
        probes.update({'lsblk:' + part['path']: {'blockdevices': [part]} for part in parts})
        return datasets, probes

    def test_storage_discovery_tracks_all_directory_and_lvm_disks_without_secrets(self):
        datasets, probes = self.storage_fixture()
        data, calls = self.discovery(datasets, probes)
        self.assertEqual([s['storage'] for s in data['storages']], ['ai-dev', 'thin-images'])
        directory, thin = [s['location'] for s in data['storages']]
        self.assertEqual(directory['path'], '/var/lib/ai-dev-storage')
        self.assertEqual(directory['mount'], {'target': '/', 'source': '/dev/mapper/pve-root', 'fstype': 'ext4'})
        self.assertEqual((thin['vgname'], thin['thinpool']), ('pve', 'data'))
        for location in (directory, thin):
            self.assertEqual({d['path'] for d in location['disks']}, {'/dev/nvme0n1', '/dev/nvme1n1'})
            self.assertEqual({d['model'] for d in location['disks']}, {'Disk One', 'Disk Two'})
        self.assertNotIn('TEST_SECRET', json.dumps(data))
        self.assertEqual(sum(args[:3] == ['pvesh', 'get', '/storage'] for args in calls), 1)
        self.assertEqual(sum(args[0] == 'pvs' for args in calls), 1)
        self.assertFalse(any('/dev/unrelated' in args for args in calls))
        code = (self.source('components/the_agent_workstation.sh')
                + 'onepve_agent_resources() { printf "%s\\n" ' + shlex.quote(json.dumps(data)) + '; }\n'
                + self.mock_cli() + 'onepve_agent_wizard create')
        result = self.shell(code, self.form())
        for text in ['/var/lib/ai-dev-storage', '/dev/mapper/pve-root', 'ext4',
                     '/dev/nvme0n1', '/dev/nvme1n1', 'Disk One', 'Disk Two', '根文件系统', 'pve', 'data']:
            self.assertIn(text, result.stdout)
        self.assertLess(result.stdout.index('根文件系统'), result.stdout.index('【计划】'))

    def test_network_and_zfs_storage_do_not_guess_local_physical_disks(self):
        datasets = {
            '/storage': [{'storage': 'remote', 'path': '/mnt/pve/remote', 'server': '192.0.2.8', 'export': '/images'},
                         {'storage': 'pool-images', 'pool': 'tank/data'}],
            '*/storage': [{'storage': 'remote', 'type': 'nfs', 'content': 'images', 'active': 1, 'avail': 600},
                          {'storage': 'pool-images', 'type': 'zfspool', 'content': 'images', 'active': 1, 'avail': 500}],
            '*/network': [{'iface': 'br-test', 'type': 'bridge', 'active': 1}], '/cluster/nextid': 219,
        }
        probes = {'findmnt:/mnt/pve/remote': {'filesystems': [
            {'target': '/mnt/pve/remote', 'source': '192.0.2.8:/images', 'fstype': 'nfs4'}]}}
        data, calls = self.discovery(datasets, probes)
        remote, pool = [s['location'] for s in data['storages']]
        self.assertEqual((remote['server'], remote['export']), ('192.0.2.8', '/images'))
        self.assertEqual(remote['mount']['source'], '192.0.2.8:/images')
        self.assertEqual(pool['pool'], 'tank/data')
        self.assertEqual(remote['disks'], [])
        self.assertEqual(pool['disks'], [])
        self.assertFalse(any(args[0] in ('lsblk', 'pvs') for args in calls))

    def test_optional_storage_probes_fail_without_blocking_selection(self):
        failures = [('/storage', 'missing'), ('findmnt:/var/lib/ai-dev-storage', 'missing'),
                    ('findmnt:/var/lib/ai-dev-storage', 'timeout'),
                    ('findmnt:/var/lib/ai-dev-storage', 'invalid'),
                    ('lsblk:/dev/mapper/pve-root', 'invalid'), ('pvs', 'invalid')]
        for key, failure in failures:
            with self.subTest(key=key, failure=failure):
                datasets, probes = self.storage_fixture()
                data, _ = self.discovery(datasets, probes, {key: failure})
                self.assertEqual([s['storage'] for s in data['storages']], ['ai-dev', 'thin-images'])
                self.assertEqual(data['nextid'], 219)
        for key, value in [('pvs', {'report': [None]}),
                           ('lsblk:/dev/mapper/pve-root', {'blockdevices': [{'children': 7}]})]:
            with self.subTest(key=key, malformed=value):
                datasets, probes = self.storage_fixture()
                probes[key] = value
                data, _ = self.discovery(datasets, probes)
                self.assertEqual([s['storage'] for s in data['storages']], ['ai-dev', 'thin-images'])


if __name__ == '__main__':
    unittest.main()

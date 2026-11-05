"""Legacy menu checks use temporary configuration paths, never /etc writes."""
import hashlib
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('onepve_legacy', REPO / 'lib/onepve_legacy.py')
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)
BASH = (r'C:\Program Files\Git\bin\bash.exe' if os.name == 'nt'
        and Path(r'C:\Program Files\Git\bin\bash.exe').exists() else shutil.which('bash'))
NETWORK = ('auto lo\niface lo inet loopback\n\nauto vmbr0\niface vmbr0 inet static\n'
           '\taddress 192.0.2.3/24\n\tgateway 192.0.2.1\n\tbridge-ports eno1\n'
           '\tbridge-stp off\n\tdns-nameservers 192.0.2.53\n\n'
           'iface vmbr0 inet6 static\n\taddress 2001:db8::3/64\n\n'
           'auto vmbr1\niface vmbr1 inet static\n\taddress 198.51.100.3/24\n'
           '\tbridge-ports none\n')


class LegacyFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'interfaces'
        self.path.write_bytes(NETWORK.encode())

    def tearDown(self):
        self.temp.cleanup()

    def test_dhcp_changes_only_selected_ipv4_and_keeps_bridge_dns_ipv6(self):
        before, after = legacy.dhcp_plan(self.path, 'vmbr0')
        after = after.decode()
        self.assertEqual(before.decode(), NETWORK)
        self.assertIn('iface vmbr0 inet dhcp', after)
        self.assertIn('# onePVE previous static: address 192.0.2.3/24', after)
        self.assertIn('\tbridge-ports eno1\n\tbridge-stp off\n\tdns-nameservers 192.0.2.53\n', after)
        unchanged = NETWORK[NETWORK.index('iface vmbr0 inet6'):]
        self.assertTrue(after.endswith(unchanged))
        self.assertEqual(self.path.read_text(), NETWORK)

    def test_dhcp_rejects_ambiguous_hooks_and_included_duplicate_without_writes(self):
        for body in [NETWORK.replace('\tbridge-stp off', '\tup ip route add 10.0.0.0/8 via 192.0.2.1'),
                     NETWORK + '\niface vmbr0 inet static\n address 192.0.2.4/24\n']:
            self.path.write_text(body)
            with self.assertRaises(ValueError):legacy.dhcp_plan(self.path, 'vmbr0')
            self.assertEqual(self.path.read_text(), body)
        included = self.root / 'extra'
        included.write_text('iface vmbr0 inet static\n address 192.0.2.4/24\n')
        self.path.write_text('source ' + included.as_posix() + '\n' + NETWORK)
        with self.assertRaisesRegex(ValueError, '只有一个'):legacy.dhcp_plan(self.path, 'vmbr0')
        with self.assertRaises(ValueError):legacy.dhcp_plan(self.path, 'missing')

    def test_apply_has_exact_backup_and_rejects_preview_drift(self):
        before, after = legacy.dhcp_plan(self.path, 'vmbr1')
        digest = hashlib.sha256(before).hexdigest()
        self.path.write_bytes(before + b'# concurrent change\n')
        with self.assertRaisesRegex(ValueError, '变化'):legacy.dhcp_apply(self.path, 'vmbr1', digest)
        self.assertEqual(list(self.root.glob('*.bak')), [])
        self.path.write_bytes(before)
        backup = legacy.dhcp_apply(self.path, 'vmbr1', digest)
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(self.path.read_bytes(), after)
        if os.name == 'posix':self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == 'posix', 'symlink creation is restricted on Windows')
    def test_symlinks_are_refused(self):
        link = self.root / 'linked'
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):legacy.dhcp_plan(link, 'vmbr0')


@unittest.skipUnless(BASH, 'Bash is required for legacy menu checks')
class LegacyShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.network = self.root / 'interfaces'
        self.network.write_bytes(NETWORK.encode())
        self.node = self.root / 'nodes/testnode'
        for name in ['lxc', 'qemu-server']:(self.node / name).mkdir(parents=True)
        (self.node / 'qemu-server/103.conf').write_text('name: ai-dev\n')
        self.latest = self.root / 'current backup'
        self.archives = self.root / 'backup archives'
        binary = self.root / 'bin'; binary.mkdir()
        python = binary / 'python3'
        python.write_text('#!/usr/bin/env bash\nset -o pipefail\n' + shlex.quote(sys.executable.replace('\\', '/'))
                          + ' "$@" | tr -d "\\r"\n', encoding='utf-8')
        python.chmod(0o755)
        self.env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                        MSYS_NO_PATHCONV='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        self.env['TMPDIR'] = self.root.as_posix()

    def tearDown(self):
        self.temp.cleanup()

    def run_menu(self, name, function, inputs='', prefix=''):
        content = (REPO / 'components' / name).read_text(encoding='utf-8')
        content = content.replace('/etc/network/interfaces', self.network.as_posix())
        content = content.replace('/etc/pve/nodes/', (self.root / 'nodes').as_posix() + '/')
        component = self.root / name; component.write_text(content, encoding='utf-8')
        code = 'ONEPVE_ROOT=' + shlex.quote(REPO.as_posix()) + '\nsource ' + shlex.quote(component.as_posix()) + '\n'
        code += 'backup_dir=' + shlex.quote(self.latest.as_posix()) + '\nbackup_archive_dir=' + shlex.quote(self.archives.as_posix()) + '\n'
        code += 'onepve_backup_node() { printf "testnode\\n"; }\n'
        if os.name == 'nt':
            code += 'mktemp() { local value; value="$(command mktemp "$@")" || return; cygpath -m "$value"; };\n'
            code += 'tar() { command tar --force-local "$@"; };\n'
        code += prefix + '\n' + function
        result = subprocess.run([BASH, '--noprofile', '--norc', '-c', code], input=inputs.encode(),
                                cwd=self.root, env=self.env, capture_output=True, timeout=30)
        result.stdout = result.stdout.decode('utf-8'); result.stderr = result.stderr.decode('utf-8')
        return result

    def backup(self, prefix=''):
        return self.run_menu('the_backup_restore.sh', 'the_backup', 'y\n', prefix)

    def test_dhcp_cancel_is_readonly_and_confirm_changes_one_interface(self):
        result = self.run_menu('the_network_dhcp.sh', 'the_network_dhcp', 'vmbr0\n\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.network.read_text(), NETWORK)
        self.assertFalse(list(self.root.glob('*.bak')))
        result = self.run_menu('the_network_dhcp.sh', 'the_network_dhcp', 'vmbr0\ny\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('iface vmbr0 inet dhcp', self.network.read_text())
        self.assertIn('iface vmbr1 inet static', self.network.read_text())
        self.assertIn('未应用网络', result.stdout)

    def test_repeated_backup_does_not_overwrite_archive_or_previous_folder(self):
        first = self.backup()
        self.assertEqual(first.returncode, 0, first.stderr)
        files = list(self.archives.glob('*.tar.gz'))
        self.assertEqual(len(files), 1)
        original = files[0].read_bytes()
        self.network.write_text(NETWORK + '# second backup\n')
        second = self.backup()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(list(self.archives.glob('*.tar.gz'))), 2)
        self.assertEqual(files[0].read_bytes(), original)
        previous = list(self.root.glob('.onepve-previous-backup.*'))
        self.assertEqual(len(previous), 1)
        self.assertEqual((previous[0] / 'interfaces').read_text(), NETWORK)
        self.assertEqual((self.latest / 'interfaces').read_text(), NETWORK + '# second backup\n')

    def test_copy_or_archive_failure_keeps_old_backup_and_reports_failure(self):
        self.assertEqual(self.backup().returncode, 0)
        before = (self.latest / 'interfaces').read_bytes()
        archives = {p.name:p.read_bytes() for p in self.archives.glob('*.tar.gz')}
        for prefix in ['cp() { return 1; }', 'tar() { return 2; }']:
            result = self.backup(prefix)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('配置备份完成', result.stdout)
            self.assertIn('备份失败', result.stderr)
            self.assertEqual((self.latest / 'interfaces').read_bytes(), before)
            self.assertEqual({p.name:p.read_bytes() for p in self.archives.glob('*.tar.gz')}, archives)
            self.assertFalse(list(self.archives.glob('*.partial')))
            self.assertFalse(list(self.root.glob('.onepve-config-backup.*')))

    def test_restore_default_cancel_and_failed_copy_do_not_claim_success(self):
        self.assertEqual(self.backup().returncode, 0)
        self.network.write_text('# current\n')
        result = self.run_menu('the_backup_restore.sh', 'the_restore', '\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.network.read_text(), '# current\n')
        result = self.run_menu('the_backup_restore.sh', 'the_restore', 'y\ny\n', 'cp() { return 1; }')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('配置还原完成', result.stdout)
        self.assertIn('原备份保留', result.stderr)
        self.assertEqual((self.latest / 'interfaces').read_text(), NETWORK)
        self.assertEqual(self.network.read_text(), '# current\n')

    def test_restore_success_preserves_other_config_and_source_permissions(self):
        self.assertEqual(self.backup().returncode, 0)
        self.network.write_text('# modified\n')
        extra = self.node / 'qemu-server/200.conf'; extra.write_text('name: unrelated\n')
        mode = (self.latest / 'interfaces').stat().st_mode
        result = self.run_menu('the_backup_restore.sh', 'the_restore', 'y\ny\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('配置还原完成', result.stdout)
        self.assertEqual(self.network.read_text(), NETWORK)
        self.assertEqual(extra.read_text(), 'name: unrelated\n')
        self.assertEqual((self.latest / 'interfaces').stat().st_mode, mode)

    def test_restore_succeeds_when_pmxcfs_rejects_attribute_preservation(self):
        self.assertEqual(self.backup().returncode, 0)
        source = self.latest / 'qemu-server/103.conf'
        target = self.node / 'qemu-server/103.conf'
        if os.name == 'posix':
            source.chmod(0o600); target.chmod(0o640)
            (self.latest / 'qemu-server').chmod(0o700)
            (self.node / 'qemu-server').chmod(0o750)
            self.network.chmod(0o640)
        source_modes = {str(path):path.stat().st_mode for path in self.latest.rglob('*')}
        destination_modes = {str(path):path.stat().st_mode for path in [self.network, target, target.parent]}
        self.network.write_text('# changed network\n'); target.write_text('name: changed\n')
        refuse_attributes = '''
cp() {
  local option
  for option in "$@"; do
    case "$option" in -a|-p|--archive|--preserve*) printf 'pmxcfs refuses preserved attributes\\n' >&2; return 90;; esac
  done
  command cp "$@"
}
chmod() { printf 'pmxcfs refuses chmod\\n' >&2; return 91; }
chown() { printf 'pmxcfs refuses chown\\n' >&2; return 92; }
'''
        result = self.run_menu('the_backup_restore.sh', 'the_restore', 'y\ny\n', refuse_attributes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('配置还原完成', result.stdout)
        self.assertEqual(target.read_text(), 'name: ai-dev\n')
        self.assertEqual(self.network.read_text(), NETWORK)
        self.assertEqual({str(path):path.stat().st_mode for path in self.latest.rglob('*')}, source_modes)
        self.assertEqual({str(path):path.stat().st_mode for path in [self.network, target, target.parent]}, destination_modes)


if __name__ == '__main__':unittest.main()

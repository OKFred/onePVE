"""Missing-VM record cleanup needs consent and a fresh absence check."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import onepve_agent as agent

REAL_LOCK = agent._lock
PUBLIC = 'ssh-ed25519 ' + base64.b64encode(
    struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + bytes(range(32))).decode()


class StaleRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        for name, value in [('AGENT_STATE', self.root / 'state'), ('VM_CONFIGS', self.root / 'qemu'),
                            ('PVE_NODES', self.root / 'nodes'), ('QEMU_RUN', self.root / 'run')]:
            self.stack.enter_context(mock.patch.object(agent, name, value, create=True))
        self.stack.enter_context(mock.patch.object(agent, 'require_root'))
        self.stack.enter_context(mock.patch.object(agent, 'node_name', return_value='pve'))
        self.api = self.stack.enter_context(mock.patch.object(agent, 'pve_get', return_value=103))
        self.lock = self.stack.enter_context(mock.patch.object(agent, '_lock', side_effect=lambda *args: contextlib.nullcontext()))
        self.resources = self.stack.enter_context(mock.patch.object(agent, '_resources', return_value={'storage': 'pool'}))
        self.stack.enter_context(mock.patch.object(agent, '_select_gpu', return_value=None))
        self.prepared = {'fixture': 'prepared inputs'}
        self.prepare = self.stack.enter_context(mock.patch.object(agent, 'prepare', return_value=self.prepared))
        self.validate = self.stack.enter_context(mock.patch.object(agent, '_validate_artifacts'))
        self.bundle = self.stack.enter_context(mock.patch.object(agent, '_bundle'))
        self.stack.enter_context(mock.patch.object(agent, '_continue', side_effect=lambda state: state))
        self.stack.enter_context(mock.patch.object(agent.artifacts, 'image_options', return_value={
            'latest': {'version': 'fixed'}, 'cached': [], 'lookup_error': None, 'cache_errors': []}))
        self.config = agent.normalize(dict(vmid=103, storage='pool', bridge='vmbr0', ssh_key=PUBLIC, gpu='none',network_policy='legacy'))
        self.path = agent._state_path(103)
        self.path.parent.mkdir(mode=0o700, parents=True)
        self.path.parent.chmod(0o700)
        self.old = dict(schema_version=1, vmid=103, owner='a' * 32, phase='READY_GPU',
                        operation='create', config={'name': 'old-ai-dev'})
        self.write_record(self.old)
        self.lock_path = self.path.with_name('operation.lock'); self.lock_path.write_bytes(b'')
        self.lock_path.chmod(0o600)
        self.staged = self.path.with_name('payload-old.tar.gz'); self.staged.write_bytes(b'old staging evidence')
        self.cache = self.root / 'cache'
        self.stack.enter_context(mock.patch.object(agent.artifacts, 'CACHE_ROOT', self.cache))
        for relative in ['images/debian.qcow2', 'driver-binaries/modules.tar.gz']:
            path = self.cache / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(relative.encode())

    def write_record(self, value):
        self.path.write_text(json.dumps(value), encoding='utf-8'); self.path.chmod(0o600)

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def attempt(self, answer='y\n', stdin=None, dry_run=False):
        self.stderr = io.StringIO()
        with mock.patch.object(sys, 'stdin', stdin or io.StringIO(answer)), contextlib.redirect_stderr(self.stderr):
            return agent.create(self.config, dry_run)

    def no_input(self):
        stream = mock.Mock(); stream.readline.side_effect = AssertionError('unexpected cleanup prompt')
        return stream

    def test_consent_replaces_only_old_record_and_preserves_lock_staging_and_caches(self):
        before = self.snapshot(); inode = self.lock_path.stat().st_ino
        result = self.attempt()
        actual = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertNotEqual(actual['owner'], self.old['owner'])
        self.assertEqual(actual['phase'], 'PLANNED')
        self.assertEqual(result['previous_record_cleared']['owner'], self.old['owner'])
        self.assertEqual(result['previous_record_cleared']['phase'], self.old['phase'])
        self.assertEqual(self.lock_path.stat().st_ino, inode)
        after = self.snapshot(); before.pop(self.path.relative_to(self.root).as_posix())
        after.pop(self.path.relative_to(self.root).as_posix())
        self.assertEqual(before, after)
        self.prepare.assert_called_once(); self.bundle.assert_called_once()
        self.assertIn('103', self.stderr.getvalue()); self.assertIn('[y/N]', self.stderr.getvalue())
        self.assertTrue(all(call.args == ('/cluster/nextid', '--vmid', '103') for call in self.api.call_args_list))
        self.assertGreaterEqual(self.api.call_count, 2)

    def test_refusal_blank_or_eof_preserves_everything_without_preparing_inputs(self):
        before = self.snapshot()
        for answer in ['n\n', 'N\n', '\n', '', 'yes\n']:
            with self.subTest(answer=answer), self.assertRaises(RuntimeError):
                self.attempt(answer)
            self.assertEqual(self.snapshot(), before)
        self.resources.assert_not_called(); self.prepare.assert_not_called(); self.bundle.assert_not_called()

    def test_no_record_needs_no_cleanup_prompt(self):
        self.path.unlink()
        result = self.attempt(stdin=self.no_input())
        self.assertEqual(result['phase'], 'PLANNED')
        self.assertNotIn('previous_record_cleared', result)

    def test_plan_reports_stale_record_without_prompt_or_writes(self):
        before = self.snapshot()
        result = self.attempt(stdin=self.no_input(), dry_run=True)
        self.assertEqual(result['status'], 'PLAN_ONLY')
        self.assertEqual(result['previous_record']['owner'], self.old['owner'])
        self.assertEqual(result['previous_record']['phase'], self.old['phase'])
        self.assertEqual(self.snapshot(), before)
        self.lock.assert_not_called(); self.prepare.assert_not_called()

    def test_api_failure_or_unexpected_nextid_does_not_prove_absence(self):
        before = self.snapshot()
        for response in [None, [], {}, '0103', '103.0', ' 103', '104', 103.0, True, 104, RuntimeError('VMID already in use')]:
            with self.subTest(response=response):
                self.api.reset_mock(); self.api.side_effect = response if isinstance(response, Exception) else None
                self.api.return_value = response
                with self.assertRaises((RuntimeError, ValueError)):
                    self.attempt(stdin=self.no_input())
                self.assertEqual(self.snapshot(), before)
        self.resources.assert_not_called(); self.prepare.assert_not_called()

    def test_canonical_string_nextid_still_confirms_the_requested_vmid(self):
        self.api.return_value = '103'
        result = self.attempt()
        self.assertEqual(result['phase'], 'PLANNED')
        self.assertIn('previous_record_cleared', result)
        self.assertGreaterEqual(self.api.call_count, 2)

    def test_qemu_lxc_and_remaining_pid_refuse_without_cleanup_prompt(self):
        paths = [agent.VM_CONFIGS / '103.conf', agent.PVE_NODES / 'remote/qemu-server/103.conf',
                 agent.VM_CONFIGS.parent / 'lxc/103.conf', agent.PVE_NODES / 'remote/lxc/103.conf',
                 agent.QEMU_RUN / '103.pid']
        for path in paths:
            with self.subTest(path=path):
                path.parent.mkdir(parents=True, exist_ok=True); path.write_text('occupied')
                original = self.path.read_bytes()
                with self.assertRaises(RuntimeError): self.attempt(stdin=self.no_input())
                self.assertEqual(self.path.read_bytes(), original)
                path.unlink()
        self.prepare.assert_not_called()

    def test_preparation_failure_keeps_old_record_until_new_inputs_are_ready(self):
        before = self.snapshot(); self.prepare.side_effect = RuntimeError('mirror unavailable')
        with self.assertRaisesRegex(RuntimeError, 'mirror unavailable'): self.attempt()
        self.assertEqual(self.snapshot(), before); self.bundle.assert_not_called()

    def test_reappearing_vmid_during_prepare_keeps_old_record(self):
        self.api.side_effect = [103, RuntimeError('VMID was recreated')]
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'recreated'): self.attempt()
        self.assertEqual(self.snapshot(), before); self.bundle.assert_not_called()

    def test_record_changed_during_prepare_is_not_replaced(self):
        changed = dict(self.old, phase='INSTALL_RUNNING')
        def prepare():
            self.write_record(changed)
            return self.prepared
        self.prepare.side_effect = prepare
        with self.assertRaises(RuntimeError): self.attempt()
        self.assertEqual(json.loads(self.path.read_text()), changed); self.bundle.assert_not_called()

    def test_record_appearing_during_normal_create_is_not_overwritten(self):
        self.path.unlink()
        def prepare():
            self.write_record(self.old)
            return self.prepared
        self.prepare.side_effect = prepare
        with self.assertRaisesRegex(RuntimeError, 'appeared'):
            self.attempt(stdin=self.no_input())
        self.assertEqual(json.loads(self.path.read_text()), self.old); self.bundle.assert_not_called()

    def test_atomic_state_write_failure_does_not_remove_old_record(self):
        before = self.snapshot()
        with mock.patch.object(agent, 'atomic_json', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'): self.attempt()
        self.assertEqual(self.snapshot(), before); self.bundle.assert_not_called()

    def test_untrusted_record_is_not_offered_for_automatic_cleanup(self):
        for value in [[], None, dict(self.old, vmid=104), dict(self.old, owner='../foreign'),
                      dict(self.old, operation='unknown'), dict(self.old, phase='../state')]:
            with self.subTest(value=value):
                self.write_record(value); original = self.path.read_bytes()
                with self.assertRaises((RuntimeError, ValueError)): self.attempt(stdin=self.no_input())
                self.assertEqual(self.path.read_bytes(), original)
        self.path.write_text('{invalid json', encoding='utf-8')
        with self.assertRaises((RuntimeError, ValueError)): self.attempt(stdin=self.no_input())
        self.prepare.assert_not_called()

    def test_redirected_record_is_not_removed(self):
        outside = self.root / 'foreign.json'; self.path.rename(outside)
        try:
            self.path.symlink_to(outside)
        except OSError:
            self.skipTest('Symlinks unavailable on this test host')
        original = outside.read_bytes()
        with self.assertRaises(RuntimeError): self.attempt(stdin=self.no_input())
        self.assertTrue(self.path.is_symlink()); self.assertEqual(outside.read_bytes(), original)

    def test_hardlinked_or_redirected_parent_record_is_retained(self):
        outside = self.root / 'linked.json'; os.link(self.path, outside)
        original = outside.read_bytes()
        with self.assertRaises(RuntimeError): self.attempt(stdin=self.no_input())
        self.assertEqual(outside.read_bytes(), original); outside.unlink()
        directory = self.root / 'redirected'; self.path.parent.rename(directory)
        try:
            self.path.parent.symlink_to(directory, target_is_directory=True)
        except OSError:
            self.skipTest('Directory symlinks unavailable on this test host')
        with self.assertRaises(RuntimeError): self.attempt(stdin=self.no_input())
        self.assertEqual((directory / 'state.json').read_bytes(), original)
        self.prepare.assert_not_called()

    @unittest.skipUnless(agent.fcntl is not None and hasattr(os, 'geteuid') and os.geteuid() == 0,
                         'Real root-owned Linux VMID locking')
    def test_cleanup_holds_existing_lock_without_replacing_its_inode(self):
        inode = self.lock_path.stat().st_ino
        def prepare():
            with self.assertRaises(BlockingIOError):
                with REAL_LOCK(103):
                    pass
            return self.prepared
        self.prepare.side_effect = prepare
        with mock.patch.object(agent, '_lock', REAL_LOCK):
            self.attempt()
        self.assertEqual(self.lock_path.stat().st_ino, inode)


if __name__ == '__main__':
    unittest.main()

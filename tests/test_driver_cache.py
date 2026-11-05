import base64
import contextlib
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import onepve_agent as agent
import onepve_common as common
import onepve_driver_cache as cache


SPEC = dict(kernel='6.12.113+deb13-amd64', version='2025.12.10.2', sha256='a' * 64)
HEADERS = dict(symvers_sha256='b' * 64, config_sha256='c' * 64)
ARCH = 'x86_64'
MODULE_BYTES = {name + '.ko': ('module-' + name).encode() for name in cache.MODULES}


class DriverCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(cache.os, 'uname', return_value=types.SimpleNamespace(machine=ARCH), create=True))
        self.stack.enter_context(mock.patch.object(cache, '_headers', return_value=dict(HEADERS)))
        self.stack.enter_context(mock.patch.object(cache.gpu, '_secure_module_policy'))

    def archive(self, spec=None, arch=ARCH, headers=None, changes=None):
        blobs = {'modules/' + name: value for name, value in MODULE_BYTES.items()}
        manifest = dict(schema_version=1, identity=cache.identity(spec or SPEC, arch), headers=headers or HEADERS,
                        files={name: hashlib.sha256(value).hexdigest() for name, value in blobs.items()})
        if changes:
            changes(manifest, blobs)
        blobs['manifest.json'] = json.dumps(manifest).encode()
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as stream:
            for name, body in blobs.items():
                info = tarfile.TarInfo(name); info.size = len(body); stream.addfile(info, io.BytesIO(body))
        return output.getvalue()

    def fake_guest(self):
        directory = self.root / ('guest-' + str(len(list(self.root.glob('guest-*')))))
        directory.mkdir(); source = directory / 'source'; source.mkdir(); (source / 'dkms.conf').write_bytes(b'TRUSTED_LOCAL_SOURCE=1\n')
        native_dkms = directory / 'dkms'
        built = native_dkms / SPEC['version'] / SPEC['kernel'] / ARCH / 'module'
        installed = directory / 'installed'
        state = dict(registered=False, installed=False, calls=[], source=source, directory=directory)
        def mapped(value='.'):
            if str(value) == '/var/lib/dkms/i915-sriov-dkms':
                return native_dkms
            return Path(value)
        def verify(kernel, unused, modules):
            self.assertEqual(kernel, SPEC['kernel'])
            self.assertEqual({p.name for p in modules}, set(MODULE_BYTES))
            for path in modules:
                self.assertEqual(path.read_bytes(), MODULE_BYTES[path.name])
            return [dict(path=str(p), sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in modules]
        def run(argv, **kwargs):
            state['calls'].append(list(argv)); answer = ''; rc = 0
            if argv[:2] == ['dkms', 'status']:
                answer = 'added' if state['registered'] else ''
            elif argv[:2] == ['dkms', 'ldtarball']:
                self.assertFalse(state['registered'])
                with tarfile.open(argv[2]) as stream:
                    expected = {'dkms_main_tree/' + SPEC['kernel'] + '/' + ARCH + '/module/' + n for n in MODULE_BYTES}
                    self.assertEqual(set(stream.getnames()), expected | {'dkms_binaries_only/dkms.conf'})
                    self.assertEqual(stream.extractfile('dkms_binaries_only/dkms.conf').read(), b'TRUSTED_LOCAL_SOURCE=1\n')
                    built.mkdir(parents=True)
                    for name in MODULE_BYTES:
                        member = 'dkms_main_tree/' + SPEC['kernel'] + '/' + ARCH + '/module/' + name
                        (built / name).write_bytes(stream.extractfile(member).read())
                state['registered'] = True
            elif argv[:2] == ['dkms', 'add']:
                state['registered'] = True
            elif argv[:2] == ['dkms', 'build']:
                self.assertNotIn('-j', argv)
                self.assertIsNone(kwargs.get('timeout', 60))
                built.mkdir(parents=True, exist_ok=True)
                for name, body in MODULE_BYTES.items(): (built / name).write_bytes(body)
            elif argv[:2] == ['dkms', 'install']:
                self.assertTrue(built.exists()); self.assertNotIn('-j', argv)
                self.assertIsNone(kwargs.get('timeout', 60))
                installed.mkdir(exist_ok=True)
                for name in MODULE_BYTES: (installed / name).write_bytes((built / name).read_bytes())
                state['installed'] = True
            elif argv[0] == 'modinfo':
                field = argv[argv.index('-F') + 1]
                if '-k' in argv:
                    if field == 'version': answer = SPEC['version'] + '-sriov' if state['installed'] else ''
                    elif field == 'filename': answer = str(installed / (argv[-1] + '.ko'))
                elif field == 'version': answer = SPEC['version'] + '-sriov'
                else: self.fail(str(argv))
            else:
                self.fail('Unexpected guest command: ' + str(argv))
            return subprocess.CompletedProcess(argv, rc, answer, '')
        return state, mapped, verify, run

    def prepare_guest(self, data=None, extra=None, installed=False):
        state, mapped, verify, run = self.fake_guest()
        if installed:
            state['registered'] = True
            run(['dkms', 'build'], timeout=None); run(['dkms', 'install'], timeout=None)
            state['calls'].clear()
        incoming = state['directory'] / 'input.tar.gz'; outgoing = state['directory'] / 'output.tar.gz'
        if data is not None: incoming.write_bytes(data)
        with mock.patch.object(cache, 'Path', side_effect=mapped), \
             mock.patch.object(cache, 'run', side_effect=run), \
             mock.patch.object(cache, '_dkms', side_effect=lambda *args: run(['dkms', *args], timeout=None)), \
             mock.patch.object(cache.gpu, '_verify_built_modules', side_effect=verify), \
             contextlib.ExitStack() as stack:
            if extra: extra(stack)
            report = cache.prepare(SPEC, state['source'], incoming, outgoing)
        return report, state, outgoing

    def test_miss_build_publish_then_fresh_guest_reuses_without_compile(self):
        first, first_guest, archive = self.prepare_guest()
        self.assertEqual(first['status'], 'BUILT'); self.assertTrue(first['exported'])
        self.assertEqual(sum(call[:2] == ['dkms', 'build'] for call in first_guest['calls']), 1)
        root = self.root / 'host-cache'
        path = cache.store(root, SPEC, ARCH, archive.read_bytes())
        self.assertEqual(cache.load(root, SPEC, ARCH), path.read_bytes())
        second, second_guest, _ = self.prepare_guest(cache.load(root, SPEC, ARCH))
        self.assertEqual(second['status'], 'HIT')
        self.assertFalse(any(call[:2] == ['dkms', 'build'] for call in second_guest['calls']))
        self.assertTrue(any(call[:2] == ['dkms', 'install'] for call in second_guest['calls']))

    def test_long_build_inherits_job_log_without_short_timeout(self):
        with mock.patch.object(cache.subprocess, 'run') as execute:
            cache._dkms('build', '-m', 'i915-sriov-dkms', '-v', SPEC['version'], '-k', SPEC['kernel'])
        execute.assert_called_once_with(['dkms', 'build', '-m', 'i915-sriov-dkms', '-v', SPEC['version'], '-k', SPEC['kernel']], check=True)

    def test_already_installed_modules_are_verified_and_exported_without_build_or_install(self):
        report, guest, archive = self.prepare_guest(installed=True)
        self.assertEqual(report['status'], 'INSTALLED'); self.assertTrue(report['exported'])
        self.assertFalse(any(call[0] == 'dkms' for call in guest['calls']))
        cache.inspect_archive(archive.read_bytes(), SPEC, ARCH)

    def test_identity_header_or_checksum_mismatch_compiles_normally(self):
        candidates = [self.archive(spec=dict(SPEC, kernel='6.12.114+deb13-amd64')),
                      self.archive(spec=dict(SPEC, version='2025.12.10.1')),
                      self.archive(spec=dict(SPEC, sha256='d' * 64)), self.archive(arch='aarch64'),
                      self.archive(headers=dict(HEADERS, symvers_sha256='e' * 64)),
                      self.archive(headers=dict(HEADERS, config_sha256='f' * 64)),
                      self.archive(changes=lambda m, b: b.update({'modules/i915.ko': b'tampered'}))]
        for data in candidates:
            with self.subTest(case=hashlib.sha256(data).hexdigest()):
                report, guest, _ = self.prepare_guest(data)
                self.assertEqual(report['status'], 'BUILT')
                self.assertTrue(any(call[:2] == ['dkms', 'build'] for call in guest['calls']))
                self.assertFalse(any(call[:2] == ['dkms', 'ldtarball'] for call in guest['calls']))

    def test_crc_failure_before_import_is_a_compile_miss(self):
        count = [0]
        def extra(stack):
            previous = cache.gpu._verify_built_modules.side_effect
            def verify(*args):
                count[0] += 1
                if count[0] == 1: raise RuntimeError('Kernel/companion symbol CRC mismatch')
                return previous(*args)
            stack.enter_context(mock.patch.object(cache.gpu, '_verify_built_modules', side_effect=verify))
        report, guest, _ = self.prepare_guest(self.archive(), extra)
        self.assertEqual(report['status'], 'BUILT'); self.assertIn('CRC mismatch', report['reason'])
        self.assertTrue(any(call[:2] == ['dkms', 'build'] for call in guest['calls']))

    def test_signature_enforcement_does_not_reuse_another_vms_signed_binary(self):
        def extra(stack): stack.enter_context(mock.patch.object(cache.gpu, '_secure_module_policy', side_effect=RuntimeError('signed policy')))
        report, guest, _ = self.prepare_guest(self.archive(), extra)
        self.assertEqual(report['status'], 'BUILT'); self.assertIn('signed policy', report['reason'])
        self.assertFalse(any(call[:2] == ['dkms', 'ldtarball'] for call in guest['calls']))

    def test_export_failure_keeps_successfully_installed_driver(self):
        def extra(stack): stack.enter_context(mock.patch.object(cache, 'export_archive', side_effect=OSError('disk full')))
        report, guest, _ = self.prepare_guest(extra=extra)
        self.assertTrue(guest['installed']); self.assertFalse(report['exported'])
        self.assertIn('disk full', report['export_error'])

    def test_invalid_archive_paths_extra_files_and_truncation_are_rejected(self):
        def traversal(manifest, blobs):
            content = blobs.pop('modules/i915.ko'); blobs['../i915.ko'] = content
            manifest['files']['../i915.ko'] = manifest['files'].pop('modules/i915.ko')
        data = [self.archive(changes=traversal), self.archive(changes=lambda m, b: b.update({'extra': b'bad'})),
                self.archive()[:-12]]
        for archive in data:
            with self.subTest(archive=hashlib.sha256(archive).hexdigest()), self.assertRaises((RuntimeError, tarfile.TarError, OSError)):
                cache.inspect_archive(archive, SPEC, ARCH)

    def test_symlink_and_sparse_members_are_rejected_before_read(self):
        for kind in [tarfile.SYMTYPE, tarfile.GNUTYPE_SPARSE]:
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode='w:gz') as stream:
                for number in range(5):
                    info = tarfile.TarInfo('manifest.json' if not number else 'file-' + str(number))
                    if number == 1: info.type = kind; info.linkname = '/etc/passwd'
                    stream.addfile(info, io.BytesIO(b''))
            with self.subTest(kind=kind), self.assertRaises((RuntimeError, tarfile.TarError, ValueError)):
                cache.inspect_archive(output.getvalue(), SPEC, ARCH)

    def test_compressed_and_expanded_size_limits(self):
        data = self.archive()
        with mock.patch.object(cache, 'MAX_ARCHIVE_BYTES', 32), self.assertRaisesRegex(RuntimeError, 'compressed size'):
            cache.inspect_archive(data, SPEC, ARCH)
        with mock.patch.object(cache, 'MAX_EXPANDED_BYTES', 32), self.assertRaisesRegex(RuntimeError, 'expanded size'):
            cache.inspect_archive(data, SPEC, ARCH)

    def test_host_cache_is_keyed_by_exact_kernel_source_version_and_arch(self):
        data = self.archive(); root = self.root / 'cache'; cache.store(root, SPEC, ARCH, data)
        for spec, arch in [(dict(SPEC, kernel='6.12.114+deb13-amd64'), ARCH), (dict(SPEC, sha256='d' * 64), ARCH),
                           (dict(SPEC, version='2025.12.10.1'), ARCH), (SPEC, 'aarch64')]:
            with self.subTest(spec=spec, arch=arch), self.assertRaises(RuntimeError): cache.load(root, spec, arch)
        self.assertEqual(cache.load(root, SPEC, ARCH), data)

    def test_bad_cache_replaced_atomically_without_following_symlink(self):
        data = self.archive(); root = self.root / 'cache'; root.mkdir()
        path = cache.cache_path(root, SPEC, ARCH); path.write_bytes(b'corrupt')
        cache.store(root, SPEC, ARCH, data); self.assertEqual(cache.load(root, SPEC, ARCH), data)
        with mock.patch.object(cache.os, 'replace', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError): cache.store(root, SPEC, ARCH, data)
        self.assertEqual(path.read_bytes(), data); self.assertFalse(list(root.glob('.driver-*')))
        path.unlink(); outside = self.root / 'outside'; outside.write_bytes(b'foreign')
        try: path.symlink_to(outside)
        except OSError: self.skipTest('Symlinks unavailable')
        with self.assertRaises(RuntimeError): cache.store(root, SPEC, ARCH, data)
        self.assertEqual(outside.read_bytes(), b'foreign')


class DriverCacheTransportTests(unittest.TestCase):
    def test_guest_file_limit_closes_handle_without_reading_unbounded_data(self):
        q = object.__new__(common.Qga)
        q.call = mock.Mock(side_effect=[42, {'count': 4, 'buf-b64': base64.b64encode(b'data').decode(), 'eof': False}, None])
        with self.assertRaisesRegex(RuntimeError, 'read size limit'): q.read('/guest/archive', max_bytes=3)
        self.assertEqual(q.call.call_args, mock.call('guest-file-close', {'handle': 42}))

    def test_running_driver_resume_observes_original_inputs(self):
        state = dict(vmid=103, phase='GUEST_DRIVER_RUNNING', guest_stage='/stage',
                     jobs={'driver': {'unit': 'existing', 'result': {'status': 'RUNNING'}}})
        q = mock.MagicMock(); q.__enter__.return_value = q
        q.read.side_effect = OSError('old job had no cache report')
        with mock.patch.object(agent, 'Qga', return_value=q), mock.patch.object(agent, '_job') as job, \
             mock.patch.object(agent, '_guest', side_effect=AssertionError('do not restage')), \
             mock.patch.object(agent, '_save') as save:
            agent._prepare_driver(state)
        q.write.assert_not_called(); job.assert_called_once_with(state, 'driver', ['/usr/bin/python3', '/stage/prepare-driver.py'], retry_failed=False)
        self.assertIn('publish_error', state['driver_cache'])
        self.assertEqual(save.call_args, mock.call(state, 'GUEST_DRIVER_READY'))

    def test_host_publish_failure_does_not_fail_ready_driver(self):
        state = dict(vmid=103, phase='GUEST_DRIVER_RUNNING', guest_stage='/stage', driver_spec=SPEC, guest_arch=ARCH,
                     jobs={'driver': {'result': {'status': 'SUCCESS'}}})
        report = dict(status='BUILT', exported=True, reason='cache miss')
        q = mock.MagicMock(); q.__enter__.return_value = q
        q.read.side_effect = [json.dumps(report).encode(), b'cache bytes']
        with mock.patch.object(agent, 'Qga', return_value=q), mock.patch.object(agent, '_job'), \
             mock.patch.object(agent, '_guest', return_value={'stdout': SPEC['kernel']}), \
             mock.patch.object(agent.driver_cache, 'store', side_effect=OSError('host cache full')), \
             mock.patch.object(agent, '_save'):
            agent._prepare_driver(state)
        self.assertEqual(state['driver_cache']['status'], 'BUILT')
        self.assertIn('host cache full', state['driver_cache']['publish_error'])
        q.write.assert_not_called()

    def test_completed_driver_for_old_kernel_gets_new_inputs_and_job(self):
        current = dict(SPEC, kernel='6.12.114+deb13-amd64')
        state = dict(vmid=103, phase='GUEST_DRIVER_RUNNING', guest_stage='/stage', guest_payload='/stage/payload',
                     config={'apt_mirror': 'tuna'},
                     selected_gpu={'device': '0x7d67'}, driver_spec=SPEC, guest_arch=ARCH,
                     artifacts={'driver_sources': {SPEC['version']: '/source.tar.gz'}},
                     driver_cache_input='/stage/old-input.tar.gz', jobs={'driver': {'result': {'status': 'SUCCESS'}}})
        q = mock.MagicMock(); q.__enter__.return_value = q
        q.read.return_value = json.dumps(dict(status='BUILT', exported=False)).encode()
        with mock.patch.object(agent, 'Qga', return_value=q), mock.patch.object(agent, '_job') as job, \
             mock.patch.object(agent, '_guest', side_effect=[{'stdout': current['kernel']}, {'stdout': current['kernel']}, {'stdout': ARCH}]), \
             mock.patch.object(agent.gpu, 'driver_spec', return_value=current), \
             mock.patch.object(agent.driver_cache, 'load', side_effect=RuntimeError('cache miss')), mock.patch.object(agent, '_save'):
            agent._prepare_driver(state)
        self.assertEqual(state['driver_spec'], current)
        self.assertNotEqual(state['driver_cache_input'], '/stage/old-input.tar.gz')
        self.assertFalse(any(call.args[0] == '/stage/old-input.tar.gz' for call in q.write.call_args_list))
        script = next(call.args[1] for call in q.write.call_args_list if call.args[0] == '/stage/prepare-driver.py')
        self.assertIn(b'https://mirrors.tuna.tsinghua.edu.cn/debian-security', script)
        job.assert_called_once(); self.assertNotIn('driver', state['jobs'])


if __name__ == '__main__':
    unittest.main()

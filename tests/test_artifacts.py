import importlib
import io
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import onepve_artifacts as artifacts


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = mock.patch.object(artifacts, 'CACHE_ROOT', self.root / 'cache')
        self.patch.start()

    def tearDown(self):
        self.patch.stop(); self.temp.cleanup()

    def node_tar(self, entries):
        archive = self.root / 'node.tar.xz'
        with tarfile.open(archive, 'w:xz') as stream:
            for name, data, kind in entries:
                item = tarfile.TarInfo(name); item.mode = 0o4777
                if kind == 'file':
                    item.size = len(data); stream.addfile(item, io.BytesIO(data))
                elif kind == 'symlink':
                    item.type = tarfile.SYMTYPE; item.linkname = data; stream.addfile(item)
                else:
                    item.type = tarfile.FIFOTYPE; stream.addfile(item)
        return archive

    def test_import_does_not_write_or_download(self):
        with mock.patch.object(Path, 'mkdir', side_effect=AssertionError('mkdir at import')), \
             mock.patch.object(artifacts.urllib.request, 'build_opener', side_effect=AssertionError('network at import')):
            importlib.reload(artifacts)
        artifacts.CACHE_ROOT = self.root / 'cache'
        self.assertFalse(artifacts.CACHE_ROOT.exists())

    def test_resolve_pins_immutable_debian_generic_build_without_cache(self):
        version = '20261026-2643'
        filename = 'debian-13-generic-amd64-' + version + '.qcow2'
        digest = 'a' * 128
        metadata = json.dumps({'items': [{'data': {'info': {'version': version}}}]})
        with mock.patch.object(artifacts, '_metadata', side_effect=[metadata, digest + '  ' + filename]) as get:
            result = artifacts.resolve_image()
        self.assertIn('/' + version + '/', result['url'])
        self.assertNotIn('/latest/', result['url'])
        self.assertEqual(result['sha512'], digest)
        self.assertEqual(get.call_args_list[1].args[0], artifacts.CLOUD_ROOT + version + '/SHA512SUMS')
        self.assertFalse(artifacts.CACHE_ROOT.exists())

    def test_resolve_rejects_build_path_injection(self):
        data = json.dumps({'items': [{'data': {'info': {'version': '../../outside'}}}]})
        with mock.patch.object(artifacts, '_metadata', return_value=data):
            with self.assertRaises(RuntimeError): artifacts.resolve_image()

    def test_ambiguous_checksum_is_rejected(self):
        with self.assertRaises(RuntimeError):
            artifacts._checksum(('a' * 64 + '  file\n') * 2, 'file', 'sha256')

    def test_download_commits_verified_bytes_and_reuses_without_network(self):
        data = b'verified bytes'; digest = hashlib.sha256(data).hexdigest()
        path = self.root / 'downloads/item'
        with mock.patch.object(artifacts, '_open_url', return_value=io.BytesIO(data)):
            self.assertEqual(artifacts.download_verified('https://example.test/item', path, digest), path)
        with mock.patch.object(artifacts, '_open_url', side_effect=AssertionError('unexpected download')):
            self.assertEqual(artifacts.download_verified('https://example.test/item', path, digest), path)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(list(path.parent.glob('*.part*')), [])

    def test_download_and_cache_progress_show_expected_and_actual_sizes(self):
        data = b'x' * 1024; path = self.root / 'downloads/item'
        response = io.BytesIO(data); response.headers = {'Content-Length': str(len(data))}
        with mock.patch.object(artifacts, '_open_url', return_value=response), \
             mock.patch.object(artifacts, 'progress') as report:
            artifacts.download_verified('https://example.test/item', path, hashlib.sha256(data).hexdigest())
        self.assertIn(mock.call('下载并校验文件：item', '预计大小：0.0 MiB (1024 字节)'), report.call_args_list)
        self.assertIn(mock.call('下载校验完成：item', '文件大小：0.0 MiB (1024 字节)', remaining=0), report.call_args_list)
        with mock.patch.object(artifacts, '_open_url', side_effect=AssertionError('network')), \
             mock.patch.object(artifacts, 'progress') as report:
            artifacts.download_verified('https://example.test/item', path, hashlib.sha256(data).hexdigest())
        report.assert_called_once_with('校验已缓存文件：item', '文件大小：0.0 MiB (1024 字节)')

    def test_unknown_download_size_still_reports_actual_size(self):
        data = b'valid'; path = self.root / 'item'
        with mock.patch.object(artifacts, '_open_url', return_value=io.BytesIO(data)), \
             mock.patch.object(artifacts, 'progress') as report:
            artifacts.download_verified('https://example.test/item', path, hashlib.sha256(data).hexdigest())
        self.assertIn(mock.call('下载并校验文件：item', '预计大小：未知'), report.call_args_list)
        self.assertIn(mock.call('下载校验完成：item', '文件大小：0.0 MiB (5 字节)', remaining=0), report.call_args_list)

    def test_download_eta_uses_actual_bytes_and_throttles_reports(self):
        block = b'x' * 1024**2
        data = block * 4
        response = io.BytesIO(data); response.headers = {'Content-Length': str(len(data))}
        with mock.patch.object(artifacts, '_open_url', return_value=response), \
             mock.patch.object(artifacts.time, 'monotonic', side_effect=[0, 1, 5, 6, 10, 11]), \
             mock.patch.object(artifacts, 'progress') as report:
            artifacts.download_verified('https://example.test/item', self.root/'item', hashlib.sha256(data).hexdigest())
        samples = [call for call in report.call_args_list if call.args[0] == '下载文件：item']
        self.assertEqual(len(samples), 3)
        self.assertIn('50.0%', samples[0].args[1])
        self.assertIn('409.6 KiB/s', samples[0].args[1])
        self.assertEqual(samples[0].kwargs['remaining'], 5)
        self.assertEqual(samples[-1].kwargs['remaining'], 0)
        verify = next(i for i, call in enumerate(report.call_args_list) if call.args[0] == '校验下载文件：item')
        self.assertFalse(report.call_args_list[verify].kwargs)
        self.assertEqual(report.call_args_list[-1].kwargs, {'remaining': 0})

    def test_unknown_length_and_bad_clock_never_invent_eta(self):
        cases = [(None, [0, 5, 6]), (None, [5, 5, 5]), ('5', [5, 5, 5]),
                 ('5', [5, 4, 3]), ('5', [0, float('nan'), float('nan')])]
        for index, (length, clock) in enumerate(cases):
            data = b'valid'; response = io.BytesIO(data); response.headers = {'Content-Length': length}
            path = self.root/('item-' + str(index))
            with self.subTest(length=length, clock=clock), mock.patch.object(artifacts, '_open_url', return_value=response), \
                 mock.patch.object(artifacts.time, 'monotonic', side_effect=clock), \
                 mock.patch.object(artifacts, 'progress') as report:
                artifacts.download_verified('https://example.test/item', path, hashlib.sha256(data).hexdigest())
            samples = [call for call in report.call_args_list if call.args[0].startswith('下载文件：')]
            self.assertTrue(samples)
            self.assertTrue(all(call.kwargs['remaining'] is None for call in samples))
            self.assertIn('总大小：未知' if length is None else '速度：未知', samples[-1].args[1])
            self.assertNotIn('nan', samples[-1].args[1])

    def test_interrupted_transfer_clears_eta_and_removes_partial(self):
        class BrokenTransfer(io.BytesIO):
            headers = {'Content-Length': str(2 * 1024**2)}

            def read(self, size):
                if self.tell():
                    raise OSError('connection lost')
                return super().read(size)

        response = BrokenTransfer(b'x' * 1024**2)
        path = self.root/'item'
        with mock.patch.object(artifacts, '_open_url', return_value=response), \
             mock.patch.object(artifacts.time, 'monotonic', side_effect=[0, 5]), \
             mock.patch.object(artifacts, 'progress') as report:
            with self.assertRaisesRegex(OSError, 'connection lost'):
                artifacts.download_verified('https://example.test/item', path, 'a' * 64)
        sample = next(call for call in report.call_args_list if call.args[0] == '下载文件：item')
        self.assertEqual(sample.kwargs['remaining'], 5)
        self.assertEqual(report.call_args_list[-1].args[0], '下载或校验失败：item')
        self.assertFalse(report.call_args_list[-1].kwargs)
        self.assertFalse(path.exists()); self.assertFalse(list(self.root.glob('*.part-*')))

    def test_failed_checksum_clears_download_estimate_without_success(self):
        data = b'wrong'; response = io.BytesIO(data); response.headers = {'Content-Length': str(len(data))}
        with mock.patch.object(artifacts, '_open_url', return_value=response), \
             mock.patch.object(artifacts.time, 'monotonic', side_effect=[0, 5, 6]), \
             mock.patch.object(artifacts, 'progress') as report:
            with self.assertRaisesRegex(RuntimeError, 'checksum differs'):
                artifacts.download_verified('https://example.test/item', self.root/'item', 'a'*64)
        self.assertEqual(report.call_args_list[-1].args[0], '下载或校验失败：item')
        self.assertFalse(report.call_args_list[-1].kwargs)
        self.assertFalse(any(call.args[0].startswith('下载校验完成') for call in report.call_args_list))

    def test_existing_bad_cache_is_not_overwritten(self):
        path = self.root / 'item'; path.write_bytes(b'old evidence')
        with mock.patch.object(artifacts, '_open_url') as network:
            with self.assertRaises(RuntimeError):
                artifacts.download_verified('https://example.test/item', path, 'a' * 64)
        self.assertEqual(path.read_bytes(), b'old evidence'); network.assert_not_called()

    def test_bad_download_does_not_publish_or_leave_its_part(self):
        path = self.root / 'downloads/item'
        with mock.patch.object(artifacts, '_open_url', return_value=io.BytesIO(b'bad')):
            with self.assertRaises(RuntimeError):
                artifacts.download_verified('https://example.test/item', path, 'a' * 64)
        self.assertFalse(path.exists()); self.assertEqual(list(path.parent.iterdir()), [])

    def test_invalid_hash_url_or_algorithm_rejects_before_writes(self):
        path = self.root / 'not-created/item'
        for url, digest, algorithm in [('http://example.test/item', 'a' * 64, 'sha256'),
                                        ('https://user:secret@example.test/item', 'a' * 64, 'sha256'),
                                        ('https://example.test/item', 'bad', 'sha256'),
                                        ('https://example.test/item', 'a' * 40, 'sha1')]:
            with self.assertRaises(RuntimeError): artifacts.download_verified(url, path, digest, algorithm)
        self.assertFalse(path.parent.exists())

    def test_racing_bad_destination_is_not_replaced(self):
        path = self.root / 'item'; data = b'valid'
        def open_url(_):
            path.write_bytes(b'concurrent foreign content')
            return io.BytesIO(data)
        with mock.patch.object(artifacts, '_open_url', side_effect=open_url):
            with self.assertRaises(RuntimeError):
                artifacts.download_verified('https://example.test/item', path, hashlib.sha256(data).hexdigest())
        self.assertEqual(path.read_bytes(), b'concurrent foreign content')

    def test_fnm_first_tls_download_is_recorded_then_tamper_rejected(self):
        artifacts._private_dir(artifacts.CACHE_ROOT)
        path = artifacts.CACHE_ROOT / 'downloads/fnm.zip'
        with mock.patch.object(artifacts, '_open_url', return_value=io.BytesIO(b'fixed vendor asset')):
            artifacts._fnm_download(path)
        record = json.loads((artifacts.CACHE_ROOT / 'fnm-download.json').read_text())
        self.assertEqual(record['url'], artifacts.FNM_URL)
        self.assertEqual(record['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        path.write_bytes(b'tampered')
        with mock.patch.object(artifacts, '_open_url') as network:
            with self.assertRaises(RuntimeError): artifacts._fnm_download(path)
        network.assert_not_called()

    def test_unrecorded_fnm_existing_file_is_not_trusted(self):
        artifacts._private_dir(artifacts.CACHE_ROOT)
        path = artifacts.CACHE_ROOT / 'fnm.zip'; path.write_bytes(b'unknown')
        with self.assertRaises(RuntimeError): artifacts._fnm_download(path)

    def test_fnm_requires_linux_x64_binary(self):
        path = self.root / 'fnm.zip'
        with zipfile.ZipFile(path, 'w') as stream: stream.writestr('fnm', b'html error body')
        with self.assertRaises(RuntimeError): artifacts._validate_fnm(path)

    def test_node_tar_normalizes_modes_and_ignores_archive_uid(self):
        archive = self.node_tar([(artifacts.NODE_NAME + '/bin/node', b'node', 'file')])
        destination = self.root / 'node'
        artifacts._extract_node(archive, destination)
        path = destination / artifacts.NODE_NAME / 'bin/node'
        self.assertEqual(path.read_bytes(), b'node')
        if os.name == 'posix': self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_node_tar_rejects_traversal_before_extracting(self):
        archive = self.node_tar([(artifacts.NODE_NAME + '/../outside', b'bad', 'file')])
        destination = self.root / 'node'
        with self.assertRaises(RuntimeError): artifacts._extract_node(archive, destination)
        self.assertFalse(destination.exists()); self.assertFalse((self.root / 'outside').exists())

    def test_node_tar_rejects_external_symlink_and_symlink_ancestor_writes(self):
        cases = [[(artifacts.NODE_NAME + '/link', '/etc', 'symlink')],
                 [(artifacts.NODE_NAME + '/link', 'bin', 'symlink'),
                  (artifacts.NODE_NAME + '/link/node', b'bad', 'file')],
                 [(artifacts.NODE_NAME + '/pipe', '', 'fifo')]]
        for entries in cases:
            with self.assertRaises(RuntimeError): artifacts._extract_node(self.node_tar(entries), self.root / 'node')
            self.assertFalse((self.root / 'node').exists())

    def test_tree_manifest_detects_changed_and_unexpected_files(self):
        root = self.root / 'runtime'; root.mkdir(); (root / 'file').write_bytes(b'original')
        manifest = artifacts._tree(root)
        artifacts._verify_tree(root, manifest, 'manifest.json')
        (root / 'file').write_bytes(b'changed')
        with self.assertRaises(RuntimeError): artifacts._verify_tree(root, manifest, 'manifest.json')
        (root / 'file').write_bytes(b'original'); (root / 'extra').write_bytes(b'extra')
        with self.assertRaises(RuntimeError): artifacts._verify_tree(root, manifest, 'manifest.json')

    def test_symlink_manifest_and_redirected_cache_parent(self):
        root = self.root / 'tree'; root.mkdir(); (root / 'file').write_bytes(b'file')
        try: (root / 'internal').symlink_to('file')
        except OSError: self.skipTest('Symlinks unavailable on this test host')
        manifest = artifacts._tree(root)
        self.assertEqual(manifest['symlinks'], {'internal': 'file'})
        artifacts._verify_tree(root, manifest, 'manifest.json')
        outside = self.root / 'outside'; outside.mkdir(); (outside / 'item').write_bytes(b'file')
        redirect = self.root / 'redirect'; redirect.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            artifacts.download_verified('https://example.test/item', redirect / 'item', hashlib.sha256(b'file').hexdigest())
        (root / 'external').symlink_to(outside / 'item')
        with self.assertRaises(RuntimeError): artifacts._tree(root)

    def test_prepare_wires_all_inputs_without_any_real_host_install(self):
        image = dict(version='20261026-2643', filename='debian-13-generic-amd64-20261026-2643.qcow2',
                     url='https://example.test/image', sha512='a' * 128)
        source = dict(version='2025.12.10.2', url='https://example.test/source', sha256='b' * 64)
        def download(url, path, expected, algorithm='sha256'):
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'fixture'); return path
        with mock.patch.object(artifacts.platform, 'system', return_value='Linux'), \
             mock.patch.object(artifacts.platform, 'machine', return_value='x86_64'), \
             mock.patch.object(artifacts, 'resolve_image', return_value=image), \
             mock.patch.object(artifacts, 'driver_spec', return_value=source), \
             mock.patch.object(artifacts, 'download_verified', side_effect=download) as get, \
             mock.patch.object(artifacts, '_runtime_cache', return_value=artifacts.CACHE_ROOT / 'runtime-cache'), \
             mock.patch.object(artifacts, 'run', side_effect=AssertionError('host command')):
            result = artifacts.prepare_artifacts()
        self.assertEqual(len(get.call_args_list), 2)
        self.assertEqual(result['image']['path'], result['image_path'])
        self.assertTrue(Path(result['driver_sources']['2025.12.10.2']).is_file())

    def test_full_runtime_cache_build_uses_only_cached_node_and_no_scripts(self):
        artifacts._private_dir(artifacts.CACHE_ROOT)
        node_archive = self.node_tar([(artifacts.NODE_NAME + '/bin/node', b'node fixture', 'file'),
                                     (artifacts.NODE_NAME + '/lib/node_modules/npm/bin/npm-cli.js', b'npm fixture', 'file')])
        node_bytes = node_archive.read_bytes()
        fnm = io.BytesIO()
        header = b'\x7fELF\x02\x01' + b'\0' * 12 + b'\x3e\0'
        with zipfile.ZipFile(fnm, 'w') as stream: stream.writestr('fnm', header)
        def open_url(url):
            return io.BytesIO(fnm.getvalue() if url == artifacts.FNM_URL else node_bytes)
        def npm_install(argv, **kwargs):
            self.assertIn('--ignore-scripts', argv)
            self.assertIn('--registry=https://registry.npmjs.org', argv)
            environment = kwargs['env']
            self.assertNotIn('NODE_OPTIONS', environment)
            self.assertEqual(environment['NPM_CONFIG_USERCONFIG'], os.devnull)
            self.assertNotEqual(environment['NPM_CONFIG_GLOBALCONFIG'], os.devnull)
            self.assertTrue(Path(environment['HOME']).is_relative_to(artifacts.CACHE_ROOT))
            self.assertTrue(Path(argv[0]).is_relative_to(artifacts.CACHE_ROOT / 'tool-node'))
            prefix = Path(argv[argv.index('--prefix') + 1]); prefix.mkdir()
            (prefix / 'package.json').write_text(json.dumps({'dependencies': {'@playwright/mcp': artifacts.MCP_VERSION,
                                                                          '@openai/codex': artifacts.CODEX_VERSION}}))
            (prefix / 'package-lock.json').write_text('{}')
            for name in ['node_modules/@playwright/mcp/cli.js', 'node_modules/@openai/codex/bin/codex.js']:
                path = prefix / name; path.parent.mkdir(parents=True); path.write_text('fixture')
        sums = hashlib.sha256(node_bytes).hexdigest() + '  ' + artifacts.NODE_NAME + '.tar.xz'
        with mock.patch.object(artifacts, '_metadata', return_value=sums), \
             mock.patch.object(artifacts, '_open_url', side_effect=open_url), \
             mock.patch.object(artifacts, 'run', side_effect=npm_install) as npm, \
             mock.patch.dict(os.environ, {'NODE_OPTIONS': '--require untrusted.js', 'npm_config_registry': 'https://invalid.test'}):
            result = artifacts._runtime_cache()
        self.assertEqual(npm.call_count, 1)
        record = json.loads((result / 'runtime-manifest.json').read_text())
        self.assertEqual(record['node_version'], artifacts.NODE_VERSION)
        artifacts._verify_tree(result, record, 'runtime-manifest.json')
        with mock.patch.object(artifacts, '_open_url', side_effect=AssertionError('network')), \
             mock.patch.object(artifacts, 'run', side_effect=AssertionError('host command')):
            self.assertEqual(artifacts._runtime_cache(), result)
            (result / 'npm/node_modules/@playwright/mcp/cli.js').write_text('tampered')
            with self.assertRaises(RuntimeError): artifacts._runtime_cache()

    def test_platform_rejection_does_not_create_cache(self):
        with mock.patch.object(artifacts.platform, 'system', return_value='Windows'):
            with self.assertRaises(RuntimeError): artifacts.prepare_artifacts()
        self.assertFalse(artifacts.CACHE_ROOT.exists())


if __name__ == '__main__':
    unittest.main()

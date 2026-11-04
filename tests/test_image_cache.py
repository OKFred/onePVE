import contextlib
import hashlib
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import onepve_artifacts as artifacts


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'cache'
        self.patch = mock.patch.object(artifacts, 'CACHE_ROOT', self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def image(self, version='20261103-2651', data=b'latest image'):
        filename = 'debian-13-generic-amd64-' + version + '.qcow2'
        base = artifacts.CLOUD_ROOT + version + '/'
        return dict(version=version, filename=filename, url=base + filename,
                    sha512=hashlib.sha512(data).hexdigest(),
                    metadata_url=artifacts.CLOUD_ROOT + 'latest/debian-13-generic-amd64.json',
                    checksum_url=base + 'SHA512SUMS')

    def cache(self, image, data, record='sidecar'):
        path = self.root / 'images' / image['filename']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        entry = dict(image, path=str(path), size_bytes=len(data))
        if record == 'sidecar':
            path.with_suffix('.qcow2.json').write_text(json.dumps(entry), encoding='utf-8')
        elif record == 'prepared':
            (self.root / 'prepared.json').write_text(json.dumps({'image': entry}), encoding='utf-8')
        return path

    def prepare(self, answer):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'stdin', TerminalInput(answer)), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = artifacts.prepare_image()
        self.assertEqual(stdout.getvalue(), '')
        return result, stderr.getvalue()

    def test_online_options_without_cache_are_read_only(self):
        latest = self.image()
        with mock.patch.object(artifacts, 'resolve_image', return_value=latest):
            result = artifacts.image_options()
        self.assertEqual(result['latest'], latest)
        self.assertEqual(result['cached'], [])
        self.assertFalse(self.root.exists())

    def test_offline_options_reuse_legacy_prepared_record_without_writes(self):
        data = b'old verified image'
        image = self.image('20261026-2643', data)
        path = self.cache(image, data, record='prepared')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with mock.patch.object(artifacts, 'resolve_image', side_effect=OSError('latest unavailable')), \
             mock.patch.object(artifacts, '_metadata', side_effect=AssertionError('unnecessary lookup')):
            result = artifacts.image_options()
        self.assertIsNone(result['latest'])
        self.assertIn('latest unavailable', result['lookup_error'])
        self.assertEqual([(x['path'], x['size_bytes']) for x in result['cached']], [(str(path), len(data))])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_bad_sidecar_still_checks_valid_legacy_prepared_record(self):
        data = b'old verified image'
        image = self.image('20261026-2643', data)
        path = self.cache(image, data, record='prepared')
        for broken in ('[]', 'null', '{"image": []}', '{"image": null}', '{"sha512":'):
            with self.subTest(record=broken):
                path.with_suffix('.qcow2.json').write_text(broken, encoding='utf-8')
                with mock.patch.object(artifacts, 'resolve_image', side_effect=OSError('offline')), \
                     mock.patch.object(artifacts, '_metadata', side_effect=AssertionError('unnecessary lookup')):
                    result = artifacts.image_options()
                self.assertEqual([x['path'] for x in result['cached']], [str(path)])
                self.assertTrue(result['cache_errors'])

    def test_unrecorded_image_can_be_verified_with_its_immutable_official_checksum(self):
        data = b'completed earlier download'
        image = self.image('20261026-2643', data)
        path = self.cache(image, data, record=None)
        with mock.patch.object(artifacts, 'resolve_image', return_value=self.image()), \
             mock.patch.object(artifacts, '_metadata', return_value=image['sha512'] + '  ' + image['filename']) as get:
            result = artifacts.image_options()
        self.assertEqual(result['cached'][0]['path'], str(path))
        self.assertTrue(all(call.args[0] == image['checksum_url'] for call in get.call_args_list))
        self.assertFalse(path.with_suffix('.qcow2.json').exists())

    def test_offline_unrecorded_file_is_not_trusted_and_not_modified(self):
        data = b'unknown local bytes'
        image = self.image('20261026-2643', data)
        path = self.cache(image, data, record=None)
        with mock.patch.object(artifacts, 'resolve_image', side_effect=OSError('offline')), \
             mock.patch.object(artifacts, '_metadata', side_effect=OSError('offline')):
            with self.assertRaises(RuntimeError):
                artifacts.image_options()
        self.assertEqual(path.read_bytes(), data)
        self.assertFalse(path.with_suffix('.qcow2.json').exists())

    def test_bad_cache_is_reported_while_healthy_cache_still_available(self):
        bad = self.image('20261025-2642', b'expected')
        path = self.cache(bad, b'changed')
        good = self.image('20261026-2643', b'healthy')
        self.cache(good, b'healthy')
        with mock.patch.object(artifacts, 'resolve_image', side_effect=OSError('offline')), \
             mock.patch.object(artifacts, '_metadata', side_effect=OSError('offline')):
            result = artifacts.image_options()
        self.assertEqual([item['filename'] for item in result['cached']], [good['filename']])
        self.assertTrue(result['cache_errors'])
        self.assertEqual(path.read_bytes(), b'changed')

    def test_cache_record_cannot_supply_an_unofficial_download_url(self):
        image = self.image('20261025-2642', b'old')
        self.cache(dict(image, url='https://untrusted.test/image.qcow2'), b'old')
        good = self.image('20261026-2643', b'healthy')
        self.cache(good, b'healthy')
        with mock.patch.object(artifacts, 'resolve_image', side_effect=OSError('offline')), \
             mock.patch.object(artifacts, '_metadata', side_effect=OSError('offline')):
            result = artifacts.image_options()
        self.assertEqual([item['filename'] for item in result['cached']], [good['filename']])
        self.assertTrue(result['cache_errors'])

    def test_symlink_image_is_not_a_valid_cache(self):
        image = self.image()
        path = self.root / 'images' / image['filename']
        path.parent.mkdir(parents=True)
        outside = Path(self.temp.name) / 'outside.qcow2'
        outside.write_bytes(b'latest image')
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest('Symlinks unavailable on this test host')
        with mock.patch.object(artifacts, 'resolve_image', side_effect=OSError('offline')), \
             mock.patch.object(artifacts, '_metadata', side_effect=OSError('offline')):
            with self.assertRaises(RuntimeError):
                artifacts.image_options()
        self.assertEqual(outside.read_bytes(), b'latest image')

    def test_latest_cache_reuses_without_input_or_image_download(self):
        latest = self.image()
        path = self.cache(latest, b'latest image')
        with mock.patch.object(artifacts, 'resolve_image', return_value=latest), \
             mock.patch.object(sys, 'stdin') as stdin, \
             mock.patch.object(artifacts, '_open_url', side_effect=AssertionError('image redownload')):
            result = artifacts.prepare_image()
        self.assertEqual(result['path'], str(path))
        self.assertEqual(result['size_bytes'], len(b'latest image'))
        stdin.readline.assert_not_called()

    def test_older_cache_can_be_selected_with_version_size_and_path_on_stderr(self):
        data = b'older verified image'
        old = self.image('20261026-2643', data)
        path = self.cache(old, data)
        with mock.patch.object(artifacts, 'resolve_image', return_value=self.image()), \
             mock.patch.object(artifacts, '_open_url', side_effect=AssertionError('image redownload')):
            result, output = self.prepare('1\n')
        self.assertEqual(result['path'], str(path))
        self.assertIn(old['version'], output)
        self.assertIn(str(path), output)
        self.assertIn(str(len(data)), output)
        self.assertIn('MiB', output)

    def test_rejecting_older_image_downloads_latest_and_records_it_immediately(self):
        old = self.image('20261026-2643', b'old')
        old_path = self.cache(old, b'old')
        latest = self.image()
        with mock.patch.object(artifacts, 'resolve_image', return_value=latest), \
             mock.patch.object(artifacts, '_open_url', return_value=io.BytesIO(b'latest image')):
            result, _ = self.prepare('\n')
        path = Path(result['path'])
        self.assertEqual(result['version'], latest['version'])
        self.assertEqual(path.read_bytes(), b'latest image')
        self.assertEqual(old_path.read_bytes(), b'old')
        record = json.loads(path.with_suffix('.qcow2.json').read_text(encoding='utf-8'))
        self.assertEqual(record['sha512'], latest['sha512'])
        self.assertFalse((self.root / 'prepared.json').exists())
        with mock.patch.object(artifacts, 'resolve_image', side_effect=OSError('offline')), \
             mock.patch.object(artifacts, '_metadata', side_effect=OSError('offline')):
            offline = artifacts.image_options()
        self.assertIn(latest['filename'], [x['filename'] for x in offline['cached']])

    def test_latest_lookup_failure_still_prompts_to_reuse_local_image(self):
        data = b'old verified image'
        old = self.image('20261026-2643', data)
        path = self.cache(old, data)
        with mock.patch.object(artifacts, 'resolve_image', side_effect=OSError('latest unavailable')), \
             mock.patch.object(artifacts, '_open_url', side_effect=AssertionError('image redownload')):
            result, output = self.prepare('1\n')
        self.assertEqual(result['path'], str(path))
        self.assertIn('latest unavailable', output)

    def test_truncated_latest_response_still_offers_verified_local_image(self):
        image = self.image('20261026-2643', b'old')
        path = self.cache(image, b'old')
        with mock.patch.object(artifacts, 'resolve_image', side_effect=IncompleteRead(b'partial', 100)), \
             mock.patch.object(artifacts, '_open_url', side_effect=AssertionError('image redownload')):
            result, output = self.prepare('1\n')
        self.assertEqual(result['path'], str(path))
        self.assertIn('IncompleteRead', output)

    def test_offline_blank_or_eof_cancels_instead_of_silently_selecting_cache(self):
        old = self.image('20261026-2643', b'old')
        path = self.cache(old, b'old')
        for answer in ('\n', ''):
            with self.subTest(answer=answer), \
                 mock.patch.object(artifacts, 'resolve_image', side_effect=OSError('offline')), \
                 mock.patch.object(sys, 'stdin', TerminalInput(answer)), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    artifacts.prepare_image()
        self.assertEqual(path.read_bytes(), b'old')

    def test_latest_download_failure_offers_local_image_before_aborting(self):
        old = self.image('20261026-2643', b'old')
        path = self.cache(old, b'old')
        latest = self.image()
        with mock.patch.object(artifacts, 'resolve_image', return_value=latest), \
             mock.patch.object(artifacts, '_open_url', side_effect=OSError('download unavailable')):
            result, output = self.prepare('\n1\n')
        self.assertEqual(result['path'], str(path))
        self.assertIn('download unavailable', output)
        self.assertFalse((path.parent / latest['filename']).exists())
        self.assertFalse(list(path.parent.glob('.*.part-*')))

    def test_truncated_latest_download_still_offers_verified_local_image(self):
        old = self.image('20261026-2643', b'old')
        path = self.cache(old, b'old')
        latest = self.image()
        with mock.patch.object(artifacts, 'resolve_image', return_value=latest), \
             mock.patch.object(artifacts, '_open_url', side_effect=IncompleteRead(b'partial', 100)):
            result, output = self.prepare('\n1\n')
        self.assertEqual(result['path'], str(path))
        self.assertIn('IncompleteRead', output)
        self.assertFalse((path.parent / latest['filename']).exists())
        self.assertFalse(list(path.parent.glob('.*.part-*')))


if __name__ == '__main__':
    unittest.main()

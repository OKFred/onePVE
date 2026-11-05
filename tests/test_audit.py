import contextlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import onepve_audit as audit

IP = '192.168.11.144'
BOOT = '450445aa-3f2d-4130-999a-a75ec58b694f'
LINE = ('[NEW] ipv4 2 tcp 6 120 SYN_SENT src=192.168.11.144 dst=1.1.1.1 '
        'sport=52525 dport=443 packets=1 bytes=60 src=1.1.1.1 dst=192.168.11.144 '
        'sport=443 dport=52525 packets=0 bytes=0 [UNREPLIED] mark=0 zone=1 use=1 id=1234')


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.units = self.root / 'units'; self.units.mkdir()
        self.state = dict(vmid=103, owner='a' * 32, config=dict(expected_ip=IP, mac='BC:24:11:00:00:01'))
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.object(audit, 'AUDIT_ROOT', self.root / 'audit'))
        self.stack.enter_context(mock.patch.object(audit, 'UNIT_ROOT', self.units))
        self.stack.enter_context(mock.patch.object(audit, 'require_root'))
        self.commands = self.stack.enter_context(mock.patch.object(audit, 'command', return_value=types.SimpleNamespace(returncode=0)))
        self.stack.enter_context(mock.patch.object(audit.shutil, 'which', return_value='/usr/sbin/conntrack'))

    def tearDown(self):
        self.stack.close(); self.temp.cleanup()

    def enabled(self):
        return audit.configure(self.state, True)

    def db(self):
        self.enabled()
        return audit._database(audit._paths(103)[2], True)

    def test_disabled_default_has_no_writes_or_dependencies(self):
        with mock.patch.object(audit.shutil, 'which', side_effect=AssertionError('No dependency probe')):
            result = audit.configure(self.state, False)
        self.assertFalse(result['enabled']); self.assertFalse(audit.AUDIT_ROOT.exists())
        self.commands.assert_not_called()

    def test_enable_private_units_and_disable_keeps_expiry_and_data(self):
        result = self.enabled()
        self.assertTrue(result['enabled']); self.assertFalse(result['blocked_attempts_recorded'])
        self.assertEqual(result['retention_hours'], 72)
        cfg = json.loads(audit._paths(103)[1].read_text())
        self.assertEqual(len(cfg['unit_sha256']), 3)
        self.assertIn('OnCalendar=minutely', (self.units / 'onepve-audit-prune-103.timer').read_text())
        self.assertIn('ProtectHome=read-only', (self.units / 'onepve-audit-103.service').read_text())
        self.assertIn('Environment=PYTHONDONTWRITEBYTECODE=1', (self.units / 'onepve-audit-103.service').read_text())
        self.assertIn(audit._quote(audit._paths(103)[0] / 'onepve_audit.py'), (self.units / 'onepve-audit-103.service').read_text())
        with contextlib.closing(audit._database(audit._paths(103)[2], True)) as db:
            audit._observe(db, audit.parse_flow(LINE, IP, BOOT)); db.commit()
        audit.configure(self.state, False)
        self.assertEqual(audit.status(self.state)['flow_count'], 1)
        self.assertTrue((self.units / 'onepve-audit-prune-103.timer').exists())
        self.assertFalse(json.loads(audit._paths(103)[1].read_text())['enabled'])
        audit.configure(self.state, True)
        if os.name == 'posix':
            self.assertEqual(audit._paths(103)[0].stat().st_mode & 0o777, 0o700)
            for path in audit._paths(103)[0].iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_dependency_does_not_install_or_create(self):
        with mock.patch.object(audit.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'install it explicitly'):
                self.enabled()
        self.assertFalse(audit.AUDIT_ROOT.exists()); self.commands.assert_not_called()

    def test_invalid_identity_or_changed_owner_is_rejected(self):
        for change in [dict(vmid='103'), dict(owner='../x'), dict(expected_ip='::1'), dict(mac='FF:00:00:00:00:01')]:
            with self.subTest(change=change), self.assertRaises((RuntimeError, ValueError)):
                audit.configure(dict(self.state, **change), True)
        self.enabled()
        with self.assertRaisesRegex(RuntimeError, 'another operation'):
            audit.configure(dict(self.state, owner='b' * 32), True)
        with self.assertRaisesRegex(RuntimeError, 'changing the target'):
            audit.configure(dict(self.state, expected_ip='192.168.11.145'), True)

    def test_foreign_unit_and_modified_owned_unit_are_preserved(self):
        path = self.units / 'onepve-audit-103.service'; path.write_text('foreign')
        with self.assertRaisesRegex(RuntimeError, 'Unowned'):
            self.enabled()
        self.assertEqual(path.read_text(), 'foreign')
        path.unlink(); self.enabled(); path.write_text('externally changed')
        with self.assertRaisesRegex(RuntimeError, 'modified externally'):
            audit.remove(self.state)
        self.assertEqual(path.read_text(), 'externally changed')

    def test_source_copies_are_owned_and_only_explicit_configure_updates(self):
        self.enabled()
        path = audit._paths(103)[0] / 'onepve_audit.py'
        original = path.read_bytes()
        path.write_bytes(b'foreign change')
        with self.assertRaisesRegex(RuntimeError, 'source was modified externally'):
            audit.status(self.state)
        with self.assertRaisesRegex(RuntimeError, 'source was modified externally'):
            self.enabled()
        self.assertEqual(path.read_bytes(), b'foreign change')
        path.write_bytes(original)
        audit.status(self.state)
        audit.configure(self.state, True)
        self.assertEqual(path.read_bytes(), original)
        self.assertIn('source_sha256', json.loads(audit._paths(103)[1].read_text()))

    def test_private_cli_does_not_create_bytecode_and_remains_removable(self):
        self.enabled()
        directory = audit._paths(103)[0]
        environment = dict(os.environ)
        for name in ['PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX']:
            environment.pop(name, None)
        result = subprocess.run([sys.executable, str(directory / 'onepve_audit.py'), '--help'],
                                env=environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((directory / '__pycache__').exists())
        self.assertTrue(audit.remove(self.state)['removed'])

    def test_status_detects_current_stopped_or_stale_collection_without_writes(self):
        with contextlib.closing(self.db()) as db:
            audit._gap(db, 'old_snapshot_gap')
            audit._meta(db, 'heartbeat_utc', audit.utc(datetime.now(timezone.utc) - timedelta(minutes=6)))
            db.commit()
        path = audit._paths(103)[2]
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.commands.reset_mock()
        result = audit.status(self.state)
        self.assertTrue(result['active'])
        self.assertEqual(result['coverage_gap']['reason'], 'collector_heartbeat_stale')
        self.assertEqual(result['gap']['reason'], 'old_snapshot_gap')
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(self.commands.call_args_list, [mock.call(['systemctl', 'is-active', '--quiet', 'onepve-audit-103.service'], check=False)])
        self.commands.return_value = types.SimpleNamespace(returncode=3)
        self.assertEqual(audit.status(self.state)['coverage_gap']['reason'], 'collector_stopped')
        audit.configure(self.state, False)
        self.assertIsNone(audit.status(self.state)['coverage_gap'])

    def test_status_requires_recent_valid_heartbeat_for_enabled_active_collector(self):
        self.enabled()
        self.assertEqual(audit.status(self.state)['coverage_gap']['reason'], 'collector_heartbeat_stale')
        with contextlib.closing(audit._database(audit._paths(103)[2], True)) as db:
            for value in ['bad-date', '2026-11-06T00:00:00', audit.utc(datetime.now(timezone.utc) - timedelta(minutes=6))]:
                audit._meta(db, 'heartbeat_utc', value); db.commit()
                self.assertEqual(audit.status(self.state)['coverage_gap']['reason'], 'collector_heartbeat_stale')
            audit._meta(db, 'heartbeat_utc', audit.utc()); db.commit()
            self.assertIsNone(audit.status(self.state)['coverage_gap'])

    def test_configure_retries_every_source_unit_and_config_publication_boundary(self):
        publish = audit._publish
        save = audit.atomic_json
        for index, (boundary, after) in enumerate((number, after) for number in range(1, 7) for after in [False, True]):
            self.state['vmid'] = 200 + index
            count = 0
            def publishing(target, data, scope, pending):
                nonlocal count
                count += 1
                if count == boundary and not after: raise OSError('publication interrupted')
                result = publish(target, data, scope, pending)
                if count == boundary and after: raise OSError('publication interrupted')
                return result
            def saving(path, value, *args, **kwargs):
                if Path(path).name != 'config.json': return save(path, value, *args, **kwargs)
                if boundary == 6 and not after: raise OSError('publication interrupted')
                result = save(path, value, *args, **kwargs)
                if boundary == 6 and after: raise OSError('publication interrupted')
                return result
            with self.subTest(boundary=boundary, after=after), mock.patch.object(audit, '_publish', publishing), \
                 mock.patch.object(audit, 'atomic_json', saving):
                with self.assertRaisesRegex(OSError, 'publication interrupted'): self.enabled()
            result = audit.status(self.state)
            self.assertEqual(result['resource_health'], 'unknown')
            self.assertEqual(result['coverage_gap']['reason'], 'audit_resources_incomplete')
            self.enabled()
            self.assertEqual(audit.status(self.state)['resource_health'], 'ready')
            self.assertFalse((audit._paths(self.state['vmid'])[0] / 'pending.json').exists())
            audit.remove(self.state)

    def test_source_upgrade_interrupted_after_replacement_uses_recorded_old_and_new_hashes(self):
        self.enabled()
        original_cfg = json.loads(audit._paths(103)[1].read_text())
        source = Path(audit.__file__)
        read_bytes, publish = Path.read_bytes, audit._publish
        upgraded = source.read_bytes() + b'\n# Test a fixed-source upgrade.\n'
        def reading(path): return upgraded if path == source else read_bytes(path)
        def publishing(target, data, scope, pending):
            result = publish(target, data, scope, pending)
            if target.name == 'onepve_audit.py': raise OSError('upgrade interrupted')
            return result
        with mock.patch.object(Path, 'read_bytes', reading):
            with mock.patch.object(audit, '_publish', publishing):
                with self.assertRaisesRegex(OSError, 'upgrade interrupted'): self.enabled()
            self.assertEqual(json.loads(audit._paths(103)[1].read_text()), original_cfg)
            self.assertEqual((audit._paths(103)[0] / 'onepve_audit.py').read_bytes(), upgraded)
            self.enabled()
        self.assertEqual(audit.status(self.state)['resource_health'], 'ready')
        self.assertNotEqual(json.loads(audit._paths(103)[1].read_text())['source_sha256'], original_cfg['source_sha256'])

    def test_recorded_interrupted_temporaries_retry_but_foreign_files_are_preserved(self):
        publish, unlink, replace = audit._publish, Path.unlink, audit.os.replace
        for index, scope in enumerate(['source', 'unit']):
            self.state['vmid'] = 240 + index
            failed = False
            def replacing(src, dst):
                nonlocal failed
                match = Path(dst).parent == (audit._paths(self.state['vmid'])[0] if scope == 'source' else self.units)
                if match and Path(src).name.endswith('.tmp') and not failed:
                    failed = True; raise OSError('process interrupted')
                return replace(src, dst)
            def deleting(path, *args, **kwargs):
                if path.name.endswith('.tmp') and failed: raise OSError('process interrupted')
                return unlink(path, *args, **kwargs)
            with mock.patch.object(audit.os, 'replace', replacing), mock.patch.object(Path, 'unlink', deleting):
                with self.assertRaisesRegex(OSError, 'process interrupted'): self.enabled()
            pending = json.loads((audit._paths(self.state['vmid'])[0] / 'pending.json').read_text())
            leftovers = [audit._temporary_path(pending, record) for record in pending['temps'] if audit._temporary_path(pending, record).exists()]
            self.assertEqual(len(leftovers), 1)
            leftovers[0].write_bytes(leftovers[0].read_bytes()[:10])
            foreign = leftovers[0].with_name('.foreign-partial'); foreign.write_bytes(b'keep')
            self.enabled()
            self.assertFalse(leftovers[0].exists()); self.assertEqual(foreign.read_bytes(), b'keep')
            foreign.unlink(); audit.remove(self.state)

    def test_remove_retries_each_owned_delete_boundary_and_empty_tombstone(self):
        unlink, rmdir = Path.unlink, Path.rmdir
        targets = ['unit0', 'unit1', 'unit2', 'onepve_audit.py', 'onepve_common.py', 'flows.sqlite3', 'config.json', 'pending.json', 'directory']
        for index, (name, after) in enumerate((name, after) for name in targets for after in [False, True]):
            self.state['vmid'] = 260 + index
            self.enabled()
            directory, _, database = audit._paths(self.state['vmid'])
            with contextlib.closing(audit._database(database, True)): pass
            cfg = json.loads((directory / 'config.json').read_text())
            target = self.units / list(cfg['unit_sha256'])[int(name[-1])] if name.startswith('unit') else directory / name
            if name == 'directory': target = directory
            failed = False
            def deleting(path, *args, **kwargs):
                nonlocal failed
                if path == target and not failed:
                    failed = True
                    if after: unlink(path, *args, **kwargs)
                    raise OSError('delete interrupted')
                return unlink(path, *args, **kwargs)
            def removing(path, *args, **kwargs):
                nonlocal failed
                if path == target and not failed:
                    failed = True
                    if after: rmdir(path, *args, **kwargs)
                    raise OSError('delete interrupted')
                return rmdir(path, *args, **kwargs)
            with self.subTest(name=name, after=after), mock.patch.object(Path, 'unlink', deleting), mock.patch.object(Path, 'rmdir', removing):
                with self.assertRaisesRegex(OSError, 'delete interrupted'): audit.remove(self.state)
            audit.remove(self.state)
            self.assertFalse(directory.exists())
            self.assertFalse(any((self.units / unit).exists() for unit in cfg['unit_sha256']))

    def test_missing_owned_resources_are_unknown_and_repairable_but_changed_hash_is_rejected(self):
        self.enabled()
        cfg = json.loads(audit._paths(103)[1].read_text())
        missing = self.units / next(iter(cfg['unit_sha256'])); missing.unlink()
        self.assertEqual(audit.status(self.state)['resource_health'], 'unknown')
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            audit._load(self.state)
        self.enabled(); self.assertTrue(missing.exists())
        pending = audit._intent(cfg, None, 'configure', cfg); audit._save_pending(pending)
        source = audit._paths(103)[0] / 'onepve_common.py'; source.write_bytes(b'changed externally')
        for operation in [lambda: self.enabled(), lambda: audit.remove(self.state)]:
            with self.assertRaisesRegex(RuntimeError, 'modified externally'): operation()
        self.assertEqual(source.read_bytes(), b'changed externally')

    def test_remove_only_owned_resources_and_refuses_unknown_files(self):
        self.enabled()
        foreign = audit._paths(103)[0] / 'notes'; foreign.write_text('keep')
        with self.assertRaisesRegex(RuntimeError, 'Unexpected file'):
            audit.remove(self.state)
        foreign.unlink()
        other = self.units / 'another.service'; other.write_text('other')
        self.assertTrue(audit.remove(self.state)['removed'])
        self.assertFalse(audit._paths(103)[0].exists()); self.assertEqual(other.read_text(), 'other')
        self.assertFalse(audit.remove(self.state)['removed'])

    def test_parser_tcp_udp_nat_direction_and_nullable_accounting(self):
        flow = audit.parse_flow(LINE, IP, BOOT)
        self.assertEqual(flow['direction'], 'outbound'); self.assertEqual(flow['original_bytes'], 60)
        self.assertEqual(flow['event'], 'NEW'); self.assertEqual(flow['tcp_state'], 'SYN_SENT')
        udp = LINE.replace('tcp 6 120 SYN_SENT', 'udp 17 29').replace(' packets=1 bytes=60', '').replace(' packets=0 bytes=0', '')
        flow = audit.parse_flow(udp, IP, BOOT)
        self.assertEqual(flow['protocol'], 'udp'); self.assertIsNone(flow['reply_packets'])
        incoming = ('tcp 6 100 ESTABLISHED src=8.8.8.8 dst=203.0.113.1 sport=1234 dport=57722 '
                    'src=' + IP + ' dst=8.8.8.8 sport=22 dport=1234 id=9')
        self.assertEqual(audit.parse_flow(incoming, IP, BOOT)['direction'], 'inbound')

    def test_parser_rejects_incomplete_or_bad_metadata_and_ignores_other_vm(self):
        self.assertIsNone(audit.parse_flow(LINE.replace(IP, '192.168.11.222'), IP, BOOT))
        for line in [LINE.replace(' id=1234', ''), LINE.replace('sport=52525', 'sport=99999'),
                     LINE.replace('bytes=60', 'bytes=-1'), 'tcp connection payload=secret']:
            with self.subTest(line=line), self.assertRaises((RuntimeError, ValueError)):
                audit.parse_flow(line, IP, BOOT)

    def test_flow_dedup_long_snapshot_end_and_id_reuse(self):
        with contextlib.closing(self.db()) as db:
            first = datetime.now(timezone.utc)
            audit._observe(db, audit.parse_flow(LINE, IP, BOOT, audit.utc(first)))
            audit._observe(db, audit.parse_flow(LINE.replace('[NEW]', '[UPDATE]'), IP, BOOT, audit.utc(first + timedelta(seconds=1))))
            later = audit.utc(first + timedelta(seconds=120))
            audit._observe(db, audit.parse_flow(LINE.replace('[NEW] ', ''), IP, BOOT, later))
            row = db.execute('SELECT * FROM flows').fetchone()
            self.assertEqual(row['last_seen_utc'], later); self.assertEqual(row['started_utc'], audit.utc(first))
            audit._observe(db, audit.parse_flow(LINE.replace('[NEW]', '[DESTROY]'), IP, BOOT, audit.utc(first + timedelta(seconds=121))))
            audit._observe(db, audit.parse_flow(LINE.replace('[NEW] ', ''), IP, BOOT, later))
            self.assertEqual(db.execute('SELECT count(*) FROM flows').fetchone()[0], 1)
            audit._observe(db, audit.parse_flow(LINE, IP, BOOT, audit.utc(first + timedelta(seconds=122))))
            self.assertEqual(db.execute('SELECT count(*) FROM flows').fetchone()[0], 2)
            audit._observe(db, audit.parse_flow(LINE, IP, '550445aa-3f2d-4130-999a-a75ec58b694f', later))
            self.assertEqual(db.execute('SELECT count(*) FROM flows').fetchone()[0], 3)

    def test_retention_capacity_eviction_and_gap_status(self):
        with contextlib.closing(self.db()) as db:
            now = datetime.now(timezone.utc)
            audit._observe(db, audit.parse_flow(LINE, IP, BOOT, audit.utc(now - timedelta(hours=73))))
            audit._gap(db, 'old_gap', audit.utc(now - timedelta(hours=73)))
            audit._prune(db, now)
            self.assertEqual(db.execute('SELECT count(*) FROM flows').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM gaps').fetchone()[0], 0)
            audit._observe(db, audit.parse_flow(LINE, IP, BOOT))
            audit._prune(db, make_room=True)
            self.assertEqual(db.execute('SELECT count(*) FROM flows').fetchone()[0], 0)
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            self.assertLessEqual(db.execute('PRAGMA max_page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0], audit.MAX_BYTES)
        self.assertEqual(audit.status(self.state)['gap']['reason'], 'capacity_eviction')

    def test_export_is_bounded_private_metadata_only_and_no_overwrite(self):
        with contextlib.closing(self.db()) as db:
            audit._observe(db, audit.parse_flow(LINE, IP, BOOT)); db.commit()
        csv = self.root / 'result.csv'
        self.assertEqual(audit.export(self.state, csv, limit=1)['rows'], 1)
        self.assertIn('original_dst', csv.read_text()); self.assertNotIn('payload', csv.read_text())
        with self.assertRaisesRegex(RuntimeError, 'exists'):
            audit.export(self.state, csv)
        target = self.root / 'result.json'; audit.export(self.state, target, 'json')
        self.assertEqual(json.loads(target.read_text())[0]['ct_id'], 1234)
        if os.name == 'posix': self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_commands_are_ip_filtered_before_any_snapshot(self):
        self.enabled()
        cfg = json.loads(audit._paths(103)[1].read_text())
        commands = audit._commands(cfg, True)
        self.assertEqual(len(commands), 4)
        for argv in commands:
            self.assertIn(IP, argv); self.assertIn('-E', argv)
            self.assertTrue('--orig-src' in argv or '--reply-src' in argv)
            self.assertTrue('tcp' in argv or 'udp' in argv)
        fake_boot = self.root / 'boot'; fake_boot.write_text(BOOT)
        spawned = []
        class Process:
            def __init__(self, argv, **kwargs):
                spawned.append(argv)
                if '-L' in argv: raise RuntimeError('stop after subscriber creation')
                self.stdout = tempfile.TemporaryFile(); self.stderr = tempfile.TemporaryFile()
            def poll(self): return 0
            def wait(self, **kwargs): return 0
        with mock.patch.object(audit, 'BOOT_ID', fake_boot), mock.patch.object(audit.subprocess, 'Popen', Process), \
             mock.patch.object(audit.selectors, 'DefaultSelector'), mock.patch.object(audit.os, 'set_blocking'):
            with self.assertRaisesRegex(RuntimeError, 'stop after'):
                audit.collect(audit._paths(103)[1])
        self.assertEqual([argv[1] for argv in spawned], ['-E'] * 4 + ['-L'])
        self.assertIn('startup_subscription', audit.status(self.state)['gap']['reason'])

    def test_collector_records_gap_without_persisting_other_vm_or_raw_stderr(self):
        self.enabled()
        fake_boot = self.root / 'boot'; fake_boot.write_text(BOOT)
        spawned, keys, reads = [], [], {}
        class Process:
            def __init__(self, argv, **kwargs):
                self.stdout = tempfile.TemporaryFile(); self.stderr = tempfile.TemporaryFile()
                self.returncode = 1 if '-E' in argv else 0
                spawned.append(self)
                if len(spawned) == 1:
                    reads[self.stdout.fileno()] = (LINE + '\n' + LINE.replace(IP, '192.168.11.222') + '\ninvalid record\n').encode()
                    reads[self.stderr.fileno()] = b'ENOBUFS secret-raw-content\n'
            def poll(self): return self.returncode
            def wait(self, **kwargs): return self.returncode
        class Poller:
            def register(self, stream, unused, data):
                keys.append(types.SimpleNamespace(fd=stream.fileno(), fileobj=stream, data=data))
            def unregister(self, stream): pass
            def select(self, **kwargs): return [(key, 1) for key in keys]
            def close(self): pass
        with mock.patch.object(audit, 'BOOT_ID', fake_boot), mock.patch.object(audit.subprocess, 'Popen', Process), \
             mock.patch.object(audit.selectors, 'DefaultSelector', Poller), mock.patch.object(audit.os, 'set_blocking'), \
             mock.patch.object(audit.os, 'read', side_effect=lambda fd, size: reads.pop(fd, b'')):
            with self.assertRaisesRegex(RuntimeError, 'subscription exited'):
                audit.collect(audit._paths(103)[1])
        with contextlib.closing(audit._database(audit._paths(103)[2])) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM flows').fetchone()[0], 1)
            reasons = {row[0] for row in db.execute('SELECT reason FROM gaps')}
            self.assertTrue({'conntrack_parse_failure', 'netlink_enobufs', 'subscription_exited'} <= reasons)
            self.assertNotIn('secret-raw-content', '\n'.join(db.iterdump()))

    @unittest.skipIf(os.name != 'posix', 'POSIX symlink permissions')
    def test_symlink_database_cannot_read_or_overwrite_other_resource(self):
        self.enabled()
        other = self.root / 'other'; other.write_text('untouched'); other.chmod(0o600)
        audit._paths(103)[2].symlink_to(other)
        with self.assertRaisesRegex(RuntimeError, 'Redirected'):
            audit.status(self.state)
        self.assertEqual(other.read_text(), 'untouched')


if __name__ == '__main__':
    unittest.main()

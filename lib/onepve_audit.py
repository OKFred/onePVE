"""Optional, VM-scoped conntrack metadata; never packet capture or firewall policy."""
import argparse
import contextlib
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

if __name__ == '__main__':
    sys.dont_write_bytecode = True
from onepve_common import STATE_ROOT, atomic_json, require_root, run as command

AUDIT_ROOT = STATE_ROOT / 'audit'
UNIT_ROOT = Path('/etc/systemd/system')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
RETENTION_HOURS = 72
MAX_BYTES = 1024 ** 3
DATABASE_BYTES = (MAX_BYTES - 4 * 1024 ** 2) // 2
SNAPSHOT_SECONDS = 120
FIELDS = ('boot_id', 'ct_id', 'zone', 'direction', 'protocol', 'original_src', 'original_sport',
          'original_dst', 'original_dport', 'reply_src', 'reply_sport', 'reply_dst', 'reply_dport',
          'started_utc', 'first_seen_utc', 'last_seen_utc', 'ended_utc', 'event', 'tcp_state',
          'original_packets', 'original_bytes', 'reply_packets', 'reply_bytes')


def need(value, message):
    if not value:
        raise RuntimeError(message)


def utc(now=None):
    return (now or datetime.now(timezone.utc)).isoformat(timespec='microseconds')


def _identity(state, required=True):
    vmid, owner = state.get('vmid'), state.get('owner')
    need(type(vmid) is int and 100 <= vmid <= 999999999, 'Invalid audit VMID')
    need(isinstance(owner, str) and re.fullmatch('[0-9a-f]{32}', owner), 'Invalid audit owner')
    cfg = state.get('config', {})
    value = dict(vmid=vmid, owner=owner)
    if required:
        address = str(ipaddress.IPv4Address(state.get('expected_ip') or cfg.get('expected_ip')))
        ip = ipaddress.IPv4Address(address)
        need(not (ip.is_unspecified or ip.is_multicast or ip.is_loopback), 'Invalid audit target IPv4')
        mac = state.get('mac') or cfg.get('mac')
        need(isinstance(mac, str) and re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', mac)
             and not int(mac[:2], 16) & 1, 'Invalid audit target MAC')
        value.update(expected_ip=address, mac=mac.upper())
    return value


def _private(path, directory=False):
    need(path.resolve() == path.absolute() and not path.is_symlink(), 'Redirected audit path')
    info = path.stat()
    need(path.is_dir() if directory else path.is_file() and info.st_nlink == 1, 'Invalid audit file type')
    if os.name == 'posix':
        need(info.st_uid == os.geteuid() and not info.st_mode & 0o077, 'Audit files must be root private')


def _paths(vmid):
    directory = AUDIT_ROOT / str(vmid)
    return directory, directory / 'config.json', directory / 'flows.sqlite3'


def _validate_config(cfg, identity):
    need(isinstance(cfg, dict) and cfg.get('schema_version') == 1
         and all(cfg.get(k) == v for k, v in identity.items()), 'Audit resource belongs to another operation')
    need(_identity(cfg) == {key: cfg[key] for key in ['vmid', 'owner', 'expected_ip', 'mac']}
         and type(cfg.get('enabled')) is bool, 'Invalid saved audit identity')
    need(set(cfg.get('source_sha256', {})) == {'onepve_audit.py', 'onepve_common.py'}
         and set(cfg.get('unit_sha256', {})) == set(_units(cfg)), 'Invalid saved audit ownership record')
    need(all(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)
             for hashes in [cfg['source_sha256'], cfg['unit_sha256']] for value in hashes.values()),
         'Invalid saved audit checksum')


def _unit_file(path):
    need(path.parent.resolve() == path.parent.absolute() and path.is_file() and not path.is_symlink(),
         'Redirected audit unit')
    info = path.stat()
    need(info.st_nlink == 1 and (os.name != 'posix' or info.st_uid == os.geteuid() and not info.st_mode & 0o022),
         'Audit unit is not root protected')


def _check_files(configs, allow_missing=False):
    missing = []
    cfg = configs[-1]
    directory = _paths(cfg['vmid'])[0]
    for field, parent in [('source_sha256', directory), ('unit_sha256', UNIT_ROOT)]:
        for name in cfg[field]:
            path = parent / name
            if not os.path.lexists(path):
                missing.append(str(path)); continue
            _private(path) if field == 'source_sha256' else _unit_file(path)
            need(hashlib.sha256(path.read_bytes()).hexdigest() in {item[field][name] for item in configs},
                 'Audit ' + ('source' if field == 'source_sha256' else 'unit') + ' was modified externally')
    need(allow_missing or not missing, 'Audit resources are missing; state is unknown, explicitly configure or remove to recover')
    return missing


def _temporary_path(pending, record):
    need(record.get('scope') in ['source', 'unit']
         and re.fullmatch(r'\.onepve-audit-' + str(pending['vmid']) + '-' + pending['owner'] + r'-[0-9a-f]{32}\.tmp', record.get('name', ''))
         and type(record.get('size')) is int and 0 <= record['size'] <= 1024 ** 2
         and re.fullmatch('[0-9a-f]{64}', record.get('sha256', '')), 'Invalid audit temporary ownership')
    parent = _paths(pending['vmid'])[0] if record['scope'] == 'source' else UNIT_ROOT
    return parent / record['name']


def _operation(state):
    identity = _identity(state, False)
    directory, path, _ = _paths(identity['vmid'])
    if not os.path.lexists(directory):
        return None, None
    _private(directory, True)
    cfg = pending = None
    if os.path.lexists(path):
        _private(path); cfg = json.loads(path.read_text()); _validate_config(cfg, identity)
    intent = directory / 'pending.json'
    if os.path.lexists(intent):
        _private(intent); pending = json.loads(intent.read_text())
        need(isinstance(pending, dict) and pending.get('schema_version') == 1
             and all(pending.get(k) == v for k, v in identity.items())
             and pending.get('phase') in ['configure', 'remove']
             and isinstance(pending.get('configs'), list) and 1 <= len(pending['configs']) <= 32
             and isinstance(pending.get('temps'), list) and len(pending['temps']) <= 256,
             'Invalid audit recovery ownership record')
        for saved in pending['configs']:
            _validate_config(saved, identity)
        need(cfg is None or cfg in pending['configs'], 'Audit config differs from recorded recovery intent')
        for record in pending['temps']:
            _temporary_path(pending, record)
        cfg = cfg or pending['configs'][-1]
    need(cfg is not None or not any(directory.iterdir()), 'Unrecorded audit resources; inspect before recovery')
    return cfg, pending


def _load(state, allow_missing=False):
    cfg, pending = _operation(state)
    if cfg:
        _check_files(pending['configs'] if pending else [cfg], allow_missing)
        need(allow_missing or pending is None, 'Audit operation is incomplete; state is unknown')
    return cfg


def _quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def _units(cfg):
    stem = 'onepve-audit-' + str(cfg['vmid'])
    prune = 'onepve-audit-prune-' + str(cfg['vmid'])
    path = _paths(cfg['vmid'])[1]
    marker = '# onePVE audit owner=' + cfg['owner'] + '\n'
    base = (marker + '[Unit]\nDescription=onePVE VM conntrack metadata\nAfter=network.target\n'
            '[Service]\nType=simple\nUMask=0077\nNoNewPrivileges=yes\nProtectHome=read-only\n'
            'ProtectSystem=strict\nPrivateTmp=yes\nCapabilityBoundingSet=CAP_NET_ADMIN\n'
            'RestrictAddressFamilies=AF_NETLINK AF_UNIX\nReadWritePaths=' + _quote(path.parent) + '\n'
            'Environment=PYTHONDONTWRITEBYTECODE=1\nMemoryMax=128M\nTasksMax=16\n'
            'StandardOutput=null\n')
    entry = '/usr/bin/python3 ' + _quote(path.parent / 'onepve_audit.py')
    collector = (base + 'ExecStart=' + entry + ' run --config ' + _quote(path) + '\n'
                 'Restart=always\nRestartSec=5\n[Install]\nWantedBy=multi-user.target\n')
    cleanup = base.replace('Type=simple', 'Type=oneshot') + 'ExecStart=' + entry + ' prune --config ' + _quote(path) + '\n'
    timer = (marker + '[Unit]\nDescription=Expire onePVE conntrack metadata\n[Timer]\n'
             'OnCalendar=minutely\nPersistent=true\nUnit=' + prune + '.service\n'
             '[Install]\nWantedBy=timers.target\n')
    return {stem + '.service': collector, prune + '.service': cleanup, prune + '.timer': timer}


def _save_pending(pending):
    atomic_json(_paths(pending['vmid'])[0] / 'pending.json', pending)


def _intent(cfg, pending, phase, desired=None):
    versions = list(pending['configs']) if pending else [cfg]
    if desired is not None and desired not in versions:
        versions.append(desired)
    return dict(vmid=cfg['vmid'], owner=cfg['owner'], schema_version=1, phase=phase,
                configs=versions, temps=list(pending['temps']) if pending else [])


def _cleanup_temporaries(pending):
    for record in pending['temps']:
        path = _temporary_path(pending, record)
        if os.path.lexists(path):
            _private(path) if record['scope'] == 'source' else _unit_file(path)
            size = path.stat().st_size
            need(size <= record['size'] and (size < record['size'] or hashlib.sha256(path.read_bytes()).hexdigest() == record['sha256']),
                 'Audit temporary content differs from recorded publication')
            path.unlink()
    pending['temps'] = []
    _save_pending(pending)


def _publish(target, data, scope, pending):
    record = dict(scope=scope, name='.onepve-audit-' + str(pending['vmid']) + '-' + pending['owner'] + '-' + uuid.uuid4().hex + '.tmp',
                  size=len(data), sha256=hashlib.sha256(data).hexdigest())
    temporary = _temporary_path(pending, record)
    need(target.parent.resolve() == target.parent.absolute(), 'Redirected audit publication directory')
    pending['temps'].append(record)
    _save_pending(pending)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if scope == 'source' else 0o644)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _stop_unit(name, disable=False):
    argv = ['systemctl', 'disable', '--now', name] if disable else ['systemctl', 'stop', name]
    result = command(argv, check=False)
    need(result.returncode == 0 or command(['systemctl', 'is-active', '--quiet', name], check=False).returncode != 0,
         'Could not stop audit unit: ' + name)


def configure(state, enabled):
    """Enable collection; disabling preserves metadata and its expiry timer."""
    require_root()
    need(type(enabled) is bool, 'Audit enabled must be boolean')
    identity = _identity(state, enabled)
    previous, pending = _operation(state)
    if not enabled and previous is None:
        return status(state)
    if previous:
        _check_files(pending['configs'] if pending else [previous], True)
        need(not pending or pending['phase'] != 'remove', 'Audit removal is incomplete; run remove again first')
        if enabled:
            need(all(previous[key] == value for key, value in identity.items()),
                 'Remove previous audit resources before changing the target identity')
    if enabled:
        binary = shutil.which('conntrack')
        need(binary, 'conntrack-tools is required; install it explicitly before enabling audit')
        directory, path, _ = _paths(identity['vmid'])
        sources = {name: (Path(__file__).parent / name).read_bytes()
                   for name in ['onepve_audit.py', 'onepve_common.py']}
        cfg = dict(identity, schema_version=1, enabled=True, conntrack=str(Path(binary).resolve()),
                   source_sha256={name: hashlib.sha256(data).hexdigest() for name, data in sources.items()},
                   retention_hours=RETENTION_HOURS, cap_bytes=MAX_BYTES)
        units = _units(cfg)
        for name in units:
            need(not os.path.lexists(UNIT_ROOT / name) or previous is not None,
                 'Unowned audit unit already exists')
        need(AUDIT_ROOT.parent.resolve() == AUDIT_ROOT.parent.absolute(), 'Redirected audit root')
        AUDIT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private(AUDIT_ROOT, True)
        if not directory.exists():
            need(directory.parent.resolve() == directory.parent.absolute(), 'Redirected audit directory')
            directory.mkdir(parents=True, mode=0o700)
        _private(directory, True)
        cfg['unit_sha256'] = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in units.items()}
        pending = _intent(previous or cfg, pending, 'configure', cfg)
        _save_pending(pending)
        _cleanup_temporaries(pending)
        if previous:
            for name in previous['unit_sha256']:
                _stop_unit(name)
        for name, data in sources.items():
            _publish(directory / name, data, 'source', pending)
        for name, text in units.items():
            _publish(UNIT_ROOT / name, text.encode(), 'unit', pending)
        atomic_json(path, cfg)
        _check_files([cfg])
        (directory / 'pending.json').unlink()
        command(['systemctl', 'daemon-reload'])
        command(['systemctl', 'enable', '--now', 'onepve-audit-prune-' + str(cfg['vmid']) + '.timer'])
        command(['systemctl', 'enable', 'onepve-audit-' + str(cfg['vmid']) + '.service'])
        command(['systemctl', 'restart', 'onepve-audit-' + str(cfg['vmid']) + '.service'])
    else:
        need(pending is None, 'Audit configuration is incomplete; retry enabling or remove before disabling')
        _check_files([previous])
        _stop_unit('onepve-audit-' + str(previous['vmid']) + '.service', True)
        atomic_json(_paths(previous['vmid'])[1], dict(previous, enabled=False))
    return status(state)


def _database(path, create=False):
    for suffix in ['', '-journal', '-wal', '-shm']:
        candidate = Path(str(path) + suffix)
        if os.path.lexists(candidate):
            _private(candidate)
    if create and not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if create else '?mode=ro'), uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    if create:
        # DELETE mode avoids an unbounded WAL retained by a simultaneous export reader.
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute('PRAGMA auto_vacuum=INCREMENTAL')
        db.execute('PRAGMA secure_delete=ON')
        page = db.execute('PRAGMA page_size').fetchone()[0]
        # Reserve another database's worth for the rollback journal, plus its page headers.
        db.execute('PRAGMA max_page_count=' + str(max(16, DATABASE_BYTES // page)))
        db.executescript('''CREATE TABLE IF NOT EXISTS flows (
            id INTEGER PRIMARY KEY, identity TEXT NOT NULL, boot_id TEXT, ct_id INTEGER, zone INTEGER,
            direction TEXT, protocol TEXT, original_src TEXT, original_sport INTEGER,
            original_dst TEXT, original_dport INTEGER, reply_src TEXT, reply_sport INTEGER,
            reply_dst TEXT, reply_dport INTEGER, started_utc TEXT, first_seen_utc TEXT,
            last_seen_utc TEXT, ended_utc TEXT, event TEXT, tcp_state TEXT,
            original_packets INTEGER, original_bytes INTEGER, reply_packets INTEGER, reply_bytes INTEGER);
            CREATE INDEX IF NOT EXISTS flow_identity ON flows(identity,id DESC);
            CREATE INDEX IF NOT EXISTS flow_expiry ON flows(last_seen_utc);
            CREATE TABLE IF NOT EXISTS gaps (observed_utc TEXT, reason TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT);''')
    return db


def _meta(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, value))


def _gap(db, reason, observed=None):
    db.execute('INSERT INTO gaps VALUES (?,?)', (observed or utc(), reason))
    db.execute('DELETE FROM gaps WHERE rowid NOT IN (SELECT rowid FROM gaps ORDER BY rowid DESC LIMIT 1000)')


def _prune(db, now=None, make_room=False):
    cutoff = utc((now or datetime.now(timezone.utc)) - timedelta(hours=RETENTION_HOURS))
    with db:
        db.execute('DELETE FROM flows WHERE last_seen_utc < ?', (cutoff,))
        db.execute('DELETE FROM gaps WHERE observed_utc < ?', (cutoff,))
        page = db.execute('PRAGMA page_size').fetchone()[0]
        used = db.execute('PRAGMA page_count').fetchone()[0] - db.execute('PRAGMA freelist_count').fetchone()[0]
        if make_room or used * page > DATABASE_BYTES * 0.9:
            count = db.execute('SELECT count(*) FROM flows').fetchone()[0]
            db.execute('DELETE FROM flows WHERE id IN (SELECT id FROM flows ORDER BY last_seen_utc LIMIT ?)',
                       (max(1, count // 10),))
            _gap(db, 'capacity_eviction')
    db.execute('PRAGMA incremental_vacuum')


def parse_flow(line, expected_ip, boot_id, observed=None):
    """Parse only documented conntrack text fields; never store unparsed output."""
    protocol = re.search(r'(?:^|\s)(tcp|udp)\s+\d+\s+\d+(?:\s|$)', line)
    need(protocol is not None, 'Unrecognized conntrack flow')
    pairs = re.findall(r'(?:^|\s)(src|dst|sport|dport|packets|bytes|id|zone)=([^\s]+)', line)
    values = {}
    for key, value in pairs:
        values.setdefault(key, []).append(value)
    need(all(len(values.get(key, [])) == 2 for key in ['src', 'dst', 'sport', 'dport'])
         and len(values.get('id', [])) == 1, 'Incomplete conntrack tuple')
    ips = [str(ipaddress.IPv4Address(item)) for key in ['src', 'dst'] for item in values[key]]
    if expected_ip not in ips:
        return None
    result = dict(boot_id=boot_id, ct_id=int(values['id'][0]), zone=int(values.get('zone', ['0'])[0]),
                  direction='outbound' if values['src'][0] == expected_ip or values['dst'][1] == expected_ip else 'inbound',
                  protocol=protocol[1])
    for index, prefix in enumerate(['original', 'reply']):
        for field in ['src', 'dst', 'sport', 'dport']:
            value = values[field][index]
            if field in ['sport', 'dport']:
                value = int(value)
                need(0 <= value <= 65535, 'Invalid conntrack port')
            result[prefix + '_' + field] = value
        for field in ['packets', 'bytes']:
            counters = values.get(field, [])
            need(len(counters) in [0, 2], 'Incomplete conntrack counters')
            value = int(counters[index]) if counters else None
            need(value is None or 0 <= value < 2**63, 'Invalid conntrack counter')
            result[prefix + '_' + field] = value
    event = re.search(r'\[(NEW|UPDATE|DESTROY)\]', line)
    result['event'] = event[1] if event else 'SNAPSHOT'
    result['tcp_state'] = next((word for word in ['SYN_SENT', 'SYN_RECV', 'ESTABLISHED', 'FIN_WAIT',
                                                'CLOSE_WAIT', 'LAST_ACK', 'TIME_WAIT', 'CLOSE', 'LISTEN']
                                if re.search(r'\b' + word + r'\b', line)), None) if protocol[1] == 'tcp' else None
    result['observed_utc'] = observed or utc()
    need(0 <= result['ct_id'] < 2**32 and 0 <= result['zone'] < 2**16, 'Invalid conntrack identity')
    return result


def _observe(db, flow):
    observed = flow['observed_utc']
    identity = hashlib.sha256(json.dumps([flow[k] for k in FIELDS[:13]], separators=(',', ':')).encode()).hexdigest()
    previous = db.execute('SELECT id,ended_utc,last_seen_utc FROM flows WHERE identity=? ORDER BY id DESC LIMIT 1', (identity,)).fetchone()
    if previous and observed < previous['last_seen_utc']:
        return
    if previous and not (previous['ended_utc'] and flow['event'] in ['NEW', 'SNAPSHOT']):
        assignments = 'last_seen_utc=?,event=?,tcp_state=?,ended_utc=COALESCE(?,ended_utc)'
        parameters = [observed, flow['event'], flow['tcp_state'], observed if flow['event'] == 'DESTROY' else None]
        for key in FIELDS[-4:]:
            assignments += ',' + key + '=COALESCE(?,' + key + ')'
            parameters.append(flow[key])
        db.execute('UPDATE flows SET ' + assignments + ' WHERE id=?', parameters + [previous['id']])
    else:
        data = dict(flow, started_utc=observed if flow['event'] == 'NEW' else None,
                    first_seen_utc=observed, last_seen_utc=observed,
                    ended_utc=observed if flow['event'] == 'DESTROY' else None)
        db.execute('INSERT INTO flows (identity,' + ','.join(FIELDS) + ') VALUES (' + ','.join('?' for _ in range(len(FIELDS) + 1)) + ')',
                   [identity] + [data[key] for key in FIELDS])
    _meta(db, 'last_observed_utc', observed)


def _commands(cfg, events):
    return [[cfg['conntrack'], '-E' if events else '-L', '-f', 'ipv4', '-p', protocol,
             selector, cfg['expected_ip'], '-o', 'extended,id']
            for protocol in ['tcp', 'udp'] for selector in ['--orig-src', '--reply-src']]


def collect(path):
    require_root()
    _private(path)
    cfg = json.loads(path.read_text())
    need(_paths(cfg['vmid'])[1] == path and _load(cfg) == cfg and cfg['enabled'], 'Audit collection is not enabled')
    boot_id = BOOT_ID.read_text().strip()
    need(re.fullmatch(r'[0-9a-f-]{36}', boot_id), 'Invalid host boot identity')
    db = _database(_paths(cfg['vmid'])[2], True)
    poller, children = selectors.DefaultSelector(), {}
    stopping = False

    def stop(*unused):
        nonlocal stopping
        stopping = True

    def spawn(events):
        for argv in _commands(cfg, events):
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            children[process] = dict(events=events, started=time.monotonic(), streams=2, observed_utc=utc())
            for kind, stream in [('stdout', process.stdout), ('stderr', process.stderr)]:
                os.set_blocking(stream.fileno(), False)
                poller.register(stream, selectors.EVENT_READ, (process, kind, bytearray()))

    handlers = {kind: signal.signal(kind, stop) for kind in [signal.SIGTERM, signal.SIGINT]}
    try:
        _prune(db)
        _gap(db, 'collector_startup_subscription_window')
        db.commit()
        # conntrack has no readiness handshake: explicitly retain this startup coverage gap.
        spawn(True)
        spawn(False)
        next_snapshot = time.monotonic() + SNAPSHOT_SECONDS
        while not stopping:
            for key, _ in poller.select(timeout=1):
                process, kind, buffer = key.data
                data = os.read(key.fd, 65536)
                buffer.extend(data)
                if len(buffer) > 131072:
                    _gap(db, 'oversized_conntrack_record'); buffer.clear()
                while b'\n' in buffer or not data and buffer:
                    raw, _, remaining = buffer.partition(b'\n')
                    buffer[:] = remaining
                    if kind == 'stderr':
                        if b'ENOBUFS' in raw or b'No buffer space' in raw:
                            _gap(db, 'netlink_enobufs'); next_snapshot = 0
                        elif raw and children[process]['events']:
                            _gap(db, 'subscription_stderr')
                        continue
                    try:
                        observed = None if children[process]['events'] else children[process]['observed_utc']
                        flow = parse_flow(raw.decode('ascii'), cfg['expected_ip'], boot_id, observed)
                        if flow:
                            _observe(db, flow)
                    except (ValueError, RuntimeError, UnicodeError):
                        _gap(db, 'conntrack_parse_failure')
                    except sqlite3.DatabaseError:
                        db.rollback(); _prune(db, make_room=True)
                        _gap(db, 'database_write_gap')
                if not data:
                    poller.unregister(key.fileobj); key.fileobj.close()
                    children[process]['streams'] -= 1
            for process, child in list(children.items()):
                if not child['events'] and time.monotonic() - child['started'] > 30 and process.poll() is None:
                    _gap(db, 'snapshot_timeout'); process.terminate()
                if process.poll() is not None and child['streams'] == 0:
                    del children[process]
                    if child['events']:
                        _gap(db, 'subscription_exited'); db.commit()
                        raise RuntimeError('Audit subscription exited; coverage gap recorded')
                    if process.returncode:
                        _gap(db, 'snapshot_failed')
            if time.monotonic() >= next_snapshot and not any(not child['events'] for child in children.values()):
                _prune(db)
                spawn(False)
                next_snapshot = time.monotonic() + SNAPSHOT_SECONDS
            _meta(db, 'heartbeat_utc', utc())
            try:
                db.commit()
            except sqlite3.DatabaseError:
                db.rollback(); _prune(db, make_room=True)
                _gap(db, 'database_write_gap'); db.commit()
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
        for process in children:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            process.stdout.close(); process.stderr.close()
        poller.close(); db.commit(); db.close()
        for kind, handler in handlers.items():
            signal.signal(kind, handler)


def prune(path):
    require_root()
    _private(path)
    cfg = json.loads(path.read_text())
    need(_paths(cfg['vmid'])[1] == path and _load(cfg) == cfg, 'Invalid audit cleanup config')
    database = _paths(cfg['vmid'])[2]
    if database.exists():
        with contextlib.closing(_database(database, True)) as db:
            _prune(db)


def status(state):
    cfg, pending = _operation(state)
    answer = dict(enabled=False, active=False, retention_hours=RETENTION_HOURS, cap_bytes=MAX_BYTES,
                  database_cap_bytes=DATABASE_BYTES, bytes=0, flow_count=0, gap=None, coverage_gap=None,
                  resource_health='absent', blocked_attempts_recorded=False,
                  filter_scope='Host conntrack receives namespace-wide metadata, filters target IPv4 in userspace; Python rechecks tuples before storage.',
                  supported_scope='One fixed IPv4, one VM NIC; no IPv6 or VM forwarding/NAT gateway audit.',
                  semantics='Observed TCP/UDP conntrack flows; NEW is not proof of permission or success. UDP is a flow, not a connection.')
    if cfg is None:
        return answer
    missing = _check_files(pending['configs'] if pending else [cfg], True)
    answer['resource_health'] = 'unknown' if missing or pending else 'ready'
    if missing:
        answer['missing_resources'] = missing
    service = 'onepve-audit-' + str(cfg['vmid']) + '.service'
    answer.update(enabled=cfg['enabled'], expected_ip=cfg['expected_ip'], mac=cfg['mac'],
                  active=command(['systemctl', 'is-active', '--quiet', service], check=False).returncode == 0)
    database = _paths(cfg['vmid'])[2]
    answer['bytes'] = sum(path.stat().st_size for path in [database, Path(str(database) + '-journal'), Path(str(database) + '-wal'), Path(str(database) + '-shm')] if path.exists())
    if database.exists():
        with contextlib.closing(_database(database)) as db:
            cutoff = utc(datetime.now(timezone.utc) - timedelta(hours=RETENTION_HOURS))
            answer['flow_count'] = db.execute('SELECT count(*) FROM flows WHERE last_seen_utc>=?', (cutoff,)).fetchone()[0]
            row = db.execute('SELECT observed_utc,reason FROM gaps WHERE observed_utc>=? ORDER BY rowid DESC LIMIT 1', (cutoff,)).fetchone()
            answer['gap'] = dict(row) if row else None
            answer.update(dict(db.execute('SELECT key,value FROM meta')))
    if answer['resource_health'] == 'unknown':
        answer['coverage_gap'] = dict(reason='audit_resources_incomplete', observed_utc=utc())
    elif answer['enabled']:
        reason = 'collector_stopped' if not answer['active'] else None
        if reason is None:
            try:
                heartbeat = datetime.fromisoformat(answer['heartbeat_utc'])
                age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
                if not 0 <= age <= 300:
                    reason = 'collector_heartbeat_stale'
            except (KeyError, ValueError, TypeError):
                reason = 'collector_heartbeat_stale'
        if reason:
            answer['coverage_gap'] = dict(reason=reason, observed_utc=utc())
    return answer


def export(state, path, format='csv', limit=10000):
    require_root()
    need(format in ['csv', 'json'] and type(limit) is int and 1 <= limit <= 100000, 'Invalid audit export options')
    cfg = _load(state)
    need(cfg is not None, 'No audit resource exists')
    database = _paths(cfg['vmid'])[2]
    need(database.is_file(), 'No audit observations exist')
    path = Path(path).absolute()
    need(path.parent.resolve() == path.parent and not os.path.lexists(path), 'Export target exists or is redirected')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    count = 0
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream, contextlib.closing(_database(database)) as db:
            cutoff = utc(datetime.now(timezone.utc) - timedelta(hours=RETENTION_HOURS))
            rows = db.execute('SELECT ' + ','.join(FIELDS) + ' FROM flows WHERE last_seen_utc>=? ORDER BY last_seen_utc DESC LIMIT ?', (cutoff, limit))
            if format == 'csv':
                writer = csv.DictWriter(stream, fieldnames=FIELDS); writer.writeheader()
                for row in rows:
                    writer.writerow(dict(row)); count += 1
            else:
                stream.write('[')
                for row in rows:
                    stream.write((',' if count else '') + json.dumps(dict(row))); count += 1
                stream.write(']\n')
        return dict(path=str(path), format=format, rows=count, limit=limit, metadata_only=True)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def remove(state):
    require_root()
    cfg, pending = _operation(state)
    if cfg is None:
        directory = _paths(_identity(state, False)['vmid'])[0]
        if directory.exists():
            # A crash after the last ownership record unlink can leave only an empty private directory.
            _private(directory, True)
            need(not any(directory.iterdir()), 'Unrecorded audit resources; inspect before recovery')
            directory.rmdir()
            return dict(removed=True)
        return dict(removed=False)
    _check_files(pending['configs'] if pending else [cfg], True)
    directory, _, _ = _paths(cfg['vmid'])
    allowed = {'config.json', 'pending.json', 'onepve_audit.py', 'onepve_common.py', 'flows.sqlite3',
               'flows.sqlite3-journal', 'flows.sqlite3-wal', 'flows.sqlite3-shm'}
    if pending:
        allowed.update(record['name'] for record in pending['temps'] if record['scope'] == 'source')
    for path in directory.iterdir():
        need(path.name in allowed, 'Unexpected file in audit directory; inspect before removal')
        _private(path)
    pending = _intent(cfg, pending, 'remove')
    _save_pending(pending)
    _cleanup_temporaries(pending)
    for name in cfg['unit_sha256']:
        _stop_unit(name, name.endswith('.timer') or name == 'onepve-audit-' + str(cfg['vmid']) + '.service')
    for name in cfg['unit_sha256']:
        (UNIT_ROOT / name).unlink(missing_ok=True)
    for path in directory.iterdir():
        need(path.name in allowed, 'Unexpected file in audit directory; inspect before removal')
        _private(path)
        if path.name not in ['config.json', 'pending.json']:
            path.unlink()
    command(['systemctl', 'daemon-reload'])
    (directory / 'config.json').unlink(missing_ok=True)
    (directory / 'pending.json').unlink()
    directory.rmdir()
    return dict(removed=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['configure', 'run', 'prune', 'status', 'export', 'remove'])
    parser.add_argument('--config', type=Path)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--enable', action='store_true')
    parser.add_argument('--path', type=Path)
    parser.add_argument('--format', choices=['csv', 'json'], default='csv')
    parser.add_argument('--limit', type=int, default=10000)
    args = parser.parse_args(argv)
    if args.action in ['run', 'prune']:
        need(args.config is not None, '--config is required')
        return collect(args.config) if args.action == 'run' else prune(args.config)
    need(args.state is not None, '--state is required')
    _private(args.state)
    state = json.loads(args.state.read_text())
    if args.action == 'configure': result = configure(state, args.enable)
    elif args.action == 'status': result = status(state)
    elif args.action == 'remove': result = remove(state)
    else:
        need(args.path is not None, '--path is required')
        result = export(state, args.path, args.format, args.limit)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

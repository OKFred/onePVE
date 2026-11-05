"""Exercise real conntrack collection and SQLite inside a disposable network namespace."""
import csv
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import onepve_audit as audit
import network_namespace as packets


def run_checks():
    if os.environ.get('ONEPVE_TEST_NETNS') != '1' or os.geteuid() != 0 or os.readlink('/proc/self/ns/net') == os.readlink('/proc/1/ns/net'):
        raise RuntimeError('Refusing audit test outside an explicit disposable network namespace')
    children = []; nodes = {}; collector = None
    def launch(args, ready=False):
        child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(child)
        if ready and child.stdout.readline().strip() != 'ready':raise RuntimeError(child.stderr.read())
        return child
    def node(name, address, tap):
        process = launch(['unshare', '--net', '--', sys.executable, packets.HERE, 'hold'], True)
        nodes[name] = process.pid
        packets.call('ip', 'link', 'add', tap, 'type', 'veth', 'peer', 'name', 'peer-' + name)
        packets.call('ip', 'link', 'set', 'peer-' + name, 'netns', str(process.pid))
        packets.call('ip', 'link', 'set', tap, 'master', 'vmbr0'); packets.call('ip', 'link', 'set', tap, 'up')
        prefix = packets.namespace_command(process.pid, 'ip')
        packets.call(*prefix, 'link', 'set', 'peer-' + name, 'name', 'eth0')
        packets.call(*prefix, 'link', 'set', 'lo', 'up'); packets.call(*prefix, 'link', 'set', 'eth0', 'up')
        packets.call(*prefix, 'address', 'add', address + '/24', 'dev', 'eth0')
    def flow(name, protocol, destination, port):
        answer = packets.call(*packets.namespace_command(nodes[name], sys.executable, packets.HERE, 'client', protocol, destination, str(port), '0'))
        if answer.stdout.strip() != 'yes':raise AssertionError('Test flow did not complete: ' + str((name, protocol, destination, port)))
    def wait_for(predicate, message):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if collector and collector.poll() is not None:raise RuntimeError('Collector exited early: ' + collector.stderr.read())
            try:
                if predicate():return
            except (sqlite3.DatabaseError, FileNotFoundError):pass
            time.sleep(.1)
        raise AssertionError(message)
    try:
        packets.call('ip', 'link', 'add', 'vmbr0', 'type', 'bridge'); packets.call('ip', 'link', 'set', 'vmbr0', 'up')
        for name, address, tap in [('guest','10.7.0.103','tap103i0'), ('other','10.7.0.104','tap104i0'), ('server','10.7.0.1','srv0')]:
            node(name, address, tap)
        packets.call('nft', '-f', '-', input='table bridge audit_test {\n chain track {\n type filter hook forward priority 0; policy accept;\n ct state { new, established, related } counter accept\n }\n}\n')
        Path('/proc/sys/net/netfilter/nf_conntrack_acct').write_text('1')
        for name in ['guest', 'server']:
            launch(packets.namespace_command(nodes[name], sys.executable, packets.HERE, 'serve', name), True)
        flow('guest', 'udp', '10.7.0.1', 24443)
        flow('other', 'udp', '10.7.0.1', 24443)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); units = root / 'units'; units.mkdir(mode=0o700)
            audit.AUDIT_ROOT = root / 'audit'; audit.UNIT_ROOT = units
            state = dict(vmid=103, owner='a'*32, config=dict(expected_ip='10.7.0.103', mac='02:00:00:00:01:03'))
            with mock.patch.object(audit, 'command', return_value=types.SimpleNamespace(returncode=0)):
                audit.configure(state, True)
            _, config, database = audit._paths(103)
            collector = launch([sys.executable, str(Path(__file__).resolve()), 'collect', str(audit.AUDIT_ROOT), str(config), str(units)])
            def rows():
                with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
                    connection.row_factory = sqlite3.Row
                    return [dict(row) for row in connection.execute('SELECT * FROM flows')]
            wait_for(lambda:any(row['protocol']=='udp' and row['original_dport']==24443 and row['event']=='SNAPSHOT' for row in rows()), 'Startup snapshot was not collected')
            wait_for(lambda:len(Path('/proc/' + str(collector.pid) + '/task/' + str(collector.pid) + '/children').read_text().split()) >= 4, 'Event subscriptions not running')
            for protocol in ['tcp', 'udp']:
                flow('guest', protocol, '10.7.0.1', 24444)
                flow('other', protocol, '10.7.0.1', 24444)
            flow('server', 'tcp', '10.7.0.103', 22)
            wait_for(lambda:len(rows()) >= 4 and any(row['protocol']=='tcp' and row['started_utc'] for row in rows()), 'Live events were not collected')
            observed = rows()
            assert all('10.7.0.103' in [row['original_src'],row['original_dst'],row['reply_src'],row['reply_dst']] for row in observed)
            assert not any('10.7.0.104' in [row['original_src'],row['original_dst'],row['reply_src'],row['reply_dst']] for row in observed)
            assert {'tcp','udp'} <= {row['protocol'] for row in observed}
            assert {'inbound','outbound'} <= {row['direction'] for row in observed}
            assert all(row['ct_id'] is not None and row['boot_id'] and row['first_seen_utc'] for row in observed)
            assert any(row['original_packets'] is not None for row in observed)
            with mock.patch.object(audit, 'command', return_value=types.SimpleNamespace(returncode=0)):
                status = audit.status(state)
            assert status['active'] and status['flow_count'] >= 4 and status['coverage_gap'] is None
            assert status['gap']['reason'] == 'collector_startup_subscription_window'
            assert status['blocked_attempts_recorded'] is False
            exported = root / 'flows.json'; csvfile = root / 'flows.csv'
            assert audit.export(state, exported, 'json')['rows'] == len(observed)
            assert audit.export(state, csvfile, 'csv')['rows'] == len(observed)
            assert all(set(row) == set(audit.FIELDS) for row in json.loads(exported.read_text()))
            with csvfile.open() as stream:assert len(list(csv.DictReader(stream))) == len(observed)
            child_ids = Path('/proc/' + str(collector.pid) + '/task/' + str(collector.pid) + '/children').read_text().split()
            collector.send_signal(signal.SIGTERM); stdout, stderr = collector.communicate(timeout=7)
            assert collector.returncode == 0, stderr
            assert all(not Path('/proc/' + pid).exists() for pid in child_ids)
            with mock.patch.object(audit, 'command', return_value=types.SimpleNamespace(returncode=3)):
                stopped = audit.status(state)
            assert stopped['coverage_gap']['reason'] == 'collector_stopped'
            assert stopped['gap']['reason'] == 'collector_startup_subscription_window'
            return {'flows':len(observed), 'protocols':['tcp','udp'], 'snapshot':True, 'events':True,
                    'other_vm_excluded':True, 'metadata_export':True, 'sigterm_clean':True,
                    'startup_gap':'collector_startup_subscription_window', 'stopped_gap':'collector_stopped'}
    finally:
        for child in reversed(children):
            if child.poll() is None:child.terminate()
        for child in children:
            try:child.communicate(timeout=3)
            except subprocess.TimeoutExpired:child.kill(); child.communicate()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'collect':
        audit.AUDIT_ROOT = Path(sys.argv[2]); audit.UNIT_ROOT = Path(sys.argv[4]); audit.collect(Path(sys.argv[3]))
    else:
        try:print(json.dumps(run_checks(), indent=2))
        except subprocess.CalledProcessError as error:
            print(error.stderr, file=sys.stderr); raise

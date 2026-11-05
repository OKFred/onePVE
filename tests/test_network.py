import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import onepve_network as network


def config(**changes):
    value = dict(vmid=103, network_policy='isolated', connection_audit=False,
                 address=None, gateway=None, expected_ip='10.7.0.103', isolation_gateway='10.7.0.1',
                 dhcp_server='10.7.0.53', nameserver='10.7.0.53', management_cidrs=['10.7.0.2/32'],
                 mac='02:00:00:00:01:03', bridge='vmbr0')
    value.update(changes)
    return network.normalize(value)


def state(**changes):
    value = dict(vmid=103, owner='a' * 32, config=config(), network={'protected_cidrs':network.PRIVATE})
    value.update(changes)
    return value


def result(stdout=''):
    return types.SimpleNamespace(stdout=stdout, stderr='', returncode=0)


def live_rules():
    value = state()
    table = network.identity(value)
    return {'nftables':[
        {'metainfo':{'version':'1.1.3', 'release_name':'test'}},
        {'table':{'family':'bridge', 'name':table, 'handle':1, 'comment':'onePVE owner=' + value['owner']}},
        {'chain':{'family':'bridge', 'table':table, 'name':'outbound', 'handle':2}},
        {'rule':{'family':'bridge', 'table':table, 'chain':'outbound', 'handle':3,
                 'expr':[{'match':{'op':'==', 'left':{'payload':{'protocol':'tcp', 'field':'dport'}}, 'right':22}},
                         {'counter':{'packets':2, 'bytes':100}}, {'drop':None}]}}]}


class NetworkTests(unittest.TestCase):
    def test_isolation_and_audit_defaults_are_explicit(self):
        value = config()
        self.assertEqual(value['network_policy'], 'isolated')
        self.assertFalse(value['connection_audit'])
        for changes in [dict(management_cidrs=[]), dict(nameserver=None), dict(expected_ip=None),
                        dict(dhcp_server=None), dict(isolation_gateway=None), dict(connection_audit=1),
                        dict(management_cidrs=['0.0.0.0/0']), dict(lan_allow=['10.0.0.0/8,tcp,0']),
                        dict(lan_allow=['10.0.0.0/8,icmp,53']), dict(lan_allow=['0.0.0.0/0,tcp,22'])]:
            with self.subTest(changes=changes), self.assertRaises((RuntimeError, ValueError, TypeError)):
                config(**changes)
        self.assertTrue(network.managed(config(network_policy='lan')))
        self.assertFalse(network.managed(config(network_policy='legacy', management_cidrs=[], nameserver=None,
                                                expected_ip=None, dhcp_server=None, isolation_gateway=None)))
        with self.assertRaises(RuntimeError):config(network_policy='lan', connection_audit=True, nameserver=None)

    def test_preflight_is_readonly_and_protects_routes_and_local_addresses(self):
        outputs = ['{"nftables":[]}',
                   json.dumps([{'dst':'default'}, {'dst':'11.22.33.0/24'}, {'dst':'10.7.0.0/24'}]),
                   json.dumps([{'addr_info':[{'local':'12.34.56.78'}]}]), '']
        with mock.patch.object(network.shutil, 'which', side_effect=lambda name:'/usr/bin/' + name), \
             mock.patch.object(network, 'run', side_effect=[result(value) for value in outputs]) as run:
            value = network.preflight(config())
        self.assertIn('11.22.33.0/24', value['protected_cidrs'])
        self.assertIn('12.34.56.78/32', value['protected_cidrs'])
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ['nft', '--json', 'list', 'tables'], ['ip', '-j', '-4', 'route', 'show', 'table', 'all'],
            ['ip', '-j', '-4', 'address', 'show'], ['nft', '-c', '-f', '-']])
        with mock.patch.object(network.shutil, 'which', return_value=None), mock.patch.object(network, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'nftables'):network.preflight(config())
        run.assert_not_called()

    def test_rules_match_only_one_tap_and_order_narrow_exceptions_before_denial(self):
        text = network.rules(state(config=config(lan_allow=['10.7.0.9/32,tcp,24443'])))
        self.assertIn('hook prerouting priority -150', text)
        self.assertIn('hook postrouting priority 0', text)
        self.assertIn('iifname "tap103i0"', text)
        self.assertIn('oifname "tap103i0"', text)
        self.assertNotIn('tap104', text)
        self.assertIn('ct direction reply ct state established', text)
        self.assertIn('ether saddr != 02:00:00:00:01:03 counter drop', text)
        self.assertIn('ip saddr != 10.7.0.103 counter drop', text)
        self.assertLess(text.index('ip daddr 10.7.0.9/32 tcp dport 24443'), text.index('ip daddr { 0.0.0.0/8'))
        self.assertIn('ether type != ip counter drop', text)
        self.assertIn('ether type arp counter drop', text)

    def test_apply_checks_atomic_transaction_before_publish_and_never_flushes_other_tables(self):
        original = state()
        with mock.patch.object(network, 'host_protected', return_value=[]), \
             mock.patch.object(network, '_live', side_effect=[{'nftables':[]}, {'nftables':[]}]), \
             mock.patch.object(network, 'run') as run:
            digest = network.apply(original)
        self.assertEqual(len(digest), 64)
        self.assertEqual(run.call_args_list[0].args[0], ['nft', '-c', '-f', '-'])
        self.assertEqual(run.call_args_list[1].args[0], ['nft', '-f', '-'])
        self.assertEqual(run.call_args_list[0].kwargs['input'], run.call_args_list[1].kwargs['input'])
        self.assertNotIn('flush ruleset', run.call_args_list[1].kwargs['input'])
        with mock.patch.object(network, 'host_protected', return_value=[]), mock.patch.object(network, '_live', return_value=None), \
             mock.patch.object(network, 'run', side_effect=RuntimeError('syntax failed')) as run:
            with self.assertRaisesRegex(RuntimeError, 'syntax'):network.apply(original)
        self.assertEqual(run.call_count, 1)

    def test_foreign_owned_table_is_not_replaced_or_removed(self):
        foreign = result(json.dumps({'nftables':[{'table':{'comment':'onePVE owner=' + 'b' * 32}}]}))
        with mock.patch.object(network, 'host_protected', return_value=[]), \
             mock.patch.object(network.subprocess, 'run', return_value=foreign), mock.patch.object(network, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'ownership'):network.apply(state())
        run.assert_not_called()

    def test_rules_signature_ignores_counters_and_handles_but_detects_expression_drift(self):
        original = live_rules(); observed = copy.deepcopy(original)
        observed['nftables'][0]['metainfo']['version'] = 'future'
        observed['nftables'][1]['table']['handle'] = 11
        observed['nftables'][2]['chain']['handle'] = 22
        observed['nftables'][3]['rule']['handle'] = 33
        observed['nftables'][3]['rule']['expr'][1]['counter'] = {'packets':500, 'bytes':20000}
        self.assertEqual(network._signature(original), network._signature(observed))
        observed['nftables'][3]['rule']['expr'][0]['match']['right'] = 23
        self.assertNotEqual(network._signature(original), network._signature(observed))

    def test_remove_rejects_foreign_unit_and_deletes_only_owned_table(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); units = root / 'units'; units.mkdir()
            value = state()
            with mock.patch.object(network, 'ROOT', root / 'network'), mock.patch.object(network, 'UNITS', units):
                directory, unit = network._paths(value); directory.mkdir(parents=True)
                evidence = directory / 'diagnostic.txt'; evidence.write_text('retain')
                unit.write_text('# other tool\n')
                with mock.patch.object(network, 'run') as run, mock.patch.object(network, '_live') as live:
                    with self.assertRaisesRegex(RuntimeError, 'Foreign network unit'):network.remove(value)
                run.assert_not_called(); live.assert_not_called()
                self.assertEqual(unit.read_text(), '# other tool\n')
                foreign = units / 'other.service'; foreign.write_text('leave alone')
                unit.write_text('# onePVE owner=' + value['owner'] + '\nowned\n')
                with mock.patch.object(network, 'run') as run, mock.patch.object(network, '_live', return_value=live_rules()):
                    answer = network.remove(value)
                self.assertTrue(answer['removed']); self.assertFalse(unit.exists())
                self.assertEqual([call.args[0] for call in run.call_args_list], [
                    ['systemctl', 'disable', '--now', unit.name], ['systemctl', 'daemon-reload'],
                    ['nft', 'delete', 'table', 'bridge', network.identity(value)]])
                self.assertEqual(foreign.read_text(), 'leave alone')
                self.assertEqual(evidence.read_text(), 'retain')

    def guard(self, text, value=None):
        value = state() if value is None else value
        cfg = mock.MagicMock(); cfg.is_symlink.return_value = False
        cfg.stat.return_value = types.SimpleNamespace(st_uid=0, st_mode=0o600)
        cfg.read_text.return_value = json.dumps(value)
        directory = mock.MagicMock(); directory.__truediv__.return_value = cfg
        vmroot = mock.MagicMock(); vmroot.__truediv__.return_value.read_text.return_value = text
        with mock.patch.object(network.argparse.ArgumentParser, 'parse_args', return_value=types.SimpleNamespace(config=cfg)), \
             mock.patch.object(network.os, 'geteuid', return_value=0, create=True), \
             mock.patch.object(network, 'Path', return_value=vmroot), \
             mock.patch.object(network, '_paths', return_value=(directory, mock.MagicMock())), \
             mock.patch.object(network, '_applied'), mock.patch.object(network, 'apply') as apply:
            network.main()
        apply.assert_called_once_with(value)

    def test_boot_guard_rejects_other_bridge_prefix_and_extra_nic(self):
        text = 'description: onePVE owner=' + 'a' * 32 + '\nnet0: virtio=02:00:00:00:01:03,bridge=vmbr0\n'
        self.guard(text)
        for changed in [text.replace('bridge=vmbr0', 'bridge=vmbr0evil'),
                        text + 'net1: virtio=02:00:00:00:02:04,bridge=vmbr0\n',
                        text.replace('01:03', '01:04'),
                        text.replace('bridge=vmbr0', 'bridge=vmbr0,bridge=vmbr1'),
                        text.replace('bridge=vmbr0', 'bridge=vmbr0,trunks=12')]:
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):self.guard(changed)

    def test_boot_guard_rejects_vlan_drift(self):
        text = 'description: onePVE owner=' + 'a' * 32 + '\nnet0: virtio=02:00:00:00:01:03,bridge=vmbr0,tag=12\n'
        self.guard(text, state(config=config(vlan=12)))
        with self.assertRaises(RuntimeError):self.guard(text, state(config=config(vlan=13)))
        with self.assertRaises(RuntimeError):self.guard(text, state(config=config(vlan=None)))


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'geteuid') and os.geteuid() == 0,
                     'Persistence permission checks require POSIX root; no host services are changed')
class NetworkPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.units = self.root / 'units'; self.units.mkdir(mode=0o700)
        self.stack = mock.patch.multiple(network, ROOT=self.root / 'network', UNITS=self.units)
        self.stack.start()

    def tearDown(self):
        self.stack.stop(); self.temp.cleanup()

    def test_install_persists_owned_guard_unit_config_rules_and_readback_baseline(self):
        value = state(); live = live_rules()
        with mock.patch.object(network, 'host_protected', return_value=[]), \
             mock.patch.object(network, '_live', return_value=live), mock.patch.object(network, 'run') as run:
            answer = network.install(value)
        directory, unit = network._paths(value)
        self.assertEqual(answer['directory'], str(directory))
        self.assertEqual((directory / 'guard.py').read_text(), Path(network.__file__).read_text())
        self.assertEqual(json.loads((directory / 'config.json').read_text())['owner'], value['owner'])
        self.assertIn('iifname "tap103i0"', (directory / 'rules.nft').read_text())
        baseline = json.loads((directory / 'applied.json').read_text())
        self.assertEqual(baseline, {'owner':value['owner'], 'ruleset_sha256':network._signature(live)})
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for name in ['guard.py', 'config.json', 'rules.nft', 'applied.json']:
            self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)
        content = unit.read_text()
        self.assertIn('Before=pve-guests.service\n', content)
        self.assertIn('After=local-fs.target pve-cluster.service\n', content)
        self.assertIn('WantedBy=multi-user.target\n', content)
        self.assertIn('ExecStart=/usr/bin/python3 ' + str(directory / 'guard.py') + ' apply --config ' + str(directory / 'config.json'), content)
        self.assertEqual(unit.stat().st_mode & 0o777, 0o644)
        self.assertEqual([call.args[0] for call in run.call_args_list[:2]],
                         [['systemctl', 'daemon-reload'], ['systemctl', 'enable', unit.name]])
        with mock.patch.object(network.subprocess, 'run') as invoked:
            exec(compile(network.hook(value), '<pre-start-test>', 'exec'), {})
        invoked.assert_called_once_with(['/usr/bin/python3', str(directory / 'guard.py'), 'apply', '--config',
                                         str(directory / 'config.json')], check=True, timeout=45)
        with mock.patch.object(network, '_live', return_value=live):self.assertTrue(network.status(value)['rules_match'])

    def test_status_detects_rules_or_baseline_drift_but_accepts_counter_updates(self):
        value = state(); directory, unit = network._paths(value); directory.mkdir(parents=True, mode=0o700)
        live = live_rules(); baseline = directory / 'applied.json'
        network._write(baseline, json.dumps({'owner':value['owner'], 'ruleset_sha256':network._signature(live)}))
        with mock.patch.object(network, '_live', return_value=live):
            self.assertTrue(network.status(value)['rules_match'])
        incremented = copy.deepcopy(live)
        incremented['nftables'][3]['rule']['expr'][1]['counter']['packets'] = 17
        with mock.patch.object(network, '_live', return_value=incremented):
            answer = network.status(value)
            self.assertTrue(answer['rules_match']); self.assertEqual(answer['drop_packets_since_rule_load'], 17)
        incremented['nftables'][3]['rule']['expr'][0]['match']['right'] = 23
        with mock.patch.object(network, '_live', return_value=incremented):
            self.assertFalse(network.status(value)['rules_match'])
        network._write(baseline, json.dumps({'owner':value['owner'], 'ruleset_sha256':'0' * 64}))
        with mock.patch.object(network, '_live', return_value=live):self.assertFalse(network.status(value)['rules_match'])
        baseline.unlink()
        with mock.patch.object(network, '_live', return_value=live):self.assertFalse(network.status(value)['rules_match'])
        with mock.patch.object(network, '_live', return_value=None):self.assertFalse(network.status(value)['rules_match'])


class NetworkNamespaceTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix' and hasattr(os, 'geteuid') and os.geteuid() == 0,
                         'Real packet validation requires root Linux and a fresh network namespace')
    def test_packets_in_fresh_namespace(self):
        for command in ['unshare', 'nsenter', 'ip', 'nft']:
            if not shutil.which(command):self.skipTest(command + ' unavailable')
        probe = subprocess.run(['unshare', '--net', '--', 'nft', 'add', 'table', 'bridge', 'onepve_test_probe'],
                               capture_output=True, text=True, timeout=10)
        if probe.returncode and any(message in probe.stderr for message in ['Operation not supported', 'Operation not permitted']):
            self.skipTest('Kernel/network namespace lacks nft bridge family: ' + probe.stderr.splitlines()[0])
        self.assertEqual(probe.returncode, 0, probe.stderr)
        environment = dict(os.environ, ONEPVE_TEST_NETNS='1')
        answer = subprocess.run(['unshare', '--net', '--', sys.executable, str(Path(__file__).with_name('network_namespace.py'))],
                                capture_output=True, text=True, env=environment, timeout=90)
        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertGreaterEqual(len(json.loads(answer.stdout)['checks']), 39)


if __name__ == '__main__':unittest.main()

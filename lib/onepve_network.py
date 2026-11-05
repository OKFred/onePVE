"""Per-VM bridge filtering. Never flush the host ruleset or change its firewall backend."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path('/var/lib/onepve/network')
UNITS = Path('/etc/systemd/system')
PRIVATE = ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
           '169.254.0.0/16', '172.16.0.0/12', '192.168.0.0/16', '192.0.0.0/24',
           '192.0.2.0/24', '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24',
           '224.0.0.0/4', '240.0.0.0/4']


def need(value, message):
    if not value:
        raise RuntimeError(message)


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, capture_output=True, text=True, timeout=30, **kwargs)


def ipv4(value):
    need(isinstance(value,str) and value, 'A fixed/reserved IPv4 is required for host network isolation/audit')
    address = ipaddress.IPv4Address(value)
    need(not (address.is_unspecified or address.is_loopback or address.is_multicast or address.is_reserved), 'Use a usable IPv4 address')
    return str(address)


def cidrs(values):
    need(isinstance(values, list) and len(values) <= 128, 'Too many/invalid IPv4 networks')
    result = []
    for value in values:
        network = ipaddress.IPv4Network(value, strict=False)
        need(network.prefixlen > 0, 'A /0 exception would defeat isolation')
        result.append(str(network))
    return [str(value) for value in ipaddress.collapse_addresses(ipaddress.IPv4Network(value) for value in result)]


def normalize(config):
    config = dict(config)
    config.setdefault('network_policy', 'isolated')
    config.setdefault('connection_audit', False)
    config.setdefault('management_cidrs', [])
    config.setdefault('lan_allow', [])
    config.setdefault('protected_cidrs', [])
    config.setdefault('dhcp_server', None)
    config.setdefault('isolation_gateway', None)
    config.setdefault('vlan', None)
    need(config['network_policy'] in {'isolated', 'lan', 'legacy'}, 'Unknown network policy')
    need(type(config['connection_audit']) is bool, 'Audit must be explicitly enabled/disabled')
    config['management_cidrs'] = cidrs(config['management_cidrs'])
    config['protected_cidrs'] = cidrs(config['protected_cidrs'])
    if config['vlan'] is not None:
        need(type(config['vlan']) is int and 1 <= config['vlan'] <= 4094, 'Invalid VLAN tag')
    for name in ['dhcp_server', 'isolation_gateway']:
        if config[name]:
            config[name] = ipv4(config[name])
    need(isinstance(config['lan_allow'], list) and len(config['lan_allow']) <= 128, 'Invalid LAN exceptions')
    exceptions = []
    for entry in config['lan_allow']:
        need(isinstance(entry, str), 'LAN exception: IPv4/CIDR,tcp|udp,port')
        fields = entry.split(',')
        need(len(fields) == 3 and fields[1] in {'tcp', 'udp'} and fields[2].isdigit(), 'LAN exception: IPv4/CIDR,tcp|udp,port')
        network = cidrs([fields[0]])[0]; port = int(fields[2])
        need(1 <= port <= 65535, 'Invalid LAN exception port')
        exceptions.append(f'{network},{fields[1]},{port}')
    config['lan_allow'] = sorted(set(exceptions))
    if managed(config):
        config['expected_ip'] = ipv4(config.get('expected_ip'))
        need(config['management_cidrs'], 'Isolation/audit requires --management-cidr for SSH administrators/jump host')
        need(config.get('nameserver'), 'Isolation/audit requires one explicit IPv4 --nameserver')
        config['nameserver'] = ipv4(config['nameserver'])
        need(config.get('gateway') or config['isolation_gateway'], 'Isolation/audit requires the IPv4 gateway (DHCP: --isolation-gateway)')
        if not config.get('address'):
            need(config['dhcp_server'], 'DHCP isolation/audit requires the reserved IP and --dhcp-server')
            need(config.get('mac'), 'DHCP isolation/audit requires --mac; reserve its expected IPv4 on the DHCP server before creation')
    return config


def managed(config):
    return config.get('network_policy') in {'isolated', 'lan'} or config.get('connection_audit', False)


def identity(state):
    vmid = state['vmid']; owner = state['owner']
    need(type(vmid) is int and 100 <= vmid <= 999999999 and isinstance(owner,str) and re.fullmatch('[0-9a-f]{32}', owner), 'Invalid network owner')
    return f'onepve_{vmid}_{owner[:12]}'


def preflight(config, states=()):
    """Read-only; finish before downloads, VM creation or destructive rebuild."""
    if not managed(config):
        return {'policy': config.get('network_policy', 'legacy'), 'managed': False}
    need(shutil.which('nft'), 'Install nftables on PVE before using isolation (apt install nftables); no host firewall backend change is needed')
    need(shutil.which('systemctl'), 'Network guard requires systemd')
    if config['connection_audit']:
        need(shutil.which('conntrack'), 'Optional audit requires conntrack on PVE (apt install conntrack)')
    run(['nft', '--json', 'list', 'tables'])
    protected = host_protected(config)
    for state in states:
        other = state.get('config') or {}
        if state.get('vmid') != config['vmid'] and state.get('phase') != 'REMOVED':
            need(other.get('expected_ip') != config['expected_ip'], 'Expected IPv4 is already reserved by another recorded VM')
    result={'policy': config['network_policy'], 'managed': True, 'ipv6': 'blocked',
            'protected_cidrs': protected, 'audit': config['connection_audit'],
            'first_boot': 'link_down until host rules and pre-start guard are verified'}
    probe=dict(config,mac=config.get('mac') or '52:54:00:00:00:01')
    run(['nft','-c','-f','-'],input=rules(dict(vmid=config['vmid'],owner='0'*32,config=probe,network=result)))
    return result


def host_protected(config):
    protected = list(PRIVATE) + config['protected_cidrs']
    routes = json.loads(run(['ip', '-j', '-4', 'route', 'show', 'table', 'all']).stdout)
    for route in routes:
        destination = route.get('dst')
        if destination and destination != 'default' and ipaddress.IPv4Network(destination, strict=False).prefixlen:
            protected.append(destination)
    addresses = json.loads(run(['ip', '-j', '-4', 'address', 'show']).stdout)
    for interface in addresses:
        for address in interface.get('addr_info', []):
            if address.get('local'):
                protected.append(address['local'] + '/32')
    return cidrs(protected)


def rules(state):
    config = state['config']; table = identity(state); tap = f'tap{state["vmid"]}i0'
    mac = config['mac'].lower()
    need(re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac), 'Network guard requires the actual VM MAC')
    address = ipv4(config['expected_ip']); dns = ipv4(config['nameserver'])
    management = cidrs(config['management_cidrs'])
    protected = cidrs(state['network']['protected_cidrs'])
    arp = cidrs([config.get('gateway') or config['isolation_gateway'], address, dns]
                + ([config['dhcp_server']] if config.get('dhcp_server') else []) + management
                + [item.split(',')[0] for item in config['lan_allow']])
    def group(values):
        return '{ ' + ', '.join(values) + ' }'
    out = [f'ether saddr != {mac} counter drop']
    if not config.get('address'):
        for source in ['0.0.0.0', address]:
            out.append(f'ip saddr {source} ip daddr {group([config["dhcp_server"], "255.255.255.255"])} udp sport 68 udp dport 67 counter accept')
    out += [f'arp saddr ether {mac} arp saddr ip {group([address, "0.0.0.0"])} arp daddr ip {group(arp)} counter accept',
            'ether type arp counter drop', f'ether type != ip counter drop',
            f'ip saddr != {address} counter drop', 'ct state invalid counter drop',
            f'ct direction reply ct state established ip daddr {group(management)} tcp sport 22 counter accept',
            f'ip daddr {dns} udp dport 53 counter accept', f'ip daddr {dns} tcp dport 53 counter accept']
    for entry in config['lan_allow']:
        destination, proto, port = entry.split(',')
        out.append(f'ip daddr {destination} {proto} dport {port} counter accept')
    if config['network_policy'] == 'isolated':
        out.append(f'ip daddr {group(protected)} counter drop')
    out += ['meta l4proto { tcp, udp } counter accept', 'counter drop']
    inbound = [f'arp daddr ip {address} counter accept']
    if not config.get('address'):
        inbound.append(f'ip saddr {config["dhcp_server"]} ip daddr {group([address, "255.255.255.255"])} udp sport 67 udp dport 68 counter accept')
    inbound += [f'ether type != ip counter drop', f'ip daddr != {address} counter drop',
                'ct state invalid counter drop', 'ct state { established, related } counter accept',
                f'ip saddr {group(management)} tcp dport 22 ct state new counter accept', 'counter drop']
    # bridge conntrack runs at -200; matching ct also loads its bridge hooks.
    return ('table bridge ' + table + ' {\n'
            + f' comment "onePVE owner={state["owner"]}"\n'
            + ' chain outbound {\n  ' + '\n  '.join(out) + '\n }\n'
            + ' chain inbound {\n  ' + '\n  '.join(inbound) + '\n }\n'
            + f' chain from_vm {{ type filter hook prerouting priority -150; policy accept; iifname "{tap}" jump outbound; }}\n'
            + f' chain to_vm {{ type filter hook postrouting priority 0; policy accept; oifname "{tap}" jump inbound; }}\n'
            + '}\n')


def _write(path, data, mode=0o600):
    path = Path(path)
    need(path.parent.resolve() == path.parent.absolute() and not path.is_symlink(), 'Redirected network guard path')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, mode); os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _paths(state):
    identity(state)
    return ROOT / (str(state['vmid']) + '-' + state['owner']), UNITS / f'onepve-network-{state["vmid"]}.service'


def _live(state):
    table = identity(state)
    result = subprocess.run(['nft', '--json', 'list', 'table', 'bridge', table], capture_output=True, text=True, timeout=10)
    if result.returncode:
        need('No such file' in result.stderr, 'Cannot inspect network table: ' + result.stderr[-300:])
        return None
    data = json.loads(result.stdout)
    tables = [x['table'] for x in data.get('nftables', []) if 'table' in x]
    need(len(tables) == 1 and tables[0].get('comment') == 'onePVE owner=' + state['owner'], 'Network table ownership differs')
    return data


def _signature(data):
    def clean(value):
        if isinstance(value,list):return [clean(item) for item in value]
        if isinstance(value,dict):
            return {key:({} if key=='counter' else clean(item)) for key,item in value.items() if key not in {'handle','metainfo'}}
        return value
    records=[item for item in data.get('nftables',[]) if 'metainfo' not in item]
    return hashlib.sha256(json.dumps(clean(records),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _applied(state):
    path=_paths(state)[0]/'applied.json'
    _write(path,json.dumps(dict(owner=state['owner'],ruleset_sha256=_signature(_live(state))))+'\n')


def apply(state):
    state['network']['protected_cidrs']=cidrs(state['network']['protected_cidrs']+host_protected(state['config']))
    content = rules(state); table = identity(state)
    transaction = (f'delete table bridge {table}\n' if _live(state) is not None else '') + content
    run(['nft', '-c', '-f', '-'], input=transaction)
    run(['nft', '-f', '-'], input=transaction)
    need(_live(state) is not None, 'Network table was not installed')
    return hashlib.sha256(content.encode()).hexdigest()


def install(state):
    """Called with the VM NIC disconnected. Publish an owned persistent boot guard."""
    directory, unit = _paths(state)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    need(directory.resolve() == directory.absolute() and directory.stat().st_uid == 0 and not directory.stat().st_mode & 0o077, 'Network state must be root private')
    source = Path(__file__).read_text()
    command = f'/usr/bin/python3 {directory}/guard.py apply --config {directory}/config.json'
    content = (f'# onePVE owner={state["owner"]}\n[Unit]\nDescription=onePVE VM {state["vmid"]} network guard\n'
               'Before=pve-guests.service\nAfter=local-fs.target pve-cluster.service\n[Service]\nType=oneshot\nRemainAfterExit=yes\n'
               f'ExecStart={command}\n[Install]\nWantedBy=multi-user.target\n')
    if unit.exists():
        need(not unit.is_symlink() and unit.read_text() == content, 'Existing network boot unit differs; retained')
    _write(directory/'guard.py', source)
    _write(directory/'config.json', json.dumps(state, indent=2) + '\n')
    _write(directory/'rules.nft', rules(state))
    _write(unit, content, 0o644)
    run(['systemctl', 'daemon-reload']); run(['systemctl', 'enable', unit.name])
    digest = apply(state)
    _applied(state)
    return {'directory': str(directory), 'unit': unit.name, 'sha256': digest, 'table': identity(state)}


def hook(state):
    directory, _ = _paths(state)
    return (f"import subprocess\nsubprocess.run(['/usr/bin/python3', {str(directory/'guard.py')!r}, 'apply', '--config', {str(directory/'config.json')!r}], check=True, timeout=45)\n")


def validate_vm_nic(state, config):
    need(not any(re.fullmatch(r'net[1-9]\d*',key) for key in config), 'Unmanaged extra VM NIC; operation refused')
    need('net0' in config, 'VM NIC is missing; operation refused')
    fields=[item.split('=',1) for item in config['net0'].split(',')]
    need(all(len(item)==2 for item in fields) and len({item[0] for item in fields})==len(fields),'Invalid/duplicate NIC options')
    nic=dict(fields);expected=state['config']
    need(nic.get('virtio','').lower()==expected['mac'].lower(), 'VM MAC differs; operation refused')
    need(nic.get('bridge')==expected['bridge'], 'VM bridge differs; operation refused')
    expected_tag=str(expected['vlan']) if expected.get('vlan') else None
    need(nic.get('tag')==expected_tag, 'VM VLAN differs; operation refused')
    need(not nic.get('trunks'), 'Unmanaged VLAN trunks; operation refused')


def status(state):
    if not managed(state.get('config', {})):
        return {'policy': state.get('config', {}).get('network_policy', 'legacy'), 'managed': False}
    directory, unit = _paths(state)
    live = _live(state)
    baseline=directory/'applied.json';expected=None
    if baseline.is_file():
        need(not baseline.is_symlink() and baseline.stat().st_uid==0 and not baseline.stat().st_mode&0o077,'Unprotected network baseline')
        recorded=json.loads(baseline.read_text());need(recorded.get('owner')==state['owner'],'Network baseline owner differs')
        expected=recorded.get('ruleset_sha256')
    counters = [x['rule'].get('expr', []) for x in (live or {}).get('nftables', []) if 'rule' in x]
    dropped = sum(e['counter'].get('packets', 0) for expressions in counters
                  if any('drop' in e for e in expressions) for e in expressions if 'counter' in e)
    return {'policy': state['config']['network_policy'], 'managed': True, 'table_present': live is not None,
            'rules_match':live is not None and expected is not None and _signature(live)==expected,
            'boot_unit_present': unit.is_file(), 'config_present': (directory/'config.json').is_file(),
            'drop_packets_since_rule_load': dropped, 'ipv6': 'blocked', 'audit_enabled': state['config']['connection_audit']}


def remove(state):
    directory, unit = _paths(state)
    if unit.exists():
        need(not unit.is_symlink() and unit.read_text().startswith('# onePVE owner=' + state['owner'] + '\n'), 'Foreign network unit retained')
        run(['systemctl', 'disable', '--now', unit.name]); unit.unlink(); run(['systemctl', 'daemon-reload'])
    if _live(state) is not None:
        run(['nft', 'delete', 'table', 'bridge', identity(state)])
    # Logs and configuration remain as recovery evidence; nothing outside this owner is removed.
    return {'removed': True, 'retained': str(directory)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['apply']); parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    need(os.geteuid() == 0, 'Network guard requires root')
    need(not args.config.is_symlink() and args.config.stat().st_uid == 0 and not args.config.stat().st_mode & 0o077, 'Network config is not root private')
    state = json.loads(args.config.read_text())
    need(args.config==_paths(state)[0]/'config.json','Unexpected network config location')
    config = Path('/etc/pve/qemu-server') / (str(int(state['vmid'])) + '.conf')
    text = config.read_text()
    need('onePVE owner=' + state['owner'] in text, 'VM ownership differs; network rules were not changed')
    validate_vm_nic(state,dict(line.split(': ',1) for line in text.splitlines() if ': ' in line))
    apply(state)
    _applied(state)


if __name__ == '__main__':
    main()

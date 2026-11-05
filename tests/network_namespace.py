"""Real packet checks, run only inside an explicitly isolated Linux network namespace."""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import onepve_network as network

HERE = str(Path(__file__).resolve())
MAC = '02:00:00:00:01:03'


def call(*args, **kwargs):
    return subprocess.run(list(args), check=True, capture_output=True, text=True, timeout=15, **kwargs)


def namespace_command(pid, *args):
    return ['nsenter', '-t', str(pid), '-n', '--', *args]


def serve(name):
    def worker(proto, port, host):
        sock = socket.socket(socket.AF_INET, proto)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        if proto == socket.SOCK_DGRAM:sock.setsockopt(socket.IPPROTO_IP, 8, 1)  # Linux IP_PKTINFO
        if proto == socket.SOCK_STREAM:
            sock.listen()
        ready.append(sock)
        while True:
            if proto == socket.SOCK_DGRAM:
                data, ancillary, _, peer = sock.recvmsg(2048, 128)
                for level, kind, value in ancillary:
                    if level == socket.IPPROTO_IP and kind == 8:
                        interface, _, destination = struct.unpack('=I4s4s', value)
                        source = socket.inet_aton('10.7.0.53') if port == 67 else destination
                        ancillary = [(level, kind, struct.pack('=I4s4s', interface, source, bytes(4)))]
                sock.sendmsg([data], ancillary, 0, peer)
            else:
                connection, _ = sock.accept()
                with connection:
                    data = connection.recv(2048)
                    if data:connection.sendall(data)
    ready = []
    for proto in [socket.SOCK_STREAM, socket.SOCK_DGRAM]:
        for port in [22, 53, 24443, 24444]:
            threading.Thread(target=worker, args=(proto, port, '0.0.0.0'), daemon=True).start()
    if name == 'private':threading.Thread(target=worker, args=(socket.SOCK_DGRAM, 67, '0.0.0.0'), daemon=True).start()
    deadline = time.monotonic() + 5
    while len(ready) < 8 + (name == 'private'):
        if time.monotonic() > deadline:raise RuntimeError('Server startup failed')
        time.sleep(.01)
    print('ready', flush=True); threading.Event().wait()


def client(proto, host, port, broadcast=False):
    kind = socket.SOCK_STREAM if proto == 'tcp' else socket.SOCK_DGRAM
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.settimeout(.7)
        if broadcast:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1); sock.bind(('0.0.0.0', 68))
        data = ('onepve-' + uuid.uuid4().hex).encode()
        try:
            if kind == socket.SOCK_STREAM:
                sock.connect((host, int(port))); sock.sendall(data); reply = sock.recv(2048)
            else:
                sock.sendto(data, (host, int(port))); reply, _ = sock.recvfrom(2048)
            return reply == data
        except OSError:
            return False


def packet(mac, destination_mac, source, destination, marker, ipv6=False, arp=False, sport=30444, dport=53):
    def hardware(value):return bytes.fromhex(value.replace(':', ''))
    ethernet = hardware(destination_mac) + hardware(mac)
    if arp:
        return ethernet + b'\x08\x06' + struct.pack('!HHBBH', 1, 0x0800, 6, 4, 1) + hardware(mac) + socket.inet_aton(source) + bytes(6) + socket.inet_aton(destination) + marker
    udp = struct.pack('!HHHH', sport, dport, len(marker) + 8, 0) + marker
    if ipv6:
        header = struct.pack('!IHBB', 6 << 28, len(udp), 17, 64) + socket.inet_pton(socket.AF_INET6, source) + socket.inet_pton(socket.AF_INET6, destination)
        return ethernet + b'\x86\xdd' + header + udp
    header = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0, socket.inet_aton(source), socket.inet_aton(destination))
    value = sum(struct.unpack('!10H', header)); value = (value & 65535) + (value >> 16); value = (value & 65535) + (value >> 16)
    header = header[:10] + struct.pack('!H', (~value) & 65535) + header[12:]
    return ethernet + b'\x08\x00' + header + udp


def sniff(marker):
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)); sock.bind(('eth0', 0)); sock.settimeout(1)
    print('ready', flush=True)
    try:
        while marker.encode() not in sock.recv(65535):pass
        return True
    except TimeoutError:return False


def emit(encoded):
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW) as sock:
        sock.bind(('eth0', 0)); sock.send(bytes.fromhex(encoded))


def run_checks():
    if os.environ.get('ONEPVE_TEST_NETNS') != '1' or os.geteuid() != 0:
        raise RuntimeError('Requires explicit root network-namespace test launcher')
    if os.readlink('/proc/self/ns/net') == os.readlink('/proc/1/ns/net'):
        raise RuntimeError('Refusing to create interfaces in the initial network namespace')
    children = []; nodes = {}; assertions = []
    def proc(args):
        result = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(result)
        if result.stdout.readline().strip() != 'ready':raise RuntimeError('Child failed: ' + result.stderr.read())
        return result
    def node(name, host, tap, mac):
        held = proc(['unshare', '--net', '--', sys.executable, HERE, 'hold'])
        nodes[name] = held.pid
        call('ip', 'link', 'add', tap, 'type', 'veth', 'peer', 'name', 'peer-' + name)
        call('ip', 'link', 'set', 'peer-' + name, 'netns', str(held.pid))
        call('ip', 'link', 'set', tap, 'master', 'vmbr0'); call('ip', 'link', 'set', tap, 'up')
        prefix = namespace_command(held.pid, 'ip')
        call(*prefix, 'link', 'set', 'peer-' + name, 'name', 'eth0')
        call(*prefix, 'link', 'set', 'lo', 'up'); call(*prefix, 'link', 'set', 'eth0', 'address', mac)
        call(*prefix, 'link', 'set', 'eth0', 'up'); call(*prefix, 'address', 'add', host + '/24', 'dev', 'eth0')
        if name != 'public':call(*prefix, 'route', 'add', 'default', 'via', '10.7.0.1')
        return held.pid
    def check(value, message):
        if not value:raise AssertionError(message)
        assertions.append(message)
    def connects(name, proto, host, port, broadcast=False):
        result = call(*namespace_command(nodes[name], sys.executable, HERE, 'client', proto, host, str(port), str(int(broadcast))))
        return result.stdout.strip() == 'yes'
    def observed(source, destination, frame, marker):
        listener = proc(namespace_command(nodes[destination], sys.executable, HERE, 'sniff', marker))
        call(*namespace_command(nodes[source], sys.executable, HERE, 'emit', frame.hex()))
        stdout, stderr = listener.communicate(timeout=3)
        if listener.returncode:raise RuntimeError(stderr)
        return stdout.strip() == 'yes'
    try:
        call('ip', 'link', 'add', 'vmbr0', 'type', 'bridge'); call('ip', 'link', 'set', 'vmbr0', 'up')
        call('ip', 'address', 'add', '10.7.0.254/24', 'dev', 'vmbr0')
        call('ip', 'address', 'add', '9.9.9.9/32', 'dev', 'vmbr0')
        call('ip', 'route', 'add', '11.22.33.0/24', 'dev', 'vmbr0')
        node('guest', '10.7.0.103', 'tap103i0', MAC)
        node('management', '10.7.0.2', 'mgmt0', '02:00:00:00:00:02')
        node('private', '10.7.0.9', 'private0', '02:00:00:00:00:09')
        public = node('public', '10.7.0.1', 'public0', '02:00:00:00:00:01')
        node('other', '10.7.0.104', 'tap104i0', '02:00:00:00:01:04')
        call(*namespace_command(public, 'ip', 'address', 'add', '8.8.8.8/32', 'dev', 'eth0'))
        call(*namespace_command(nodes['private'], 'ip', 'address', 'add', '10.7.0.53/32', 'dev', 'eth0'))
        call(*namespace_command(nodes['private'], 'ip', 'address', 'add', '11.22.33.9/32', 'dev', 'eth0'))
        bridge_mac = json.loads(call('ip', '-j', 'link', 'show', 'vmbr0').stdout)[0]['address']
        for name in ['guest', 'other']:
            for destination, mac in [('9.9.9.9', bridge_mac), ('11.22.33.9', '02:00:00:00:00:09')]:
                call(*namespace_command(nodes[name], 'ip', 'route', 'add', destination + '/32', 'dev', 'eth0'))
                call(*namespace_command(nodes[name], 'ip', 'neigh', 'replace', destination, 'lladdr', mac, 'nud', 'permanent', 'dev', 'eth0'))
        state = {'vmid':103, 'owner':'a' * 32, 'config':network.normalize({
            'network_policy':'isolated', 'connection_audit':False, 'address':None,
            'expected_ip':'10.7.0.103', 'isolation_gateway':'10.7.0.1', 'dhcp_server':'10.7.0.53',
            'nameserver':'10.7.0.53', 'management_cidrs':['10.7.0.2/32'],
            'lan_allow':['10.7.0.9/32,tcp,24443', '10.7.0.9/32,udp,24443'], 'mac':MAC}),
            'network':{'protected_cidrs':network.PRIVATE + ['10.7.0.254/32']}}
        digest = network.apply(state)
        check(len(digest) == 64, 'nft transaction installs and reports digest')
        for name in nodes:proc(namespace_command(nodes[name], sys.executable, HERE, 'serve', name))
        proc([sys.executable, HERE, 'serve', 'host'])
        for proto in ['tcp', 'udp']:
            check(connects('guest', proto, '8.8.8.8', 24444), 'public ' + proto + ' allowed')
            check(not connects('guest', proto, '10.7.0.9', 24444), 'private ' + proto + ' blocked')
            check(connects('guest', proto, '10.7.0.9', 24443), 'exact whitelist ' + proto + ' allowed')
            check(connects('guest', proto, '10.7.0.53', 53), 'DNS ' + proto + ' allowed')
            check(not connects('guest', proto, '10.7.0.53', 24444), 'DNS host other ' + proto + ' port blocked')
            check(connects('other', proto, '10.7.0.9', 24444), 'unmanaged VM ' + proto + ' unaffected')
            for destination in ['9.9.9.9', '11.22.33.9']:
                check(not connects('guest', proto, destination, 24444), 'host address/route ' + destination + ' ' + proto + ' blocked')
                check(connects('other', proto, destination, 24444), 'other VM host address/route ' + destination + ' ' + proto + ' unaffected')
        check(connects('management', 'tcp', '10.7.0.103', 22), 'management SSH request and reply allowed')
        check(not connects('management', 'tcp', '10.7.0.103', 24444), 'other inbound connection blocked')
        check(not connects('guest', 'tcp', '10.7.0.2', 22), 'guest cannot initiate management SSH')
        check(connects('guest', 'udp', '255.255.255.255', 67, True), 'DHCP broadcast and authorized response allowed')
        for label, mac, source, destination, ipv6, arp in [
            ('valid raw DNS', MAC, '10.7.0.103', '10.7.0.53', False, False),
            ('forged source IP', MAC, '10.7.0.104', '10.7.0.53', False, False),
            ('forged source MAC', '02:00:00:00:01:04', '10.7.0.103', '10.7.0.53', False, False),
            ('IPv6', MAC, 'fd00::103', 'fd00::53', True, False),
            ('ARP permitted target', MAC, '10.7.0.103', '10.7.0.53', False, True),
            ('ARP forged sender', MAC, '10.7.0.1', '10.7.0.53', False, True),
            ('ARP other target', MAC, '10.7.0.103', '10.7.0.88', False, True)]:
            marker = uuid.uuid4().hex
            frame = packet(mac, '02:00:00:00:00:09', source, destination, marker.encode(), ipv6, arp)
            expected = label in {'valid raw DNS', 'ARP permitted target'}
            check(observed('guest', 'private', frame, marker) == expected, label + (' allowed' if expected else ' blocked'))
        marker = uuid.uuid4().hex
        frame = packet(MAC, 'ff:ff:ff:ff:ff:ff', '0.0.0.0', '255.255.255.255', marker.encode(), sport=68, dport=67)
        check(observed('guest', 'private', frame, marker), 'initial DHCP from zero address allowed')
        for source in ['10.7.0.53', '10.7.0.9']:
            marker = uuid.uuid4().hex
            frame = packet('02:00:00:00:00:09', MAC, source, '10.7.0.103', marker.encode(), sport=67, dport=68)
            check(observed('private', 'guest', frame, marker) == (source == '10.7.0.53'), 'DHCP response identity ' + source)
        marker = uuid.uuid4().hex
        frame = packet('02:00:00:00:01:04', '02:00:00:00:00:09', 'fd00::104', 'fd00::53', marker.encode(), ipv6=True)
        check(observed('other', 'private', frame, marker), 'other VM IPv6 unaffected')
        for kind in ['other L2', 'nested VLAN']:
            marker = uuid.uuid4().hex
            frame = packet(MAC, '02:00:00:00:00:09', '10.7.0.103', '10.7.0.53', marker.encode())
            frame = frame[:12] + (b'\x88\xb5' + frame[14:] if kind == 'other L2' else b'\x81\x00\x00\x0c' + frame[12:])
            check(not observed('guest', 'private', frame, marker), kind + ' blocked')
        network.apply(state)
        check(connects('guest', 'tcp', '8.8.8.8', 24444), 'atomic repeated apply retains connectivity')
        return assertions
    finally:
        for child in reversed(children):
            if child.poll() is None:child.terminate()
        for child in children:
            try:child.communicate(timeout=2)
            except subprocess.TimeoutExpired:child.kill(); child.communicate()


if __name__ == '__main__':
    action = sys.argv[1] if len(sys.argv) > 1 else 'run'
    if action == 'hold':print('ready', flush=True); threading.Event().wait()
    elif action == 'serve':serve(sys.argv[2])
    elif action == 'client':print('yes' if client(sys.argv[2], sys.argv[3], sys.argv[4], bool(int(sys.argv[5]))) else 'no')
    elif action == 'sniff':print('yes' if sniff(sys.argv[2]) else 'no')
    elif action == 'emit':emit(sys.argv[2])
    elif action == 'run':
        try:print(json.dumps({'checks':run_checks()}, indent=2))
        except subprocess.CalledProcessError as error:
            print(error.stderr, file=sys.stderr); raise
    else:raise RuntimeError('Unknown test action')

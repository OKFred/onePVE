"""Run against Debian's real websockify, never an existing desktop/service."""
import base64
import importlib.util
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def worker():
    module_path, web, port, backend_port = sys.argv[2:]
    sys.path.insert(0, str(Path(module_path).parent))
    import novnc_proxy
    proxy = novnc_proxy.make_proxy(16080)
    proxy.listen_port = int(port); proxy.target_port = int(backend_port); proxy.web = web
    proxy.start_server()


def free_port():
    with socket.socket() as value:
        value.bind(('127.0.0.1', 0))
        return value.getsockname()[1]


@unittest.skipUnless(sys.platform == 'linux' and importlib.util.find_spec('websockify'), 'Debian websockify integration')
class RealViewerBoundaryTests(unittest.TestCase):
    def test_http_and_websocket_boundaries_before_backend(self):
        accepted = []
        class FakeVnc(socketserver.BaseRequestHandler):
            def handle(self):
                accepted.append(True)
                self.request.sendall(b'RFB 003.008\n')
                self.request.settimeout(2)
                try:
                    self.request.recv(1024)
                except OSError:
                    pass
        backend = socketserver.ThreadingTCPServer(('127.0.0.1', 0), FakeVnc)
        backend.daemon_threads = True
        thread = threading.Thread(target=backend.serve_forever, daemon=True); thread.start()
        proxy = None
        try:
            with tempfile.TemporaryDirectory(prefix='onepve-proxy-test-') as directory:
                web = Path(directory); (web / 'vnc.html').write_text('noVNC synthetic viewer')
                port = free_port()
                proxy = subprocess.Popen([sys.executable, __file__, '--proxy-worker',
                    str(ROOT / 'guest/payload/share/novnc_proxy.py'), str(web), str(port),
                    str(backend.server_address[1])], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                end = time.monotonic() + 10
                while True:
                    if proxy.poll() is not None:
                        self.fail('Synthetic proxy exited before readiness')
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.2):
                            break
                    except OSError:
                        if time.monotonic() > end:
                            self.fail('Synthetic proxy did not listen')
                        time.sleep(.1)
                def request(hosts, origins=(), websocket=False, method='GET', target='/vnc.html'):
                    with socket.create_connection(('127.0.0.1', port), timeout=3) as client:
                        lines = [f'{method} {target} HTTP/1.1'] + ['Host: ' + value for value in hosts]
                        if websocket:
                            lines += ['Upgrade: websocket', 'Connection: Upgrade', 'Sec-WebSocket-Version: 13',
                                      'Sec-WebSocket-Key: ' + base64.b64encode(os.urandom(16)).decode(), 'Sec-WebSocket-Protocol: binary']
                        else:
                            lines += ['Connection: close']
                        lines += ['Origin: ' + value for value in origins]
                        client.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
                        response = b''
                        while b'\r\n\r\n' not in response:
                            block = client.recv(4096)
                            if not block:
                                break
                            response += block
                        return response.split(b'\r\n\r\n', 1)[0]
                for method in ['GET', 'HEAD']:
                    header = request(['127.0.0.1:16080'], method=method)
                    self.assertIn(b' 200 ', header)
                    self.assertIn(b'X-Frame-Options: DENY', header)
                    self.assertIn(b"Content-Security-Policy: frame-ancestors 'none'", header)
                self.assertIn(b' 404 ', request(['127.0.0.1:16080'], target='/'))
                for hosts in [[], ['evil.test:16080'], ['127.0.0.1:16080', '127.0.0.1:16080']]:
                    for websocket in [False, True]:
                        self.assertIn(b' 403 ', request(hosts, ['http://127.0.0.1:16080'], websocket))
                for origins in [[], ['null'], ['http://evil.test'], ['http://localhost:6080'],
                                ['http://localhost:16080.evil'], ['http://localhost:16080', 'http://localhost:16080']]:
                    self.assertIn(b' 403 ', request(['127.0.0.1:16080'], origins, True))
                self.assertEqual(accepted, [], 'Rejected browser traffic must not touch the VNC backend')
                for host in ['127.0.0.1:16080', 'localhost:16080']:
                    self.assertIn(b' 101 ', request([host], ['http://' + host], True))
                end = time.monotonic() + 2
                while len(accepted) < 2 and time.monotonic() < end:
                    time.sleep(.05)
                self.assertEqual(len(accepted), 2)
        finally:
            if proxy and proxy.poll() is None:
                # This is the owned synthetic test process group, not any VM service.
                os.killpg(proxy.pid, 15)
                try:
                    proxy.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proxy.pid, 9); proxy.wait(timeout=3)
            backend.shutdown(); backend.server_close(); thread.join(timeout=3)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--proxy-worker':
        worker()
    else:
        unittest.main()

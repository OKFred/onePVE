"""Small shared PVE and QEMU Guest Agent primitives; import has no side effects."""
import base64
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

STATE_ROOT = Path('/var/lib/onepve')
_progress = None


class _Progress:
    def __init__(self, stream):
        self.stream = stream
        self.started = self.stage_started = time.monotonic()
        self.stage = self.detail = ''
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

    def _write(self, waiting=False):
        if not self.stage or self.stopped.is_set():
            return
        now = time.monotonic()
        elapsed = max(0, int(now - self.started))
        stage_elapsed = max(0, int(now - self.stage_started))
        message = '【进度】%s | 本次已用时 %02d:%02d | 本阶段 %02d:%02d' % (
            self.stage, elapsed // 60, elapsed % 60, stage_elapsed // 60, stage_elapsed % 60)
        if waiting:
            message += ' | 等待中'
        if self.detail:
            message += ' | ' + self.detail
        try:
            print(message, file=self.stream, flush=True)
        except (OSError, ValueError):
            self.stopped.set()

    def update(self, stage, detail=''):
        with self.lock:
            if (stage, detail) == (self.stage, self.detail):
                return
            if stage != self.stage:
                self.stage_started = time.monotonic()
            self.stage, self.detail = stage, detail
            self._write()

    def _heartbeat(self):
        while not self.stopped.wait(30):
            with self.lock:
                self._write(waiting=True)

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)


@contextlib.contextmanager
def progress_session():
    """Keep JSON on stdout and enable human progress only on a terminal."""
    global _progress
    try:
        terminal = sys.stderr is not None and sys.stderr.isatty()
    except (OSError, ValueError):
        terminal = False
    if _progress is not None or not terminal:
        yield
        return
    reporter = _Progress(sys.stderr)
    _progress = reporter
    try:
        yield
    finally:
        reporter.close()
        _progress = None


def progress(stage, detail=''):
    if _progress is not None:
        _progress.update(stage, detail)


def repo_root():
    return Path(__file__).resolve().parents[1]


def require_root():
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise RuntimeError('Run this action as root on the PVE host')


def run(argv, check=True, timeout=60, **kwargs):
    result = subprocess.run([str(x) for x in argv], capture_output=True, text=True,
                            timeout=timeout, **kwargs)
    if check and result.returncode:
        raise RuntimeError('Command failed: ' + str(argv[0]) + ': ' + result.stderr.strip()[-1600:])
    return result


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('Refusing symlink state: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.resolve() != path.parent.absolute():
        raise RuntimeError('Refusing redirected state directory')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def node_name():
    return socket.gethostname().split('.')[0]


def pve_get(path, *args):
    return json.loads(run(['pvesh', 'get', path, *args, '--output-format', 'json']).stdout)


def vm_config(vmid):
    text = run(['qm', 'config', str(vmid)]).stdout
    return dict(line.split(': ', 1) for line in text.splitlines() if ': ' in line)


class Qga:
    def __init__(self, vmid):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(20)
        self.socket.connect('/var/run/qemu-server/' + str(int(vmid)) + '.qga')
        self.stream = self.socket.makefile('rwb', buffering=0)
        self.sequence = 0
        token = int(time.time() * 1000000)
        self.stream.write(b'\xff' + json.dumps({'execute': 'guest-sync-delimited',
                          'arguments': {'id': token}}).encode() + b'\n')
        while self._read().get('return') != token:
            pass

    def _read(self):
        while True:
            data = self.stream.readline()
            if not data:
                raise ConnectionError('Guest Agent disconnected')
            try:
                return json.loads(data.rsplit(b'\xff', 1)[-1])
            except ValueError:
                continue

    def call(self, name, arguments=None):
        self.sequence += 1
        request = {'execute': name, 'id': self.sequence}
        if arguments is not None:
            request['arguments'] = arguments
        self.stream.write(json.dumps(request).encode() + b'\n')
        while True:
            response = self._read()
            if response.get('id', self.sequence) != self.sequence:
                continue
            if 'error' in response:
                raise RuntimeError('Guest Agent: ' + str(response['error']))
            return response['return']

    def execute(self, argv, timeout=60):
        pid = self.call('guest-exec', {'path': str(argv[0]), 'arg': [str(x) for x in argv[1:]],
                                     'capture-output': True})['pid']
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.call('guest-exec-status', {'pid': pid})
            if status.get('exited'):
                return {'returncode': status.get('exitcode', -1),
                        'stdout': base64.b64decode(status.get('out-data', '')).decode(errors='replace'),
                        'stderr': base64.b64decode(status.get('err-data', '')).decode(errors='replace')}
            time.sleep(.25)
        raise TimeoutError('Guest command outcome uncertain; inspect before retrying, PID ' + str(pid))

    def read(self, path, max_bytes=None):
        handle = self.call('guest-file-open', {'path': str(path), 'mode': 'rb'})
        blocks, size = [], 0
        try:
            while True:
                item = self.call('guest-file-read', {'handle': handle, 'count': 65536})
                block = base64.b64decode(item.get('buf-b64', ''))
                size += len(block)
                if max_bytes is not None and size > max_bytes:
                    raise RuntimeError('Guest file exceeds the read size limit: ' + str(path))
                blocks.append(block)
                if item.get('eof') or not item.get('count'):
                    return b''.join(blocks)
        finally:
            self.call('guest-file-close', {'handle': handle})

    def write(self, path, data):
        handle = self.call('guest-file-open', {'path': str(path), 'mode': 'wb'})
        try:
            for offset in range(0, len(data), 48000):
                block = data[offset:offset + 48000]
                result = self.call('guest-file-write', {'handle': handle,
                                  'buf-b64': base64.b64encode(block).decode(), 'count': len(block)})
                if result.get('count') != len(block):
                    raise IOError('Incomplete Guest Agent file write')
        finally:
            self.call('guest-file-close', {'handle': handle})

    def close(self):
        self.stream.close(); self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

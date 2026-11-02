import ast
import email.message
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if 'pwd' not in sys.modules:
    try:
        import pwd
    except ImportError:
        sys.modules['pwd'] = types.ModuleType('pwd')
installer = load('guest_installer', ROOT / 'guest/install.py')


class ManifestTests(unittest.TestCase):
    def test_32_gib_disk_allows_partition_overhead_with_31_gib_filesystem_floor(self):
        with self.assertRaisesRegex(RuntimeError, '>=32 GiB disk'):
            installer.require_disk_capacity(31 * 1024**3 - 1)
        self.assertIsNone(installer.require_disk_capacity(31 * 1024**3))
        self.assertIsNone(installer.require_disk_capacity(int(31.9 * 1024**3)))
        self.assertIsNone(installer.require_disk_capacity(32 * 1024**3))

    def read(self, value):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            path.write_text(json.dumps(value))
            return installer.manifest(path)

    def test_defaults_and_explicit_pins(self):
        values = self.read({})
        self.assertEqual(values['node_version'], '26.13.0')
        self.assertEqual(values['codex_version'], '0.166.0')
        self.assertEqual(self.read({'viewer_port': 16090})['viewer_port'], 16090)

    def test_untrusted_config_rejected(self):
        for value in [{'user': 'root'}, {'user': '../root'}, {'node_version': '26; reboot'},
                      {'node_version': 'latest'}, {'mcp_version': '0.0.83\nnode=x'},
                      {'fnm_version': 'v1.39.0; id'}, {'viewer_port': True}, {'viewer_port': 80},
                      {'start_desktop': 'false'}, {'gpu_bdf': '/dev/dri/renderD128'},
                      {'fnm_sha256': 'x'*64}, {'unknown': 'value'}]:
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                self.read(value)

    def test_profile_seed_is_before_first_launch_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(installer.os, 'chown', create=True):
            home = Path(directory)
            installer.seed_profile(home, (1000, 1000))
            prefs = home / '.local/share/onepve-agent/chromium/Default/Preferences'
            keyring = home / '.local/share/keyrings/login.keyring'
            self.assertEqual(json.loads(prefs.read_text())['session']['restore_on_startup'], 1)
            self.assertTrue(keyring.read_text().startswith('[keyring]\n'))
            prefs.write_text('{"user-changed":true}')
            keyring.write_text('existing credential bytes must stay untouched')
            installer.seed_profile(home, (1000, 1000))
            self.assertEqual(prefs.read_text(), '{"user-changed":true}')
            self.assertEqual(keyring.read_text(), 'existing credential bytes must stay untouched')

    def test_existing_other_keyring_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(installer.os, 'chown', create=True):
            home = Path(directory); keyrings = home / '.local/share/keyrings'
            keyrings.mkdir(parents=True); (keyrings / 'custom.keyring').write_text('original')
            installer.seed_profile(home, (1000, 1000))
            self.assertEqual(sorted(p.name for p in keyrings.iterdir()), ['custom.keyring'])

    def test_cached_runtime_exact_versions_hashes_and_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {**installer.DEFAULTS, 'cache_dir': str(root.resolve())}
            contents = {'runtime/fnm.zip': b'archive', 'runtime/node.tar.xz': b'node',
                        'npm/package.json': b'{}', 'npm/package-lock.json': b'{}',
                        'npm/node_modules/@playwright/mcp/package.json': b'{"version":"0.0.83"}',
                        'npm/node_modules/@openai/codex/package.json': b'{"version":"0.166.0"}'}
            for name, body in contents.items():
                file = root / name; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(body)
            metadata = {key: values[key] for key in ['node_version', 'fnm_version', 'mcp_version', 'codex_version']}
            metadata['files'] = {name: hashlib.sha256(body).hexdigest() for name, body in contents.items()}
            record = root / 'runtime-manifest.json'; record.write_text(json.dumps(metadata))
            self.assertEqual(installer.runtime_cache(values), root.resolve())
            metadata['symlinks'] = {'npm/node_modules/.bin/codex': '../@openai/codex/bin/codex.js'}
            record.write_text(json.dumps(metadata))
            with self.assertRaisesRegex(RuntimeError, 'symlinks differ'):
                installer.runtime_cache(values)
            metadata['symlinks'] = {}; record.write_text(json.dumps(metadata))
            (root / 'runtime/node.tar.xz').write_bytes(b'changed')
            with self.assertRaisesRegex(RuntimeError, 'hash differs'):
                installer.runtime_cache(values)
            (root / 'runtime/node.tar.xz').write_bytes(contents['runtime/node.tar.xz'])
            metadata['node_version'] = '26.14.0'; record.write_text(json.dumps(metadata))
            with self.assertRaisesRegex(RuntimeError, 'pin differs'):
                installer.runtime_cache(values)
            metadata['node_version'] = values['node_version']; metadata['files']['missing'] = '0'*64
            record.write_text(json.dumps(metadata))
            with self.assertRaisesRegex(RuntimeError, 'missing files'):
                installer.runtime_cache(values)


class FakeOriginError(Exception):
    pass


class KeyringStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = load('guest_keyring_start', ROOT / 'guest/payload/share/keyring_start.py')

    def test_empty_keyring_unlock_reads_only_alias_and_locked_metadata(self):
        collection = '/org/freedesktop/secrets/collection/login'
        results = [types.SimpleNamespace(stdout='o "' + collection + '"'),
                   types.SimpleNamespace(stdout='aoo 1 "' + collection + '" "/"'),
                   types.SimpleNamespace(stdout='b false')]
        with patch.object(self.helper.subprocess, 'run', side_effect=results) as command:
            result = self.helper.unlock_default()
            self.assertFalse(result['prompt_created'])
            self.assertEqual(result['read_secret_calls'], 0)
            issued = [call.args[0] for call in command.call_args_list]
            self.assertEqual(len(issued), 3)
            self.assertTrue(all(not {'GetSecrets', 'GetSecret', 'Prompt'} & set(args) for args in issued))

    def test_nonempty_keyring_prompt_is_never_invoked(self):
        collection = '/org/freedesktop/secrets/collection/login'
        results = [types.SimpleNamespace(stdout='o "' + collection + '"'),
                   types.SimpleNamespace(stdout='aoo 0 "/org/freedesktop/secrets/prompt/p1"')]
        with patch.object(self.helper.subprocess, 'run', side_effect=results) as command:
            with self.assertRaisesRegex(RuntimeError, 'interactive authorization'):
                self.helper.unlock_default()
            self.assertEqual(command.call_count, 2)


class FakeExpectOrigin:
    def __init__(self, source):
        self.allowed = source.split()

    def authenticate(self, headers, *_):
        if headers.get('Origin') not in self.allowed:
            raise FakeOriginError()


class FakeHandler:
    def parse_request(self):
        return True

    def handle_upgrade(self):
        try:
            self.server.auth_plugin.authenticate(self.headers, '127.0.0.1', 5900)
        except FakeOriginError:
            self.send_error(403, 'Origin rejected')
            return
        self.backend_connected = True

    def end_headers(self):
        self.ended = True

    def send_header(self, key, value):
        self.response_headers[key] = value

    def send_error(self, code, message):
        self.error = code


class FakeProxy:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def proxy_module():
    proxy = types.ModuleType('websockify.websocketproxy')
    proxy.ProxyRequestHandler = FakeHandler; proxy.WebSocketProxy = FakeProxy
    auth = types.ModuleType('websockify.auth_plugins'); auth.ExpectOrigin = FakeExpectOrigin
    with patch.dict(sys.modules, {'websockify': types.ModuleType('websockify'),
                                'websockify.websocketproxy': proxy, 'websockify.auth_plugins': auth}):
        return load('guest_proxy', ROOT / 'guest/payload/share/novnc_proxy.py')


class ViewerBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proxy = proxy_module()

    def handler(self, hosts, origins):
        result = object.__new__(self.proxy.LocalViewerHandler)
        result.server = self.proxy.make_proxy(16080)
        result.headers = email.message.Message()
        for host in hosts:
            result.headers['Host'] = host
        for origin in origins:
            result.headers['Origin'] = origin
        result.error = None; result.backend_connected = False; result.response_headers = {}
        return result

    def test_fixed_private_backend_and_no_http_origin_requirement(self):
        for host in ['127.0.0.1:16080', 'localhost:16080', 'LOCALHOST:16080']:
            handler = self.handler([host], [])
            self.assertTrue(handler.parse_request())
            self.assertFalse(handler.server.web_auth)
            self.assertEqual(handler.server.listen_host, '127.0.0.1')
            self.assertEqual(handler.server.target_host, '127.0.0.1')
            self.assertEqual(handler.server.target_port, 5900)
            self.assertTrue(handler.server.file_only)

    def test_bad_missing_duplicate_host_rejected_before_backend(self):
        for hosts in [[], ['evil.example:16080'], ['127.0.0.1:6080'], ['localhost.evil:16080'],
                      ['127.0.0.1:16080', '127.0.0.1:16080'], ['localhost:16080', 'evil.example']]:
            with self.subTest(hosts=hosts):
                handler = self.handler(hosts, ['http://127.0.0.1:16080'])
                self.assertFalse(handler.parse_request()); self.assertEqual(handler.error, 403)
                self.assertFalse(handler.backend_connected)

    def test_bad_missing_duplicate_origin_rejected_before_backend(self):
        for origins in [[], ['null'], ['http://evil.example'], ['https://127.0.0.1:16080'],
                        ['http://127.0.0.1:6080'], ['http://localhost.evil:16080'],
                        ['http://localhost:16080', 'http://localhost:16080']]:
            with self.subTest(origins=origins):
                handler = self.handler(['127.0.0.1:16080'], origins)
                self.assertTrue(handler.parse_request()); handler.handle_upgrade()
                self.assertEqual(handler.error, 403); self.assertFalse(handler.backend_connected)

    def test_both_allowed_origins_upgrade_and_every_response_is_unframeable(self):
        for host in ['127.0.0.1:16080', 'localhost:16080']:
            handler = self.handler([host], ['http://' + host])
            self.assertTrue(handler.parse_request()); handler.handle_upgrade()
            self.assertTrue(handler.backend_connected)
            handler.end_headers()
            self.assertEqual(handler.response_headers['X-Frame-Options'], 'DENY')
            self.assertEqual(handler.response_headers['Content-Security-Policy'], "frame-ancestors 'none'")
            self.assertEqual(handler.response_headers['X-Content-Type-Options'], 'nosniff')


class PayloadContractTests(unittest.TestCase):
    def test_control_panel_uses_mapped_frame_bounds_and_monitor_workarea(self):
        source = (ROOT / 'guest/payload/share/controls.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'bounded_position')
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<panel geometry>', 'exec'), namespace)
        position = namespace['bounded_position']
        for area, frame in [((0, 0, 1600, 900), (370, 134)), ((0, 24, 1600, 842), (370, 134)),
                            ((-1920, 0, 1920, 1080), (370, 134))]:
            x, y = position(area, *frame)
            self.assertGreaterEqual(x, area[0]); self.assertGreaterEqual(y, area[1])
            self.assertLessEqual(x + frame[0], area[0] + area[2])
            self.assertLessEqual(y + frame[1], area[1] + area[3])
        self.assertEqual(position((10, 20, 300, 100), 400, 200), (10, 20))
        self.assertIn("connect('map-event', self.queue_position)", source)
        self.assertIn('get_frame_extents()', source)
        self.assertIn('get_workarea()', source)

    def test_chromium_kernel_argv_and_flattened_title_preserve_exact_flags(self):
        runtime = load('guest_runtime_argv', ROOT / 'guest/payload/share/runtime_config.py')
        executable = b'/usr/lib/chromium/chromium'
        with tempfile.TemporaryDirectory() as directory:
            process = Path(directory)
            with patch.object(runtime.os, 'readlink', return_value=executable.decode()):
                (process / 'cmdline').write_bytes(executable + b'\0--user-data-dir=/home/test/profile with spaces\0--type=renderer\0')
                self.assertEqual(runtime.process_argv(process), [executable, b'--user-data-dir=/home/test/profile with spaces', b'--type=renderer'])
                (process / 'cmdline').write_bytes(executable + b' --user-data-dir=/home/test/profile --type=gpu-process --no-sandbox\0')
                self.assertEqual(runtime.process_argv(process), [executable, b'--user-data-dir=/home/test/profile', b'--type=gpu-process', b'--no-sandbox'])
                (process / 'cmdline').write_bytes(executable + b' --user-data-dir="unterminated\0')
                with self.assertRaisesRegex(RuntimeError, 'Malformed Chromium'):
                    runtime.process_argv(process)
            with patch.object(runtime.os, 'readlink', return_value='/usr/bin/python3'):
                title = executable + b' --user-data-dir=/home/test/profile'
                (process / 'cmdline').write_bytes(title + b'\0')
                self.assertEqual(runtime.process_argv(process), [title])
            with patch.object(runtime.os, 'readlink', side_effect=PermissionError('/proc/1/exe')) as readlink:
                (process / 'cmdline').write_bytes(b'/sbin/init\0')
                self.assertEqual(runtime.process_argv(process), [b'/sbin/init'])
                readlink.assert_not_called()

    def test_minimal_desktop_explicitly_installs_gsettings_command(self):
        tree = ast.parse((ROOT / 'guest/install.py').read_text(encoding='utf-8'))
        installs = [node for node in ast.walk(tree) if isinstance(node, ast.List)
                    and len(node.elts) >= 2 and isinstance(node.elts[0], ast.Constant)
                    and node.elts[0].value == 'apt-get' and isinstance(node.elts[1], ast.Constant)
                    and node.elts[1].value == 'install']
        self.assertEqual(len(installs), 1)
        self.assertIn('libglib2.0-bin', [node.value for node in installs[0].elts if isinstance(node, ast.Constant)])
        startup = (ROOT / 'guest/payload/bin/agent-input-start').read_text(encoding='utf-8')
        self.assertIn('gsettings set org.freedesktop.ibus.general', startup)

    def test_user_transition_uses_absolute_runuser_with_restricted_path(self):
        tree = ast.parse((ROOT / 'guest/install.py').read_text(encoding='utf-8'))
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'agent')
        namespace = dict(user='agent', env={'PATH': '/usr/local/bin:/usr/bin:/bin'}, run=installer.run)
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<actual installer agent helper>', 'exec'), namespace)
        with patch.object(installer.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as process:
            namespace['agent']('/usr/bin/id', '-u')
        self.assertEqual(process.call_args.args[0][:4], ['/usr/sbin/runuser', '-u', 'agent', '--'])
        self.assertEqual(process.call_args.kwargs['env']['PATH'], '/usr/local/bin:/usr/bin:/bin')
        for source in [ROOT / 'guest/install.py', ROOT / 'guest/payload/bin/agent-verify',
                       ROOT / 'guest/payload/bin/agent-set-mode']:
            calls = [node.value for node in ast.walk(ast.parse(source.read_text(encoding='utf-8')))
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)
                     and node.value.rsplit('/', 1)[-1] == 'runuser']
            self.assertEqual(calls, ['/usr/sbin/runuser'], str(source))

    def test_services_keep_browser_independent_and_manual_gate(self):
        systemd = ROOT / 'guest/payload/systemd'
        chrome = (systemd / 'onepve-agent-chromium.service').read_text(encoding='utf-8')
        mcp = (systemd / 'onepve-agent-mcp.service').read_text(encoding='utf-8')
        self.assertNotIn('PartOf=onepve-agent-mcp.service', chrome)
        self.assertIn('KillMode=mixed', chrome)
        self.assertIn('SendSIGKILL=no', chrome)
        self.assertIn('Requires=onepve-agent-keyring.service', chrome)
        keyring = (systemd / 'onepve-agent-keyring.service').read_text(encoding='utf-8')
        self.assertIn('ExecCondition=/usr/bin/test -f /etc/onepve-agent/empty-keyring', keyring)
        self.assertIn('ExecCondition=/opt/onepve-agent/bin/agent-mcp-permitted', mcp)
        session = (ROOT / 'guest/payload/bin/agent-session-start').read_text(encoding='utf-8')
        self.assertIn('systemctl --user import-environment', session)
        self.assertNotIn('dbus-run-session', session)
        wayland = (ROOT / 'guest/payload/bin/agent-run-wayland').read_text(encoding='utf-8')
        self.assertIn('>> "$STATE_DIR/wayland.log" 2>&1', wayland)

    def test_no_old_machine_pins_or_password_bridge(self):
        for file in (ROOT / 'guest/payload').rglob('*'):
            if file.is_file() and '__pycache__' not in file.parts:
                body = file.read_text(encoding='utf-8')
                for forbidden in ['/home/agent/', '/run/user/1000/', '0000:01:00.0', '0x7d67', 'agent-browser-sriov', 'vnc-auth-front']:
                    self.assertNotIn(forbidden, body, str(file))
        vnc = (ROOT / 'guest/payload/bin/agent-run-vnc').read_text(encoding='utf-8')
        self.assertIn('-localhost yes -SecurityTypes None', vnc)
        self.assertNotIn('I-KNOW-THIS-IS-INSECURE', vnc)
        controls = (ROOT / 'guest/payload/share/controls.py').read_text(encoding='utf-8')
        sway = (ROOT / 'guest/payload/share/wayland/sway.conf').read_text(encoding='utf-8')
        self.assertIn("GLib.set_prgname('onepve-agent-controls')", controls)
        self.assertIn('app_id="onepve-agent-controls" title="^浏览器控制$"', sway)


if __name__ == '__main__':
    unittest.main()

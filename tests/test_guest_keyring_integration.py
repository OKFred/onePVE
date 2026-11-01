"""Prove a brand-new keyring on an owned private D-Bus; never inspect user secrets."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def worker():
    directory = Path(sys.argv[2]).resolve()
    home = directory / 'home'; runtime = directory / 'runtime'
    if (Path(os.environ['HOME']) != home or Path(os.environ['XDG_RUNTIME_DIR']) != runtime
            or not os.environ.get('DBUS_SESSION_BUS_ADDRESS')
            or '/run/user/' in os.environ['DBUS_SESSION_BUS_ADDRESS']):
        raise RuntimeError('Expected the isolated test home, runtime and private D-Bus')
    spec = importlib.util.spec_from_file_location('onepve_keyring_installer', ROOT / 'guest/install.py')
    installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
    installer.seed_profile(home, (os.getuid(), os.getgid()))
    control = runtime / 'keyring'; control.mkdir(mode=0o700)
    daemon = subprocess.Popen(['gnome-keyring-daemon', '--foreground', '--components=secrets',
                               '--control-directory=' + str(control)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    report = {'status': 'NOT_VERIFIED', 'daemon_pid': daemon.pid, 'read_secret_calls': 0,
              'home': str(home), 'private_bus': True, 'daemon_ended': False}
    def call(service, path, interface, method, *arguments):
        return subprocess.run(['busctl', '--user', '--timeout=3', 'call', service, path, interface,
                              method, *arguments], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    try:
        bus = call('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                   'GetConnectionUnixProcessID', 's', 'org.freedesktop.DBus')
        report['bus_pid'] = int(bus.split()[1])
        deadline = time.monotonic() + 10
        unlocked = False
        while True:
            if daemon.poll() is not None:
                raise RuntimeError('Owned keyring daemon exited before readiness')
            try:
                owner = call('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                             'GetNameOwner', 's', 'org.freedesktop.secrets')
                collection = json.loads(call('org.freedesktop.secrets', '/org/freedesktop/secrets',
                    'org.freedesktop.Secret.Service', 'ReadAlias', 's', 'default')[2:])
                if collection == '/':
                    raise RuntimeError('No default collection')
                if not unlocked:
                    helper_spec = importlib.util.spec_from_file_location('onepve_keyring_start',
                        ROOT / 'guest/payload/share/keyring_start.py')
                    helper = importlib.util.module_from_spec(helper_spec); helper_spec.loader.exec_module(helper)
                    result = helper.unlock_default()
                    if result['status'] != 'EMPTY_DEFAULT_KEYRING_UNLOCKED' or result['prompt_created']:
                        raise RuntimeError('Empty default keyring was not unlocked without a prompt')
                    unlocked = True
                    report['startup_helper'] = result
                locked = subprocess.run(['busctl', '--user', '--timeout=3', 'get-property',
                    'org.freedesktop.secrets', collection, 'org.freedesktop.Secret.Collection', 'Locked'],
                    capture_output=True, text=True, check=True, timeout=5).stdout.strip()
                assert locked == 'b false', 'Brand-new default collection is locked'
                report.update(status='PASS_FRESH_DEFAULT_KEYRING_UNLOCKED', default_alias=collection,
                              locked=False, owner=owner)
                break
            except (subprocess.SubprocessError, RuntimeError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(.1)
    finally:
        if daemon.poll() is None:
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill(); daemon.wait(timeout=3)
        report['daemon_ended'] = daemon.poll() is not None
        print(json.dumps(report), flush=True)


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('dbus-run-session')
                     and shutil.which('gnome-keyring-daemon') and shutil.which('busctl') and os.getuid() >= 1000,
                     'Debian ordinary-user GNOME keyring integration')
class FreshKeyringTests(unittest.TestCase):
    def test_default_alias_is_unlocked_without_existing_home_or_bus(self):
        with tempfile.TemporaryDirectory(prefix='onepve-keyring-test-') as directory:
            root = Path(directory)
            for name in ['home', 'runtime', 'config', 'cache']:
                (root / name).mkdir(mode=0o700)
            env = dict(PATH='/usr/local/bin:/usr/bin:/bin', LANG='C.UTF-8', HOME=str(root / 'home'),
                       XDG_DATA_HOME=str(root / 'home/.local/share'), XDG_RUNTIME_DIR=str(root / 'runtime'),
                       XDG_CONFIG_HOME=str(root / 'config'), XDG_CACHE_HOME=str(root / 'cache'))
            # Reuse only the owned synthetic home on a second cold daemon/bus.
            for cold_start in [1, 2]:
                process = subprocess.Popen(['dbus-run-session', '--', sys.executable, __file__, '--worker', directory],
                                           env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                           start_new_session=True)
                try:
                    stdout, stderr = process.communicate(timeout=35)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL); process.communicate(timeout=3)
                    self.fail('Owned keyring test group exceeded its deadline')
                self.assertEqual(process.returncode, 0, stderr + '\n' + stdout)
                report = json.loads(next(line for line in reversed(stdout.splitlines()) if line.startswith('{')))
                self.assertEqual(report['status'], 'PASS_FRESH_DEFAULT_KEYRING_UNLOCKED')
                self.assertTrue(report['daemon_ended'])
                self.assertEqual(report['read_secret_calls'], 0)
                for key in ['daemon_pid', 'bus_pid']:
                    process_path = Path('/proc', str(report[key])); deadline = time.monotonic() + 3
                    while process_path.exists() and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertFalse(process_path.exists(), key + ' should have ended')
                report.update(all_owned_pids_ended=True, cold_start=cold_start)
                print(json.dumps(report), flush=True)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        worker()
    else:
        unittest.main()

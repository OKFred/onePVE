#!/usr/bin/python3
"""Unlock only the dedicated empty-password default collection; never read secrets."""
import json
import os
from pathlib import Path
import shlex
import subprocess


def unlock_default():
    def bus(*arguments):
        return subprocess.run(['busctl', '--user', '--timeout=5', *arguments],
                              capture_output=True, text=True, check=True, timeout=7).stdout.strip()
    service = 'org.freedesktop.secrets'
    path = '/org/freedesktop/secrets'
    interface = 'org.freedesktop.Secret.Service'
    fields = shlex.split(bus('call', service, path, interface, 'ReadAlias', 's', 'default'))
    if len(fields) != 2 or fields[0] != 'o' or fields[1] == '/':
        raise RuntimeError('Dedicated default keyring is unavailable')
    collection = fields[1]
    # GNOME's standard Unlock first tries the empty password. A non-root prompt
    # path means user interaction would be needed; never invoke that Prompt.
    result = shlex.split(bus('call', service, path, interface, 'Unlock', 'ao', '1', collection))
    if result != ['aoo', '1', collection, '/']:
        raise RuntimeError('Default keyring requires interactive authorization')
    locked = bus('get-property', service, collection, 'org.freedesktop.Secret.Collection', 'Locked')
    if locked != 'b false':
        raise RuntimeError('Dedicated default keyring remains locked')
    return {'status': 'EMPTY_DEFAULT_KEYRING_UNLOCKED', 'default_alias': collection,
            'prompt_created': False, 'read_secret_calls': 0}


def main():
    from runtime_config import config
    settings = config()
    policy = Path('/etc/onepve-agent/empty-keyring')
    if (os.getuid() != settings['uid'] or os.geteuid() != settings['uid']
            or policy.is_symlink() or policy.stat().st_uid != 0 or policy.stat().st_mode & 0o022
            or policy.read_text() != 'empty-password-login\n'):
        raise RuntimeError('Only the owned ordinary-user empty-keyring policy is supported')
    expected = 'unix:path=/run/user/' + str(settings['uid']) + '/bus'
    if os.environ.get('DBUS_SESSION_BUS_ADDRESS') != expected:
        raise RuntimeError('The original normal user D-Bus is required')
    print(json.dumps(unlock_default()))


if __name__ == '__main__':
    main()

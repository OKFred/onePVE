"""Read root-owned, non-secret guest configuration."""
import json
import os
from pathlib import Path
import pwd
import re
import shlex


def process_argv(process):
    """Keep kernel argv bytes; decode Chromium's flattened process title only."""
    process = Path(process)
    arguments = (process / 'cmdline').read_bytes().split(b'\0')
    while arguments and not arguments[-1]:
        arguments.pop()
    if len(arguments) != 1 or not arguments[0].startswith(b'/usr/lib/chromium/chromium '):
        return arguments
    if os.readlink(process / 'exe') != '/usr/lib/chromium/chromium':
        return arguments
    try:
        parsed = shlex.split(arguments[0].decode('utf-8'), posix=True)
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeError('Malformed Chromium process title') from error
    if not parsed or parsed[0] != '/usr/lib/chromium/chromium':
        raise RuntimeError('Unexpected Chromium process-title executable')
    return [argument.encode('utf-8') for argument in parsed]


def config():
    path = Path('/etc/onepve-agent/config.env')
    if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
        raise RuntimeError('Unsafe guest configuration')
    values = {}
    for line in path.read_text().splitlines():
        fields = shlex.split(line)
        if len(fields) != 1 or '=' not in fields[0]:
            raise RuntimeError('Invalid guest configuration')
        key, value = fields[0].split('=', 1); values[key] = value
    account = pwd.getpwnam(values['AGENT_USER'])
    if account.pw_uid < 1000 or account.pw_dir != values['AGENT_HOME']:
        raise RuntimeError('Dedicated user identity changed')
    return dict(user=account.pw_name, uid=account.pw_uid, gid=account.pw_gid,
                home=Path(account.pw_dir), viewer_port=int(values['VIEWER_PORT']))


def mode():
    path = Path('/etc/onepve-agent/mode.json')
    if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
        raise RuntimeError('Unsafe display mode file')
    value = json.loads(path.read_text())
    if value.get('mode') not in {'x11', 'wayland'}:
        raise RuntimeError('Unknown display mode')
    if value['mode'] == 'wayland' and not re.fullmatch(r'0000:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]', value.get('gpu_bdf', '')):
        raise RuntimeError('Wayland needs an exact selected guest PCI BDF')
    return value

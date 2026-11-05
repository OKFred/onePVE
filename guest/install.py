#!/usr/bin/env python3
"""Install the dedicated Debian guest; never operates PVE or client machines."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import zipfile

ROOT = Path('/opt/onepve-agent')
CONFIG = Path('/etc/onepve-agent')
PAYLOAD = Path(__file__).resolve().parent / 'payload'
DEFAULTS = dict(user='agent', node_version='26.13.0', mcp_version='0.0.83',
                fnm_version='v1.39.0', codex_version='0.166.0', viewer_port=16080,
                start_desktop=True, gpu_bdf='', fnm_sha256='', cache_dir='')


def manifest(path):
    supplied = json.loads(path.read_text())
    if not isinstance(supplied, dict) or supplied.keys() - DEFAULTS.keys():
        raise ValueError('Unknown guest manifest fields')
    values = {**DEFAULTS, **supplied}
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', values['user']) or values['user'] == 'root':
        raise ValueError('Use a dedicated ordinary user')
    for key, pattern in [('node_version', r'26\.\d+\.\d+'), ('mcp_version', r'\d+\.\d+\.\d+'),
                         ('codex_version', r'\d+\.\d+\.\d+'), ('fnm_version', r'v\d+\.\d+\.\d+')]:
        if not isinstance(values[key], str) or not re.fullmatch(pattern, values[key]):
            raise ValueError('Exact version pin required: ' + key)
    if type(values['viewer_port']) is not int or not 1024 <= values['viewer_port'] <= 65535:
        raise ValueError('viewer_port must be a nonprivileged client port')
    if type(values['start_desktop']) is not bool:
        raise ValueError('start_desktop must be boolean')
    if values['gpu_bdf'] and not re.fullmatch(r'0000:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]', values['gpu_bdf']):
        raise ValueError('gpu_bdf must be an exact guest PCI BDF')
    if values['fnm_sha256'] and not re.fullmatch(r'[0-9a-fA-F]{64}', values['fnm_sha256']):
        raise ValueError('Invalid fnm SHA256')
    if not isinstance(values['cache_dir'], str) or '\n' in values['cache_dir'] or '\0' in values['cache_dir']:
        raise ValueError('Invalid runtime cache path')
    return values


def runtime_cache(values):
    if not values['cache_dir']:
        return None
    root = Path(values['cache_dir'])
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise RuntimeError('Runtime cache must be an existing real absolute directory')
    record = root / 'runtime-manifest.json'
    if record.is_symlink():
        raise RuntimeError('Unsafe runtime cache manifest')
    data = json.loads(record.read_text())
    for key in ['node_version', 'fnm_version', 'mcp_version', 'codex_version']:
        if data.get(key) != values[key]:
            raise RuntimeError('Cached version pin differs: ' + key)
    files = data.get('files')
    if not isinstance(files, dict) or not files:
        raise RuntimeError('Runtime cache needs a complete file hash manifest')
    symlinks = data.get('symlinks', {})
    if not isinstance(symlinks, dict):
        raise RuntimeError('Invalid runtime cache symlink manifest')
    actual, actual_links = set(), {}
    for file in root.rglob('*'):
        if file.is_symlink():
            target = os.readlink(file)
            if os.path.isabs(target) or not file.resolve(strict=True).is_relative_to(root):
                raise RuntimeError('Runtime cache symlink escapes or is not portable')
            actual_links[file.relative_to(root).as_posix()] = target
            continue
        if not file.is_file() or file == record:
            continue
        name = file.relative_to(root).as_posix(); actual.add(name)
        if not re.fullmatch(r'[0-9a-f]{64}', str(files.get(name, ''))):
            raise RuntimeError('Unrecorded cached file: ' + name)
        with file.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != files[name]:
                raise RuntimeError('Cached file hash differs: ' + name)
    if actual_links != symlinks:
        raise RuntimeError('Runtime cache symlinks differ from manifest')
    if actual != files.keys() or not {'runtime/fnm.zip', 'runtime/node.tar.xz', 'npm/package.json', 'npm/package-lock.json'} <= actual:
        raise RuntimeError('Runtime cache is missing files')
    for package, version in [('@playwright/mcp', values['mcp_version']), ('@openai/codex', values['codex_version'])]:
        if json.loads((root / 'npm/node_modules' / package / 'package.json').read_text())['version'] != version:
            raise RuntimeError('Cached npm package version differs')
    return root


def chown_tree(path, owner):
    for item in [path, *path.rglob('*')]:
        os.chown(item, *owner, follow_symlinks=False)


def run(args, **kwargs):
    args = list(map(str, args))
    if args[0] == 'apt-get':
        args[1:1] = ['-o', 'APT::Status-Fd=1']
        kwargs['env'] = dict(os.environ if kwargs.get('env') is None else kwargs['env'], LC_ALL='C')
    print('[onePVE guest] ' + shlex.join(args), flush=True)
    return subprocess.run(args, check=True, **kwargs)


def download(url, destination):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as source, destination.open('wb') as target:
                shutil.copyfileobj(source, target)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2)


def write(path, content, mode=0o644, owner=None, only_new=False):
    path = Path(path)
    if path.is_symlink() or path.parent.resolve() != path.parent:
        raise RuntimeError('Refusing managed file symlink: ' + str(path))
    if only_new and path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.onepve-new')
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError('Interrupted managed write: ' + str(temporary))
    with temporary.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    if owner:
        os.chown(temporary, *owner)
    temporary.replace(path)


def seed_profile(home, owner):
    """Only seed a new profile/keyring; an idempotent rerun never rewrites them."""
    profile = home / '.local/share/onepve-agent/chromium'
    prefs = profile / 'Default/Preferences'
    if not (profile / 'Local State').exists() and not prefs.exists():
        write(prefs, json.dumps({'session': {'restore_on_startup': 1}}) + '\n', 0o600, owner)
        os.chown(prefs.parent, *owner)
        prefs.parent.chmod(0o700)
    keyrings = home / '.local/share/keyrings'
    if keyrings.is_symlink():
        raise RuntimeError('Unexpected keyring directory symlink')
    if not keyrings.exists() or not any(keyrings.iterdir()):
        # GNOME's standard textual keyring format represents an empty-password
        # collection. No existing keyring or Chromium credential is copied.
        keyrings.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(keyrings, *owner)
        stamp = int(time.time())
        write(keyrings / 'login.keyring', '[keyring]\ndisplay-name=Login\nctime=%d\nmtime=%d\nlock-on-idle=false\nlock-after=false\n' % (stamp, stamp), 0o600, owner)
        write(keyrings / 'default', 'login\n', 0o600, owner)
        return True
    return False


def require_disk_capacity(total):
    # A 32 GiB VM disk also contains EFI/boot partitions and filesystem metadata.
    if total < 31 * 1024**3:
        raise RuntimeError('Expand the root filesystem on a >=32 GiB disk before installation')


def guest_swap():
    path = Path('/var/lib/onepve-agent/swapfile')
    if path.is_symlink() or path.parent.resolve() != path.parent or not path.is_file():
        raise RuntimeError('Cloud-init swapfile is missing or redirected; inspect cloud-init logs')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 4 * 1024**3:
        raise RuntimeError('Cloud-init swapfile must be a regular 4 GiB file')
    if os.name == 'posix' and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeError('Cloud-init swapfile must be root-owned with mode 0600')
    active = [line.split() for line in Path('/proc/swaps').read_text().splitlines()[1:]]
    if not any(len(fields) >= 5 and fields[0] == str(path) and int(fields[2]) >= 4 * 1024**2 - 4096 for fields in active):
        raise RuntimeError('Cloud-init swapfile is not active; inspect cloud-init logs before installation')
    entries = [line.split('#', 1)[0].split() for line in Path('/etc/fstab').read_text().splitlines()]
    if not any(len(fields) >= 4 and fields[0] == str(path) and fields[2] == 'swap'
               and 'noauto' not in fields[3].split(',') for fields in entries):
        raise RuntimeError('Cloud-init swapfile has no automatic boot entry in /etc/fstab')
    return dict(status='READY', path=str(path), size_bytes=info.st_size, active=True, persistent=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    values = manifest(args.manifest)
    if os.getuid() != 0 or os.uname().sysname != 'Linux':
        raise RuntimeError('Run as root inside the dedicated Debian guest')
    cached = runtime_cache(values)
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if release.get('ID') != 'debian' or release.get('VERSION_ID', '').strip('"').split('.')[0] != '13':
        raise RuntimeError('Only Debian 13 is supported')
    if os.cpu_count() < 4 or int(re.search(r'MemTotal:\s+(\d+)', Path('/proc/meminfo').read_text())[1]) < 7864320:
        raise RuntimeError('At least 4 vCPU and fixed 8 GiB memory are required')
    require_disk_capacity(shutil.disk_usage('/').total)
    # Executable payload/config directories must be traversable by the ordinary
    # desktop user; private state/files get explicit 0700/0600 below.
    os.umask(0o022)
    os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
    swap = guest_swap()
    print('[onePVE guest] Swap: ' + json.dumps(swap, ensure_ascii=False), flush=True)
    masked = False
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'lightdm.service']).returncode != 0:
        if not Path('/run/systemd/system/lightdm.service').is_symlink():
            run(['systemctl', 'mask', '--runtime', 'lightdm.service']); masked = True
    try:
        run(['debconf-set-selections'], input='lightdm shared/default-x-display-manager select lightdm\n', text=True)
        run(['apt-get', 'update'])
        run(['apt-get', 'install', '-y', '--no-install-recommends',
             'ca-certificates', 'curl', 'unzip', 'python3', 'python3-gi', 'gir1.2-gtk-3.0', 'libglib2.0-bin',
             'openssh-server', 'qemu-guest-agent', 'spice-vdagent', 'iproute2', 'procps',
             'lightdm', 'lightdm-gtk-greeter', 'xfce4', 'xfce4-terminal', 'xserver-xorg',
             'x11-utils', 'x11-xserver-utils', 'xauth', 'xdg-utils', 'xdg-user-dirs',
             'dbus-user-session', 'dbus-x11', 'chromium', 'gnome-keyring', 'libpam-gnome-keyring',
             'libsecret-tools', 'tigervnc-scraping-server', 'tigervnc-tools', 'novnc', 'websockify',
             'sway', 'wayvnc', 'mesa-utils', 'libegl1', 'libegl-mesa0', 'libgl1-mesa-dri', 'libgles2', 'libgbm1',
             'zenity', 'libnotify-bin', 'fonts-noto-cjk',
             'ibus', 'ibus-libpinyin', 'ibus-gtk', 'ibus-gtk3', 'ibus-gtk4', 'im-config'])
        user = values['user']
        try:
            account = pwd.getpwnam(user)
        except KeyError:
            run(['useradd', '--create-home', '--shell', '/bin/bash', user]); account = pwd.getpwnam(user)
        home = Path(account.pw_dir)
        if account.pw_uid < 1000 or home != Path('/home') / user or home.is_symlink():
            raise RuntimeError('Expected a normal dedicated /home user')
        owner = (account.pw_uid, account.pw_gid)
        for group in ['video', 'render']:
            run(['usermod', '-aG', group, user])
        for relative, mode in [('.local', 0o700), ('.local/share', 0o700), ('.local/state', 0o700),
                               ('.config', 0o700), ('.config/systemd', 0o755), ('.config/systemd/user', 0o755), ('.config/autostart', 0o755),
                               ('.local/share/applications', 0o755), ('Desktop', 0o755), ('workspaces', 0o755),
                               ('Downloads', 0o700), ('Downloads/onepve-agent', 0o700), ('.local/share/onepve-agent', 0o700),
                               ('.local/share/onepve-agent/chromium', 0o700), ('.local/state/onepve-agent', 0o700),
                               ('.local/share/fnm', 0o700), ('.local/share/onepve-agent/mcp', 0o700)]:
            directory = home / relative
            if directory.is_symlink():
                raise RuntimeError('Unexpected home symlink: ' + str(directory))
            directory.mkdir(parents=True, exist_ok=True); directory.chmod(mode); os.chown(directory, *owner)
        created_keyring = seed_profile(home, owner)
        if created_keyring:
            write(CONFIG / 'empty-keyring', 'empty-password-login\n')
        fnm = home / '.local/share/fnm/fnm'
        if fnm.is_symlink():
            raise RuntimeError('Unexpected fnm executable symlink')
        env = dict(HOME=str(home), USER=user, LOGNAME=user, PATH='/usr/local/bin:/usr/bin:/bin',
                   PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD='1', XDG_RUNTIME_DIR='/run/user/' + str(account.pw_uid))
        def agent(*command, **kw):
            return run(['/usr/sbin/runuser', '-u', user, '--', *map(str, command)], env=env, **kw)
        with tempfile.TemporaryDirectory(prefix='onepve-guest-') as temp:
            temp = Path(temp)
            if not cached:
                download('https://nodejs.org/dist/index.json', temp / 'node-index.json')
                entry = next((x for x in json.loads((temp / 'node-index.json').read_text()) if x['version'] == 'v' + values['node_version']), None)
                if not entry or not entry.get('lts'):
                    raise RuntimeError('The exact requested Node26 release is not marked LTS')
            version = subprocess.run([str(fnm), '--version'], text=True, capture_output=True).stdout.strip() if fnm.exists() else ''
            if version != 'fnm ' + values['fnm_version'][1:]:
                if subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip() != 'amd64':
                    raise RuntimeError('This guest payload targets amd64')
                archive_path = cached / 'runtime/fnm.zip' if cached else temp / 'fnm.zip'
                if not cached:
                    download('https://github.com/Schniz/fnm/releases/download/' + values['fnm_version'] + '/fnm-linux.zip', archive_path)
                digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
                if values['fnm_sha256'] and digest != values['fnm_sha256'].lower():
                    raise RuntimeError('fnm archive checksum mismatch')
                with zipfile.ZipFile(archive_path) as archive:
                    binary = archive.read('fnm')
                fnm.write_bytes(binary); fnm.chmod(0o755); os.chown(fnm, *owner)
            fnm_dir = str(fnm.parent)
            installation = fnm.parent / 'node-versions' / ('v' + values['node_version']) / 'installation'
            if cached and not installation.exists():
                with tarfile.open(cached / 'runtime/node.tar.xz', 'r:xz') as archive:
                    archive.extractall(temp / 'node', filter='data')
                source = temp / 'node' / ('node-v' + values['node_version'] + '-linux-x64')
                if not (source / 'bin/node').is_file() or installation.parent.resolve() != installation.parent:
                    raise RuntimeError('Unexpected official Node archive layout')
                versions_dir = fnm.parent / 'node-versions'
                versions_dir.mkdir(mode=0o700, exist_ok=True); os.chown(versions_dir, *owner)
                installation.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(source, installation); chown_tree(installation.parent, owner)
            agent(fnm, '--fnm-dir', fnm_dir, 'install', values['node_version'], '--progress', 'never')
            agent(fnm, '--fnm-dir', fnm_dir, 'default', values['node_version'])
            node = [fnm, '--fnm-dir', fnm_dir, 'exec', '--using=' + values['node_version']]
            actual = agent(*node, 'node', '--version', capture_output=True, text=True).stdout.strip()
            if actual != 'v' + values['node_version']:
                raise RuntimeError('Actual Node version differs from pin')
            mcp = home / '.local/share/onepve-agent/mcp'
            if cached:
                # Copy as the ordinary user so a changed home cannot turn a
                # repeated installation into a privileged recursive write.
                staged = temp / 'npm'
                shutil.copytree(cached / 'npm', staged, symlinks=True)
                chown_tree(temp, owner); temp.chmod(0o700)
                agent('/bin/cp', '-a', str(staged) + '/.', str(mcp) + '/')
            else:
                write(mcp / 'package.json', '{"name":"onepve-agent-mcp","private":true,"version":"1.0.0"}\n', 0o600, owner, only_new=True)
                agent(*node, 'npm', '--prefix', mcp, 'install', '--save-exact', '--ignore-scripts', '--no-audit', '--no-fund',
                      '@playwright/mcp@' + values['mcp_version'], '@openai/codex@' + values['codex_version'])
            agent(*node, 'node', mcp / 'node_modules/@openai/codex/bin/codex.js', '--version')
        for source in sorted(PAYLOAD.rglob('*')):
            if not source.is_file() or '__pycache__' in source.parts or source.suffix in {'.pyc', '.pyo'}:
                continue
            relative = source.relative_to(PAYLOAD)
            if relative.parts[0] == 'systemd':
                destination = home / '.config/systemd/user' / source.name
                write(destination, source.read_text(), 0o644, owner)
            elif relative.parts[0] == 'desktop':
                folder = '.config/autostart' if source.name == 'agent-session-start.desktop' else '.local/share/applications'
                write(home / folder / source.name, source.read_text(), 0o644, owner)
                if source.name != 'agent-session-start.desktop':
                    write(home / 'Desktop' / source.name, source.read_text(), 0o755, owner)
            else:
                write(ROOT / relative, source.read_text(), 0o755 if relative.parts[0] == 'bin' else 0o644)
        write(CONFIG / 'config.env', ''.join(k + '=' + shlex.quote(str(v)) + '\n' for k, v in
              [('AGENT_USER', user), ('AGENT_HOME', home), ('AGENT_UID', account.pw_uid), ('VIEWER_PORT', values['viewer_port'])]))
        write(CONFIG / 'versions.env', ''.join(key.upper() + '=' + values[key] + '\n' for key in ['node_version', 'mcp_version', 'fnm_version', 'codex_version']))
        write(CONFIG / 'node.version', actual + '\n')
        write('/usr/local/bin/codex', '#!/bin/sh\nexec /opt/onepve-agent/bin/agent-codex "$@"\n', 0o755, only_new=True)
        write(CONFIG / 'mode.json', json.dumps({'mode': 'x11', 'gpu_bdf': ''}) + '\n', only_new=True)
        rules = home / 'AGENTS.md'
        content = rules.read_text() if rules.exists() else ''
        if '<!-- onepve-agent-managed-rules -->' not in content:
            write(rules, content.rstrip() + '\n\n<!-- onepve-agent-managed-rules -->\n' +
                  (PAYLOAD / 'share/AGENTS.md').read_text(), 0o644, owner)
        fragment = home / '.config/onepve-agent-fnm.bash'
        write(fragment, (PAYLOAD / 'share/fnm-interactive.bash').read_text(), 0o644, owner)
        bashrc = home / '.bashrc'
        content = bashrc.read_text() if bashrc.exists() else ''
        if '.config/onepve-agent-fnm.bash' not in content:
            write(bashrc, content + '\n[ ! -r "$HOME/.config/onepve-agent-fnm.bash" ] || . "$HOME/.config/onepve-agent-fnm.bash"\n', 0o644, owner)
        write('/etc/lightdm/lightdm.conf.d/90-onepve-agent.conf', '[Seat:*]\nautologin-user=' + user + '\nautologin-user-timeout=0\nautologin-session=xfce\nuser-session=xfce\n')
        write('/etc/ssh/sshd_config.d/90-onepve-agent.conf', 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n')
        # Only a new config is seeded; existing remote account settings are retained.
        codex = home / '.codex/config.toml'
        content = codex.read_text() if codex.exists() else ''
        if '[mcp_servers.vm_browser]' not in content:
            write(codex, content + '\n[mcp_servers.vm_browser]\nurl = "http://127.0.0.1:8931/mcp"\nstartup_timeout_sec = 30\ntool_timeout_sec = 120\ndisabled_tools = ["browser_close"]\n', 0o600, owner)
            os.chown(codex.parent, *owner); codex.parent.chmod(0o700)
        run(['sshd', '-t'])
        agent('im-config', '-n', 'ibus')
        if masked:
            run(['systemctl', 'unmask', '--runtime', 'lightdm.service']); masked = False
        run(['systemctl', 'enable', 'ssh.service', 'lightdm.service'])
        subprocess.run(['systemctl', 'enable', 'qemu-guest-agent.service'], check=False)
        run(['systemctl', 'set-default', 'graphical.target'])
        run(['systemctl', 'start', 'ssh.service'])
        subprocess.run(['systemctl', 'start', 'qemu-guest-agent.service'], check=False)
        if Path(env['XDG_RUNTIME_DIR'], 'bus').exists():
            agent('systemctl', '--user', 'daemon-reload')
        if values['start_desktop']:
            run(['systemctl', 'start', 'lightdm.service'])
        write('/var/lib/onepve-agent/install.json', json.dumps({'status': 'INSTALLED', 'user': user, 'uid': account.pw_uid,
              'swap': swap,
              'versions': {k: values[k] for k in ['node_version', 'mcp_version', 'fnm_version', 'codex_version']},
              'viewer_port': values['viewer_port'], 'credential_policy': 'ssh-public-key-only; dedicated empty-password keyring'}, indent=2) + '\n', 0o600)
        selected_mode = json.loads((CONFIG / 'mode.json').read_text())['mode']
        print(json.dumps({'status': 'GUEST_INSTALLED', 'user': user, 'node': actual, 'mode': selected_mode, 'swap': swap}))
    finally:
        if masked:
            subprocess.run(['systemctl', 'unmask', '--runtime', 'lightdm.service'], check=False)


if __name__ == '__main__':
    main()

"""Host GPU preparation. VM creation and guest installation belong to menu 08."""
import argparse
import base64
import contextlib
try:
    import fcntl
except ImportError:
    fcntl = None  # Import-only inspection/tests also run on Windows.
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from onepve_common import run, atomic_json, require_root, STATE_ROOT, repo_root

PCI = Path('/sys/bus/pci/devices')
PROC = Path('/proc')
VM_CONFIGS = Path('/etc/pve/qemu-server')
GPU_STATE = STATE_ROOT / 'gpu'
PINNED_DRIVERS = {
    '2026.10.28': {'commit': 'cd1595abd4c53a8aa01be858f50c22f0f244ba3b',
                   'sha256': '031de5db5b83f117d1a1a591be3b518f31d56f6cdb7ed0d1f7577a6508dbf762'},
    '2025.12.10.2': {'commit': '626302e50b9e7de5f529b6f1499c480a73c9d763',
                    'sha256': 'da272fd933d726bf4409181c5939d39fb9ce30757a5fd1a5b17732ae051660f3'},
}


def need(value, message):
    if not value:
        raise RuntimeError(message)


def canonical_bdf(value, allow_slot=False):
    match = re.fullmatch(r'(?:(?P<domain>[0-9a-fA-F]{4}):)?(?P<bus>[0-9a-fA-F]{2}):'
                         r'(?P<slot>[0-9a-fA-F]{2})(?:\.(?P<fn>[0-7]))?', str(value).strip())
    need(match and (allow_slot or match['fn'] is not None), 'Expected one complete PCI BDF, not a mapping or all functions')
    return '%s:%s:%s%s' % ((match['domain'] or '0000').lower(), match['bus'].lower(),
                            match['slot'].lower(), '.' + match['fn'] if match['fn'] is not None else '')


def driver_spec(kernel):
    match = re.match(r'^(\d+)\.(\d+)\.', kernel)
    need(match, 'Cannot determine kernel series')
    series = tuple(map(int, match.groups()))
    if (6, 18) <= series <= (7, 3):
        version = '2026.10.28'
    elif (6, 12) <= series < (6, 18):
        version = '2025.12.10.2'
    else:
        raise RuntimeError('No pinned SR-IOV driver for kernel %s; the tool will not upgrade the kernel' % kernel)
    return dict(PINNED_DRIVERS[version], version=version, kernel=kernel,
                url='https://codeload.github.com/strongtz/i915-sriov-dkms/tar.gz/refs/tags/' + version,
                project='Community-maintained strongtz/i915-sriov-dkms; not Intel-certified')


def _read(path, default=''):
    try:
        return Path(path).read_text().strip()
    except (FileNotFoundError, NotADirectoryError):
        return default


def _link(path):
    try:
        return Path(path).resolve(strict=True).name
    except FileNotFoundError:
        return None


def _pci_refs():
    refs, unresolved = [], []
    for config in sorted(VM_CONFIGS.glob('*.conf')):
        for raw in config.read_text().splitlines():
            line = raw.strip()
            if line.startswith('['):
                break
            key, separator, value = line.partition(':')
            if not separator or line.startswith('#'):
                continue
            if re.fullmatch(r'hostpci\d+', key):
                if 'mapping=' in value:
                    unresolved.append({'kind': 'unresolved_mapping', 'vmid': config.stem})
                    continue
                source = value.strip().split(',', 1)[0].removeprefix('host=')
                try:
                    refs.extend({'kind': 'vm_config', 'vmid': config.stem,
                                 'bdf': canonical_bdf(part, allow_slot=True)} for part in source.split(';'))
                except RuntimeError:
                    unresolved.append({'kind': 'unresolved_pci_config', 'vmid': config.stem})
            elif key == 'args' and re.search(r'vfio|legacy-igd', value, re.I):
                unresolved.append({'kind': 'unresolved_raw_qemu_args', 'vmid': config.stem})
    return refs, unresolved


def _matches(address, bdf):
    return address == bdf or ('.' not in address and bdf.startswith(address + '.'))


def _holders(device):
    group = _link(device / 'iommu_group')
    targets = {'/dev/vfio/' + group} if group else set()
    targets.update('/dev/vfio/devices/' + p.name for p in (device / 'vfio-dev').glob('vfio*'))
    targets.update('/dev/dri/' + p.name for p in (device / 'drm').glob('*')
                   if re.fullmatch(r'(?:card|renderD)\d+', p.name))
    found = []
    for process in PROC.iterdir() if PROC.exists() else []:
        if not process.name.isdecimal():
            continue
        try:
            args = (process / 'cmdline').read_bytes().decode(errors='replace').split('\0')
            executable = Path(os.readlink(process / 'exe')).name
            qemu = executable.startswith('qemu-system-') or executable == 'kvm'
            vmid = args[args.index('-id') + 1] if qemu and '-id' in args and args.index('-id') + 1 < len(args) else None
            if qemu:
                addresses = re.findall(r'(?<![\da-fA-F:])(?:[\da-fA-F]{4}:)?[\da-fA-F]{2}:[\da-fA-F]{2}\.[0-7](?![\da-fA-F])', ' '.join(args))
                if device.name in {canonical_bdf(x) for x in addresses}:
                    found.append({'kind': 'qemu_device', 'pid': process.name, 'vmid': vmid})
            for descriptor in (process / 'fd').iterdir():
                try:
                    target = os.readlink(descriptor).removesuffix(' (deleted)')
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if target in targets:
                    found.append({'kind': 'device_fd', 'pid': process.name, 'vmid': vmid})
                elif target == 'anon_inode:[vfio-device]' and not qemu:
                    found.append({'kind': 'unmapped_vfio_fd', 'pid': process.name, 'vmid': None})
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            found.append({'kind': 'unreadable_process', 'pid': process.name, 'vmid': None})
    unique = {json.dumps(x, sort_keys=True): x for x in found}
    return list(unique.values())


def inventory():
    refs, unresolved = _pci_refs()
    result = []
    for path in sorted(PCI.glob('*')):
        kind = _read(path / 'class')
        if not (kind.startswith('0x03') or (path / 'physfn').exists() or list(path.glob('virtfn*'))):
            continue
        bdf = path.name
        group = _link(path / 'iommu_group')
        group_members = sorted(p.name for p in (path / 'iommu_group/devices').glob('*'))
        consumers = list(unresolved)
        consumers.extend(x for x in refs if any(_matches(x['bdf'], member) for member in group_members or [bdf]))
        consumers.extend(_holders(path))
        result.append({'bdf': bdf, 'vendor': _read(path / 'vendor'), 'device': _read(path / 'device'),
                       'class': kind, 'driver': _link(path / 'driver'), 'iommu_group': group,
                       'group_members': group_members, 'physfn': _link(path / 'physfn'),
                       'vfs': sorted(p.resolve().name for p in path.glob('virtfn*')),
                       'total_vfs': int(_read(path / 'sriov_totalvfs', '0')),
                       'enabled_vfs': int(_read(path / 'sriov_numvfs', '0')),
                       'boot_vga': _read(path / 'boot_vga', '0') == '1', 'consumers': consumers})
    return result


def _one(bdf):
    bdf = canonical_bdf(bdf)
    devices = [item for item in inventory() if item['bdf'] == bdf]
    need(len(devices) == 1, 'GPU/VF does not exist: ' + bdf)
    return devices[0]


def validate_assignment(bdf, vmid):
    device = _one(bdf)
    target = str(vmid) if vmid is not None else None
    need(device['iommu_group'], 'Device has no IOMMU group')
    need(device['group_members'] == [device['bdf']], 'Only individually isolated GPU/VFs are supported')
    need(device['driver'] == 'vfio-pci', 'Prepare the device in menu 03 before assigning it')
    need(not device['boot_vga'], 'The host boot GPU cannot be assigned by this workflow')
    if device['physfn']:
        pf = _one(device['physfn'])
        need(pf['driver'] == 'i915' and pf['vendor'] == '0x8086', 'Intel VF parent is not ready under i915')
    blocked = [user for user in device['consumers'] if target is None or user.get('vmid') != target]
    need(not blocked, 'Device/group is occupied: ' + json.dumps(blocked, sort_keys=True))
    return device


def available_vfs(target_vmid=None):
    result = []
    for device in inventory():
        if not device['physfn']:
            continue
        try:
            result.append(validate_assignment(device['bdf'], target_vmid))
        except RuntimeError:
            continue
    return result


def _fingerprint(path):
    path = Path(path)
    if not os.path.lexists(path):
        return {'absent': True}
    info = path.lstat()
    need(stat.S_ISREG(info.st_mode), 'Expected a regular managed file: ' + str(path))
    attributes = {name: hashlib.sha256(os.getxattr(path, name)).hexdigest()
                  for name in os.listxattr(path)} if hasattr(os, 'listxattr') else {}
    return {'uid': info.st_uid, 'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode),
            'mtime_ns': info.st_mtime_ns, 'sha256': _sha(path), 'xattrs': attributes}


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): digest.update(block)
    return digest.hexdigest()


def _save(state):
    GPU_STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    need(GPU_STATE.resolve() == GPU_STATE and GPU_STATE.stat().st_uid == 0
         and not GPU_STATE.stat().st_mode & 0o077, 'GPU state directory must be root-private')
    atomic_json(GPU_STATE / (state['resource_id'] + '.json'), state)


@contextlib.contextmanager
def _mutation_lock(dry_run):
    if dry_run:
        yield; return
    need(fcntl is not None, 'GPU preparation requires Linux')
    GPU_STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    need(GPU_STATE.resolve() == GPU_STATE and GPU_STATE.stat().st_uid == 0
         and not GPU_STATE.stat().st_mode & 0o077, 'GPU state directory must be root-private')
    descriptor = os.open(GPU_STATE/'operation.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def _file_owned(entry, current):
    if 'after' in entry:
        return current == entry['after']
    return (entry.get('before', {}).get('absent') and current.get('sha256') == entry.get('intent_sha256')
            and current.get('uid') == 0 and current.get('gid') == 0
            and current.get('mode') == entry.get('intent_mode') and not current.get('xattrs'))


def _owned_file(state, path, content, mode=0o644):
    path = Path(path)
    before = _fingerprint(path)
    entry = state['files'].get(str(path))
    if entry:
        need(_file_owned(entry, before), 'Managed file was edited outside onePVE: ' + str(path))
    else:
        need(before.get('absent'), 'Refusing to replace an existing file: ' + str(path))
        data = content if isinstance(content, bytes) else content.encode()
        state['files'][str(path)] = {'before': before, 'intent_sha256': hashlib.sha256(data).hexdigest(), 'intent_mode': mode}
        _save(state)
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    need(path.parent.resolve() == path.parent, 'Managed file ancestor is a symlink')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(content if isinstance(content, bytes) else content.encode())
        stream.flush(); os.fsync(stream.fileno())
    path.chmod(mode)
    state['files'][str(path)]['after'] = _fingerprint(path)
    _save(state)


def _secure_module_policy():
    need(_read('/proc/sys/kernel/modules_disabled', '0') == '0', 'Kernel module loading is disabled')
    need(_read('/sys/module/module/parameters/sig_enforce', 'N') in {'N', '0'}, 'Module signature enforcement requires a separately signed driver')
    lockdown = Path('/sys/kernel/security/lockdown')
    need(not lockdown.exists() or '[none]' in lockdown.read_text(), 'Secure Boot/lockdown requires a signed driver; onePVE will not disable it')
    for path in Path('/sys/firmware/efi/efivars').glob('SecureBoot-*'):
        need(path.read_bytes()[4:5] != b'\x01', 'Secure Boot requires a signed driver; automatic unsigned preparation is refused')


def _module_field(name, field, kernel):
    p = run(['modinfo', '-k', kernel, '-F', field, name], check=False)
    return p.stdout.strip() if p.returncode == 0 else ''


def _download_source(spec, cache):
    cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if cache.exists():
        need(not cache.is_symlink() and hashlib.sha256(cache.read_bytes()).hexdigest() == spec['sha256'], 'Cached driver archive checksum differs')
        return
    temporary = cache.with_suffix(cache.suffix + '.partial')
    need(not temporary.exists(), 'Interrupted source download exists; inspect it before retrying')
    with urllib.request.urlopen(spec['url'], timeout=60) as response, temporary.open('xb') as output:
        digest, size = hashlib.sha256(), 0
        while block := response.read(1024 * 1024):
            size += len(block); need(size <= 32 * 1024 * 1024, 'Driver source archive exceeds expected limit')
            digest.update(block); output.write(block)
    need(digest.hexdigest() == spec['sha256'], 'Downloaded driver source checksum differs')
    temporary.replace(cache)


def _extract_source(archive, destination):
    need(not destination.exists(), 'Driver source directory already exists')
    with tarfile.open(archive, 'r:gz') as stream:
        members = stream.getmembers()
        roots = {m.name.split('/')[0] for m in members}
        need(len(roots) == 1 and all(not Path(m.name).is_absolute() and not m.name.startswith('/')
             and '\\' not in m.name and ':' not in m.name and '..' not in Path(m.name).parts
             and (m.isfile() or m.isdir()) for m in members), 'Unexpected driver archive paths/types')
        need(sum(m.size for m in members) <= 256 * 1024 * 1024, 'Driver source expands beyond expected size')
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            stream.extractall(temporary, members=members)
            extracted = Path(temporary) / roots.pop()
            for path in [extracted, *extracted.rglob('*')]:
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o022)
                if hasattr(os, 'chown'): os.chown(path, 0, 0)
            shutil.move(str(extracted), str(destination))


def _source_hash(source):
    need(source.resolve() == source and source.stat().st_uid == 0 and not source.stat().st_mode & 0o022,
         'DKMS source root is redirected or writable outside root')
    records = {}
    for path in sorted(source.rglob('*')):
        info = path.lstat()
        need(not path.is_symlink() and info.st_uid == 0 and not info.st_mode & 0o022,
             'DKMS source tree is redirected or writable outside root')
        if path.is_file(): records[str(path.relative_to(source))] = _sha(path)
    return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


def _verify_built_modules(kernel, build, modules):
    names = {'i915', 'xe', 'kvmgt', 'intel_sriov_compat'}
    need(len(modules) == 4 and {p.name.split('.ko')[0] for p in modules} == names, 'Expected four companion modules')
    def pairs(text, label):
        result = {}
        for line in text.splitlines():
            fields = line.split()
            need(len(fields) == 2 and re.fullmatch(r'(?:0x)?[0-9a-fA-F]{1,8}', fields[0]), 'Invalid symbol CRC evidence: ' + label)
            crc, name = fields; crc = crc.lower().removeprefix('0x').zfill(8)
            need(name not in result, 'Duplicate symbol CRC evidence: ' + name)
            result[name] = crc
        return result
    path = Path('/lib/modules') / kernel / 'build/Module.symvers'
    need(path.is_file(), 'Missing symbol CRC evidence: ' + str(path))
    symbols = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        need(len(fields) >= 3, 'Invalid kernel Module.symvers evidence')
        # The replacement modules supply their own ABI; never resolve an absent
        # replacement export from the stock i915/xe/kvmgt table instead.
        if Path(fields[2]).name.removesuffix('.ko') in names:continue
        value = pairs(' '.join(fields[:2]), str(path))
        need(not set(value).intersection(symbols), 'Duplicate kernel symbol CRC evidence')
        symbols.update(value)
    need(symbols, 'Kernel symbol CRC table is empty')
    exports = {}; magics = {}
    for module in modules:
        magic = run(['modinfo', '-F', 'vermagic', str(module)]).stdout.strip()
        need(magic.split() and magic.split()[0] == kernel, 'Built module vermagic does not match the selected kernel')
        magics[str(module)] = magic
        exported = run(['modprobe', '--show-exports', str(module)], check=False, env=dict(os.environ, LC_ALL='C'))
        no_exports = (exported.returncode == 1 and not exported.stdout.strip()
                      and exported.stderr.strip().endswith(': No data available'))
        need(exported.returncode == 0 or no_exports, 'Cannot inspect companion exports: ' + str(module) + ': ' + exported.stderr[-300:])
        actual = pairs(exported.stdout, str(module))
        need(not set(actual).intersection(exports) and not set(actual).intersection(symbols), 'Duplicate kernel/companion symbol exporter')
        exports.update(actual)
    symbols.update(exports)
    records = []
    for module in modules:
        required = run(['modprobe', '--dump-modversions', str(module)]).stdout
        imports = pairs(required, str(module));need(imports, 'Built module has no checked symbol CRCs')
        for name, crc in imports.items():
            need(symbols.get(name) == crc, 'Kernel/companion symbol CRC mismatch: ' + name)
        records.append({'path': str(module), 'vermagic': magics[str(module)],
                        'sha256': _sha(module), 'symbols_checked': len(imports),
                        'companion_symbols_checked': len(set(imports).intersection(exports)),
                        'kernel_symvers_sha256': _sha(path)})
    return records


def _ensure_driver(state, spec):
    kernel, version = spec['kernel'], spec['version']
    if _module_field('i915', 'version', kernel) == version + '-sriov':
        for name in ['i915', 'xe', 'kvmgt', 'intel_sriov_compat']:
            magic = _module_field(name, 'vermagic', kernel).split()
            need(_module_field(name, 'version', kernel) == version + '-sriov'
                 and magic and magic[0] == kernel, 'Existing SR-IOV companion modules are incomplete or have wrong vermagic')
        state['driver'] = dict(spec, owned=False); _save(state); return
    _secure_module_policy()
    other_intel = [d for d in inventory() if d['vendor'] == '0x8086' and not d['physfn'] and d['bdf'] != state['bdf']]
    need(not other_intel, 'Another Intel GPU shares the driver; automatic replacement is refused')
    source = Path('/usr/src') / ('i915-sriov-dkms-' + version)
    previous = state.get('reusable_source', {})
    if source.exists():
        need(previous.get('owned') and previous.get('source') == str(source)
             and previous.get('version') == version and previous.get('sha256') == spec['sha256']
             and previous.get('source_tree_sha256') == _source_hash(source),
             'Existing unowned/edited DKMS source; refusing to replace it')
    existing = run(['dkms', 'status', '-m', 'i915-sriov-dkms', '-v', version], check=False) if shutil.which('dkms') else None
    need(not existing or not existing.stdout.strip(), 'This DKMS version is already registered outside onePVE')
    headers = Path('/lib/modules') / kernel / 'build/Makefile'
    packages = ['build-essential', 'dkms', 'ca-certificates', 'pve-firmware']
    if not headers.exists():
        candidates = ['proxmox-headers-' + kernel, 'pve-headers-' + kernel]
        available = [p for p in candidates if run(['apt-cache', 'show', p], check=False).returncode == 0]
        need(available, 'Matching installed-kernel headers are unavailable; no kernel upgrade will be attempted')
        packages.append(available[0])
    state['status'] = 'INSTALLING_DRIVER_PREREQUISITES'; _save(state)
    run(['apt-get', 'update'], timeout=None)
    run(['apt-get', 'install', '-y', '--no-install-recommends', *packages], timeout=None)
    need(headers.exists(), 'Matching kernel headers were not installed')
    cache = Path('/var/cache/onepve/drivers') / (version + '.tar.gz')
    _download_source(spec, cache)
    if not source.exists(): _extract_source(cache, source)
    state['driver'] = dict(spec, owned=True, source=str(source), source_tree_sha256=_source_hash(source), stage='SOURCE_EXTRACTED'); _save(state)
    run(['dkms', 'add', '-m', 'i915-sriov-dkms', '-v', version], timeout=None)
    state['driver']['stage'] = 'BUILDING'; _save(state)
    run(['dkms', 'build', '-m', 'i915-sriov-dkms', '-v', version, '-k', kernel], timeout=None)
    build = Path('/var/lib/dkms/i915-sriov-dkms') / version / 'build'
    output = Path('/var/lib/dkms/i915-sriov-dkms') / version / kernel / os.uname().machine / 'module'
    modules = sorted(output.glob('*.ko*'))
    state['driver']['build_checks'] = _verify_built_modules(kernel, build, modules); _save(state)
    run(['dkms', 'install', '-m', 'i915-sriov-dkms', '-v', version, '-k', kernel], timeout=None)
    installed = [_module_field(name, 'filename', kernel) for name in ['i915', 'xe', 'kvmgt', 'intel_sriov_compat']]
    need(all(installed), 'DKMS did not install all companion modules')
    state['driver'].update(stage='INSTALLED', installed={p: _fingerprint(Path(p)) for p in installed})
    _save(state)


def boot_method():
    if Path('/sys/firmware/efi').exists() and Path('/etc/kernel/proxmox-boot-uuids').exists():
        result = run(['proxmox-boot-tool', 'status'], check=False)
        need(result.returncode == 0, 'Cannot determine proxmox-boot-tool boot configuration')
        if re.search(r'configured with:\s*uefi', result.stdout, re.I):
            return 'systemd-boot'
    need(Path('/etc/default/grub').is_file(), 'Unsupported bootloader; no boot files changed')
    return 'grub'


def _stage_boot(state, parameters):
    cmdline = _read('/proc/cmdline').split()
    need(not any(x in cmdline for x in ['intel_iommu=off', 'amd_iommu=off', 'pci=nommconf']), 'Conflicting explicit boot policy needs operator review')
    missing = []
    for argument in parameters:
        key, _, value = argument.partition('=')
        existing = [item.partition('=')[2] for item in cmdline if item.partition('=')[0] == key]
        if key == 'module_blacklist' and existing:
            modules = {part for item in existing for part in item.split(',')}
            need(all(re.fullmatch(r'[A-Za-z0-9_-]+', part) for part in modules), 'Unrecognized module blacklist token')
            need('i915' not in modules, 'The boot policy blacklists i915; review the conflicting configuration first')
            if value not in modules: missing.append(key + '=' + ','.join(sorted(modules | {value})))
        else:
            need(not existing or set(existing) == {value}, 'Conflicting boot parameter requires review: ' + key)
            if not existing: missing.append(argument)
    if not missing:
        return False
    method = boot_method(); state['boot_method'] = method
    if method == 'grub':
        path = Path('/etc/default/grub.d') / ('onepve-gpu-' + state['resource_id'] + '.cfg')
        value = ' '.join(missing)
        _owned_file(state, path, '# Managed by onePVE; existing command line remains intact.\nGRUB_CMDLINE_LINUX_DEFAULT="${GRUB_CMDLINE_LINUX_DEFAULT} ' + value + '"\n')
        run(['update-grub'], timeout=120)
    else:
        path = Path('/etc/kernel/cmdline')
        before = _fingerprint(path)
        need(not before.get('absent'), 'systemd-boot command line is missing')
        old = path.read_bytes()
        state['cmdline_before'] = dict(metadata=before, bytes=base64.b64encode(old).decode())
        # This bootloader has one canonical command-line file rather than snippets.
        new = old.decode().strip() + ' ' + ' '.join(missing) + '\n'
        state['cmdline_intent_sha256'] = hashlib.sha256(new.encode()).hexdigest(); _save(state)
        path.write_text(new)
        state['cmdline_after'] = _fingerprint(path); _save(state)
        run(['proxmox-boot-tool', 'refresh'], timeout=120)
    run(['update-initramfs', '-u', '-k', os.uname().release], timeout=180)
    return True


def _iommu_parameter():
    cpu = _read('/proc/cpuinfo')
    if 'GenuineIntel' in cpu: return 'intel_iommu=on'
    if 'AuthenticAMD' in cpu: return 'amd_iommu=on'
    raise RuntimeError('Unsupported CPU vendor; no IOMMU boot policy was guessed')


def _install_service(state):
    runtime = Path('/usr/local/lib/onepve-gpu') / state['resource_id'] / 'host-ready.py'
    _owned_file(state, runtime, (repo_root() / 'gpu/host-ready.py').read_bytes(), 0o755)
    unit = 'onepve-gpu-' + state['resource_id'] + '.service'
    content = ('[Unit]\nDescription=onePVE GPU resource ' + state['bdf'] + '\nAfter=local-fs.target pve-cluster.service\n'
               'Before=pve-guests.service\n\n[Service]\nType=oneshot\nRemainAfterExit=yes\n'
               'ExecStart=/usr/bin/python3 ' + str(runtime) + ' --state ' + str(GPU_STATE / (state['resource_id'] + '.json')) + '\n'
               'TimeoutStartSec=60\n\n[Install]\nWantedBy=multi-user.target\n')
    _owned_file(state, Path('/etc/systemd/system') / unit, content)
    state['service'] = unit; _save(state)
    run(['systemctl', 'daemon-reload']); run(['systemctl', 'enable', unit])
    return runtime


def _adopt(device, mode, dry_run):
    record = GPU_STATE / (device['bdf'].replace(':', '_').replace('.', '_') + '.json')
    if record.exists():
        old = json.loads(record.read_text())
        if old.get('owned'):
            need(old['mode'] == mode and old['bdf'] == device['bdf']
                 and old['status'] in {'READY', 'NEEDS_REBOOT'}, 'Owned GPU preparation needs review before adoption')
            need(all(_file_owned(item, _fingerprint(Path(name))) for name, item in old['files'].items()), 'Owned GPU file drift')
            old['status'] = 'READY'
            if not dry_run: _save(old)
            return old
    state = {'resource_id': device['bdf'].replace(':', '_').replace('.', '_'), 'bdf': device['bdf'],
             'mode': mode, 'status': 'EXTERNAL_READY', 'owned': False, 'device': device,
             'files': {}, 'external_provider_preserved': True}
    if not dry_run:
        _save(state)
    return state


def _new_state(device, mode):
    resource = device['bdf'].replace(':', '_').replace('.', '_')
    path = GPU_STATE / (resource + '.json')
    previous = None
    if path.exists():
        old = json.loads(path.read_text())
        if old.get('owned') and old.get('status') in {'READY', 'NEEDS_REBOOT'}:
            need(old['bdf'] == device['bdf'] and old['mode'] == mode, 'Resource mode differs; restore it first')
            for name, record in old['files'].items():
                need(_fingerprint(Path(name)) == record['after'], 'Owned file drift: ' + name)
            need(old['status'] != 'READY', 'Owned resource is no longer ready at runtime; inspect its startup service instead of resetting it')
            return old
        allowed = old.get('status') in {'RESTORED', 'EXTERNAL_READY', 'EXTERNAL_PRESERVED'}
        allowed |= old.get('status') == 'RESTORED_NEEDS_REBOOT' and old.get('boot_id') != _read('/proc/sys/kernel/random/boot_id')
        need(allowed, 'Interrupted/failed preparation requires status/restore; a restored driver may require reboot')
        if old.get('owned') and old.get('status', '').startswith('RESTORED'):
            previous = old.get('driver')
    result = {'resource_id': resource, 'bdf': device['bdf'], 'vendor': device['vendor'], 'device_id': device['device'],
            'mode': mode, 'owned': True, 'status': 'PREPARING', 'files': {},
            'kernel': os.uname().release, 'boot_id': _read('/proc/sys/kernel/random/boot_id')}
    if previous: result['reusable_source'] = previous
    return result


def _prepare_sriov(pf, num_vfs=1, dry_run=False):
    require_root(); device = _one(pf)
    need(type(num_vfs) is int and 1 <= num_vfs <= 7, 'VF count must be 1..7')
    need(device['vendor'] == '0x8086' and not device['physfn'], 'Select an Intel GPU physical function')
    if device['driver'] == 'i915' and device['enabled_vfs']:
        need(device['enabled_vfs'] >= num_vfs, 'Existing VF allocation will not be reset or expanded')
        children = [_one(bdf) for bdf in device['vfs']]
        need(len(children) == device['enabled_vfs'], 'Existing VF enumeration is incomplete; retry read-only detection later')
        need(all(x['driver'] == 'vfio-pci' and x['group_members'] == [x['bdf']] for x in children),
             'Existing VFs are not prepared/isolated; no automatic rebinding of external VFs')
        return dict(_adopt(device, 'sriov', dry_run), vfs=children, dry_run=dry_run)
    need(not device['consumers'], 'PF/group is occupied; no driver or VF changes allowed')
    need(not device['enabled_vfs'], 'Existing VFs must not be reset')
    spec = driver_spec(os.uname().release)
    actions = ['verify module-signing policy', 'install matching-kernel headers and pinned DKMS source if necessary',
               'verify vermagic and symbol CRCs', 'stage owned boot configuration and VF-ready service']
    if dry_run:
        return {'status': 'DRY_RUN', 'bdf': device['bdf'], 'mode': 'sriov', 'driver': spec, 'actions': actions,
                'num_vfs': num_vfs, 'host_reboot_automatic': False}
    state = _new_state(device, 'sriov')
    if state['status'] in {'READY', 'NEEDS_REBOOT'}:
        return state
    state.update(num_vfs=num_vfs, original_num_vfs=0, original_autoprobe=_read(PCI / device['bdf'] / 'sriov_drivers_autoprobe', '1'))
    _save(state)
    try:
        _ensure_driver(state, spec)
        parameters = ['intel_iommu=on', 'i915.enable_guc=3', 'i915.max_vfs=' + str(num_vfs), 'module_blacklist=xe']
        if device['device'] == '0x7d67': parameters.append('i915.force_probe=7d67')
        boot_changed = _stage_boot(state, parameters)
        if not boot_changed and state.get('driver', {}).get('owned'):
            run(['update-initramfs', '-u', '-k', state['kernel']], timeout=180)
        _install_service(state)
        state['status'] = 'NEEDS_REBOOT'; _save(state)
        return state
    except BaseException as error:
        state['status'] = 'FAILED'; state['error'] = str(error); _save(state); raise


def _prepare_passthrough(bdf, dry_run=False):
    require_root(); device = _one(bdf)
    need(not device['physfn'] and not device['vfs'], 'Use SR-IOV preparation for PF/VF devices')
    need(not device['iommu_group'] or device['group_members'] == [device['bdf']], 'Only a singly isolated device is supported; no ACS override is added')
    need(not device['boot_vga'], 'Host boot GPU passthrough requires a separate console plan; refusing automatic preparation')
    if device['driver'] == 'vfio-pci':
        return dict(_adopt(device, 'passthrough', dry_run), dry_run=dry_run)
    need(not device['consumers'], 'Device/group is occupied')
    if dry_run:
        return {'status': 'DRY_RUN', 'mode': 'passthrough', 'bdf': device['bdf'],
                'actions': ['stage selected-BDF vfio binding service'], 'host_reboot_automatic': False}
    state = _new_state(device, 'passthrough')
    if state['status'] in {'READY', 'NEEDS_REBOOT'}:
        return state
    state['original_driver'] = device['driver']; _save(state)
    try:
        if not device['iommu_group']: _stage_boot(state, [_iommu_parameter()])
        _install_service(state)
        state['status'] = 'NEEDS_REBOOT'; _save(state)
        return state
    except BaseException as error:
        state['status'] = 'FAILED'; state['error'] = str(error); _save(state); raise


def status():
    devices = inventory()
    by_bdf = {item['bdf']: item for item in devices}
    records = []
    for path in sorted(GPU_STATE.glob('*.json')):
        record = json.loads(path.read_text())
        restored = record.get('status', '').startswith('RESTORED')
        record['file_drift'] = [name for name, item in record.get('files', {}).items()
                                if (_fingerprint(Path(name)) != item['before'] if restored
                                    else not _file_owned(item, _fingerprint(Path(name))))]
        device = by_bdf.get(record['bdf'])
        ready = device and device['driver'] == 'vfio-pci' if record['mode'] == 'passthrough' else (
            device and device['driver'] == 'i915' and device['enabled_vfs'] >= record.get('num_vfs', 1)
            and len(device['vfs']) == device['enabled_vfs']
            and all(by_bdf.get(child, {}).get('driver') == 'vfio-pci' for child in device['vfs']))
        record['runtime_status'] = 'READY' if ready else 'NOT_READY'
        records.append(record)
    return {'devices': devices, 'resources': records, 'host_kernel': os.uname().release,
            'iommu_available': bool(list(Path('/sys/kernel/iommu_groups').glob('*')))}


def _restore(resource_id, dry_run=False):
    require_root(); need(re.fullmatch(r'[0-9a-f]{4}_[0-9a-f]{2}_[0-9a-f]{2}_[0-7]', resource_id), 'Invalid resource ID')
    path = GPU_STATE / (resource_id + '.json'); need(path.is_file() and not path.is_symlink(), 'GPU resource state is missing')
    state = json.loads(path.read_text())
    if state.get('status', '').startswith('RESTORED'):
        return state
    if not state.get('owned'):
        if not dry_run:
            state['status'] = 'EXTERNAL_PRESERVED'; _save(state)
        return {'status': 'EXTERNAL_PRESERVED', 'bdf': state['bdf'], 'external_configuration_removed': False}
    device = _one(state['bdf'])
    need(state['status'] != 'RESTORED', 'Resource is already restored')
    affected = [device] + [_one(x) for x in device['vfs']]
    need(not any(x['consumers'] for x in affected), 'Detach/stop device consumers before restoring GPU preparation')
    for name, entry in state['files'].items():
        current = _fingerprint(Path(name))
        need(current.get('absent') and entry['before'].get('absent') or _file_owned(entry, current),
             'Refusing to undo externally modified file: ' + name)
    if 'cmdline_before' in state:
        current = _fingerprint(Path('/etc/kernel/cmdline'))
        need(current == state.get('cmdline_after') or current == state['cmdline_before']['metadata']
             or ('cmdline_after' not in state and current.get('sha256') == state.get('cmdline_intent_sha256')
                 and all(current.get(k) == state['cmdline_before']['metadata'].get(k) for k in ['uid','gid','mode'])),
             'Canonical boot command line was edited outside onePVE')
    if state.get('driver', {}).get('owned'):
        need(not any(x['vendor'] == '0x8086' and not x['physfn'] and x['bdf'] != state['bdf'] for x in inventory()),
             'Another Intel GPU now shares the owned driver; automatic driver removal is refused')
        for name, expected in state['driver'].get('installed', {}).items():
            need(_fingerprint(Path(name)) == expected, 'Driver module was edited outside onePVE')
    if dry_run:
        return {'status': 'DRY_RUN', 'bdf': state['bdf'], 'actions': ['release only unused owned devices', 'restore exact owned configuration and driver version']}
    if state.get('service'):
        run(['systemctl', 'disable', '--now', state['service']], timeout=90)
    if state['mode'] == 'sriov' and device['enabled_vfs']:
        need(device['enabled_vfs'] == state['num_vfs'], 'VF allocation changed outside onePVE')
        (PCI / state['bdf'] / 'sriov_numvfs').write_text('0')
        (PCI / state['bdf'] / 'sriov_drivers_autoprobe').write_text(state['original_autoprobe'])
    elif state['mode'] == 'passthrough' and device['driver'] == 'vfio-pci':
        target = PCI / state['bdf']; (target / 'driver/unbind').write_text(state['bdf'])
        (target / 'driver_override').write_text('\n')
        run(['modprobe', state['original_driver']], check=True) if state.get('original_driver') else None
        Path('/sys/bus/pci/drivers_probe').write_text(state['bdf'])
    for name in reversed(list(state['files'])):
        entry = state['files'][name]
        need(entry['before'].get('absent'), 'Unexpected original file; do not invent a restore')
        if Path(name).exists(): Path(name).unlink()
    if 'cmdline_before' in state:
        original = state['cmdline_before']; target = Path('/etc/kernel/cmdline')
        target.write_bytes(base64.b64decode(original['bytes'])); target.chmod(original['metadata']['mode'])
        os.chown(target, original['metadata']['uid'], original['metadata']['gid'])
        os.utime(target, ns=(original['metadata']['mtime_ns'], original['metadata']['mtime_ns']))
    driver = state.get('driver', {})
    if driver.get('owned'):
        registered = run(['dkms', 'status', '-m', 'i915-sriov-dkms', '-v', driver['version']], check=False)
        if registered.stdout.strip():
            run(['dkms', 'remove', '-m', 'i915-sriov-dkms', '-v', driver['version'], '--all'], timeout=None)
        # Keep source/build evidence; it is small and may explain a failed build.
    run(['systemctl', 'daemon-reload'])
    if state.get('boot_method') == 'grub':
        run(['update-grub'], timeout=120)
    elif state.get('boot_method') == 'systemd-boot':
        run(['proxmox-boot-tool', 'refresh'], timeout=120)
    if driver.get('owned') or state.get('boot_method'):
        run(['update-initramfs', '-u', '-k', state['kernel']], timeout=180)
    state['status'] = 'RESTORED_NEEDS_REBOOT' if driver.get('owned') or state.get('boot_method') else 'RESTORED'; _save(state)
    return state


def prepare_sriov(pf, num_vfs=1, dry_run=False):
    require_root()
    with _mutation_lock(dry_run):
        return _prepare_sriov(pf, num_vfs, dry_run)


def prepare_passthrough(bdf, dry_run=False):
    require_root()
    with _mutation_lock(dry_run):
        return _prepare_passthrough(bdf, dry_run)


def restore(resource_id, dry_run=False):
    require_root()
    with _mutation_lock(dry_run):
        return _restore(resource_id, dry_run)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('status'); commands.add_parser('inventory')
    commands.add_parser('available-vfs').add_argument('--vmid', type=int)
    for name in ['prepare-sriov', 'prepare-passthrough']:
        command = commands.add_parser(name)
        command.add_argument(*(['--bdf', '--pf'] if name == 'prepare-sriov' else ['--bdf']), dest='bdf', required=True)
        command.add_argument('--dry-run', action='store_true')
        if name == 'prepare-sriov': command.add_argument('--num-vfs', type=int, default=1)
    command = commands.add_parser('restore'); command.add_argument('--resource-id', '--resource', dest='resource_id', required=True); command.add_argument('--dry-run', action='store_true')
    command = commands.add_parser('check-ready'); command.add_argument('--bdf', required=True); command.add_argument('--vmid', type=int, required=True)
    command = commands.add_parser('driver-spec'); command.add_argument('--kernel', required=True)
    args = parser.parse_args(argv)
    if args.action == 'inventory': result = inventory()
    elif args.action == 'status': result = status()
    elif args.action == 'available-vfs': result = available_vfs(args.vmid)
    elif args.action == 'prepare-sriov': result = prepare_sriov(args.bdf, args.num_vfs, args.dry_run)
    elif args.action == 'prepare-passthrough': result = prepare_passthrough(args.bdf, args.dry_run)
    elif args.action == 'restore': result = restore(args.resource_id, args.dry_run)
    elif args.action == 'check-ready': result = validate_assignment(args.bdf, args.vmid)
    else: result = driver_spec(args.kernel)
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({'status': 'ERROR', 'error': str(error)}, ensure_ascii=False), file=sys.stderr); sys.exit(1)

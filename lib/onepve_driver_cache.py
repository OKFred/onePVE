"""Small, verified guest-module archives; no cache or host changes at import."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import zlib

from onepve_common import run
import onepve_gpu as gpu

MAX_ARCHIVE_BYTES = 512 * 1024**2
MAX_EXPANDED_BYTES = 1024**3
MODULES = {'i915', 'xe', 'kvmgt', 'intel_sriov_compat'}


def need(value, message):
    if not value:
        raise RuntimeError(message)


def _dkms(*args):
    # Long builds inherit the persistent job log and have no short command timeout.
    subprocess.run(['dkms', *args], check=True)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def identity(spec, arch):
    for value in [spec['kernel'], spec['version'], arch]:
        need(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,127}', value),
             'Invalid driver cache identity')
    need(re.fullmatch(r'[0-9a-f]{64}', spec['sha256']), 'Invalid driver source checksum')
    return dict(kernel=spec['kernel'], arch=arch, driver_version=spec['version'], source_sha256=spec['sha256'])


def key(spec, arch):
    return hashlib.sha256(json.dumps(identity(spec, arch), sort_keys=True).encode()).hexdigest()


def cache_path(root, spec, arch):
    return Path(root) / (key(spec, arch) + '.tar.gz')


def _private(path, create=False):
    path = Path(path).absolute()
    need(path.resolve() == path, 'Driver cache path is redirected')
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    need(path.is_dir() and not path.is_symlink(), 'Driver cache directory is unavailable')
    if os.name == 'posix':
        info = path.stat()
        need(info.st_uid == os.geteuid() and not info.st_mode & 0o022, 'Driver cache directory is not privately owned')
    return path


def inspect_archive(data, spec, arch):
    """Validate before extraction; never allow executable metadata from a cache."""
    need(0 < len(data) <= MAX_ARCHIVE_BYTES, 'Driver archive exceeds the compressed size limit')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
            expanded = compressed.read(MAX_EXPANDED_BYTES + 1)
    except (EOFError, zlib.error) as error:
        raise RuntimeError('Incomplete or corrupt compressed driver archive') from error
    need(len(expanded) <= MAX_EXPANDED_BYTES, 'Driver archive exceeds the expanded size limit')
    with tarfile.open(fileobj=io.BytesIO(expanded), mode='r:') as stream:
        members = stream.getmembers()
        need(len(members) == 5 and all(m.type == tarfile.REGTYPE and m.sparse is None for m in members),
             'Driver archive must contain five regular files')
        need(all(0 <= m.size <= MAX_EXPANDED_BYTES for m in members) and sum(m.size for m in members) <= MAX_EXPANDED_BYTES,
             'Driver archive member sizes exceed the expanded size limit')
        names = [m.name for m in members]
        need(len(set(names)) == 5 and 'manifest.json' in names, 'Duplicate or missing cache entries')
        need(next(m.size for m in members if m.name == 'manifest.json') <= 16384, 'Driver manifest is too large')
        blobs = {m.name: stream.extractfile(m).read() for m in members}
    manifest = json.loads(blobs.pop('manifest.json'))
    need(isinstance(manifest, dict) and manifest.get('schema_version') == 1, 'Unknown driver cache schema')
    need(manifest.get('identity') == identity(spec, arch), 'Driver cache kernel, architecture or source differs')
    files = manifest.get('files')
    need(isinstance(files, dict) and set(files) == set(blobs), 'Driver cache file list differs')
    actual_names = set()
    for name, content in blobs.items():
        match = re.fullmatch(r'modules/(i915|xe|kvmgt|intel_sriov_compat)\.ko(?:\.(?:xz|gz|zst))?', name)
        need(match and match[1] not in actual_names, 'Unexpected or duplicate driver module')
        actual_names.add(match[1])
        need(files[name] == hashlib.sha256(content).hexdigest(), 'Driver cache module checksum differs: ' + name)
    need(actual_names == MODULES, 'Driver cache lacks companion modules')
    headers = manifest.get('headers')
    need(isinstance(headers, dict) and set(headers) == {'symvers_sha256', 'config_sha256'}
         and all(isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v) for v in headers.values()),
         'Driver cache lacks kernel ABI evidence')
    return manifest, blobs


def load(root, spec, arch):
    root = _private(root)
    path = cache_path(root, spec, arch)
    need(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_ARCHIVE_BYTES,
         'No usable compiled-driver cache')
    if os.name == 'posix':
        need(path.stat().st_uid == os.geteuid() and not path.stat().st_mode & 0o022,
             'Driver archive is not privately owned')
    data = path.read_bytes()
    inspect_archive(data, spec, arch)
    return data


def store(root, spec, arch, data):
    inspect_archive(data, spec, arch)
    root = _private(root, True)
    path = cache_path(root, spec, arch)
    need(not path.is_symlink(), 'Refusing redirected driver archive')
    descriptor, name = tempfile.mkstemp(prefix='.driver-', dir=root)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return path


def _headers(kernel):
    root = Path('/lib/modules') / kernel / 'build'
    config = root / '.config'
    if not config.is_file():
        config = Path('/boot') / ('config-' + kernel)
    return dict(symvers_sha256=digest(root / 'Module.symvers'), config_sha256=digest(config))


def export_archive(path, spec, arch, modules):
    gpu._verify_built_modules(spec['kernel'], None, modules)
    manifest = dict(schema_version=1, identity=identity(spec, arch), headers=_headers(spec['kernel']),
                    files={'modules/' + p.name: digest(p) for p in modules})
    path = Path(path)
    need(path.parent.resolve() == path.parent.absolute() and not path.is_symlink(), 'Driver export path is redirected')
    descriptor, name = tempfile.mkstemp(prefix='.driver-export-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output, tarfile.open(fileobj=output, mode='w:gz', compresslevel=1) as stream:
            body = json.dumps(manifest, sort_keys=True).encode()
            info = tarfile.TarInfo('manifest.json'); info.size = len(body); info.mode = 0o600
            stream.addfile(info, io.BytesIO(body))
            for module in modules:
                need(module.is_file() and not module.is_symlink(), 'Module export must be a regular file')
                stream.add(module, arcname='modules/' + module.name, recursive=False)
        need(Path(name).stat().st_size <= MAX_ARCHIVE_BYTES, 'Driver export is too large')
        inspect_archive(Path(name).read_bytes(), spec, arch)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return path


def import_archive(path, spec, arch, source):
    """Verify modules, then let DKMS register/import a sanitized binary archive."""
    gpu._secure_module_policy()
    path = Path(path)
    need(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_ARCHIVE_BYTES,
         'Compiled-driver input is missing or too large')
    manifest, blobs = inspect_archive(path.read_bytes(), spec, arch)
    need(manifest['headers'] == _headers(spec['kernel']), 'Cached driver kernel headers or configuration differ')
    target = Path('/var/lib/dkms/i915-sriov-dkms') / spec['version'] / spec['kernel'] / arch
    registered = run(['dkms', 'status', '-m', 'i915-sriov-dkms', '-v', spec['version']], check=False).stdout.strip()
    need(not registered and not target.exists(), 'Existing DKMS tree is retained; use its normal build/install path')
    with tempfile.TemporaryDirectory(prefix='.onepve-driver-', dir=path.parent) as temporary:
        directory = Path(temporary)
        modules = []
        for name, content in blobs.items():
            module = directory / Path(name).name; module.write_bytes(content); modules.append(module)
        for module in modules:
            need(run(['modinfo', '-F', 'version', str(module)]).stdout.strip() == spec['version'] + '-sriov',
                 'Cached module version differs')
        gpu._verify_built_modules(spec['kernel'], None, modules)
        native = directory / 'dkms.tar.gz'
        with tarfile.open(native, 'w:gz', compresslevel=1) as stream:
            for module in modules:
                stream.add(module, arcname='dkms_main_tree/' + spec['kernel'] + '/' + arch + '/module/' + module.name)
            # Cache metadata must never supply a shell script for DKMS to source.
            stream.add(Path(source) / 'dkms.conf', arcname='dkms_binaries_only/dkms.conf')
        _dkms('ldtarball', str(native))
    return manifest


def prepare(spec, source, incoming, outgoing):
    """Guest path: failed cache validation is a compile miss, never a bypass."""
    kernel, arch = spec['kernel'], os.uname().machine
    names = sorted(MODULES)
    installed = all(run(['modinfo', '-k', kernel, '-F', 'version', name], check=False).stdout.strip()
                    == spec['version'] + '-sriov' for name in names)
    report = dict(status='INSTALLED' if installed else 'BUILT', reason='Matching modules already installed' if installed else 'No compiled cache', key=key(spec, arch))
    if not installed:
        try:
            import_archive(incoming, spec, arch, source)
            report.update(status='HIT', reason='Kernel, source, ABI and all four modules verified')
        except (RuntimeError, OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
            report['reason'] = str(error)
        registered = run(['dkms', 'status', '-m', 'i915-sriov-dkms', '-v', spec['version']], check=False)
        if not registered.stdout.strip():
            _dkms('add', '-m', 'i915-sriov-dkms', '-v', spec['version'])
        if report['status'] != 'HIT':
            _dkms('build', '-m', 'i915-sriov-dkms', '-v', spec['version'], '-k', kernel)
        modules = sorted((Path('/var/lib/dkms/i915-sriov-dkms') / spec['version'] / kernel / arch / 'module').glob('*.ko*'))
        gpu._verify_built_modules(kernel, None, modules)
        _dkms('install', '-m', 'i915-sriov-dkms', '-v', spec['version'], '-k', kernel)
    modules = [Path(run(['modinfo', '-k', kernel, '-F', 'filename', name]).stdout.strip()) for name in names]
    evidence = gpu._verify_built_modules(kernel, None, modules)
    for module in modules:
        need(run(['modinfo', '-F', 'version', str(module)]).stdout.strip() == spec['version'] + '-sriov',
             'Installed module version differs')
    try:
        export_archive(outgoing, spec, arch, modules)
        report.update(exported=True, bytes=Path(outgoing).stat().st_size)
    except (RuntimeError, OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
        report.update(exported=False, export_error=str(error))
    return dict(report, modules=evidence)

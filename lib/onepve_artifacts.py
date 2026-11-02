"""Verified deployment inputs. Only prepare_artifacts/download_verified write a cache."""
import contextlib
import datetime
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import posixpath
import re
import shutil
import stat
import struct
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile

from onepve_common import atomic_json, run
from onepve_gpu import driver_spec

CACHE_ROOT = Path('/var/cache/onepve')
CLOUD_ROOT = 'https://cloud.debian.org/images/cloud/trixie/'
NODE_VERSION = '26.13.0'
FNM_VERSION = 'v1.39.0'
MCP_VERSION = '0.0.83'
CODEX_VERSION = '0.166.0'
NODE_NAME = 'node-v' + NODE_VERSION + '-linux-x64'
NODE_ROOT = 'https://nodejs.org/dist/v' + NODE_VERSION + '/'
FNM_URL = 'https://github.com/Schniz/fnm/releases/download/' + FNM_VERSION + '/fnm-linux.zip'
VERSIONS = dict(node_version=NODE_VERSION, fnm_version=FNM_VERSION,
                mcp_version=MCP_VERSION, codex_version=CODEX_VERSION)
MAX_DOWNLOAD_BYTES = 2 * 1024**3


def need(value, message):
    if not value:
        raise RuntimeError(message)


def _url(value):
    parsed = urllib.parse.urlsplit(value)
    need(parsed.scheme == 'https' and parsed.hostname and not parsed.username
         and not parsed.password and not parsed.fragment, 'Artifact URL must be HTTPS without credentials or fragment')
    return value


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _url(newurl))


def _open_url(url):
    return urllib.request.build_opener(_HTTPSRedirect()).open(_url(url), timeout=60)


def _metadata(url):
    with _open_url(url) as response:
        data = response.read(2 * 1024**2 + 1)
    need(len(data) <= 2 * 1024**2, 'Artifact metadata is unexpectedly large')
    return data.decode('utf-8')


def _checksum(text, filename, algorithm):
    length = hashlib.new(algorithm).digest_size * 2
    matches = []
    for line in text.splitlines():
        found = re.fullmatch(r'([0-9a-fA-F]{' + str(length) + r'})\s+\*?(.+)', line.strip())
        if found and found[2] == filename:
            matches.append(found[1].lower())
    need(len(matches) == 1, 'Expected exactly one official checksum for ' + filename)
    return matches[0]


def resolve_image():
    """Read official metadata only; resolve latest once to an immutable build URL."""
    metadata_url = CLOUD_ROOT + 'latest/debian-13-generic-amd64.json'
    data = json.loads(_metadata(metadata_url))
    try:
        version = data['items'][0]['data']['info']['version']
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError('Unexpected Debian generic image metadata') from error
    need(isinstance(version, str) and re.fullmatch(r'20[0-9]{6}-[0-9]+', version), 'Unexpected Debian image build ID')
    datetime.datetime.strptime(version[:8], '%Y%m%d')
    filename = 'debian-13-generic-amd64-' + version + '.qcow2'
    base = CLOUD_ROOT + version + '/'
    checksum_url = base + 'SHA512SUMS'
    digest = _checksum(_metadata(checksum_url), filename, 'sha512')
    return dict(version=version, filename=filename, url=base + filename,
                sha512=digest, metadata_url=metadata_url, checksum_url=checksum_url)


def _private_dir(path):
    path = Path(path).absolute()
    need(path.parent.resolve() == path.parent, 'Cache directory has a redirected parent')
    if os.path.lexists(path):
        need(path.is_dir() and not path.is_symlink(), 'Cache path must be a real directory')
        if os.name == 'posix':
            need(path.stat().st_uid == os.geteuid() and not path.stat().st_mode & 0o022,
                 'Cache directory is not privately owned')
    else:
        path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def _digest(path, algorithm='sha256'):
    path = Path(path)
    need(path.is_file() and not path.is_symlink(), 'Cached artifact must be a regular file: ' + str(path))
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def _download(url, path, expected, algorithm, before_commit=None):
    _url(url)
    need(algorithm in {'sha256', 'sha512'}, 'Only SHA256 and SHA512 are accepted')
    path = Path(path).absolute()
    need(path.parent.resolve() == path.parent, 'Artifact path has a redirected parent')
    if os.path.lexists(path):
        need(expected is not None and _digest(path, algorithm) == expected,
             'Existing artifact differs or has no trusted digest; it was not overwritten: ' + str(path))
        return path
    _private_dir(path.parent)
    descriptor, part_name = tempfile.mkstemp(prefix='.' + path.name + '.part-', dir=path.parent)
    part = Path(part_name)
    digest, count = hashlib.new(algorithm), 0
    try:
        with os.fdopen(descriptor, 'wb') as output, _open_url(url) as response:
            while block := response.read(1024 * 1024):
                count += len(block)
                need(count <= MAX_DOWNLOAD_BYTES, 'Artifact exceeds the download size limit')
                digest.update(block); output.write(block)
            output.flush(); os.fsync(output.fileno())
        actual = digest.hexdigest()
        need(expected is None or actual == expected, 'Downloaded artifact checksum differs: ' + path.name)
        need(count > 0, 'Downloaded artifact is empty')
        if before_commit:
            before_commit(actual)
        # A hard-link commit never replaces a concurrently created/bad destination.
        try:
            os.link(part, path)
        except FileExistsError:
            need(_digest(path, algorithm) == actual, 'Artifact appeared with different content; not overwritten')
        return path
    finally:
        part.unlink(missing_ok=True)


def download_verified(url, path, expected, algorithm='sha256'):
    need(algorithm in {'sha256', 'sha512'}, 'Only SHA256 and SHA512 are accepted')
    need(isinstance(expected, str) and re.fullmatch(r'[0-9a-fA-F]{' + str(hashlib.new(algorithm).digest_size * 2) + '}', expected),
         'A complete expected checksum is required')
    return _download(url, path, expected.lower(), algorithm)


def _fnm_download(path):
    """GitHub's fixed release is trusted on first HTTPS download, then locally pinned."""
    record = CACHE_ROOT / 'fnm-download.json'
    if os.path.lexists(record):
        need(not record.is_symlink(), 'Refusing redirected fnm download record')
        data = json.loads(record.read_text())
        need(data.get('url') == FNM_URL and data.get('version') == FNM_VERSION, 'fnm cache version changed')
        return download_verified(FNM_URL, path, data['sha256'])
    need(not os.path.lexists(path), 'Unrecorded fnm archive exists; inspect it before retrying')
    return _download(FNM_URL, path, None, 'sha256',
                     lambda digest: atomic_json(record, dict(url=FNM_URL, version=FNM_VERSION, sha256=digest)))


def _validate_fnm(path):
    with zipfile.ZipFile(path) as stream:
        names = set()
        for item in stream.infolist():
            _relative(item.filename.rstrip('/'))
            need(item.filename not in names and item.file_size <= 128 * 1024**2,
                 'Unexpected fnm archive entries')
            names.add(item.filename)
            need(not stat.S_ISLNK(item.external_attr >> 16), 'fnm archive contains a symlink')
        need('fnm' in names, 'fnm archive has no Linux binary')
        with stream.open('fnm') as binary:
            header = binary.read(20)
            need(len(header) == 20 and header[:6] == b'\x7fELF\x02\x01'
                 and struct.unpack('<H', header[18:20])[0] == 62,
                 'fnm archive does not contain a Linux x86-64 ELF binary')


def _relative(value):
    need(isinstance(value, str) and value and not value.startswith(('/', '\\'))
         and not re.match(r'^[A-Za-z]:', value) and '\\' not in value and '\x00' not in value,
         'Invalid archive/cache path')
    path = PurePosixPath(value)
    need('..' not in path.parts and str(path) == value, 'Non-canonical archive/cache path')
    return path


def _tree(root, exclude=()):
    root = Path(root).resolve(strict=True)
    files, symlinks = {}, {}
    for parent, directories, names in os.walk(root, followlinks=False):
        for name in sorted(directories + names):
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if relative in exclude:
                continue
            _relative(relative)
            kind = path.lstat().st_mode
            if stat.S_ISLNK(kind):
                target = os.readlink(path)
                need(not os.path.isabs(target) and not re.match(r'^[A-Za-z]:', target), 'Absolute cache symlink')
                need(path.resolve(strict=True).is_relative_to(root), 'Cache symlink escapes its tree')
                symlinks[relative] = target
            elif stat.S_ISREG(kind):
                files[relative] = _digest(path)
            else:
                need(stat.S_ISDIR(kind), 'Unexpected special file in artifact cache')
    return dict(files=files, symlinks=symlinks)


def _verify_tree(root, document, manifest_name):
    need(isinstance(document.get('files'), dict) and isinstance(document.get('symlinks', {}), dict), 'Malformed cache manifest')
    for name, digest in document['files'].items():
        _relative(name)
        need(isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest), 'Malformed cached file digest')
    for name in document.get('symlinks', {}):
        _relative(name)
    actual = _tree(root, exclude=(manifest_name,))
    need(actual['files'] == document['files'] and actual['symlinks'] == document.get('symlinks', {}),
         'Artifact cache changed; refusing to execute or copy it')


def _extract_node(archive, destination):
    """Extract official Node without accepting traversal, special files or redirected writes."""
    destination = Path(destination)
    need(not os.path.lexists(destination), 'Node extraction destination already exists')
    with tarfile.open(archive, 'r:xz') as stream:
        members = stream.getmembers()
        names, links = set(), {}
        for member in members:
            path = _relative(member.name.rstrip('/'))
            need(path.parts[0] == NODE_NAME and member.name.rstrip('/') not in names, 'Unexpected/duplicate Node archive path')
            names.add(str(path))
            need(member.isfile() or member.isdir() or member.issym(), 'Node archive contains a special file or hard link')
            if member.issym():
                target = member.linkname
                need(target and not target.startswith(('/', '\\')) and '\\' not in target
                     and not re.match(r'^[A-Za-z]:', target), 'Invalid Node archive symlink')
                normalized = PurePosixPath(posixpath.normpath(str(path.parent / target)))
                need(normalized.parts[0] == NODE_NAME and '..' not in normalized.parts, 'Node archive symlink escapes its root')
                links[str(path)] = target
        for name in names:
            need(not any(str(parent) in links for parent in PurePosixPath(name).parents), 'Node archive writes through a symlink')
        destination.mkdir(mode=0o700)
        for member in members:
            target = destination / member.name.rstrip('/')
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with stream.extractfile(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
        for name, target in links.items():
            (destination / name).parent.mkdir(parents=True, exist_ok=True)
            (destination / name).symlink_to(target)
        for name in links:
            need((destination / name).resolve(strict=True).is_relative_to(destination.resolve()), 'Invalid Node symlink chain')


def _tool_node(archive, archive_digest):
    target = CACHE_ROOT / 'tool-node'
    manifest = target / 'tool-manifest.json'
    if os.path.lexists(target):
        need(target.is_dir() and not target.is_symlink(), 'Cached Node tool is redirected')
        need(manifest.is_file() and not manifest.is_symlink(), 'Unrecorded cached Node tool exists')
        data = json.loads(manifest.read_text())
        need(data.get('archive_sha256') == archive_digest, 'Cached Node tool version differs')
        _verify_tree(target, data, manifest.name)
    else:
        staging = Path(tempfile.mkdtemp(prefix='.tool-node.part-', dir=CACHE_ROOT))
        extracted = staging / 'extracted'
        _extract_node(archive, extracted)
        data = dict(_tree(extracted), archive_sha256=archive_digest)
        atomic_json(extracted / manifest.name, data)
        extracted.rename(target)
        staging.rmdir()
    binary = target / NODE_NAME / 'bin/node'
    npm = target / NODE_NAME / 'lib/node_modules/npm/bin/npm-cli.js'
    need(binary.is_file() and npm.is_file(), 'Official Node archive lacks Node/npm')
    return binary, npm


def _runtime_cache():
    target = CACHE_ROOT / 'runtime-cache'
    manifest = target / 'runtime-manifest.json'
    if os.path.lexists(target):
        need(target.is_dir() and not target.is_symlink() and manifest.is_file() and not manifest.is_symlink(),
             'Runtime cache is incomplete or redirected')
        data = json.loads(manifest.read_text())
        need(all(data.get(key) == value for key, value in VERSIONS.items()), 'Runtime cache versions differ from pins')
        required = {'runtime/fnm.zip', 'runtime/node.tar.xz', 'npm/package.json', 'npm/package-lock.json',
                    'npm/node_modules/@playwright/mcp/cli.js', 'npm/node_modules/@openai/codex/bin/codex.js'}
        need(required <= set(data.get('files', {})), 'Runtime cache is missing required inputs')
        _verify_tree(target, data, manifest.name)
        return target
    downloads = _private_dir(CACHE_ROOT / 'downloads')
    node_filename = NODE_NAME + '.tar.xz'
    node_digest = _checksum(_metadata(NODE_ROOT + 'SHASUMS256.txt'), node_filename, 'sha256')
    node_archive = download_verified(NODE_ROOT + node_filename, downloads / node_filename, node_digest)
    fnm_archive = _fnm_download(downloads / ('fnm-' + FNM_VERSION + '-linux.zip'))
    _validate_fnm(fnm_archive)
    node, npm = _tool_node(node_archive, node_digest)
    staging = Path(tempfile.mkdtemp(prefix='.runtime-cache.part-', dir=CACHE_ROOT))
    runtime = _private_dir(staging / 'runtime')
    shutil.copyfile(fnm_archive, runtime / 'fnm.zip')
    shutil.copyfile(node_archive, runtime / 'node.tar.xz')
    home = _private_dir(CACHE_ROOT / 'npm-home')
    global_config = home / 'empty-global.npmrc'
    if os.path.lexists(global_config):
        need(global_config.is_file() and not global_config.is_symlink() and global_config.read_bytes() == b'',
             'Cached npm configuration was changed')
    else:
        with global_config.open('xb'):
            pass
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in {'NODE_OPTIONS', 'NODE_PATH'} and not key.upper().startswith('NPM_CONFIG_')}
    environment.update(PATH=str(node.parent) + os.pathsep + os.environ.get('PATH', ''),
                       HOME=str(home), NPM_CONFIG_USERCONFIG=os.devnull, NPM_CONFIG_GLOBALCONFIG=str(global_config),
                       NPM_CONFIG_REGISTRY='https://registry.npmjs.org',
                       npm_config_cache=str(CACHE_ROOT / 'npm-download-cache'))
    run([str(node), str(npm), 'install', '--ignore-scripts', '--no-audit', '--no-fund',
         '--prefix', str(staging / 'npm'), '--save-exact', '--registry=https://registry.npmjs.org',
         '@playwright/mcp@' + MCP_VERSION, '@openai/codex@' + CODEX_VERSION],
        timeout=1200, env=environment)
    package = json.loads((staging / 'npm/package.json').read_text())
    need(package.get('dependencies') == {'@playwright/mcp': MCP_VERSION, '@openai/codex': CODEX_VERSION},
         'npm did not install the exact requested root dependencies')
    need((staging / 'npm/package-lock.json').is_file(), 'npm lockfile is missing')
    for relative in ['node_modules/@playwright/mcp/cli.js', 'node_modules/@openai/codex/bin/codex.js']:
        need((staging / 'npm' / relative).is_file(), 'npm runtime entrypoint is missing: ' + relative)
    data = dict(_tree(staging), schema_version=1, **VERSIONS)
    atomic_json(staging / manifest.name, data)
    _verify_tree(staging, data, manifest.name)
    staging.rename(target)
    return target


def prepare_artifacts():
    """Prepare every non-apt input before the caller deletes/rebuilds a VM."""
    need(platform.system() == 'Linux' and platform.machine() in {'x86_64', 'amd64'},
         'Runtime cache preparation requires the x86-64 Linux PVE host')
    image = resolve_image()
    _private_dir(CACHE_ROOT)
    lock_path = CACHE_ROOT / 'prepare.lock'
    need(not lock_path.is_symlink(), 'Refusing redirected cache lock')
    with lock_path.open('a') as lock:
        if fcntl:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        target = CACHE_ROOT / 'images' / image['filename']
        download_verified(image['url'], target, image['sha512'], 'sha512')
        spec = driver_spec('6.12.113+deb13-amd64')
        source = CACHE_ROOT / 'drivers' / ('i915-sriov-dkms-' + spec['version'] + '.tar.gz')
        download_verified(spec['url'], source, spec['sha256'])
        runtime = _runtime_cache()
        record = dict(image=dict(image, path=str(target)), image_path=str(target),
                      runtime_cache=str(runtime), runtime_manifest=str(runtime / 'runtime-manifest.json'),
                      driver_sources={spec['version']: str(source)})
        atomic_json(CACHE_ROOT / 'prepared.json', record)
        return record

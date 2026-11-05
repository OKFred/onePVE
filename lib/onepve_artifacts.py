"""Verified deployment inputs. Only prepare_artifacts/download_verified write a cache."""
import contextlib
import datetime
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import http.client
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import posixpath
import re
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

from onepve_common import atomic_json, progress, run
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
    image = _image_info(version)
    image['sha512'] = _checksum(_metadata(image['checksum_url']), image['filename'], 'sha512')
    return image


def _image_info(version):
    need(isinstance(version, str) and re.fullmatch(r'20[0-9]{6}-[0-9]+', version), 'Unexpected Debian image build ID')
    datetime.datetime.strptime(version[:8], '%Y%m%d')
    filename = 'debian-13-generic-amd64-' + version + '.qcow2'
    base = CLOUD_ROOT + version + '/'
    checksum_url = base + 'SHA512SUMS'
    return dict(version=version, filename=filename, url=base + filename,
                metadata_url=CLOUD_ROOT + 'latest/debian-13-generic-amd64.json', checksum_url=checksum_url)


def _format_size(size):
    return '未知' if size is None else '%.1f MiB (%d 字节)' % (size / 1024**2, size)


def _cached_images(latest):
    images, errors = [], []
    directory = CACHE_ROOT / 'images'
    if not os.path.lexists(directory):
        return images, errors
    try:
        need(directory.is_dir() and directory.resolve() == directory.absolute(), 'Image cache directory is redirected')
        if os.name == 'posix':
            for parent in [CACHE_ROOT, directory]:
                need(parent.stat().st_uid == os.geteuid() and not parent.stat().st_mode & 0o022,
                     'Image cache directory is not privately owned')
    except (RuntimeError, OSError) as error:
        return [], [str(error)]
    for path in sorted(directory.glob('debian-13-generic-amd64-*.qcow2'), reverse=True):
        try:
            need(path.is_file() and not path.is_symlink(), 'Image cache must be a regular file')
            version = path.name.removeprefix('debian-13-generic-amd64-').removesuffix('.qcow2')
            image = _image_info(version)
            progress('校验本地 Debian 镜像：' + path.name, '文件大小：' + _format_size(path.stat().st_size))
            actual = _digest(path, 'sha512')
            if latest and latest['filename'] == path.name:
                recorded = latest
            else:
                recorded = None
                for record in [path.with_suffix(path.suffix + '.json'), CACHE_ROOT / 'prepared.json']:
                    if not record.exists():
                        continue
                    try:
                        need(record.is_file() and not record.is_symlink(), 'Image checksum record is redirected')
                        if os.name == 'posix':
                            need(record.stat().st_uid == os.geteuid() and not record.stat().st_mode & 0o022,
                                 'Image checksum record is not privately owned')
                        data = json.loads(record.read_text(encoding='utf-8'))
                        need(isinstance(data, dict), 'Image checksum record must be an object')
                        data = data.get('image', data)
                        need(isinstance(data, dict), 'Image checksum record must contain an image object')
                        if data.get('filename') != path.name:
                            continue
                        need(all(data.get(key) == image[key] for key in ['version', 'filename', 'url', 'checksum_url']),
                             'Image checksum record does not match the official build URL')
                        need(isinstance(data.get('sha512'), str) and re.fullmatch(r'[0-9a-f]{128}', data['sha512']),
                             'Image has no trusted SHA512 checksum')
                        need(data['sha512'] == actual, 'Cached Debian image checksum differs')
                        recorded = data
                        break
                    except (RuntimeError, OSError, ValueError) as error:
                        errors.append(record.name + ': ' + str(error))
                if recorded is None:
                    image['sha512'] = _checksum(_metadata(image['checksum_url']), path.name, 'sha512')
                    recorded = image
            need(all(recorded.get(key) == image[key] for key in ['version', 'filename', 'url', 'checksum_url']),
                 'Image checksum record does not match the official build URL')
            digest = recorded.get('sha512')
            need(isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{128}', digest), 'Image has no trusted SHA512 checksum')
            need(actual == digest, 'Cached Debian image checksum differs')
            images.append(dict(image, sha512=digest, path=str(path), size_bytes=path.stat().st_size))
        except (RuntimeError, OSError, ValueError, http.client.HTTPException) as error:
            errors.append(path.name + ': ' + str(error))
    images.sort(key=lambda image: tuple(map(int, image['version'].split('-'))), reverse=True)
    return images, errors


def image_options():
    """Read-only discovery: online failure must not bypass the local cache."""
    progress('读取 Debian 镜像版本和官方校验信息')
    latest, lookup_error = None, None
    try:
        latest = resolve_image()
    except (RuntimeError, OSError, ValueError, http.client.HTTPException) as error:
        lookup_error = str(error)
        progress('在线镜像查询失败，继续检查本地镜像', lookup_error)
    cached, errors = _cached_images(latest)
    need(latest or cached, '线上与本地均无可用 Debian 镜像；在线：' + str(lookup_error)
         + '；本地：' + ('; '.join(errors) or '未找到已校验镜像'))
    return dict(latest=latest, cached=cached, lookup_error=lookup_error, cache_errors=errors)


def _choose_image(options):
    latest, cached = options['latest'], options['cached']
    for image in cached:
        if latest and (image['filename'], image['sha512']) == (latest['filename'], latest['sha512']):
            progress('复用已校验的最新 Debian 镜像：' + image['filename'], '文件大小：' + _format_size(image['size_bytes']))
            return image
    if options['lookup_error']:
        print('【镜像提示】无法确认线上最新版：' + options['lookup_error'], file=sys.stderr, flush=True)
    for error in options['cache_errors']:
        print('【镜像提示】跳过不可用缓存：' + error, file=sys.stderr, flush=True)
    if cached:
        print('【本地镜像】已有构建可能不是最新版；' + ('线上最新版：' + latest['version'] if latest else '当前无法确认最新版'), file=sys.stderr)
        for number, image in enumerate(cached, 1):
            print('%d. %s | %s | %s' % (number, image['version'], _format_size(image['size_bytes']), image['path']), file=sys.stderr)
        while True:
            print('输入序号复用已有镜像，或回车' + ('下载最新版' if latest else '取消') + '：', end='', file=sys.stderr, flush=True)
            answer = sys.stdin.readline().strip()
            if not answer:
                need(latest, '已取消：未选择本地镜像，线上镜像不可用')
                break
            if answer.isdecimal() and 1 <= int(answer) <= len(cached):
                return cached[int(answer) - 1]
            print('请输入列表中的序号。', file=sys.stderr)
    return latest


def prepare_image():
    options = image_options()
    image = _choose_image(options)
    target = CACHE_ROOT / 'images' / image['filename']
    try:
        download_verified(image['url'], target, image['sha512'], 'sha512')
    except (RuntimeError, OSError, ValueError, http.client.HTTPException) as error:
        cached, errors = _cached_images(options['latest'])
        cached = [item for item in cached if item['filename'] != image['filename']]
        need(cached, '镜像准备失败，且没有其他可用本地镜像：' + str(error) + '; ' + '; '.join(errors))
        image = _choose_image(dict(latest=None, cached=cached, lookup_error='镜像下载／校验失败：' + str(error), cache_errors=errors))
        target = CACHE_ROOT / 'images' / image['filename']
        download_verified(image['url'], target, image['sha512'], 'sha512')
    image = dict(image, path=str(target), size_bytes=target.stat().st_size)
    # Save immediately: later runtime failures must not strand a verified image offline.
    atomic_json(target.with_suffix(target.suffix + '.json'), image)
    return image


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
        progress('校验已缓存文件：' + path.name, '文件大小：' + _format_size(path.stat().st_size))
        need(expected is not None and _digest(path, algorithm) == expected,
             'Existing artifact differs or has no trusted digest; it was not overwritten: ' + str(path))
        return path
    progress('下载并校验文件：' + path.name)
    _private_dir(path.parent)
    descriptor, part_name = tempfile.mkstemp(prefix='.' + path.name + '.part-', dir=path.parent)
    part = Path(part_name)
    digest, count = hashlib.new(algorithm), 0
    try:
        with os.fdopen(descriptor, 'wb') as output, _open_url(url) as response:
            length = response.headers.get('Content-Length') if hasattr(response, 'headers') else None
            size = int(length) if length and re.fullmatch(r'[0-9]+', length) else None
            progress('下载并校验文件：' + path.name, '预计大小：' + _format_size(size))
            started = last_report = time.monotonic()
            while True:
                block = response.read(1024 * 1024)
                if block:
                    count += len(block)
                    need(count <= MAX_DOWNLOAD_BYTES, 'Artifact exceeds the download size limit')
                    digest.update(block); output.write(block)
                now = time.monotonic()
                if not block or now - last_report >= 5:
                    elapsed = now - started
                    speed = count / elapsed if elapsed > 0 else 0
                    speed = speed if math.isfinite(speed) and speed > 0 else 0
                    detail = ('已下载：' + _format_size(count) + ' / 总大小：' + _format_size(size)
                              + ' | 速度：' + ('%.1f KiB/s' % (speed / 1024) if speed else '未知'))
                    remaining = None
                    if size and count <= size:
                        detail += ' | %.1f%%' % (100 * count / size)
                        if speed:
                            remaining = (size - count) / speed
                    progress('下载文件：' + path.name, detail, remaining=remaining)
                    last_report = now
                if not block:
                    break
            progress('校验下载文件：' + path.name, '文件大小：' + _format_size(count))
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
        progress('下载校验完成：' + path.name, '文件大小：' + _format_size(count), remaining=0)
        return path
    except BaseException:
        progress('下载或校验失败：' + path.name, '已下载：' + _format_size(count))
        raise
    finally:
        part.unlink(missing_ok=True)


def download_verified(url, path, expected, algorithm='sha256'):
    need(algorithm in {'sha256', 'sha512'}, 'Only SHA256 and SHA512 are accepted')
    need(isinstance(expected, str) and re.fullmatch(r'[0-9a-fA-F]{' + str(hashlib.new(algorithm).digest_size * 2) + '}', expected),
         'A complete expected checksum is required')
    return _download(url, path, expected.lower(), algorithm)


def _fnm_download(path):
    """GitHub's fixed release is trusted on first HTTPS download, then locally pinned."""
    record = CACHE_ROOT / ('fnm-download-' + FNM_VERSION + '.json')
    legacy = CACHE_ROOT / 'fnm-download.json'
    if not os.path.lexists(record) and _record_matches(legacy, dict(url=FNM_URL, version=FNM_VERSION)):
        record = legacy
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


def _record_matches(path, expected):
    if not path.is_file() or path.is_symlink():
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and all(data.get(key) == value for key, value in expected.items())


def _cache_directory(name, manifest, expected):
    key = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]
    target, legacy = CACHE_ROOT / (name + '-' + key), CACHE_ROOT / name
    if (not os.path.lexists(target) and legacy.is_dir() and not legacy.is_symlink()
            and _record_matches(legacy / manifest, expected)):
        return legacy
    return target


def _tool_node(archive, archive_digest):
    target = _cache_directory('tool-node', 'tool-manifest.json', {'archive_sha256': archive_digest})
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
    target = _cache_directory('runtime-cache', 'runtime-manifest.json', VERSIONS)
    manifest = target / 'runtime-manifest.json'
    if os.path.lexists(target):
        progress('校验 Node.js、MCP 和 Codex 运行时缓存')
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
    progress('准备固定版本的 Playwright MCP 和 Codex CLI 缓存')
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
    _private_dir(CACHE_ROOT)
    lock_path = CACHE_ROOT / 'prepare.lock'
    need(not lock_path.is_symlink(), 'Refusing redirected cache lock')
    with lock_path.open('a') as lock:
        if fcntl:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        image = prepare_image()
        target = Path(image['path'])
        spec = driver_spec('6.12.113+deb13-amd64')
        source = CACHE_ROOT / 'drivers' / ('i915-sriov-dkms-' + spec['version'] + '.tar.gz')
        download_verified(spec['url'], source, spec['sha256'])
        runtime = _runtime_cache()
        progress('部署镜像和运行时缓存已准备完成')
        record = dict(image=dict(image, path=str(target)), image_path=str(target),
                      runtime_cache=str(runtime), runtime_manifest=str(runtime / 'runtime-manifest.json'),
                      driver_sources={spec['version']: str(source)})
        atomic_json(CACHE_ROOT / 'prepared.json', record)
        return record

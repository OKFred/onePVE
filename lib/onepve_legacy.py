"""Validate legacy configuration operations without applying host networking."""
import argparse
import difflib
import glob
import hashlib
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import tempfile


def regular(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ValueError('必须是普通文件，不能是链接：' + str(path))
    if os.name == 'posix' and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
        raise ValueError('文件归属或写权限不安全：' + str(path))
    if info.st_size > 1024 * 1024:
        raise ValueError('配置文件超过 1 MiB：' + str(path))
    return path.read_bytes()


def interface_stanzas(text):
    lines = text.splitlines(keepends=True)
    result = []
    for i, line in enumerate(lines):
        if not re.match(r'^iface\s', line):
            continue
        match = re.match(r'^iface\s+(\S+)\s+(\S+)\s+(\S+)', line)
        if not match:
            raise ValueError('无法识别 iface 定义：' + line.strip())
        end = i + 1
        while end < len(lines) and not re.match(r'^(?:iface|auto|allow-\S+|source|source-directory|mapping)\s', lines[end]):
            end += 1
        result.append((match[1], match[2], match[3], i, end))
    return lines, result


def included_definitions(path, seen=None):
    """Includes are read only to reject a second definition of the selected interface."""
    path = Path(path).absolute()
    seen = set() if seen is None else seen
    if path in seen or len(seen) >= 64:
        raise ValueError('网络配置包含循环或过多引用；请手工检查')
    seen.add(path)
    text = regular(path).decode('utf-8')
    _, entries = interface_stanzas(text)
    definitions = [(name, family) for name, family, _, _, _ in entries]
    for line in text.splitlines():
        if not re.match(r'^source(?:-directory)?\s', line):
            continue
        fields = shlex.split(line, comments=True)
        if len(fields) < 2:
            raise ValueError('无法识别 source 配置')
        for value in fields[1:]:
            pattern = Path(value) if Path(value).is_absolute() else path.parent / value
            for match in sorted(glob.glob(str(pattern))):
                current = Path(match)
                if fields[0] == 'source-directory':
                    if current.is_symlink() or not current.is_dir():
                        raise ValueError('source-directory 不是普通目录')
                    files = [p for p in current.iterdir() if re.fullmatch(r'[A-Za-z0-9_-]+', p.name)]
                else:
                    files = [current]
                for source in files:
                    definitions.extend(included_definitions(source, seen))
    return definitions


def dhcp_plan(path, interface):
    path = Path(path).absolute()
    original = regular(path)
    lines, entries = interface_stanzas(original.decode('utf-8'))
    matches = [entry for entry in entries if entry[:3] == (interface, 'inet', 'static')]
    if len(matches) != 1 or included_definitions(path).count((interface, 'inet')) != 1:
        raise ValueError('所选接口必须在主文件中只有一个明确的 IPv4 static 定义')
    _, _, _, start, end = matches[0]
    lines[start] = re.sub(r'^(iface\s+\S+\s+inet\s+)static\b', r'\1dhcp', lines[start], count=1)
    for index in range(start + 1, end):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if not line[0].isspace():
            raise ValueError('目标接口属性必须缩进；请手工检查：' + line.strip())
        option = line.split(None, 1)[0]
        if option in {'pre-up', 'up', 'post-up', 'pre-down', 'down', 'post-down', 'pointopoint'}:
            raise ValueError('目标接口包含路由／执行钩子或点对点设置，拒绝自动转换：' + option)
        if option in {'address', 'netmask', 'gateway', 'broadcast', 'network'}:
            lines[index] = '# onePVE previous static: ' + line.lstrip()
    return original, ''.join(lines).encode('utf-8')


def dhcp_apply(path, interface, expected):
    path = Path(path).absolute()
    if path.parent.resolve() != path.parent:
        raise ValueError('配置目录不能通过链接重定向')
    before, after = dhcp_plan(path, interface)
    if hashlib.sha256(before).hexdigest() != expected:
        raise ValueError('预览后配置已变化，未写入；请重新预览')
    info = path.stat()
    descriptor, name = tempfile.mkstemp(prefix=path.name + '.onepve-before-dhcp-', suffix='.bak', dir=path.parent)
    backup = Path(name)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(before); stream.flush(); os.fsync(stream.fileno())
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.onepve-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(after); stream.flush(); os.fsync(stream.fileno())
        temporary.chmod(stat.S_IMODE(info.st_mode))
        if os.name == 'posix':
            os.chown(temporary, info.st_uid, info.st_gid)
        if regular(path) != before or path.stat().st_ino != info.st_ino:
            raise ValueError('写入前配置发生变化，原文件未替换')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def directory(path):
    path = Path(path).absolute()
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or path.resolve() != path:
        raise ValueError('目录不能通过链接重定向：' + str(path))
    if os.name == 'posix' and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
        raise ValueError('目录归属或写权限不安全，请先审查：' + str(path))


def backup_tree(path):
    path = Path(path).absolute(); directory(path)
    if {p.name for p in path.iterdir()} != {'interfaces', 'lxc', 'qemu-server'}:
        raise ValueError('备份必须且只能包含 interfaces、lxc、qemu-server')
    regular(path / 'interfaces')
    for name in ['lxc', 'qemu-server']:
        directory(path / name)
        for file in (path / name).iterdir():
            if not re.fullmatch(r'[1-9][0-9]*\.conf', file.name):
                raise ValueError('备份内存在非 VM 配置文件：' + str(file))
            regular(file)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['dhcp-list', 'dhcp-plan', 'dhcp-apply', 'backup-paths', 'backup-check', 'restore-check'])
    parser.add_argument('paths', nargs='+')
    args = parser.parse_args(argv); values = args.paths
    if args.action == 'dhcp-list':
        _, entries = interface_stanzas(regular(values[0]).decode('utf-8'))
        for name, family, method, _, _ in entries:
            if family == 'inet' and method == 'static':
                print(name)
    elif args.action == 'dhcp-plan':
        before, after = dhcp_plan(*values)
        print(hashlib.sha256(before).hexdigest())
        print(''.join(difflib.unified_diff(before.decode().splitlines(keepends=True), after.decode().splitlines(keepends=True),
                                         fromfile=values[0] + '（当前）', tofile=values[0] + '（仅修改 ' + values[1] + '）')), end='')
    elif args.action == 'dhcp-apply':
        print('原文件备份：' + str(dhcp_apply(*values)))
        print('已写入；未应用网络。确认管理入口后，再由你手动应用。')
    elif args.action == 'backup-paths':
        latest, archives = map(lambda value: Path(value).absolute(), values)
        if latest == archives or latest in archives.parents or archives in latest.parents:
            raise ValueError('当前备份目录与归档目录必须是两个独立目录')
        for value in values:
            path = Path(value).absolute(); directory(path.parent)
            if path.exists() or path.is_symlink():
                directory(path)
        if Path(values[0]).exists() and any(Path(values[0]).iterdir()):
            backup_tree(values[0])
    elif args.action == 'backup-check':
        backup_tree(values[0])
    else:
        source, network, node = map(Path, values)
        backup_tree(source); regular(network)
        for name in ['lxc', 'qemu-server']:
            directory(node / name)
            for file in (source / name).iterdir():
                target = node / name / file.name
                if target.exists() or target.is_symlink():
                    regular(target)
    return 0


if __name__ == '__main__':
    try:sys.exit(main())
    except (OSError, ValueError, TypeError) as error:
        print('onePVE: ' + str(error), file=sys.stderr); sys.exit(1)

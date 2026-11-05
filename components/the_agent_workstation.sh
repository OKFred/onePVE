#!/usr/bin/env bash

ONEPVE_ROOT="${ONEPVE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
if ! declare -F onepve_menu_run >/dev/null; then
  source "$ONEPVE_ROOT/components/the_pass_through.sh"
fi

onepve_agent_resources() {
  python3 - <<'PY'
import json, socket, subprocess
def get(path):
    return json.loads(subprocess.check_output(['pvesh', 'get', path, '--output-format', 'json'], text=True))

def optional_json(args):
    try:
        return json.loads(subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=5))
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}

disk_cache = {}
def physical_disks(source):
    if not isinstance(source, str) or not source.strip().startswith('/dev/'):
        return []
    source = source.strip().split('[', 1)[0]
    if source not in disk_cache:
        tree = optional_json(['lsblk', '--json', '--bytes', '--inverse', '--tree',
                              '--output', 'PATH,TYPE,MODEL,SIZE', '--', source])
        disks = {}
        def collect(nodes):
            if not isinstance(nodes, list):
                return
            for item in nodes:
                if not isinstance(item, dict):
                    continue
                if item.get('type') == 'disk' and isinstance(item.get('path'), str) and item['path']:
                    disks[item['path']] = {k: item.get(k) for k in ('path', 'model', 'size')}
                    if not isinstance(disks[item['path']]['model'], str):
                        disks[item['path']]['model'] = None
                collect(item.get('children') or [])
        if isinstance(tree, dict):
            collect(tree.get('blockdevices') or [])
        disk_cache[source] = list(disks.values())
    return disk_cache[source]

node = socket.gethostname().split('.')[0]
storages = [s for s in get('/nodes/' + node + '/storage')
            if s.get('active') and s.get('enabled', 1) and 'images' in s.get('content', '').split(',')]
bridges = [b for b in get('/nodes/' + node + '/network') if b.get('type') == 'bridge' and b.get('active')]
if not storages or not bridges:
    raise SystemExit('没有可用的 images 存储或活动网桥；本向导不会修改宿主网络。')
index = optional_json(['pvesh', 'get', '/storage', '--output-format', 'json'])
configs = {c['storage']: c for c in index if isinstance(c, dict) and isinstance(c.get('storage'), str)} if isinstance(index, list) else {}
pvs = []
if any(s['type'] in ('lvm', 'lvmthin') for s in storages):
    data = optional_json(['pvs', '--reportformat', 'json', '-o', 'vg_name,pv_name'])
    reports = data.get('report') if isinstance(data, dict) else None
    if isinstance(reports, list):
        for report in reports:
            rows = report.get('pv') if isinstance(report, dict) else None
            if isinstance(rows, list):
                pvs.extend(p for p in rows if isinstance(p, dict))
for storage in storages:
    config = configs.get(storage['storage'], {})
    location = {k: config[k] for k in ('path', 'vgname', 'thinpool', 'pool', 'server', 'export', 'share')
                if isinstance(config.get(k), str) and config[k]}
    sources = []
    if location.get('path'):
        data = optional_json(['findmnt', '--json', '--target', location['path'], '--output', 'TARGET,SOURCE,FSTYPE'])
        mounts = data.get('filesystems') if isinstance(data, dict) else None
        if mounts and isinstance(mounts, list) and isinstance(mounts[0], dict):
            location['mount'] = {k: mounts[0].get(k) for k in ('target', 'source', 'fstype')}
            sources.append(location['mount'].get('source'))
    if storage['type'] in ('lvm', 'lvmthin') and location.get('vgname'):
        sources.extend(p['pv_name'] for p in pvs if isinstance(p.get('vg_name'), str)
                       and p['vg_name'].strip() == location['vgname'] and isinstance(p.get('pv_name'), str))
    disks = {d['path']: d for source in sources for d in physical_disks(source)}
    location['disks'] = [disks[path] for path in sorted(disks)]
    storage['location'] = location
storages.sort(key=lambda s: (-int(s.get('avail', 0)), s['storage']))
bridges.sort(key=lambda b: b['iface'])
print(json.dumps({'nextid': int(get('/cluster/nextid')), 'storages': storages, 'bridges': bridges}, ensure_ascii=False))
PY
}

onepve_agent_number() {
  [[ "$1" =~ ^[0-9]{1,9}$ ]] && (( 10#$1 >= $2 ))
}

onepve_agent_wizard() {
  local action="$1" metadata defaults vmid name storage bridge cores memory disk
  local key_source public_key mac expected_ip gpu viewer answer confirmation apt_mirror
  local network address='' gateway='' nameserver=''
  local -a settings argv
  metadata="$(onepve_agent_resources)" || return
  defaults="$(python3 - "$metadata" <<'PY'
import json, sys
x = json.loads(sys.argv[1])
print(x['nextid']); print(x['storages'][0]['storage']); print(x['bridges'][0]['iface'])
print('可选磁盘存储（单选，填写存储 ID；新目录需先在 PVE 注册）：')
for s in x['storages']:
    print('存储: %s (%s, 可用 %.1f GiB)' % (s['storage'], s['type'], int(s.get('avail', 0)) / 1024**3))
    location = s.get('location', {})
    if location.get('path'):
        print('  目录: ' + location['path'])
    for key, label in [('vgname', 'LVM 卷组'), ('thinpool', 'Thin pool'), ('pool', '存储池'),
                       ('server', '服务端'), ('export', '导出目录'), ('share', '共享')]:
        if location.get(key):
            print('  %s: %s' % (label, location[key]))
    mount = location.get('mount', {})
    if mount:
        print('  挂载: %s | 来源: %s | 文件系统: %s' %
              (mount.get('target') or '未识别', mount.get('source') or '未识别', mount.get('fstype') or '未识别'))
        if mount.get('target') == '/':
            print('  注意: 与宿主根文件系统共享空间；不同存储名不代表独立磁盘。')
    for d in location.get('disks', []):
        capacity = '%.1f GiB' % (int(d['size']) / 1024**3) if str(d.get('size', '')).isdigit() else '容量未知'
        print('  物理盘: %s (%s, %s)' % (d['path'], (d.get('model') or '型号未知').strip(), capacity))
    if not location.get('disks'):
        print('  物理盘: 未识别或由存储池/远端管理。')
print('可选网桥（单选；工作机仅创建1张网卡）：')
for b in x['bridges']:
    print('网桥: ' + b['iface'])
PY
)" || return
  mapfile -t settings <<<"$defaults"
  printf '\n08 Debian 13 Agent 工作机\n'
  printf '%s\n' "${settings[@]:3}"
  printf '接下来依次填写：VMID → 名称 → 选择一个存储 ID → 选择一个网桥。\n'
  if [[ "$action" == rebuild ]]; then
    printf '重建会删除指定 VM 的原有磁盘和资料。本入口不提供备份。\n'
    read -r -p '要重建的 VMID：' vmid || return
  else
    read -r -p "VMID [${settings[0]}]：" vmid || return
    vmid="${vmid:-${settings[0]}}"
  fi
  onepve_agent_number "$vmid" 100 || { printf 'VMID 必须是100到999999999的整数。\n'; return 1; }
  read -r -p '名称 [ai-dev]：' name || return
  name="${name:-ai-dev}"
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$ ]] || { printf '名称无效。\n'; return 1; }
  read -r -p "选择一个磁盘存储 ID [${settings[1]}]：" storage || return
  storage="${storage:-${settings[1]}}"
  read -r -p "选择一个网桥 [${settings[2]}]：" bridge || return
  bridge="${bridge:-${settings[2]}}"
  python3 - "$metadata" "$storage" "$bridge" <<'PY'
import json, sys
x = json.loads(sys.argv[1])
if sys.argv[2] not in {s['storage'] for s in x['storages']} or sys.argv[3] not in {b['iface'] for b in x['bridges']}:
    raise SystemExit('存储或网桥不在当前可用列表中。')
PY
  [[ $? -eq 0 ]] || return 1
  read -r -p 'vCPU，最低4 [4]：' cores || return; cores="${cores:-4}"
  read -r -p '固定内存 MiB，最低8192 [8192]：' memory || return; memory="${memory:-8192}"
  read -r -p '磁盘 GiB，最低32 [32]：' disk || return; disk="${disk:-32}"
  onepve_agent_number "$cores" 4 && onepve_agent_number "$memory" 8192 && onepve_agent_number "$disk" 32 || {
    printf '资源配置低于最低要求或不是有效整数。\n'; return 1;
  }
  printf '客机 Debian APT 源：tuna清华、ustc中科大、official官方；不修改PVE宿主源。\n'
  read -r -p '选择客机 APT 镜像源 [tuna]：' apt_mirror || return
  apt_mirror="${apt_mirror:-tuna}"
  case "$apt_mirror" in
    official|tuna|ustc) ;;
    *) printf 'APT 镜像源只能为tuna、ustc或official。\n'; return 1 ;;
  esac
  read -r -p 'SSH 公钥文件路径（或输入 paste 粘贴公钥，切勿提供私钥）：' key_source || return
  if [[ "$key_source" == paste ]]; then
    read -r -p '粘贴单行 SSH 公钥：' public_key || return
    [[ "$public_key" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521))[[:space:]]+[A-Za-z0-9+/]+=*([[:space:]].*)?$ ]] || {
      printf '必须提供有效格式的 SSH 公钥。\n'; return 1;
    }
    argv=(--ssh-key "$public_key")
  else
    [[ "$key_source" != '~/'* ]] || key_source="$HOME/${key_source:2}"
    [[ -f "$key_source" && -r "$key_source" ]] || { printf '公钥文件不可读。\n'; return 1; }
    IFS= read -r public_key < "$key_source" || [[ -n "$public_key" ]]
    [[ "$public_key" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521))[[:space:]] ]] || {
      printf '文件必须是 SSH 公钥，不能使用私钥。\n'; return 1;
    }
    argv=(--ssh-key-file "$key_source")
  fi
  read -r -p '显卡：auto复用已有VF、none软件桌面、或已准备的PCI地址 [auto]：' gpu || return
  gpu="${gpu:-auto}"
  [[ "$gpu" == auto || "$gpu" == none ]] || onepve_pci_input "$gpu" || { printf '显卡选择无效。\n'; return 1; }
  read -r -p 'MAC（留空自动生成；重建可填写原MAC）：' mac || return
  [[ -z "$mac" || "$mac" =~ ^([[:xdigit:]]{2}:){5}[[:xdigit:]]{2}$ ]] || { printf 'MAC 格式无效。\n'; return 1; }
  read -r -p '网络方式：dhcp自动分配、static静态地址 [dhcp]：' network || return
  network="${network:-dhcp}"
  case "$network" in
    dhcp)
      printf '保留MAC不保证DHCP分配原地址；需要固定SSH／FRPC目标时请使用静态地址或已有DHCP保留。\n'
      read -r -p '期望IPv4地址（可空；仅核验，不会分配此地址）：' expected_ip || return
      ;;
    static)
      read -r -p '静态IPv4地址／CIDR（例如192.168.1.20/24）：' address || return
      read -r -p 'IPv4网关（须在同一子网）：' gateway || return
      read -r -p 'DNS服务器（单个IPv4地址）：' nameserver || return
      read -r -p "期望IPv4地址 [${address%/*}]：" expected_ip || return
      expected_ip="${expected_ip:-${address%/*}}"
      ;;
    *) printf '网络方式只能为dhcp或static。\n'; return 1 ;;
  esac
  python3 - "$network" "$address" "$gateway" "$nameserver" "$expected_ip" <<'PY'
import ipaddress, sys
mode, address, gateway, nameserver, expected = sys.argv[1:]
try:
    if expected:
        ipaddress.IPv4Address(expected)
    if mode == 'static':
        if '/' not in address:
            raise ValueError('静态地址必须显式包含CIDR前缀')
        interface = ipaddress.IPv4Interface(address)
        route = ipaddress.IPv4Address(gateway)
        dns = ipaddress.IPv4Address(nameserver)
        for value in (interface.ip, route, dns):
            if value.is_loopback or value.is_multicast or value.is_unspecified or value.is_reserved:
                raise ValueError('不能使用回环、组播、保留或未指定地址')
        if route not in interface.network or route == interface.ip:
            raise ValueError('网关须在同一子网且不能是VM自身地址')
        for value in (interface.ip, route):
            if interface.network.prefixlen < 31 and value in (interface.network.network_address, interface.network.broadcast_address):
                raise ValueError('VM和网关不能使用网络地址或广播地址')
        if expected and ipaddress.IPv4Address(expected) != interface.ip:
            raise ValueError('期望地址必须与静态地址一致')
except ValueError as error:
    raise SystemExit('网络配置无效：' + str(error))
PY
  [[ $? -eq 0 ]] || return 1
  read -r -p '本机查看端口 [16080]：' viewer || return; viewer="${viewer:-16080}"
  onepve_agent_number "$viewer" 1024 && (( 10#$viewer <= 65535 )) || { printf '查看端口须为1024到65535。\n'; return 1; }
  argv+=(--vmid "$vmid" --name "$name" --storage "$storage" --bridge "$bridge"
    --cores "$cores" --memory "$memory" --disk "$disk" --gpu "$gpu" --viewer-port "$viewer" --apt-mirror "$apt_mirror")
  [[ -z "$mac" ]] || argv+=(--mac "$mac")
  [[ -z "$expected_ip" ]] || argv+=(--expected-ip "$expected_ip")
  if [[ "$network" == static ]]; then
    argv+=(--address "$address" --gateway "$gateway" --nameserver "$nameserver")
  fi
  if [[ "$action" == rebuild ]]; then
    onepve_menu_run agent rebuild "${argv[@]}" --confirm-vmid "$vmid" --dry-run || return
    read -r -p "确认无备份删除并重建此VM，请输入VMID $vmid：" confirmation || return
    [[ "$confirmation" == "$vmid" ]] || { printf 'VMID不匹配，已取消。\n'; return 1; }
    onepve_menu_run agent rebuild "${argv[@]}" --confirm-vmid "$vmid"
  else
    onepve_menu_run agent plan "${argv[@]}" || return
    read -r -p '创建以上工作机？[y/N]：' answer || return
    [[ "$answer" == y || "$answer" == Y ]] || return 0
    onepve_menu_run agent create "${argv[@]}"
  fi
}

the_agent_workstation() {
  local choice vmid
  while true; do
    printf '\n08 Agent 工作机：只使用03已准备的显卡，不修改宿主驱动\n'
    printf '%s\n' '1. 创建 Debian 13 工作机' '2. 查看状态和日志位置' \
      '3. 验收指定工作机' '4. 显式重建（删除原VM，不自动备份）' \
      '5. 继续未完成的安装' '0. 返回'
    read -r -p '选择：' choice || return
    case "$choice" in
      1) onepve_agent_wizard create ;;
      2) onepve_menu_run agent status ;;
      3 | 5)
        read -r -p 'VMID：' vmid || return
        onepve_agent_number "$vmid" 100 || { printf 'VMID无效。\n'; continue; }
        if [[ "$choice" == 3 ]]; then
          onepve_menu_run agent verify --vmid "$vmid"
        else
          onepve_menu_run agent resume --vmid "$vmid"
        fi
        ;;
      4) onepve_agent_wizard rebuild ;;
      0 | 00 | '') return ;;
      *) printf '选择无效。\n' ;;
    esac
  done
}

#!/usr/bin/env bash
ONEPVE_ROOT="${ONEPVE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
backup_dir="${backup_dir:-$HOME/$(basename -- "$ONEPVE_ROOT")_backup}"
backup_archive_dir="${backup_archive_dir:-$HOME/$(basename -- "$ONEPVE_ROOT")_backup_archive}"

onepve_backup_node() {
  local name
  name="$(cat /etc/hostname)" || return
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$ ]] || { printf '节点名称无效。\n' >&2; return 1; }
  printf '%s\n' "$name"
}

the_backup_restore() {
  printf '本菜单仅备份／还原网络与 VM 配置，不包含虚拟磁盘或账号资料。\n'
  the_restore || return
  the_backup
}

the_restore() {
  local node answer
  node="$(onepve_backup_node)" || return
  [[ -d "$backup_dir" ]] || { printf '没有可还原的配置备份。\n'; return 0; }
  python3 "$ONEPVE_ROOT/lib/onepve_legacy.py" backup-check "$backup_dir" || return
  read -r -p '是否还原网络与虚拟机配置文件？[y/N]：' answer || return
  [[ "$answer" == y || "$answer" == Y ]] || { printf '已取消还原。\n'; return 0; }
  printf '将覆盖 /etc/network/interfaces 和节点 %s 中备份包含的 VM 配置；不还原磁盘，也不删除其他配置。\n' "$node"
  read -r -p '配置与现有磁盘可能不匹配，确认继续？[y/N]：' answer || return
  [[ "$answer" == y || "$answer" == Y ]] || { printf '已取消还原。\n'; return 0; }
  python3 "$ONEPVE_ROOT/lib/onepve_legacy.py" restore-check "$backup_dir" /etc/network/interfaces "/etc/pve/nodes/$node" || return
  # pmxcfs fixes its own permissions; copy content without preserving source attributes.
  if ! { cp -- "$backup_dir/interfaces" /etc/network/interfaces &&
         cp -r -- "$backup_dir/lxc/." "/etc/pve/nodes/$node/lxc/" &&
         cp -r -- "$backup_dir/qemu-server/." "/etc/pve/nodes/$node/qemu-server/"; }; then
    printf '还原失败，可能已有部分配置写入；原备份保留。未自动应用网络或重启。\n' >&2
    return 1
  fi
  printf '配置还原完成；未自动应用网络或重启。\n'
}

the_backup() (
  umask 077
  local node answer staging archive_part backup_file previous=''
  node="$(onepve_backup_node)" || return
  read -r -p '是否备份网络与虚拟机配置文件（不含磁盘）？[y/N]：' answer || return
  [[ "$answer" == y || "$answer" == Y ]] || { printf '已取消备份。\n'; return 0; }
  python3 "$ONEPVE_ROOT/lib/onepve_legacy.py" backup-paths "$backup_dir" "$backup_archive_dir" || return
  [[ -f /etc/network/interfaces && ! -L /etc/network/interfaces &&
     -d /etc/pve/nodes/"$node"/lxc && ! -L /etc/pve/nodes/"$node"/lxc &&
     -d /etc/pve/nodes/"$node"/qemu-server && ! -L /etc/pve/nodes/"$node"/qemu-server ]] || {
    printf '备份源缺失或不是普通配置路径。\n' >&2; return 1;
  }
  mkdir -p -- "$backup_archive_dir" || return
  staging="$(mktemp -d "$(dirname -- "$backup_dir")/.onepve-config-backup.XXXXXXXX")" || return
  archive_part="$(mktemp "$backup_archive_dir/$node-$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX.partial")" || { rmdir -- "$staging"; return 1; }
  trap 'rm -f -- "$archive_part"; [[ -z "$staging" ]] || rm -rf -- "$staging"' EXIT
  if ! { mkdir -- "$staging/lxc" "$staging/qemu-server" &&
         cp -- /etc/network/interfaces "$staging/interfaces" &&
         cp -a -- "/etc/pve/nodes/$node/lxc/." "$staging/lxc/" &&
         cp -a -- "/etc/pve/nodes/$node/qemu-server/." "$staging/qemu-server/" &&
         python3 "$ONEPVE_ROOT/lib/onepve_legacy.py" backup-check "$staging" &&
         tar -czf "$archive_part" -C "$staging" .; }; then
    printf '备份失败；旧备份与旧归档未改动。\n' >&2; return 1
  fi
  backup_file="${archive_part%.partial}.tar.gz"
  ln -- "$archive_part" "$backup_file" || { printf '归档发布失败，未覆盖已有文件。\n' >&2; return 1; }
  if [[ -e "$backup_dir" ]]; then
    previous="$(mktemp -d "$(dirname -- "$backup_dir")/.onepve-previous-backup.XXXXXXXX")" || return
    rmdir -- "$previous" || return
    mv -T -- "$backup_dir" "$previous" || { printf '归档已保存，旧备份目录未替换：%s\n' "$backup_file" >&2; return 1; }
  fi
  if ! mv -T -- "$staging" "$backup_dir"; then
    [[ -z "$previous" ]] || mv -T -- "$previous" "$backup_dir"
    printf '归档已保存，但当前备份目录更新失败：%s\n' "$backup_file" >&2; return 1
  fi
  staging=''
  printf '配置备份完成：%s\n当前备份目录：%s\n' "$backup_file" "$backup_dir"
  [[ -z "$previous" ]] || printf '上一份目录保留在：%s\n' "$previous"
)

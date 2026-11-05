#!/usr/bin/env bash
ONEPVE_ROOT="${ONEPVE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"

the_network_dhcp() {
  local file=/etc/network/interfaces choices interface preview digest answer
  choices="$(python3 "$ONEPVE_ROOT/lib/onepve_legacy.py" dhcp-list "$file")" || return
  [[ -n "$choices" ]] || { printf '主配置文件中没有可转换的 IPv4 static 接口。\n'; return 0; }
  printf '只转换一个接口；不会自动应用网络或重启。可选接口：\n%s\n' "$choices"
  read -r -p '要转换为 DHCP 的接口／网桥（留空取消）：' interface || return
  [[ -n "$interface" ]] || return 0
  [[ $'\n'"$choices"$'\n' == *$'\n'"$interface"$'\n'* ]] || { printf '接口不在列表中。\n' >&2; return 1; }
  preview="$(python3 "$ONEPVE_ROOT/lib/onepve_legacy.py" dhcp-plan "$file" "$interface")" || return
  digest="${preview%%$'\n'*}"
  [[ "$digest" =~ ^[[:xdigit:]]{64}$ ]] || return 1
  printf '%s\n' "${preview#*$'\n'}"
  read -r -p '确认仅修改以上接口？管理地址可能变化。[y/N]：' answer || return
  [[ "$answer" == y || "$answer" == Y ]] || { printf '已取消，未写入配置。\n'; return 0; }
  python3 "$ONEPVE_ROOT/lib/onepve_legacy.py" dhcp-apply "$file" "$interface" "$digest"
}

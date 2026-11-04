#!/usr/bin/env bash
# GPU preparation is separate from VM creation and never reboots the host.

ONEPVE_ROOT="${ONEPVE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"

if ! declare -F onepve_cli >/dev/null; then
  onepve_cli() {
    python3 "$ONEPVE_ROOT/onepve.py" "$@"
  }
fi

onepve_menu_run() (
  local directory result answer rc=0 summary_rc=0
  umask 077
  directory="$(mktemp -d "${TMPDIR:-/tmp}/onepve-json.XXXXXXXX")" || return
  result="$directory/result.json"
  trap 'rm -f -- "$result"; rmdir -- "$directory"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM HUP
  # Keep stderr attached to the terminal for progress and interactive image choices.
  ONEPVE_MENU_ERRORS=1 onepve_cli "$@" >"$result" || rc=$?
  python3 "$ONEPVE_ROOT/lib/onepve_menu.py" "$result" "$rc" "$@" || summary_rc=$?
  read -r -p '是否用 nano 查看原始 JSON？[y/N]：' answer || answer=''
  if [[ "$answer" == y || "$answer" == Y ]]; then
    if command -v nano >/dev/null 2>&1; then
      nano -v -- "$result" || printf '查看器已退出，操作结果保持不变。\n'
    else
      printf '未安装 nano，已跳过查看；操作结果保持不变。\n'
    fi
  fi
  [[ "$rc" -eq 0 ]] || return "$rc"
  return "$summary_rc"
)

onepve_pci_input() {
  [[ "$1" =~ ^([[:xdigit:]]{4}:)?[[:xdigit:]]{2}:[[:xdigit:]]{2}\.[0-7]$ ]]
}

onepve_gpu_notice() {
  if [[ -t 1 ]]; then
    printf '\033[1;%sm%s\033[0m\n' "$1" "$2"
  else
    printf '%s\n' "$2"
  fi
}

onepve_gpu_apply() {
  local answer rc
  onepve_menu_run gpu "$@" --dry-run || {
    rc=$?
    onepve_gpu_notice 31 '【预检失败】未执行应用，请查看上方错误详情。'
    return "$rc"
  }
  read -r -p '应用以上变更？[y/N]：' answer || return
  if [[ "$answer" != y && "$answer" != Y ]]; then
    onepve_gpu_notice 33 '【已取消】未应用变更。'
    return 0
  fi
  onepve_menu_run gpu "$@"
}

the_pass_through() {
  local choice pci count resource
  while true; do
    printf '\n03 显卡直通／SR-IOV：先准备宿主设备，再到08创建系统\n'
    printf '范围说明：仅准备显卡，不创建/删除 VM，也不会自动重启宿主。\n'
    printf '%s\n' '1. 检测硬件与占用（只读）' '2. 查看准备状态（只读）' \
      '3. 准备 Intel SR-IOV' '4. 准备选定设备独占直通' \
      '5. 撤销 onePVE 拥有的配置' '0. 返回'
    read -r -p '选择：' choice || return
    case "$choice" in
      1) onepve_menu_run gpu inventory ;;
      2) onepve_menu_run gpu status ;;
      3)
        onepve_menu_run gpu inventory || continue
        read -r -p 'Intel PF 地址，例如 0000:00:02.0：' pci || return
        onepve_pci_input "$pci" || { printf 'PCI 地址格式无效。\n'; continue; }
        read -r -p 'VF 数量 [1]：' count || return
        count="${count:-1}"
        [[ "$count" =~ ^[1-9][0-9]{0,2}$ ]] || { printf 'VF 数量必须是正整数。\n'; continue; }
        onepve_gpu_apply prepare-sriov --pf "$pci" --num-vfs "$count"
        ;;
      4)
        onepve_menu_run gpu inventory || continue
        read -r -p '要独占直通的 PCI 地址：' pci || return
        onepve_pci_input "$pci" || { printf 'PCI 地址格式无效。\n'; continue; }
        onepve_gpu_apply prepare-passthrough --bdf "$pci"
        ;;
      5)
        onepve_menu_run gpu status || continue
        read -r -p '要恢复的资源 ID（按状态输出填写）：' resource || return
        [[ "$resource" =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]*$ ]] || { printf '资源 ID 无效。\n'; continue; }
        onepve_gpu_apply restore --resource "$resource"
        ;;
      0 | 00 | '') return ;;
      *) printf '选择无效。\n' ;;
    esac
  done
}

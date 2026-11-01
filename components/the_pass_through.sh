#!/usr/bin/env bash
# GPU preparation is separate from VM creation and never reboots the host.

ONEPVE_ROOT="${ONEPVE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"

onepve_cli() {
  python3 "$ONEPVE_ROOT/onepve.py" "$@"
}

onepve_pci_input() {
  [[ "$1" =~ ^([[:xdigit:]]{4}:)?[[:xdigit:]]{2}:[[:xdigit:]]{2}\.[0-7]$ ]]
}

onepve_gpu_apply() {
  local answer
  onepve_cli gpu "$@" --dry-run || return
  read -r -p '应用以上变更？[y/N]：' answer || return
  [[ "$answer" == y || "$answer" == Y ]] || return 0
  onepve_cli gpu "$@"
}

the_pass_through() {
  local choice pci count resource
  while true; do
    printf '\n03 显卡直通／SR-IOV：先准备宿主设备，再到08创建系统\n'
    printf '%s\n' '1. 检测硬件与占用（只读）' '2. 查看准备状态（只读）' \
      '3. 准备 Intel SR-IOV' '4. 准备选定设备独占直通' \
      '5. 撤销 onePVE 拥有的配置' '0. 返回'
    read -r -p '选择：' choice || return
    case "$choice" in
      1) onepve_cli gpu inventory ;;
      2) onepve_cli gpu status ;;
      3)
        onepve_cli gpu inventory || continue
        read -r -p 'Intel PF 地址，例如 0000:00:02.0：' pci || return
        onepve_pci_input "$pci" || { printf 'PCI 地址格式无效。\n'; continue; }
        read -r -p 'VF 数量 [1]：' count || return
        count="${count:-1}"
        [[ "$count" =~ ^[1-9][0-9]{0,2}$ ]] || { printf 'VF 数量必须是正整数。\n'; continue; }
        onepve_gpu_apply prepare-sriov --pf "$pci" --num-vfs "$count"
        ;;
      4)
        onepve_cli gpu inventory || continue
        read -r -p '要独占直通的 PCI 地址：' pci || return
        onepve_pci_input "$pci" || { printf 'PCI 地址格式无效。\n'; continue; }
        onepve_gpu_apply prepare-passthrough --bdf "$pci"
        ;;
      5)
        onepve_cli gpu status || continue
        read -r -p '要恢复的资源 ID（按状态输出填写）：' resource || return
        [[ "$resource" =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]*$ ]] || { printf '资源 ID 无效。\n'; continue; }
        onepve_gpu_apply restore --resource "$resource"
        ;;
      0 | 00 | '') return ;;
      *) printf '选择无效。\n' ;;
    esac
    printf '本菜单不会创建/删除 VM，也不会自动重启宿主。\n'
  done
}

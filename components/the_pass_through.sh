#!/usr/bin/env bash
# GPU preparation is separate from VM creation and never reboots the host.

ONEPVE_ROOT="${ONEPVE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"

onepve_cli() {
  python3 "$ONEPVE_ROOT/onepve.py" "$@"
}

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
  local answer result status rc
  onepve_cli gpu "$@" --dry-run || {
    rc=$?
    onepve_gpu_notice 31 '【预检失败】未执行应用，请查看上方错误详情。'
    return "$rc"
  }
  read -r -p '应用以上变更？[y/N]：' answer || return
  if [[ "$answer" != y && "$answer" != Y ]]; then
    onepve_gpu_notice 33 '【已取消】未应用变更。'
    return 0
  fi
  if result="$(onepve_cli gpu "$@")"; then
    printf '%s\n' "$result"
  else
    rc=$?
    [[ -z "$result" ]] || printf '%s\n' "$result"
    onepve_gpu_notice 31 '【执行失败】操作未完成，请查看上方错误详情并检查准备状态。'
    return "$rc"
  fi
  if ! status="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<< "$result" 2>/dev/null)"; then
    onepve_gpu_notice 31 '【未确认】无法解析执行结果，请查看上方详情并检查准备状态。'
    return 1
  fi
  case "$status" in
    READY) onepve_gpu_notice 32 '【已就绪】显卡设备准备完成，可前往08创建 Agent 工作机。' ;;
    EXTERNAL_READY) onepve_gpu_notice 32 '【已就绪】已复用现有显卡/VF 配置，无需重复安装驱动，可前往08创建工作机。' ;;
    NEEDS_REBOOT) onepve_gpu_notice 33 '【待重启】配置已写入，需手动重启 PVE 后检查准备状态；尚未重启。' ;;
    RESTORED) onepve_gpu_notice 32 '【已撤销】onePVE 拥有的显卡配置已恢复。' ;;
    RESTORED_NEEDS_REBOOT) onepve_gpu_notice 33 '【待重启】显卡配置已撤销，需手动重启 PVE 完成恢复；尚未重启。' ;;
    EXTERNAL_PRESERVED) onepve_gpu_notice 33 '【已保留】配置由外部管理，未撤销外部驱动或服务。' ;;
    DRY_RUN) onepve_gpu_notice 33 '【仅预览】未应用变更。'; return 1 ;;
    ERROR | FAILED) onepve_gpu_notice 31 '【执行失败】操作未完成，请查看上方详情并检查准备状态。'; return 1 ;;
    *) onepve_gpu_notice 31 '【未确认】无法识别结果状态，请查看上方详情并检查准备状态。'; return 1 ;;
  esac
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
  done
}

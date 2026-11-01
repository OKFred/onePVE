#!/usr/bin/env bash

ONEPVE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

onepve_load_components() {
  local previous="$PWD" component
  # Legacy components resolve their own dependencies relative to cwd.
  cd -- "$ONEPVE_ROOT" || return
  for component in the_repo_localization the_basic_setup the_pass_through \
    the_cluster_exit the_swap_configuration the_network_dhcp the_backup_restore \
    the_agent_workstation; do
    source "$ONEPVE_ROOT/components/$component.sh" || { cd -- "$previous"; return 1; }
  done
  cd -- "$previous"
}

menu_title() {
  date
  printf '%s\n' 'onePVE 工具箱（宿主操作需要 root）' \
    '先用03准备显卡，再用08创建 Agent 工作机。'
}

menu_back() {
  read -r -p '按回车返回。' _
}

main() {
  if [[ "$EUID" -ne 0 ]]; then
    printf '请在 PVE 宿主上以 root 运行本菜单。\n' >&2
    return 1
  fi
  onepve_load_components || return
  local choice
  while true; do
    menu_title
    printf '%s\n' '01. 更换国内源' '02. 安装基础工具' \
      '03. 显卡直通／SR-IOV 准备' '04. 退出集群' '05. SWAP 配置' \
      '06. 网卡改 DHCP' '07. 旧版配置备份与还原' '08. Agent 工作机' \
      '09. 关于与操作说明' '00. 退出'
    read -r -p '选择：' choice || return 0
    case "$choice" in
      01 | 1) the_repo_localization ;;
      02 | 2) the_basic_setup ;;
      03 | 3) the_pass_through ;;
      04 | 4) the_cluster_exit ;;
      05 | 5) the_swap_configuration ;;
      06 | 6) the_network_dhcp ;;
      07 | 7) the_backup_restore ;;
      08 | 8) the_agent_workstation ;;
      09 | 9) cat -- "$ONEPVE_ROOT/readme.md" ;;
      00 | 0 | '') return 0 ;;
      *) printf '选择无效。\n' ;;
    esac
  done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

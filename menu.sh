#!/bin/bash
#@description: 菜单化显示工具箱列表
#@author: Fred Zhang Qi
#@datetime: 2023-12-24

#文件依赖
#⚠️import--需要引入包含函数的文件
source ./components/the_repo_localization.sh
source ./components/the_basic_setup.sh
source ./components/the_pass_through.sh
source ./components/the_cluster_exit.sh
source ./components/the_swap_configuration.sh
source ./components/the_network_dhcp.sh
source ./components/the_backup_restore.sh

menu_title() {
  date
  echo "Root Required--执行需要管理员权限。请注意"
  echo "*********************"
  echo "*****   工具箱Tool   *****"
}

menu_back() {
  echo
  echo -n "press any key--按任意键返回."
  read
  clear
}

main() {
  while (true); do
    menu_title
    echo "01. local repo--更换国内源"
    echo "02. basic setup--安装基础工具nano、wget、git等"
    echo "03. graphic card pass through--配置显卡直通"
    echo "04. cluster exit--退出集群"
    echo "05. SWAP config--启用/禁用SWAP"
    echo "06. static IP to DHCP--网卡改DHCP"
    echo "07. backup and restore configs--重点文件备份&还原"
    echo "08. more--更多"
    echo "09. about--关于"
    echo "00. exit--退出"
    echo
    echo -n "your choice--请输入你的选择："
    read the_user_choice
    case "$the_user_choice" in
    01 | 1) the_repo_localization ;;
    02 | 2) the_basic_setup ;;
    03 | 3) the_pass_through ;;
    04 | 4) the_cluster_exit ;;
    05 | 5) the_swap_configuration ;;
    06 | 6) the_network_dhcp ;;
    07 | 7) the_backup_restore ;;
    08 | 8) echo 'wait and see--敬请期待' ;;
    09 | 9) nano readme.md ;;
    00 | 0 | "") exit 1 ;;
    u) echo "???" ;;
    *) echo "error input--输入有误，请重新输入！" && menu_back ;;
    esac
    echo
  done
}

clear
main

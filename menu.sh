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
  #clear
  date
  echo "执行需要管理员权限。请注意"
  echo "*********************"
  echo "*****   工具箱   *****"
}

menu_back() {
  echo
  echo -n "按任意键返回."
  read
}

main() {
  while (true); do
    menu_title
    echo "01. 更换国内源"
    echo "02. 安装基础工具nano、wget、git等"
    echo "03. 配置显卡直通"
    echo "04. 退出集群"
    echo "05. 启用/禁用SWAP"
    echo "06. 网卡改DHCP"
    echo "07. 重点文件备份&还原"
    echo "08. 更多"
    echo "09. 关于"
    echo "00. 退出"
    echo
    echo -n "请输入你的选择："
    read the_user_choice
    case "$the_user_choice" in
    01 | 1) the_repo_localization ;;
    02 | 2) the_basic_setup ;;
    03 | 3) the_pass_through ;;
    04 | 4) the_cluster_exit ;;
    05 | 5) the_swap_configuration ;;
    06 | 6) the_network_dhcp ;;
    07 | 7) the_backup_restore ;;
    08 | 8) echo '敬请期待' ;;
    09 | 9) nano readme.md ;;
    00 | 0) exit 1 ;;
    u) echo "???" ;;
    *) echo "输入有误，请重新输入！" && menu_back ;;
    esac
    echo
  done
}

clear
main

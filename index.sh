#!/bin/bash
#@description: 用于PVE虚拟机环境初始化
#@author: Fred Zhang Qi
#@datetime: 2023-12-17

#文件依赖
#⚠️import--需要引入包含函数的文件
source ./components/the_repo_localization.sh
source ./components/the_pass_through.sh

main() {
  echo -e "\033[32m"
  date
  echo "执行需要管理员权限。请注意"
  echo "# 🚩 ① PVE换源："
  the_repo_localization

  echo "# 🚩 ② 安装基础工具："
  apt install -y nano net-tools htop

  echo "# 🚩 ③ 直通设置："
  the_pass_through

  echo "# 🚩 ④ 更新grub配置 & initramfs："
  sed -i "s/\"quiet\"/\"quiet intel_iommu=on initcall_blacklist=sysfb_init\"/g" /etc/default/grub
  update-grub
  update-initramfs -u -k all

  echo "# 🚩 ⑤ 禁用SWAP(可选)："
  echo -e "\033[33m 🚀SWAP disable--是否禁用SWAP？(y/n)"
  read disable_swap
  if [ "$disable_swap" = "y" ]; then
    sed -i "s/\\/dev\/pve\/swap/#\/dev\/pve\/swap/g" /etc/fstab
  fi
  echo -e "\033[0m"

  echo "# 🚩  ⑥ 网卡改DHCP(可选)："
  echo -e "\033[33m 🚀DHCP--是否需要动态ip地址？(y/n)"
  read need_dhcp
  if [ "$need_dhcp" = "y" ]; then
    sed -i "s/\taddress/#address/g" /etc/network/interfaces
    sed -i "s/\tgateway/#gateway/g" /etc/network/interfaces
    sed -i "s/inet static/inet dhcp/g" /etc/network/interfaces
  fi
  echo -e "\033[0m"
  cat /etc/network/interfaces | grep dhcp

  echo "# 🚩  ⑦重启PVE"
  echo -e "\033[33m 🚀reboot--是否需要重启？(y/n)"
  read need_reboot
  if [ "$need_reboot" != "y" ]; then
    echo "done--大功告成"
  else
    echo '感谢使用， bye~'
    reboot
  fi
  echo -e "\033[0m"
}

main

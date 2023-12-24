#!/bin/bash
#@description:  配置DHCP动态ip地址
#@author: Fred Zhang Qi
#@datetime: 2023-12-24

#dependencies--文件依赖
# none

the_network_dhcp() {
  echo -e "\033[33m 🚀DHCP--是否需要动态ip地址？(y/n)"
  read need_dhcp
  if [ "$need_dhcp" = "y" ]; then
    sed -i "s/\taddress/#address/g" /etc/network/interfaces
    sed -i "s/\tgateway/#gateway/g" /etc/network/interfaces
    sed -i "s/inet static/inet dhcp/g" /etc/network/interfaces
  fi
  echo -e "\033[0m"
  cat /etc/network/interfaces | grep dhcp
}

#!/bin/bash
#@description:  安装基础工具：
#@author: Fred Zhang Qi
#@datetime: 2023-12-24

#dependencies--文件依赖
# none

the_basic_setup() {
  echo -e "\033[33m 🚀basic--是否需要安装基础工具？(y/n)"
  read need_basic
  echo -e "\033[0m"
  if [ "$need_basic" != "y" ]; then
    echo "不需要，跳过..."
  else
    apt install -y nano net-tools htop wget curl git
  fi
}

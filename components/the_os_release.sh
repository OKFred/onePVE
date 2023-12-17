#!/bin/bash
#@description:  检测linux系统发行版本(意义不大，纯粹练手)
#@author: Fred Zhang Qi
#@datetime: 2023-12-16

#dependencies--文件依赖
# none

the_os_release() {
  local os=""
  if [ -f /etc/os-release ]; then
    . /etc/os-release
    if [ "$ID" = "ubuntu" ]; then
      os="Ubuntu"
    elif [ "$ID" = "debian" ]; then
      os="Debian"
    else
      os="false"
    fi
  else
    os="false"
  fi
  echo "$os"
}

the_code_name() {
  local code_name=""
  if [ -f /etc/os-release ]; then
    # 导入文件内容并提取 VERSION_CODENAME 的值
    . /etc/os-release
    code_name="$VERSION_CODENAME"
  else
    echo "false"
  fi
  echo "$code_name"
}

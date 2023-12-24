#!/bin/bash
#@description:  配置SWAP内存交换空间
#@author: Fred Zhang Qi
#@datetime: 2023-12-24

#dependencies--文件依赖
# none

the_swap_configuration() {
  echo -e "\033[33m 🚀需要需要内存交换空间(SWAP)？(y/n)"
  read need_use_swap
  echo -e "\033[0m"
  local swap_config_file=/etc/fstab
  the_file_backup $swap_config_file
  if [ "$need_use_swap" == "y" ]; then
    local search_regex='^#*\/dev'
    local replace_regex='\/dev'
    local flags='g'
    local sed_command="/$search_regex/$replace_regex/$flags"
    echo $sed_command
    sed "s"$sed_command $swap_config_file #启用SWAP
  else
    local search_regex='^\/dev'
    local replace_regex='#\/dev'
    local flags='g'
    local sed_command="/$search_regex/$replace_regex/$flags"
    echo $sed_command
    sed "s"$sed_command $swap_config_file #禁用SWAP
  fi
}

the_file_backup() {
  local target=$1
  local backup_file="$target.bak"
  if [ ! -f "$backup_file" ]; then
    cp $target $backup_file
    echo "已备份文件$target为$backup_file"
  else
    echo "备份文件已存在，跳过"
  fi
}

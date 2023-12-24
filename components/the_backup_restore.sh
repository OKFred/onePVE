#!/bin/bash
#@description:  安装基础工具：
#@author: Fred Zhang Qi
#@datetime: 2023-12-24

#dependencies--文件依赖
# none
backup_dir=$HOME/backup
backup_archive_dir=$HOME/backup_archive

the_backup_restore() {
  the_restore
  the_backup
}

the_restore(){
  #判断backup_dir是否非空，非空则询问是否还原
  if [ "$(ls -A $backup_dir)" ]; then
    read -p "是否还原重要文件？(y/n)" need_restore
    if [ $need_restore == "y" ]; then
      echo -e "\033[33m 🚀开始还原"
      local this_node_name=$(get_this_node_name)
      local qemu_server_folder="/etc/pve/nodes/$this_node_name/qemu-server/"
      local new_network_file="$backup_dir/interfaces"
      cp $new_network_file /root/test/interfaces
      cp -r $backup_dir/* /root/test/
      echo -e "\033[33m 🚀还原完成"
    else
      echo -e "\033[33m 🚀取消还原"
    fi
  else
    echo -e "\033[33m 🚀备份文件夹为空，无法还原"
  fi
}

the_backup() {
  local network_file="/etc/network/interfaces"
  local this_node_name=$(get_this_node_name)
  local qemu_server_folder="/etc/pve/nodes/$this_node_name/qemu-server/"
  local new_network_file="$backup_dir/interfaces"
  read -p "是否备份重要文件？(y/n)" need_backup
  if [ $need_backup == "y" ]; then
    echo -e "\033[33m 🚀开始备份"
    mkdir -p $backup_dir
    mkdir -p $backup_archive_dir
    cp $network_file $new_network_file
    cp -r $qemu_server_folder $backup_dir
    local backup_file="$backup_archive_dir/$this_node_name-$(date +%Y%m%d%H%M%S).tar.gz"
    tar -zcvf $backup_file $backup_dir
    echo -e "\033[33m 🚀备份完成"
  else
    echo -e "\033[33m 🚀取消备份"
  fi
  echo -e "\033[0m"
}

get_this_node_name() {
  local node_name=""
  if [ -f /etc/hostname ]; then
    node_name=$(cat /etc/hostname)
  else
    return 1
  fi
  echo "$node_name"
}

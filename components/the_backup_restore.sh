#!/bin/bash
#@description:  安装基础工具：
#@author: Fred Zhang Qi
#@datetime: 2023-12-24

#dependencies--文件依赖
# none

current_dir=$(pwd) # 获取当前工作目录的绝对路径
current_folder=$(basename "$PWD")

backup_dir=$HOME/"$current_folder"_backup
backup_archive_dir=$HOME/"$current_folder"_backup_archive

the_backup_restore() {
  the_restore
  the_backup
}

the_restore() {
  #判断backup_dir是否非空，非空则询问是否还原
  echo -e "\033[33m"
  local this_node_name=$(get_this_node_name)
  local qemu_server_folder="/etc/pve/nodes/$this_node_name/qemu-server/"
  local network_file="/etc/network/interfaces"
  local new_network_file="$backup_dir/interfaces"
  local new_qemu_server_folder="$backup_dir/qemu-server/"
  if [ "$(ls -A $backup_dir)" ]; then
    read -p "是否还原网络配置和虚拟机配置文件？(y/n)" need_restore
    if [ "$need_restore" == "y" ]; then
      echo -e "\033[31m"
      read -p "将覆盖$network_file 和 $qemu_server_folder 下的所有文件，是否继续？(y/n)" need_restore
      if [ "$need_restore" != "y" ]; then
        echo -e "\033[31m 🚀取消还原"
        return 1
      fi
      echo -e "\033[33m 🚀开始还原"
      #先批量设置下权限777
      chmod -R 777 $backup_dir
      cp $new_network_file $network_file
      cp -r $new_qemu_server_folder/* $qemu_server_folder
      echo -e "\033[33m 🚀还原完成"
    else
      echo -e "\033[33m 🚀取消还原"
    fi
  else
    echo -e "\033[33m 🚀备份文件夹为空，无法还原"
  fi
}

the_backup() {
  local this_node_name=$(get_this_node_name)
  local qemu_server_folder="/etc/pve/nodes/$this_node_name/qemu-server/"
  local network_file="/etc/network/interfaces"
  local new_network_file="$backup_dir/interfaces"
  read -p "是否备份网络配置和虚拟机配置文件？(y/n)" need_backup
  if [ $need_backup == "y" ]; then
    echo -e "\033[33m 🚀开始备份"
    mkdir -p $backup_dir
    mkdir -p $backup_archive_dir
    cp $network_file $new_network_file
    cp -r $qemu_server_folder $backup_dir
    local backup_file="$backup_archive_dir/$this_node_name-$(date +%Y%m%d).tar.gz"
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

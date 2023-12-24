#!/bin/bash
#@description:  安装基础工具：
#@author: Fred Zhang Qi
#@datetime: 2023-12-24

#dependencies--文件依赖
# none
backup_dir=$HOME/backup
backup_file=$backup_dir/backup.tar.gz

the_backup_restore() {
  if [ -f $backup_file ]; then
    echo -e "\033[33m 🚀找到备份文件，是否还原？(y/n)"
    read need_restore
    #先解压
    #然后二次确认
    if [ $need_restore == "y" ]; then
      tar -zxvf $backup_file -C $backup_dir
      echo -e "\033[33m 🚀解压完成"
      ls -al $backup_dir
      echo -e "\033[33m 🚀请确认是否还原？(y/n)"
      read need_restore_confirmation
      if [ $need_restore_confirmation == "y" ]; then
        echo -e "\033[33m 🚀开始还原"
        echo -e "\033[33m 🚀还原完成"
      else
        echo -e "\033[33m 🚀取消还原"
      fi
    else
      echo -e "\033[33m 🚀取消还原"
    fi
  else
    echo "备份文件不存在"
  fi
  echo -e "\033[0m"
  the_backup
}

the_backup() {
  local network_file="/etc/network/interfaces"
  local this_node_name=$(get_this_node_name)
  local nodes_folder="/etc/pve/nodes/$this_node_name"
  local nodes_backup_folder="$backup_dir"
  local network_backup_file="$backup_dir/interfaces"
  read -p "是否备份重要文件？(y/n)" need_backup
  if [ $need_backup == "y" ]; then
    echo -e "\033[33m 🚀开始备份"
    mkdir -p $nodes_backup_folder
    cp $network_file $network_backup_file
    cp -r $nodes_folder $nodes_backup_folder
    tar -zcvf $backup_file $backup_dir
    echo -e "\033[33m 🚀备份完成"
  else
    echo -e "\033[33m 🚀取消备份"
  fi
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

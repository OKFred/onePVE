#!/bin/bash
#@description:  退出集群
#@author: Fred Zhang Qi
#@datetime: 2023-12-24

#dependencies--文件依赖
# none

the_cluster_exit() {
  systemctl stop corosync.service
  #、node上集群系统文件设置为本地模式
  systemctl stop pve-cluster corosync
  pmxcfs -l
  cp -a /etc/corosync /etc/corosync.bak
  rm -rf /etc/corosync/*
  rm -rf /etc/pve/corosync.conf
  killall pmxcfs
  systemctl start pve-cluster.service
  check_invalid_nodes_and_if_delete
}

check_invalid_nodes_and_if_delete() {
  local nodes_folder="/etc/pve/nodes"
  local folder_array=()
  local folder_to_delete=()
  local this_node_name=$(get_this_node_name)
  for folder in $(ls $nodes_folder); do
    folder_array+=("$folder")
    if [ "$folder" != "$this_node_name" ]; then
      read -p "节点目录$folder似乎无效，是否清除？?(y/n)" answer
      if [ "$answer" = "y" ]; then
        folder_to_delete+=("$folder")
      fi
    fi
  done
  # 删除无效节点目录
  echo "需要清理的节点目录数量：${#folder_to_delete[@]}"
  for folder in "${folder_to_delete[@]}"; do
    rm -rf "$nodes_folder/$folder"
  done
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

#!/bin/bash
#@description:  （显卡）直通设置：
#@author: Fred Zhang Qi
#@datetime: 2023-12-17

#dependencies--文件依赖
#none

the_pass_through() {
  the_module_replace
  the_blacklist_replace
  the_boot_configuration
}

the_module_replace() {
  sources_list="/etc/modules"
  backup_file="$sources_list.bak"

  if [ ! -f "$backup_file" ]; then
    mv $sources_list $backup_file
    echo "已备份原有modules -> modules.bak"
    echo "# 设置直通
vfio
vfio_iommu_type1
vfio_pci
vfio_virqfd
" >/etc/modules
  else
    echo "备份文件已存在，跳过"
  fi
}

the_blacklist_replace() {
  sources_list="/etc/modprobe.d/pve-blacklist.conf"
  backup_file="$sources_list.bak"

  if [ ! -f "$backup_file" ]; then
    mv $sources_list $backup_file
    echo "已备份原有pve-blacklist.conf -> pve-blacklist.conf.bak"
    echo "# 更新硬件黑名单
# This file contains a list of modules which are not supported by Proxmox VE 
# nvidiafb see bugreport https://bugzilla.proxmox.com/show_bug.cgi?id=701
blacklist nvidiafb
blacklist i915
blacklist nouveau
blacklist nvidia
blacklist snd_hda_codec_hdmi
blacklist snd_hda_intel
blacklist snd_hda_codec
blacklist snd_hda_core
blacklist radeon
blacklist amdgpu
options vfio_iommu_type1 allow_unsafe_interrupts=1
" >/etc/modprobe.d/pve-blacklist.conf
  else
    echo "备份文件已存在，跳过"
  fi
}

the_boot_configuration() {
  echo -e "\033[33m 🚀是否需要更新grub配置 & initramfs？(y/n)"
  read need_config_boot
  echo -e "\033[0m"
  if [ "$need_config_boot" != "y" ]; then
    echo "不需要，跳过..."
  else
    sed -i "s/\"quiet\"/\"quiet intel_iommu=on initcall_blacklist=sysfb_init\"/g" /etc/default/grub
    update-grub
    update-initramfs -u -k all
  fi
}

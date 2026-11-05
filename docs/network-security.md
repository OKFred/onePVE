# 工作机网络与可选连接审计

新建工作机默认 `isolated`：允许正常访问公网 TCP/UDP，阻止主动访问内网。可以在创建时明确选择 `lan`，或在08菜单10中切换。连接审计独立选择，默认关闭；开启后在PVE保存最近72小时的TCP/UDP流元数据。已有工作机不自动迁移策略。

## 创建前准备

- PVE需要 `nftables`；可选审计还需要 `conntrack`。缺少依赖时预检说明所需的 `apt install nftables conntrack`，不会自行安装或更换宿主防火墙后端。
- 使用静态IPv4，或先在路由器设置MAC与IPv4的DHCP保留。向导留空MAC时会提前生成并展示，确认创建前可据此设置保留；DHCP命令行必须传 `--mac`。期望IPv4不仅用于核验，也用于宿主源地址绑定；改变地址会断网。DHCP模式还需明确网关与DHCP服务器IPv4。
- 填写一个DNS服务器IPv4，以及允许SSH进入的管理来源CIDR。来源是**VM实际看到的地址**：例如FRPC转发机的内网IPv4/32，未必是当前电脑地址。允许管理机发起SSH并不允许VM反向主动访问管理机。
- 网桥只选一个。需要VLAN时先在PVE／交换机／路由器准备，向导只为VM设置已有VLAN标签，不创建或改动宿主网桥。

命令行示例，地址与密钥路径应替换为实际值：

```bash
python3 onepve.py agent plan --vmid 103 --storage YOUR_STORAGE --bridge vmbr0 \
  --ssh-key-file /root/agent.pub --gpu none \
  --address 192.168.50.30/24 --gateway 192.168.50.1 --nameserver 192.168.50.1 \
  --management-cidr 192.168.50.1/32 \
  --lan-allow 192.168.50.2/32,tcp,8006
```

确认计划后将 `plan` 换成 `create`。不传 `--network-policy` 即为隔离；`--network-policy lan` 明确允许内网出站，`--connection-audit` 才开启审计。多条放行规则重复传 `--lan-allow IPv4/CIDR,tcp或udp,端口`。DNS只开放指定地址的TCP/UDP 53，DHCP只开放指定服务器与客户端必需的广播／端口；网关不会因此得到任意端口放行。

## 宿主侧边界

每台VM拥有一个独立的 `bridge` family nft表，只匹配它的 `tap<VMID>i0`。不会 `flush ruleset`、开启全局PVE防火墙、修改其他VM规则或切换PVE防火墙后端。其他已有防火墙仍可能进一步限制流量；本工具的accept不会覆盖外部drop。

隔离规则包含私网、CGNAT、链路本地、回环、保留／组播地址，以及加载规则时宿主的IPv4路由和本地地址。公网地址型内网可用 `--protected-cidr` 明确补充。规则加载、VM启动及重新应用策略时刷新保护地址；运行中修改宿主路由后应重新应用策略。不能识别经公网反向代理或应用代理间接提供的内网服务，精确白名单也意味着信任该目标端口。

两种新建策略均固定源MAC／IPv4、只允许必要ARP、阻断IPv6／其他以太网协议及额外VLAN封装。入站仅放行管理来源SSH、已跟踪的回复和所需DHCP；前端、MCP、VNC仍通过SSH隧道使用。`lan`仅放开IPv4 TCP/UDP出站目的地址限制，不开放所有入站端口。第一版不提供IPv6、额外地址、多网卡或VM充当路由／NAT网关的配置。

新VM创建时网卡为 `link_down=1`。安装规则、保存启动检查、启用可选审计并核对规则读回指纹后才启网。宿主重启时owned systemd单元在 `pve-guests` 前加载规则；VM的pre-start hook再次核对归属、MAC、网桥、VLAN和单网卡条件，随后原子加载规则。GPU模式和软件回退都保留该检查。依赖缺失或hook失败时启动被拒绝，不以开放网络作为回退。

持久文件位于 `/var/lib/onepve/network/<VMID>-<owner>/`，对应单元为 `onepve-network-<VMID>.service`。规则表名包含VMID和owner；删除／重建仅移除相同owner的表与单元，保留配置作为诊断证据。不要手工删除hook、规则、启动单元或添加第二张网卡；这些宿主侧管理操作可能破坏隔离边界。客机内关闭自身防火墙不会关闭宿主规则。

## 查看、切换与恢复

08菜单6为只读诊断，7查看日志尾部，8切软件桌面，9重试已有GPU，10调整策略／白名单／审计，11查看规则与审计状态，12导出审计CSV。所有原菜单仍保留；JSON照常只在明确输入Y后用nano只读查看。

```bash
python3 onepve.py agent network-status --vmid 103
python3 onepve.py agent network --vmid 103 --network-policy lan --dry-run
python3 onepve.py agent network --vmid 103 --network-policy lan
python3 onepve.py agent network --vmid 103 --network-policy isolated \
  --lan-allow 192.168.50.2/32,tcp,8006 --audit on
python3 onepve.py agent network --vmid 103 --clear-lan-allow --audit off
```

`--lan-allow`替换整份白名单，不传则保留，`--clear-lan-allow`显式清空。`--management-cidr`同样可替换管理来源列表。切换时短暂断开该VM网卡，SSH会断开；失败保留 `network_pending` 且网卡保持断开。通过PVE控制台或宿主重新执行所需策略，成功后恢复，不手动直接启网。旧工作机缺少完整宿主guard时拒绝直接迁移，应通过明确重建使用新流程。

## 72小时审计的含义

数据由PVE侧root服务存入 `/var/lib/onepve/audit/<VMID>/flows.sqlite3`，不依赖客机自报。目录0700、文件0600，只保存观察时间、协议、方向、原始／回复IP与端口、conntrack ID／zone／宿主boot ID、TCP状态及可用的包／字节计数。未开启宿主conntrack计量时包／字节为null，不改全局sysctl。不抓取报文内容、URL、Cookie、密码或其他VM记录。

宿主conntrack工具先收到所在网络命名空间的事件，再以目标IPv4过滤输出；Python再次核验目标地址后才写入。它不是内核按VMID隔离的采集，地址依赖固定IP与单网卡绑定，也不是防篡改取证系统。

启动订阅后立即快照，每120秒更新长流；每分钟清理最后观察时间超过72小时的记录。活跃长流保留较早的开始信息，流量停止后仍依最后观察时间到期。数据库与回滚journal合计预算1GiB，容量不足可提前淘汰，状态会标记缺口。关闭采集后仍保留定时过期清理；显式删除工作机时才清理本工具拥有的审计资源。

`NEW`表示观察到conntrack流，不证明已放行或连接成功；UDP也没有TCP式连接完成语义。被规则丢弃的尝试没有逐条明细，仅提供规则加载以来丢弃包计数。启动无ready握手、ENOBUFS、采集退出、解析／写入失败、容量淘汰等标记为gap；状态另报告当前服务停止或心跳过期。快照可补当前存活流，不能恢复丢失的短流。扫描时应同时检查gap、心跳、规则指纹和流记录，不能把没有记录当成没有流量。

```bash
python3 onepve.py agent audit-export --vmid 103 --output /root/ai-dev-flows.csv
python3 onepve.py agent audit-export --vmid 103 --output /root/ai-dev-flows.json --format json --limit 10000
```

导出只读、只含未到期记录，默认最多10000条，上限100000条，不覆盖已有文件；导出文件由操作者自行管理，不受72小时自动清理。尚未添加定时扫描、自动封禁或告警。

需要异地保留可使用08 → 13的[可选SLS上传](sls-audit.md)：默认关闭，本地审计独立；启用需要核实账号共享免费额度并批准本宿主月度硬预算。

# 本轮验收记录

记录日期：2026-11-02。以下区分源码测试、当前机器实测和未完成项目；不代表其他硬件或所有恢复分支已经实机验证。日志中的地址、MAC、公钥、账号及宿主工作目录不纳入本页。

## 实测环境

| 项目 | 本轮版本／配置 |
|---|---|
| Debian 镜像 | 官方 Debian 13 generic amd64，固定构建 `20261026-2643`，校验后导入 |
| VM 资源 | 4 vCPU、固定 8 GiB、64 GiB 系统盘 |
| Node.js／fnm | `26.13.0`／`v1.39.0` |
| Playwright MCP／Codex CLI | `0.0.83`／`0.166.0` |
| 平台与核显 | Intel Core Ultra 7 265K，Intel `8086:7d67`，客机使用已有 SR-IOV VF |
| PVE 宿主内核 | `7.0.14-17-pve`，本轮保持不变 |
| 宿主 GPU 提供者 | 复用已有外部驱动、VF 与启动服务；未重新编译驱动或修改宿主启动配置 |

当前新建工作机的系统盘默认且最低为32 GiB。上表和下文记录的是此前64 GiB VM的实测结果；尚未另建32 GiB VM做实机验收，也未缩小现有VM磁盘。

## 已完成的实机验收

| 验收项 | 结果与证据范围 |
|---|---|
| 基础环境 | PASS：Debian、SSH、fnm 运行时、远端 MCP 与浏览器工作环境通过实际检查 |
| 原生 GPU | PASS：Wayland、精确 Intel VF 文件句柄、原生 EGL shader／`readPixels` 的 12,288 像素绘制检查 |
| Chromium GPU | PASS：GPU 进程持有所选 VF 的准确文件句柄；Intel WebGL1、WebGL2 实际 shader 绘制与读回通过 |
| 软件与 GPU 切换 | PASS：本轮部署源码完成真实软件桌面→GPU 桌面切换，恢复 GPU 后重新验收通过 |
| MCP 浏览器操作 | PASS：打开、输入、点击、截图与页面结果读回 |
| noVNC 人工接管 | PASS：ASCII 键盘输入与点击结果可由 MCP 读回；实际按钮暂停／恢复 MCP，Chromium PID 保持不变 |
| 手动模式冷启动 | PASS：VM boot ID 改变；重启前后人工标记均保留、MCP 均未监听；GPU 再验收通过，普通用户默认 keyring 已解锁 |
| 交还与会话恢复 | PASS：通过 Wayland 控制面板确认交还；9项检查确认持久 Cookie、会话 Cookie、localStorage 保留，原有3000／3001两页恢复，3001演示服务自动启动并使用 Node `26.13.0` |
| MCP 独立重启 | PASS：15项检查通过，MCP PID 改变而 Chromium PID、工作页面保留 |
| 查看端断开／重连 | PASS：14项检查通过，Chromium 与页面 UUID 保留 |
| SSH 隧道停止／重启 | PASS：14项检查通过，Chromium 与页面 UUID 保留；查看端最终确认为 `ViewOnly=1` |
| HTTP／WebSocket 边界 | PASS：6项实际请求见下表，未发送 RFB 输入 |
| 跨站 iframe | PASS：实际 Chrome 拒绝嵌入，浏览器报告 `frame-ancestors` 策略阻止加载；不是仅检查响应头 |

noVNC 安全请求的实际结果：

| 请求 | 实际响应 |
|---|---|
| 正确 `127.0.0.1` Host，HTML | `200`，`X-Frame-Options: DENY`、`Content-Security-Policy: frame-ancestors 'none'` |
| 正确 `localhost` Host，HTML | `200`，相同防嵌入响应头 |
| 错误 Host，HTTP | `403 Invalid viewer Host` |
| 错误 Origin，WebSocket | `403 Invalid Origin` |
| 错误 Host，WebSocket | `403 Invalid viewer Host` |
| 正确 Host 与 Origin，WebSocket | `101 Switching Protocols`，`Sec-WebSocket-Accept` 与请求匹配 |

## 沙箱观测边界

本轮 Chromium 保留默认沙箱参数，未使用 `--no-sandbox` 或 `--password-store=basic`。实测 Renderer 为 `Seccomp=2`；持有所选 VF 的 GPU 进程为 `NoNewPrivs=1`、`CapEff=0`。CDP 的 GPU full sandbox 指标仍为 `false`，这是当前默认运行配置下的实际观测，**不宣称 GPU 完整沙箱通过**。文件句柄、渲染成功和这些进程限制不能代替该指标。

Cookie 检查只使用合成值。两条合成 Cookie 的 SQLite 元数据均显示明文字段长度0、加密字段长度99、前缀`v11`；检查未读取 Cookie 值或 keyring 中的秘密。这不表示 keyring 文件受口令加密保护：本轮专用自动登录 VM 有意使用空口令 keyring。

## 源码检查与待完成项目

可在相应环境重复运行：

```bash
python3 -m unittest discover -s tests -v
bash -n menu.sh index.sh components/the_pass_through.sh components/the_agent_workstation.sh
git diff --check
```

Windows 对应测试命令为 `python -m unittest discover -s tests -v`；包含 Windows SSH 命令替身测试，不需要连接远端。Linux 的 Windows 专属检查会跳过。

| 检查 | 当前状态 |
|---|---|
| Linux 源码测试 | 135项运行成功，其中8项因平台条件跳过 |
| Windows 最终全量测试 | 135项运行成功，其中2项因平台条件跳过，耗时27.965秒 |
| 静态检查 | Bash语法与`git diff --check`通过 |
| 手动冷启动、页面／合成 Cookie／keyring 持续性 | 已通过，实测范围见上表；不使用真实网站账号代替合成检查 |
| 宿主受保护资源最终比较 | PASS：宿主boot ID与内核、其他VM配置SHA与QEMU／swtpm PID、受保护宿主配置及外部SR-IOV配置SHA、PF／VF驱动均保持不变 |

当前宿主采用外部已有 GPU 配置。本轮没有实机覆盖全新宿主的驱动安装与启动参数变更、Secure Boot 分支、新独占 GPU 硬件或宿主重启恢复；这些范围不能从本机 VF 复用成功推导。真实网站登录仍需用户首次授权。

## 2026-11-05：驱动缓存与菜单摘要

此次仅验证源码与隔离容器，没有操作PVE或现有VM，不扩展上文的核显硬件验收范围。

- Debian13容器全量运行207项检查，199项通过、8项按平台或依赖条件跳过。
- Windows最终全量运行207项检查，205项通过、2项按平台条件跳过；首次遇随机本地端口占用的SSH替身检查，单独6项与最终全量重跑均通过。
- 缓存测试覆盖首次构建、跨客机命中、内核／架构／源码／配置／符号表不匹配、模块摘要和CRC失败、异常归档、保存失败、限制QGA读取及恢复运行中任务。菜单测试覆盖关键摘要、默认不展示JSON、Y只读查看、临时文件权限与清理、错误退出码及CLI默认错误输出。
- 另在隔离Debian13、DKMS3.2.2、内核headers `6.12.115+deb13.1-amd64`环境编译四个极小测试模块，使用真实modinfo、modprobe和DKMS完成首次构建／安装、清空测试注册后导入缓存／安装且零次build、损坏缓存后重编译。容器无模块加载权限，没有加载测试模块；此检查证明DKMS缓存导入流程，不代表真实Intel驱动或硬件加速验收。

## 2026-11-05：新建VM默认swap

Cloud-init配置新增固定4 GiB swap，客机在桌面安装前验证文件权限／容量、活动状态与自动启动条目；运行验收和菜单展示实际swap容量。Windows全量216项检查中213项通过、3项按平台条件跳过；隔离Debian13容器全量216项中208项通过、8项按平台或依赖条件跳过。覆盖元数据容量余量、未启用／不足量swap、缺失或禁用的fstab条目、非标准文件及存储预留边界。没有创建真实VM或实际启用／冷启动验证swap；此变更只面向新建VM，不修改已有VM。

## 2026-11-05：已删除VM的旧部署记录

Windows全量233项检查中229项通过、4项按平台条件跳过；隔离Debian13全量233项中225项通过、8项按平台或依赖条件跳过。新增检查覆盖默认取消／明确确认、PVE VMID断言、QEMU／LXC／PID占用、记录漂移、准备或原子写入失败，以及缓存／锁文件／旧打包文件保留。Linux实际文件锁检查确认准备期间其他操作无法取得同一VMID锁，完成后锁文件inode保持不变。这里只验证命令替身与临时文件，没有在PVE执行真实清理或创建。

## 2026-11-06：恢复、网络隔离、连接审计与原菜单保留

- Windows与Debian各运行355项测试，无失败，各10项因平台／内核能力跳过。新增覆盖任务跨重启恢复、未知QGA结果不触发GPU回退、打包与下载发布中断、运行时版本切换、首次启网顺序、规则读回漂移、额外NIC／MAC／网桥／VLAN漂移、人工网络策略切换及精确清理。最后对只读诊断的NIC一致性补充复测10项，网络集成20项通过。
- 审计23项测试含12个发布前后中断、18个删除前后中断收尾、旧配置与新源码半更新、精确临时文件恢复、现存摘要漂移拒绝、72小时过期与容量淘汰、关闭后继续过期清理、gap和心跳、只读导出及私有源码运行不生成字节码。普通状态查询不写资源，资源不完整明确显示unknown。
- 06／07保留入口与资源，专项10项测试覆盖单接口精确修改／取消／漂移拒绝、旧备份与归档保留、复制或归档失败返回非零、还原双确认，以及pmxcfs不允许保存属性时仅复制配置内容。备份不含VM磁盘。其他菜单和附带ROM／源文件未移除。
- `bash -n menu.sh index.sh components/*.sh`、Python编译、`git diff --check`通过；审计生成的systemd unit／timer通过`systemd-analyze verify`。

原WSL内核未启用nft bridge family，相关实包测试准确跳过。另使用Debian13标准内核 `6.12.107+deb13-amd64` 启动一次性KVM，严格无网卡、无磁盘，内部独立网络命名空间生成所有测试流量：

- 39条真实数据包断言全部通过：公网TCP/UDP、DNS、DHCP广播与0源地址、SSH请求及状态化回复、端口白名单；私网、宿主公网本地地址及公网地址型host route、伪造MAC／IP／ARP、IPv6、其他L2／嵌套VLAN阻断；模拟其他VM的IPv4／IPv6与相同目标连接保持可用；重复原子应用规则仍可连接。
- 最新审计恢复补丁下，真实conntrack订阅／快照、TCP／UDP入站出站元数据、SQLite、其他VM过滤、JSON／CSV导出、心跳和gap、SIGTERM退出及订阅子进程回收通过。仅模拟systemd命令，未模拟conntrack、collector或SQLite。
- 最终输出 `ONEPVE_NETWORK_39_EXIT=0`、`ONEPVE_AUDIT_KERNEL_EXIT=0`、`ONEPVE_ACCEPTANCE_EXIT=0`。开发环境日志为 `/var/tmp/onepve-netcheck/acceptance-20261106.txt`，SHA256 `a1521b9d676909f3c6f6884280cae5be46c2dbed77f390427f44780b9a2b3745`，测试机已正常关机。

本轮未操作实际PVE、未创建或改动现有VM，也未重验真实Intel GPU硬件加速。新建默认策略、依赖、固定IPv4／单网卡限制、审计缺口及网络变更失败恢复见[网络与审计](network-security.md)；不把流记录当作连接成功或完整取证保证。

# onePVE

PVE 宿主机工具箱。新增两个独立入口：**先准备显卡，再创建 Agent 工作机**。

| 菜单 | 用途 |
|---|---|
| **03 显卡直通／SR-IOV** | 检测占用、准备独占 PCI 或 Intel VF、查看状态、恢复本工具配置；不创建 VM、不自动重启宿主 |
| **08 Agent 工作机** | 创建 Debian 13、使用已有设备、安装远端浏览器环境、验收与显式重建；不安装宿主驱动 |

## 运行

在 PVE 宿主上以 root 运行，依赖 Python 3 和 PVE 自带命令，不需要额外的 Python 框架或 jq。

```bash
git clone https://github.com/OKFred/onePVE
bash onePVE/menu.sh
```

已有 checkout 可以先检查本地变更，再执行 `git pull --ff-only`。不要用 `git reset --hard` 覆盖自己的修改。菜单可以从其他工作目录启动；旧的 `index.sh` 现在也只进入明确选择的菜单，不再隐式更换源、配置显卡、改网络或重启。

菜单顶部显示启动时读取的提交号，有已跟踪文件修改时标注“本地修改”；无Git的分发副本可读取`onepve-source.json`中的提交记录。更新文件后，已运行菜单仍显示原启动版本，需要退出并重新启动才能加载更新。

## 两步准备

1. 进入 **03**，先做只读检测。选择 SR-IOV 时指定 PF，默认一个 VF；已有健康设备会优先复用。首次安装驱动或修改启动参数需要重启时，结果会明确显示，菜单不会自动重启。
2. 进入 **08**，从当前 PVE 发现的存储、活动网桥和空闲 VMID 中选择。提供 SSH **公钥**文件或单行公钥，选择 `auto` 复用可用 VF、`none` 使用软件桌面，或指定已准备的 PCI 地址。

新工作机默认隔离内网，可明确选择允许内网或设置精确白名单；连接审计可选、默认关闭。需要固定IPv4：选择静态地址，或先在路由器设置MAC对应的DHCP保留，并填写网关、DNS、DHCP服务器和SSH管理来源。宿主侧绑定MAC／IPv4，规则确认前网卡保持断开；旧VM网络保持原状。依赖、规则边界、切换及72小时审计说明见[网络与审计](docs/network-security.md)。

08 → 13提供[可选阿里云SLS上传](docs/sls-audit.md)，默认关闭；全宿主VM共用持久月度预算，默认100 MiB／100000次请求，按请求预扣、异常停发、新月重新核实额度。SLS免费额度由账号共享，本地限额不能保证账号费用为零。

08创建／重建可选择客机Debian APT源：清华`tuna`（向导默认）、中科大`ustc`或官方`official`。CLI使用`--apt-mirror`，不传时保持官方源。桌面和驱动依赖沿用所选镜像，软件包与安全更新均保留HTTPS及Debian签名校验；不修改PVE宿主源，不改变cloud image、Node／npm下载策略，也不自动修改已有VM。

普通创建遇到已存在 VMID 会拒绝覆盖。**重建是独立的删除操作，不自动备份，必须再次输入目标 VMID**；需要保留旧数据时，先使用 PVE 的 VM 备份。仓库不内置任何默认删除列表。

## 工作机内容

- Debian 13 官方稳定版 **generic amd64** cloud image，下载时锁定版本及 SHA512，校验后导入；不要替换为 genericcloud。
- 默认VM名称与客机主机名为`ai-dev`，可在向导中指定其他名称；已有VM和存储配置不自动改名。
- 最低4 vCPU、固定8 GiB内存，系统盘默认且最低32 GiB；Q35／OVMF、VirtIO SCSI、discard，保留 PVE 图形救援控制台。
- 普通用户 `agent`、SSH 公钥、QEMU Guest Agent、XFCE、Chromium、中文字体与输入法。
- fnm 管理 Node.js 26，记录完整版本；MCP和服务使用绝对路径与版本文件，不依赖交互式 `.bashrc`。Codex CLI 账户由用户首次重新授权，镜像不带任何旧登录。
- GPU模式为原生 Wayland，软件模式为 XFCE/X11。两者使用同一个浏览器 profile、用户 D-Bus/keyring、接管状态与端口。
- Chromium 首次启动前设置“继续上次会话”；MCP 停止、重启或 SSH 断开不会主动关闭浏览器工作页。
- 终端进度显示当前环节的预计剩余时间：文件下载按实际字节与速度估算，客机APT使用原生下载ETA；编译、配置、验收或采样不足时明确显示未知，不虚构整程倒计时。

GPU验收失败时，恢复本次 GPU 接入并保留可用的软件桌面、MCP和noVNC，同时报告加速失败。服务 `active` 不等于硬件加速通过。

## 查看与控制

VNC、noVNC、CDP和MCP只监听 VM 回环地址。**noVNC 不再单独输入密码，必须经过 SSH 密钥隧道**，不提供局域网免认证桌面。

```bash
ssh -N -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:16080:127.0.0.1:6080 \
  -L 127.0.0.1:18931:127.0.0.1:8931 agentvm
```

用顶层浏览器页面打开 [只读桌面](http://127.0.0.1:16080/vnc.html?autoconnect=1&shared=1&resize=scale&view_only=1)。`agentvm` 是客户端自行配置的 SSH 别名；菜单会输出新 VM 地址和连接信息。若本机查看端口换成其他值，安装向导中的“本机查看端口”也要使用同一个值。

Windows可使用仓库内的隐藏重连脚本：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\client\AgentTunnel.ps1 -Action Start -SshAlias agentvm`，将`Start`换成`Status`或`Stop`即可检查或停止。SSH别名与重建后可信主机密钥更新见 [Windows连接说明](docs/windows-client.md)；脚本不会导入私钥或安装本机MCP。

noVNC代理严格校验 Host 与 WebSocket Origin，并禁止 iframe 嵌入。它只允许配置好的 localhost／127.0.0.1入口，不支持通过修改监听地址来绕过。MCP 位于远端 `http://127.0.0.1:8931/mcp`；现有 Codex 在远端连接它，本机不需要部署 MCP 服务。

人工操作前，先停下 Codex 当前浏览器任务，将 noVNC 切为允许输入，点击“接管浏览器”并确认。完成后点击“交还 Codex”并确认，再切回只读；Codex 应重新读取标签页和快照。手动标记跨重启保留，只暂停 MCP 浏览器通道，不停止 SSH 构建任务。

## 命令行与验收

菜单与命令行共用实现。自动化调用前先查看帮助和只读计划：

```bash
python3 onepve.py gpu inventory
python3 onepve.py gpu status
python3 onepve.py gpu prepare-sriov --help
python3 onepve.py agent plan --help
python3 onepve.py agent create --help
python3 onepve.py agent status
```

需要修改的操作支持 `--dry-run`，不会安装驱动、下载镜像、写状态或修改 VM。参数、实际版本、日志位置及验收结果由命令输出；中断后通过状态检查，再使用“继续未完成的安装”，不要重新删除 VM。

在终端运行时，安装过程会显示当前阶段、本次命令耗时和阶段耗时；下载、编译等长时间等待每30秒提示一次。客机安装任务同时显示VM内日志路径。进度只输出到连接终端的stderr，stdout仍保留JSON结果；将stderr重定向到文件或管道时不自动输出这些提示。

详细步骤见 [Agent工作机与恢复](docs/agent-workstation.md) 和 [显卡准备与兼容范围](docs/gpu.md)；已测环境、实机结果及尚未覆盖的范围见 [本轮验收记录](docs/validation.md)。

## 其他原有菜单

01更换源、02基础工具、04退出集群、05 SWAP、06 DHCP、07旧版配置备份还原、09说明仍保留。它们只有明确选择后才执行。**07只处理配置文件，不是 VM 磁盘／账号备份，也不是新工作机或显卡流程的回滚实现。** 附带的第三方 `pve_source` 亦不参与新流程。

# Windows 查看与接管

Windows 只需要已有 Codex、OpenSSH 和浏览器。Node、Codex CLI、Playwright MCP 和工作浏览器都安装在 VM；下面的连接脚本不会安装本机 MCP、修改 Codex 配置或导入任何私钥。以当前用户运行，不需要管理员权限。

## 1. 配置 SSH 别名与主机身份

在 `%USERPROFILE%\.ssh\config` 中准备一个专用别名，替换下面的地址、端口和已有私钥路径。`HostName` 是当前电脑能够连接的 SSH 入口：可以是 VM 地址，也可以是已有 VPN／FRPC 的入口。转发入口必须最终到达 VM 的 SSH 22 端口。

FRPC指向固定VM地址时，创建向导应选择静态地址或使用已有DHCP保留，再核对Guest Agent返回的实际IPv4。保留原MAC和填写期望地址都不能保证DHCP继续分配原地址；地址未核验前，不把SSH失败当成主机密钥问题。

```sshconfig
Host agentvm
    HostName SSH_ENTRY_HOST
    Port SSH_ENTRY_PORT
    User agent
    IdentityFile "D:/path/to/existing/private-key"
    IdentitiesOnly yes
    UserKnownHostsFile ~/.ssh/known_hosts_agentvm
    StrictHostKeyChecking yes
```

使用已有的 OpenSSH 私钥文件；公钥已在创建 VM 时交给 Cloud-init。不要将客户端私钥复制进 VM。此别名中不要另外配置 `LocalForward`、`RemoteForward` 或 `DynamicForward`，脚本会拒绝继承额外转发。私钥有口令时，后台重连需要当前用户可用的 SSH agent 先解锁密钥；脚本不会弹出口令窗口。

首次创建与重建后，都先从**已登录的 PVE 控制台或可信宿主 SSH**读取新 VM 的主机公钥和指纹。在 PVE shell 中执行，`VMID` 换成创建结果：

```bash
VMID=YOUR_VM_ID
qm guest exec "$VMID" -- /bin/cat /etc/ssh/ssh_host_ed25519_key.pub
qm guest exec "$VMID" -- /usr/bin/ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

取返回的 `out-data` 中公钥正文，不包含 JSON 引号或转义。部署状态中的 `connection.host_public_key`、`connection.host_key_fingerprint` 也来自 Guest Agent 的可信读取。未取得可信公钥前，不跳过主机校验。

在本机 PowerShell 中填入相同 SSH 入口、新公钥并查看指纹：

```powershell
$sshEntryHost = 'SSH_ENTRY_HOST'
$sshEntryPort = 22 # 换成实际入口端口；已有 FRPC 如 57722 则填写 57722
$newHostPublicKey = 'ssh-ed25519 BASE64_FROM_TRUSTED_PVE'
$knownHostsPath = Join-Path $env:USERPROFILE '.ssh\known_hosts_agentvm'
$hostPattern = if ($sshEntryPort -eq 22) { $sshEntryHost } else { '[{0}]:{1}' -f $sshEntryHost, $sshEntryPort }
$newHostPublicKey | ssh-keygen.exe -lf - -E sha256
```

确认输出的 SHA256 指纹与 PVE 读取的一致，再执行以下更新。只替换这个专用文件中的当前入口条目，保留其他记录：

```powershell
New-Item -ItemType Directory -Force -Path (Split-Path $knownHostsPath) | Out-Null
if (!(Test-Path -LiteralPath $knownHostsPath)) { New-Item -ItemType File -Path $knownHostsPath | Out-Null }
ssh-keygen.exe -R $hostPattern -f $knownHostsPath
Add-Content -LiteralPath $knownHostsPath -Encoding ascii -Value "$hostPattern $newHostPublicKey"
ssh.exe -o StrictHostKeyChecking=yes agentvm true
```

最后一条成功后再启用后台隧道。如果原配置设置了 `HostKeyAlias`，应使用该名称更新条目；不要通过删除整份 known_hosts 或 `StrictHostKeyChecking=no` 绕过重建后的身份核验。

## 2. 启动、检查、停止隧道

在本机仓库目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\client\AgentTunnel.ps1 -Action Start -SshAlias agentvm
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\client\AgentTunnel.ps1 -Action Status -SshAlias agentvm
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\client\AgentTunnel.ps1 -Action Stop -SshAlias agentvm
```

`Start` 创建隐藏的后台重连进程；断线后每三秒重新连接，SSH 连接超时为十秒，已建立连接使用 30 秒心跳、连续三次失败后重连。重复启动不会再开一份。同一个别名、配置文件路径和本机端口对应同一个运行实例；修改 `-SshConfig`、`-ViewerPort`、`-McpPort` 或 `-StateRoot` 时，停止命令也要使用相同参数。

默认只绑定 `127.0.0.1:16080 → VM:6080` 与 `127.0.0.1:18931 → VM:8931`。端口被其他程序占用会拒绝启动，请先停止原来的隧道。`Stop` 使用本次运行的控制事件，只结束脚本自己启动的 SSH 子进程，不按保存的 PID 或端口寻找并终止其他程序。

状态与最后一次 SSH 错误位于命令返回的 `StateDirectory`；查看其中 `ssh.stderr.log` 诊断认证、主机密钥或网络错误。`Running=true`、`ssh-running` 只说明重连进程／SSH 进程仍在运行；实际连接要打开 noVNC 页面确认。

无需后台重连时，也可以在 PowerShell 前台使用下面这一行；按 `Ctrl+C` 只断开转发，VM 桌面和浏览器页面继续保留：

```powershell
ssh.exe -N -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:16080:127.0.0.1:6080 -L 127.0.0.1:18931:127.0.0.1:8931 agentvm
```

## 3. 查看、接管与 Codex

隧道启动后，用浏览器**顶层页面**打开 [只读桌面](http://127.0.0.1:16080/vnc.html?autoconnect=1&shared=1&resize=scale&view_only=1)，不需要 VNC 密码。自定义 `-ViewerPort` 必须与创建向导的本机查看端口一致，否则 Host／Origin 白名单会拒绝请求。此入口禁止嵌入 iframe。

先停止 Codex 当前浏览器任务，把 noVNC 切为允许输入，再点击控制面板的“接管浏览器”并确认。人工操作完成后点击“交还 Codex”并确认，将 noVNC 切回只读，再让 Codex 重新读取标签页和快照。GPU 模式下，PVE 内置控制台显示 XFCE 救援桌面；日常 Wayland 浏览器应从独立 noVNC 查看。

Codex 的 SSH 连接使用同一 `agentvm` 别名。首次在远端授权时，在 VM 的普通用户 `agent` 终端执行：

```bash
/opt/onepve-agent/bin/agent-codex login --device-auth
```

按命令输出在本机浏览器完成用户授权；不要把登录令牌写入脚本或镜像。远端已配置 `vm_browser → http://127.0.0.1:8931/mcp`，浏览器任务使用这个远端 MCP。`18931` 保留为隧道兼容入口；使用远端 Codex 时无需在本机增加 `vm_browser` 配置。不要为刷新 SSH 主机密钥重跑旧的、本机安装 MCP 配置的连接安装器。

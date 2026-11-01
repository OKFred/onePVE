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

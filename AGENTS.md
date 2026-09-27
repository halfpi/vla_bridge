# AGENTS.md

本文件为 AI 编码 Agent 提供项目背景与工作约定。开始任何任务前，请先阅读本文。

## 1. 项目定位

本项目（`vla_bridge`）是「基于 SecureLink 的 VLA 远程推理与动作执行方案」的落地工程，整体架构以 `doc/基于 SecureLink 的 VLA 远程推理与动作执行方案.md` 为准。开始编码前请先阅读该文档。

本机（Windows）在三层架构中扮演 **Windows Application Relay**（应用层中继），职责是：在树莓派与 Ubuntu VLA 推理端之间中继经过认证的 VLA 消息，**不直接控制设备**。

本机同时是 **VLA 代码的唯一开发源头**：VLA 代码先写在 Windows 的 WSL 中，开发完成后通过 `rsync` 单向推送到 Ubuntu 推理主机测试，详见第 4 节。

## 2. 总体架构

```
摄像头、传感器、机器人
          ↕
树莓派设备与安全执行端  (pi@192.168.2.30)
          │ WebSocket/gRPC 长连接 A（树莓派主动发起）
          ▼
Windows Application Relay  (本机)
          │ SecureLink
          │ WebSocket/gRPC 长连接 B（Windows 主动发起）
          ▼
Ubuntu VLA 推理主机 10.0.80.19:8443  (narwal@10.0.80.19)
```

两条连接都由靠近设备的一侧主动建立，不需要任何反向新建连接：

- 连接 A：树莓派 -> Windows Relay
- 连接 B：Windows Relay -> Ubuntu `10.0.80.19:8443`

## 3. 远程代码位置（本机需 SSH 访问）

| 项目 | 位置 | 角色 |
| ---- | ---- | ---- |
| VLA 推理端 | `narwal@10.0.80.19:/~/work/vla` | Ubuntu，GPU/VLA 模型推理与高层动作规划 |
| Robot 设备端 | `pi@192.168.2.30:/~/robot` | 树莓派，观测采集、动作执行与本地安全控制 |

本机 `vla_bridge` 为 Windows Relay，与上述两个远程项目协同工作。

## 4. VLA 代码开发与部署流向

VLA 代码先写在 Windows 的 WSL 中，开发完成后单向推送到 Ubuntu 推理主机测试：

```text
Windows 的 WSL（唯一开发源头）
          │ rsync 单向推送（只推不拉）
          ▼
Ubuntu VLA 推理主机 /home/narwal/work/vla（运行与测试）
```

标准推送命令（在 WSL 内执行）：

```bash
rsync -avzP --delete ~/work/vla/src narwal@10.0.80.19:/home/narwal/work/vla
```

必须遵守：

- **只推不拉**：只从 Windows 的 WSL 推送到 Ubuntu，不从 Ubuntu 拷贝代码回来，也不让 Ubuntu 上的改动覆盖本地源码。
- **不使用 git 推送**：不使用 `git push`，不向 Ubuntu 推送 git 仓库，也不推送到 GitHub。代码交付只走这条 `rsync` 通道。
- **Ubuntu 侧不做版本管理**：`/home/narwal/work/vla` 只是运行与测试目录，不视为源码真源；恢复、回滚一律以本地 WSL 为准。
- **`--delete` 是破坏性的**：会删除目标端 `src` 目录中源端已不存在的文件，执行前确认路径无误。源路径不带结尾 `/`，因此目标端同步为 `/home/narwal/work/vla/src`。
- 推送走 SSH，与 VLA 服务端口 `8443` 是两条独立通道，推送不用于传递运行期消息。

## 5. 关键网络约定

- Ubuntu 物理地址：`10.0.80.19/24`，VLA 服务监听 `0.0.0.0:8443`（TLS/认证）。
- Windows SecureLink 地址：`10.30.114.4`，可访问 `10.0.80.19`。
- 树莓派不在 SecureLink 内，仅通过本地网络主动连接 Windows Relay。
- 实测往返延迟约 22～139 ms，存在抖动，不适合电机级闭环控制。

## 6. 通信与消息约定

- 首选两段 gRPC 双向流（`rpc Control(stream ClientMessage) returns (stream ServerMessage)`），兼容场景可用两段 WebSocket（连接 B：`wss://10.0.80.19:8443/vla`）。
- 每条观测至少含：`device_id`、`session_id`、`observation_seq`、`captured_at_ms`、`robot_state`、`image_encoding`、`image`。
- 每条动作至少含：`device_id`、`session_id`、`observation_seq`、`action_seq`、`created_at_ms`、`valid_until_ms`、`mode`、`actions`。
- 心跳间隔 5～15 秒，需自动重连、指数退避，重连后使用新 `session_id`，断线清空旧动作队列。

## 7. 安全约定（必须遵守）

- 两段连接均采用双向 TLS（mTLS），每台设备使用独立证书与身份，不共享密钥。
- Relay 只接受已登记树莓派，只连接固定的 Ubuntu 服务，不做通用代理 / NAT / IP 转发。
- 按消息类型、大小、速率和目标服务施加白名单；拒绝转发过期或旧会话动作。
- 所有密钥/证书仅通过环境变量或密钥文件注入，禁止硬编码到源码。
- 代码推送通道只用于从 Windows 向 Ubuntu 单向传递 VLA 代码，不得同步密钥、证书或凭据到 Ubuntu。
- 记录连接、认证、流量与动作中继的审计日志。

## 8. 编码约定

- 修改代码前先阅读相关文档与本文件，保持与总体架构一致。
- VLA 代码在 Windows 的 WSL 中开发，完成后用 `rsync` 单向推送到 Ubuntu 测试，不使用 git 推送（见第 4 节）。
- 涉及跨端协议改动时，需同步考虑三个项目（Relay / Ubuntu / 树莓派）的消息兼容性。
- 安全相关逻辑（认证、白名单、过期动作丢弃）不可简化或绕过。

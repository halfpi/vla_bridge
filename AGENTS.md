# AGENTS.md

本文件为 AI 编码 Agent 提供项目背景与工作约定。开始任何任务前，请先阅读本文。

## 1. 项目定位

本项目（`vla_bridge`）是「基于 SecureLink 的 VLA 远程推理与动作执行方案」的落地工程，整体架构以 `doc/基于 SecureLink 的 VLA 远程推理与动作执行方案.md` 为准。开始编码前请先阅读该文档。

本机（Windows）在三层架构中扮演 **Windows Application Relay**（应用层中继），职责是：在树莓派与 Ubuntu VLA 推理端之间中继经过认证的 VLA 消息，**不直接控制设备**。

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

## 4. 关键网络约定

- Ubuntu 物理地址：`10.0.80.19/24`，VLA 服务监听 `0.0.0.0:8443`（TLS/认证）。
- Windows SecureLink 地址：`10.30.114.4`，可访问 `10.0.80.19`。
- 树莓派不在 SecureLink 内，仅通过本地网络主动连接 Windows Relay。
- 实测往返延迟约 22～139 ms，存在抖动，不适合电机级闭环控制。

## 5. 通信与消息约定

- 首选两段 gRPC 双向流（`rpc Control(stream ClientMessage) returns (stream ServerMessage)`），兼容场景可用两段 WebSocket（连接 B：`wss://10.0.80.19:8443/vla`）。
- 每条观测至少含：`device_id`、`session_id`、`observation_seq`、`captured_at_ms`、`robot_state`、`image_encoding`、`image`。
- 每条动作至少含：`device_id`、`session_id`、`observation_seq`、`action_seq`、`created_at_ms`、`valid_until_ms`、`mode`、`actions`。
- 心跳间隔 5～15 秒，需自动重连、指数退避，重连后使用新 `session_id`，断线清空旧动作队列。

## 6. 安全约定（必须遵守）

- 两段连接均采用双向 TLS（mTLS），每台设备使用独立证书与身份，不共享密钥。
- Relay 只接受已登记树莓派，只连接固定的 Ubuntu 服务，不做通用代理 / NAT / IP 转发。
- 按消息类型、大小、速率和目标服务施加白名单；拒绝转发过期或旧会话动作。
- 所有密钥/证书仅通过环境变量或密钥文件注入，禁止硬编码到源码。
- 记录连接、认证、流量与动作中继的审计日志。

## 7. 编码约定

- 修改代码前先阅读相关文档与本文件，保持与总体架构一致。
- 涉及跨端协议改动时，需同步考虑三个项目（Relay / Ubuntu / 树莓派）的消息兼容性。
- 安全相关逻辑（认证、白名单、过期动作丢弃）不可简化或绕过。

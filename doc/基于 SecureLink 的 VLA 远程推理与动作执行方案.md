# 基于 SecureLink 的 VLA 远程推理与动作执行方案

## 1. 目标与现状

本方案用于连接两地设备：

- Ubuntu 主机：部署 GPU 和 VLA（Vision-Language-Action）模型，负责推理与高层动作规划。
- 树莓派：随设备移动，连接摄像头、机械臂、PLC 或移动底盘，负责观测采集、动作执行和本地安全控制。
- Windows PC：运行 SecureLink 和应用层 Relay，在树莓派与 Ubuntu 之间中继经过认证的 VLA 消息，不直接控制设备。
- 两端通过网宿 SecureLink 通信，不在同一个物理局域网内。

当前已观察到的网络状态：

- Ubuntu 物理地址：`10.0.80.19/24`。
- Ubuntu SecureLink TUN 地址：`10.30.21.99/16`。
- Windows SecureLink 地址：`10.30.114.4`。
- Windows 可以访问 Ubuntu 发布的 `10.0.80.19`。
- Ubuntu 主动访问 Windows 可能被 SecureLink 策略、客户端隔离或 Windows 防火墙阻止。
- 网宿公开资料只明确写明支持 Linux，未公开确认客户端提供 Raspberry Pi 所需的 ARM64/AArch64 构建，因此本方案不依赖树莓派安装 SecureLink。
- 实测往返延迟约为 22～139 ms，存在明显抖动。

因此，设计目标不是让两台主机像传统局域网一样双向开放，而是让通信方式适配 SecureLink 的零信任访问模型。

## 2. 总体架构

采用“树莓派设备端 + Windows 应用层中继 + Ubuntu VLA 推理端”三层架构：

```text
摄像头、传感器、机器人
          ↕
树莓派设备与安全执行端
          │ 本地以太网或专用 Wi-Fi
          │ WebSocket/gRPC 长连接 A
          ▼
Windows Application Relay
          │ SecureLink
          │ WebSocket/gRPC 长连接 B
          ▼
Ubuntu VLA 推理主机 10.0.80.19:8443
```

两条连接都由靠近设备的一侧主动建立：

```text
树莓派 -> Windows Relay
Windows Relay -> Ubuntu 10.0.80.19:8443
```

观测和动作在应用层按会话转发：

```text
树莓派观测 -> Windows Relay -> Ubuntu VLA
树莓派执行 <- Windows Relay <- Ubuntu 动作
```

Windows 不启用 IP forwarding、Internet Connection Sharing 或 NAT，不把树莓派所在网段接入 SecureLink。SecureLink 只需授权 Windows 访问 Ubuntu 的 TCP 8443 端口。

### 2.1 树莓派的角色与职责

树莓派是**设备与本地安全执行端**，随机器人移动并直接连接现场硬件。它负责：

- 接入 RGB/深度相机、机械臂、夹爪、底盘、PLC 和传感器；
- 采集图像、关节状态、末端位姿、力和设备状态；
- 完成图像缩放、压缩、时间同步和坐标变换；
- 主动连接 Windows Relay，并上传观测和执行反馈；
- 接收高层目标或短动作块并转换为设备 SDK 指令；
- 执行轨迹插值、限速、碰撞检查和动作有效期检查；
- 维护本地看门狗，在通信中断时清空动作队列并安全停止；
- 保留从观测、模型动作、安全检查到执行结果的本地审计日志。

安全优先级为：

```text
物理急停
  > PLC/MCU/设备安全控制器
  > 树莓派本地安全规则
  > Ubuntu VLA 动作
```

树莓派不是经过安全认证的控制器。物理急停、电机保护等硬安全功能必须留在 PLC、MCU 或设备控制器中，不能只依赖树莓派软件。

### 2.2 Windows 的角色与职责

Windows 是**应用层中继与 SecureLink 准入端**，不直接控制设备。它负责：

- 运行 SecureLink，并以受管设备身份访问 Ubuntu VLA 服务；
- 接受来自指定树莓派的本地 TLS/WebSocket/gRPC 连接；
- 主动建立并维持到 Ubuntu `10.0.80.19:8443` 的长连接；
- 根据 `device_id`、`session_id` 和序号双向转发观测、动作、心跳、状态与错误；
- 验证树莓派身份，只允许已登记设备使用中继；
- 施加消息类型、大小、速率和目标服务白名单；
- 处理两侧断线、背压和重连，拒绝转发过期或旧会话动作；
- 记录连接、认证、流量和动作中继审计日志。

Windows Relay 不应：

- 解析后擅自修改 VLA 动作语义；
- 代替树莓派实施最终设备安全控制；
- 开启通用代理、端口转发或路由功能；
- 允许树莓派通过它访问 SecureLink 中的其他资源。

### 2.3 Ubuntu 的角色与职责

Ubuntu 是**VLA 推理与高层规划端**，负责：

- 接收带设备与会话身份的观测；
- 运行 GPU/VLA 模型；
- 生成高层目标或有期限的短动作块；
- 维护模型上下文、模型版本和推理日志；
- 处理树莓派执行反馈并决定下一步动作；
- 在会话不一致、观测过旧或状态异常时停止生成动作。

总体职责边界是：Ubuntu 决定“做什么”，Windows 只负责“安全地中继”，树莓派和设备控制器负责“能否安全执行以及如何实时执行”。

### 2.4 VLA 代码开发与部署流向

VLA 代码的开发环境与运行环境分离，代码流向严格单向：

```text
Windows 的 WSL（唯一开发源头，代码先写在这里）
          │ rsync 单向推送（只推不拉）
          ▼
Ubuntu VLA 推理主机 /home/narwal/work/vla（运行与测试）
```

开发阶段在 Windows 的 WSL 中完成 VLA 代码编写。开发完成后，通过 `rsync` 把代码推送到 Ubuntu VLA 推理主机进行测试：

```bash
rsync -avzP --delete ~/work/vla/src narwal@10.0.80.19:/home/narwal/work/vla
```

约定：

- **只推不拉**：只从 Windows 的 WSL 向 Ubuntu 推送代码，不从 Ubuntu 拷贝代码回来，也不让 Ubuntu 上的改动反向覆盖本地源码。WSL 是唯一开发源头，Ubuntu 只作为推理运行与联调环境。
- **不使用 git 推送**：不向 Ubuntu 推送 git 仓库，也不推送到 GitHub。代码交付只通过上述 `rsync` 通道，Ubuntu 上不保留版本历史。
- **Ubuntu 侧不做版本管理，不视为源码真源**：`/home/narwal/work/vla` 只是推理运行与测试目录，不是代码真源，也不作为恢复或回滚的依据；任何找回、回退都以 Windows WSL 中的开发目录为准。
- **路径语义**：该命令源路径 `~/work/vla/src` 不带结尾 `/`，因此会在目标端同步为 `/home/narwal/work/vla/src`；若要只同步目录内容而不多建一层，需要写成 `~/work/vla/src/`。
- **`--delete` 是破坏性选项**：会删除目标端 `src` 目录中源端已不存在的文件。由于是单向推送，Ubuntu 上手工创建的临时文件也会被清除，执行前应确认路径无误。
- 该推送走 SSH，与 VLA 服务端口 `8443` 是两条独立通道：推送只用于传递代码，不用于传递运行期消息。

## 3. 为什么这种方案可以工作

### 3.1 “谁建立连接”和“谁发送数据”不是一回事

TCP 连接由 Windows 发起：

```text
Windows:随机端口 -> Ubuntu:8443
```

连接建立过程为：

```text
Windows -> Ubuntu    SYN
Ubuntu  -> Windows   SYN-ACK
Windows -> Ubuntu    ACK
```

如果 SecureLink 允许 Windows 访问 Ubuntu 的 8443 端口，那么它必须允许该连接对应的返回包，否则任何 HTTP、SSH 或 VNC 访问都无法正常工作。

连接建立之后，TCP 本身是全双工的。Windows 和 Ubuntu 都能在 SecureLink 内的长连接 B 上发送数据：

```text
Windows -> Ubuntu    中继后的摄像头图像、机器人状态
Ubuntu  -> Windows   VLA 动作结果
Windows -> Ubuntu    中继后的执行反馈、心跳
```

Ubuntu 返回动作数据不等于 Ubuntu 主动建立了一条到 Windows 的新连接。它只是通过 Windows 已获准建立的现有会话发送响应或推送数据。

动作到达 Windows 后，再通过树莓派主动建立的本地长连接 A 推送给树莓派。Windows 同样不需要主动连接树莓派：

```text
Ubuntu --连接 B 内返回--> Windows Relay --连接 A 内返回--> 树莓派
```

因此，两段网络都只需要允许靠近设备一侧发起连接，不需要任何反向新建连接。

### 3.2 状态防火墙会识别已建立会话

SecureLink 安全网关和 Windows 防火墙通常使用状态检测。它们会维护类似以下连接状态：

```text
源：Windows SecureLink 身份/地址/临时端口
目标：Ubuntu 10.0.80.19:8443
协议：TCP
状态：ESTABLISHED
策略：允许
```

属于该连接的返回数据可以通过；Ubuntu 另行发起的新连接则需要重新匹配入站策略：

```text
允许：Ubuntu:8443 -> Windows:原临时端口，属于既有连接
拒绝：Ubuntu:任意端口 -> Windows:新监听端口，属于新连接
```

这解释了为什么 Windows 可以访问 Ubuntu，而 Ubuntu 主动 `ping` 或连接 Windows 可能失败，却不影响 Ubuntu在既有 TCP 连接中返回推理结果。

### 3.3 SecureLink 控制的是会话准入，不是强制单向传输

SecureLink 的零信任策略通常判断：

- 哪个用户和设备发起访问；
- 能访问哪个应用、IP 和端口；
- 设备是否合规；
- 当前风险和信任状态；
- 是否需要二次认证或动态阻断。

“Windows 被允许访问 Ubuntu VLA 服务”表示 Windows 可以建立到该资源的会话。应用层协议可以在这条会话内进行双向交互。常规 HTTPS 请求也是同样的机制：客户端上传请求，服务器返回内容。

WebSocket 和 gRPC 双向流只是让这条已获准的长连接持续存在，并允许服务器在连接内及时发送数据。

### 3.4 此方案没有绕过 SecureLink

该设计是适配策略，而不是绕过策略：

- Windows 仍需通过 SecureLink 完成身份认证和设备检查。
- Windows 只能访问管理员授权的 Ubuntu 地址和端口。
- 流量仍经过 SecureLink 隧道和安全网关。
- 管理员撤销授权、设备信任降低或会话过期后，连接仍可被切断。
- Ubuntu 和 Windows 不会因此获得任意的网络互访能力。
- 树莓派没有获得 SecureLink 路由，也不能借助 Relay 任意访问企业资源。
- Relay 只在应用层转发经过认证和授权的 VLA 消息，不提供通用 TCP/SOCKS/HTTP 代理。

## 4. 仍然可能受到的限制

这种架构能适配客户端隔离，但不意味着完全不受限制。以下情况仍会导致失败：

### 4.1 SecureLink 不允许访问 Ubuntu 8443

管理员需要配置明确的资源与访问策略：

```text
主体：指定 Windows 用户或设备
资源：Ubuntu 10.0.80.19
协议：TCP
端口：8443
动作：允许
```

### 4.2 SecureLink 只支持应用代理，不支持任意长连接

如果租户采用的是 Web 应用代理而不是三层网络接入，需要确认代理是否支持：

- WebSocket Upgrade；
- HTTP/2 长连接；
- gRPC；
- 长连接空闲超时时间；
- 单次请求体和传输速率限制。

如果 gRPC/HTTP2 被代理限制，可优先改用 `wss://` WebSocket，或让管理员把该服务配置为 TCP 应用。

### 4.3 空闲超时或网络切换会断开连接

SecureLink 网关、NAT 或代理可能清理长期无数据的连接。因此协议必须包含：

- 每 5～15 秒一次的应用层心跳；
- 自动重连和指数退避；
- 会话 ID 和动作序号；
- 重连后重新同步机器人状态；
- 断线后清空旧动作队列。

具体心跳间隔应小于租户配置的最短空闲超时。

本方案同时存在连接 A（树莓派到 Windows）和连接 B（Windows 到 Ubuntu）。Relay 必须分别监控两条连接：任意一侧断开，都要立即停止转发动作并通知另一侧，不能把旧动作缓存在队列中等待设备重连后继续执行。

### 4.4 策略可以在会话中途动态变化

零信任系统可能根据账户状态、设备健康度或风险评分动态撤销权限。应用不能假设长连接永远有效，必须正确处理：

- TCP Reset；
- TLS 失败；
- 认证过期；
- 服务端拒绝；
- SecureLink 重连和地址变化。

### 4.5 网络延迟不适合实时伺服控制

SecureLink 可以解决可达性和访问控制问题，不能消除广域网络延迟和抖动。当前 22～139 ms 的往返延迟不适合让 Ubuntu承担电机级闭环控制。

推荐分层：

| 控制层                   | 推荐位置              |     典型频率 |
| ------------------------ | --------------------- | -----------: |
| VLA 语义决策             | Ubuntu                |     1～10 Hz |
| 高层目标或动作块生成     | Ubuntu                |     1～20 Hz |
| 轨迹插值、限速与碰撞检查 | 树莓派                |   50～200 Hz |
| 电机伺服闭环             | PLC、MCU 或设备控制器 | 100～1000 Hz |

Ubuntu 应返回带时间戳和有效期的高层目标或短动作块，而不是通过网络逐周期控制电机。

## 5. 通信协议选择

### 5.1 首选：两段 gRPC 双向流

```proto
service VLAService {
  rpc Control(stream ClientMessage)
      returns (stream ServerMessage);
}
```

树莓派到 Windows、Windows 到 Ubuntu 分别建立独立的双向流，由 Relay 按设备和会话映射两条流。优点：

- 使用一条 HTTP/2 TCP 长连接；
- Protobuf 消息结构清晰；
- 支持客户端和服务端异步发送；
- Python、C++ 和 C# 支持良好；
- 适合图像、状态、动作和错误消息复用。

前提是 SecureLink 接入方式支持 HTTP/2 和 gRPC 长连接。

### 5.2 兼容性优先：两段 WebSocket

连接 B 可以使用：

```text
wss://10.0.80.19:8443/vla
```

连接 A 使用 Windows 在本地设备网络上提供的独立 `wss://Windows地址:端口/relay`。WebSocket 支持服务器在既有连接内推送动作，并且通常比 gRPC 更容易穿过 Web 代理。

### 5.3 不建议直接跨 SecureLink 使用原生 ROS 2 DDS

ROS 2 DDS 常依赖 UDP、组播发现、动态端口以及节点间双向主动连接。这些特性与零信任网关和客户端隔离不易配合。

如果现有系统使用 ROS 2，可在树莓派和 Ubuntu 两侧保留本地 ROS 2，由 Relay 协议显式转发所需主题；也可评估 Zenoh，但仍应将跨 SecureLink 通信收敛到 Windows 发起的单一 TCP/TLS 长连接。

## 6. 应用消息设计

每条观测至少包含：

```json
{
  "device_id": "raspberrypi-01",
  "session_id": "robot-01",
  "observation_seq": 1834,
  "captured_at_ms": 1780000000000,
  "robot_state": {},
  "image_encoding": "jpeg",
  "image": "<binary>"
}
```

每条动作至少包含：

```json
{
  "device_id": "raspberrypi-01",
  "session_id": "robot-01",
  "observation_seq": 1834,
  "action_seq": 472,
  "created_at_ms": 1780000000080,
  "valid_until_ms": 1780000000580,
  "mode": "cartesian_trajectory",
  "actions": []
}
```

关键字段用途：

- `device_id`：供 Relay 和 Ubuntu 识别并授权具体树莓派。
- `session_id`：防止重连后混入旧会话数据。
- `observation_seq`：确认动作基于哪一帧观测生成。
- `action_seq`：拒绝重复、乱序动作。
- `valid_until_ms`：网络堵塞后拒绝执行过期动作。
- `mode`：明确动作的坐标系和执行语义。

对于图像流，建议只保留最新观测，避免拥塞后继续处理陈旧帧。可使用 JPEG/WebP 或 H.264/H.265 压缩，并降低到模型实际需要的分辨率。

## 7. 树莓派本地安全边界

VLA 输出不能直接无条件驱动设备。树莓派执行端必须独立实施：

- 工作空间、关节角、速度和加速度限制；
- 碰撞检测；
- 动作序号、时间戳和有效期检查；
- 看门狗和断线减速停止；
- 本地急停；
- 操作员确认和运行模式管理；
- 单一控制者租约，防止多个会话同时控制同一设备。

建议状态机：

```text
DISCONNECTED -> CONNECTING -> SYNCING -> READY -> RUNNING
                                      \-> FAULT
RUNNING --心跳超时/连接断开/动作非法--> SAFE_STOP
```

安全停机阈值应根据设备风险评估确定，不能仅依赖网络重连。

## 8. 安全配置

建议在 SecureLink 访问控制之外增加应用层安全：

- 使用 TLS；
- 正式环境的连接 A 和连接 B 均采用双向 TLS；
- 每台树莓派使用独立设备证书和身份，不共享密钥；
- Windows Relay 只接受已登记树莓派，并只连接固定的 Ubuntu 服务；
- Relay 不提供通用代理、NAT 或 IP 转发；
- Ubuntu 只监听必要端口；
- 记录用户、设备、会话、模型版本和动作审计日志；
- SecureLink 策略只授权指定 Windows 设备访问 `10.0.80.19:8443`；
- 不开放整个 Ubuntu 网段或无关端口；
- 代码推送通道（SSH/`rsync`）只用于从 Windows 向 Ubuntu 单向推送 VLA 代码，不用于传递运行期消息，也不反向拉取代码；
- 推送内容不得包含密钥、证书或凭据，禁止把本地凭据同步到 Ubuntu（使用 `--exclude` 排除 `.env`、`*.pem`、`*.key`、`id_rsa*` 等）。

VLA 服务可以监听所有本机地址：

```text
0.0.0.0:8443
```

但主机防火墙应将来源限制为 SecureLink 使用的地址范围或实际网关地址。若 SecureLink 使用代理转发，应先确认服务端实际看到的源地址。

## 9. 验证步骤

### 9.1 验证基础 TCP 可达性

Ubuntu 临时监听：

```bash
python3 -m http.server 8443 --bind 0.0.0.0
```

Windows 测试：

```powershell
Test-NetConnection 10.0.80.19 -Port 8443
```

### 9.1.1 当前实测结果

Ubuntu 已执行：

```bash
python -m http.server 8443 --bind 0.0.0.0
```

服务正常监听：

```text
Serving HTTP on 0.0.0.0 port 8443
```

Windows 通过 SecureLink 测试得到：

```text
ComputerName     : 10.0.80.19
RemoteAddress    : 10.0.80.19
RemotePort       : 8443
InterfaceAlias   : 本地连接
SourceAddress    : 10.30.114.4
TcpTestSucceeded : True
```

该结果确认第一阶段验证成功，证明：

- Windows SecureLink 地址 `10.30.114.4` 能访问 Ubuntu 的 `10.0.80.19:8443`；
- SecureLink 已正确转发这条访问路径；
- 当前访问策略允许 TCP 8443；
- Ubuntu 服务正确监听，主机侧没有阻止本次 TCP 建连；
- TCP 三次握手及其返回报文能够通过 SecureLink。

可以继续验证完整 HTTP 请求和响应：

```powershell
Invoke-WebRequest http://10.0.80.19:8443/
```

如果返回 Ubuntu 当前目录的文件列表，说明 HTTP 应用层的一次请求和响应也能正常通过。

需要注意：`Test-NetConnection` 只证明 TCP 建连成功；`Invoke-WebRequest` 只证明常规的一问一答成功。二者都没有单独证明 Ubuntu 能在持久连接中异步推送动作，因此还需要执行下一节的长连接双向推送测试。

测试结束后停止临时服务。正式服务必须使用 TLS 和认证。

### 9.2 验证连接 B 的双向数据

部署最小 WebSocket 或 gRPC 测试程序：

1. Windows 主动建立连接。
2. Windows 每秒发送一个递增序号。
3. Ubuntu不等待下一次请求，主动在既有连接中推送响应序号。
4. 持续运行并观察 SecureLink 重连、网络切换和空闲状态。

如果步骤 1 成功且步骤 3 失败，应检查 SecureLink 是否采用了不支持 WebSocket/gRPC 的应用代理，而不是将其误判为客户端隔离。

本阶段的合格结果应当是：

```text
Windows --主动建立连接--> Ubuntu
Windows --上传观测数据--> Ubuntu
Windows <--主动推送动作-- Ubuntu（复用既有连接）
Windows --返回执行确认--> Ubuntu
```

Ubuntu 推送动作属于已建立 TCP 会话内的返回数据，不是 Ubuntu向 Windows 发起的新连接。此项通过后，便验证了 VLA 网络架构最关键的通信机制。

### 9.3 验证树莓派到 Windows 的连接 A

在 Windows Relay 所在的本地设备网卡上监听一个测试端口，例如 `9443`。树莓派主动连接该端口，验证：

1. 树莓派能够上传模拟观测和心跳；
2. Windows 能在既有连接内向树莓派主动推送模拟动作；
3. 拔掉树莓派网线或关闭 Wi-Fi 后，树莓派进入安全停止；
4. 网络恢复后建立新 `session_id`，不接受旧动作。

### 9.4 验证端到端 Relay

最终合格路径为：

```text
树莓派 --观测 seq=1--> Windows --观测 seq=1--> Ubuntu
树莓派 <--动作 seq=1-- Windows <--动作 seq=1-- Ubuntu
树莓派 --执行 ACK----> Windows --执行 ACK----> Ubuntu
```

Relay 必须保持 `device_id`、`session_id`、观测序号和动作序号不变，并记录两段链路各自的接收与发送时间。先使用模拟动作完成验证，不要直接连接真实执行器。

基本推送测试通过后，应加入 5～15 秒一次的心跳并持续运行至少 30 分钟，用来检查 SecureLink、代理或 NAT 是否会清理长时间连接。测试还应覆盖一次主动断网和自动重连，确认重连后旧动作不会继续执行。

### 9.5 故障定位

```text
TCP 8443 无法建立
  -> SecureLink 资源授权、路由、Ubuntu防火墙或服务监听问题

连接建立但很快断开
  -> TLS、代理协议兼容、认证或空闲超时问题

连接正常但推理超时
  -> 图像过大、带宽、模型耗时或消息队列积压

Windows收到动作但树莓派未收到
  -> Relay会话映射、连接A、设备认证或本地网络问题

树莓派收到动作但设备不执行
  -> 动作过期、树莓派本地安全限制或设备控制器问题
```

## 10. 推荐落地方案

第一阶段可采用：

```text
Ubuntu：Python + VLA 模型 + WebSocket/gRPC 服务
Windows：Python/C#/C++ Application Relay + SecureLink
树莓派：设备 SDK + 观测客户端 + Safety Controller
连接A：树莓派主动连接 Windows Relay
连接B：Windows主动连接 10.0.80.19:8443
安全：两段 mTLS + SecureLink 最小授权 + 独立设备身份
控制：Ubuntu生成高层动作块，树莓派完成实时执行与本地安全闭环
代码：VLA 代码在 Windows 的 WSL 中开发，再用 rsync 单向推送到 Ubuntu 运行测试
```

实施顺序：

> 开发前置：VLA 代码在 Windows 的 WSL 中编写，开发完成后用 `rsync -avzP --delete ~/work/vla/src narwal@10.0.80.19:/home/narwal/work/vla` 单向推送到 Ubuntu 作为测试环境，不使用 git 推送。

1. 让 SecureLink 管理员开放 Windows 到 `10.0.80.19:8443/TCP`。
2. 验证 Windows 与 Ubuntu 之间连接 B 的双向推送。
3. 验证树莓派与 Windows 之间连接 A 的双向推送。
4. 实现按 `device_id` 和 `session_id` 映射的 Relay，并完成端到端模拟消息测试。
5. 在两段连接中加入 TLS、身份认证、心跳、背压和自动重连。
6. 在树莓派实现本地安全控制器、动作有效期和断线安全停止。
7. 接入观测数据和模拟动作，暂不连接真实执行器。
8. 在低速、限位和人工急停条件下进行实机测试。
9. 最后才提高动作频率与运行范围。

## 11. 结论

该方案成立的核心原因是：树莓派不需要安装 SecureLink，也不需要获得企业网络路由。树莓派只主动连接本地 Windows Relay；Windows 再以受管 SecureLink 终端身份主动连接 Ubuntu。两条经授权的 TCP 长连接都能全双工传输，因此 Ubuntu 的动作可以沿既有连接 B 和连接 A 原路返回，不需要 Ubuntu 或 Windows 发起反向新连接。

Windows 只进行应用层消息中继，不提供通用网络转发，因此没有把树莓派偷偷接入 SecureLink，也没有绕过零信任访问边界。该方案仍受到资源授权、代理协议支持、两段连接的会话超时、带宽和延迟限制，因此必须采用持久连接、设备认证、心跳重连、过期动作丢弃、树莓派本地安全闭环和高低层控制分离。

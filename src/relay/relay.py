"""Windows Application Relay。

- 连接 A：作为 WebSocket 服务端，监听 0.0.0.0:9443，接受树莓派主动连接。
- 连接 B：作为 WebSocket 客户端，主动连接 Ubuntu VLA 服务 10.0.80.19:8443。
- 按 device_id/session_id 双向转发观测、动作、心跳与 ACK，不解析修改动作语义。
"""

import asyncio
import json
import logging

import websockets

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 9443
UBUNTU_URI = "ws://10.0.80.19:8443"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [relay] %(levelname)s %(message)s",
)
log = logging.getLogger("relay")


async def forward(src_ws, dst_ws, direction: str):
    """单向转发：从 src 读取，原样转发到 dst。"""
    try:
        async for raw in src_ws:
            try:
                msg = json.loads(raw)
                summary = (
                    f"type={msg.get('type')} device={msg.get('device_id')} "
                    f"session={msg.get('session_id')} "
                    f"obs_seq={msg.get('observation_seq')} "
                    f"action_seq={msg.get('action_seq')}"
                )
            except (json.JSONDecodeError, AttributeError):
                summary = "raw"
            log.info("[%s] 转发 %s", direction, summary)
            await dst_ws.send(raw)
    except websockets.ConnectionClosed:
        log.info("[%s] 连接关闭", direction)


async def handle_pi(pi_ws):
    """处理单个树莓派会话：建立到 Ubuntu 的连接 B 并双向转发。"""
    pi_addr = pi_ws.remote_address
    log.info("树莓派已连接（连接 A）：%s", pi_addr)
    try:
        async with websockets.connect(UBUNTU_URI) as ubuntu_ws:
            log.info("已建立到 Ubuntu 的连接 B：%s", UBUNTU_URI)
            # 双向并发转发
            await asyncio.gather(
                forward(pi_ws, ubuntu_ws, "A->B"),
                forward(ubuntu_ws, pi_ws, "B->A"),
            )
    except websockets.ConnectionClosed:
        log.info("连接 B 建立失败或关闭")
    except OSError as e:
        log.error("无法连接 Ubuntu：%s", e)
    finally:
        log.info("树莓派会话结束：%s", pi_addr)


async def main() -> None:
    log.info("Windows Relay 启动，连接 A 监听 %s:%s", LISTEN_HOST, LISTEN_PORT)
    log.info("连接 B 目标：%s", UBUNTU_URI)
    async with websockets.serve(handle_pi, LISTEN_HOST, LISTEN_PORT):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())

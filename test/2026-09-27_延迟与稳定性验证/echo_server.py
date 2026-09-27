"""连接 A 段延迟测试用 echo 服务端（Windows 本机临时使用）。

监听 0.0.0.0:9444，收到任意 JSON 消息后回显一个带 observation_seq 的 echo，
用于让树莓派测量「树莓派 <-> Windows」本地连接 A 段的往返延迟。

仅用于测试，不参与正式消息中继。测试结束即可停止。
"""

import asyncio
import json
import logging

import websockets

HOST = "0.0.0.0"
PORT = 9444

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [echo] %(levelname)s %(message)s",
)
log = logging.getLogger("echo")


async def handler(websocket):
    peer = websocket.remote_address
    log.info("客户端已连接：%s", peer)
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            seq = msg.get("observation_seq")
            reply = {
                "type": "echo",
                "observation_seq": seq,
            }
            await websocket.send(json.dumps(reply))
    except websockets.ConnectionClosed:
        log.info("客户端断开：%s", peer)


async def main() -> None:
    log.info("echo 服务端启动，监听 %s:%s", HOST, PORT)
    async with websockets.serve(handler, HOST, PORT):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())

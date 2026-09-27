"""通用延迟/稳定性探测客户端。

主动连接目标 WebSocket 服务，发送带时间戳的 observation，收到回包后
计算单次往返延迟（RTT）。支持两种模式：

- pingpong：收到回包后立即发送下一条，测纯往返延迟与抖动；
- interval：按固定间隔发送，后台匹配回包，用于长时间稳定性测试。

统计指标：样本数、min/avg/max、p50/p95/p99、标准差、相邻差抖动、
超时率，并统计自动重连次数。可在 Windows（测连接 B）或树莓派（测端到端/
连接 A）运行。

用法示例：
    # Windows 本机测连接 B（SecureLink 段），直连 Ubuntu
    python latency_probe.py --uri ws://10.0.80.19:8443 --label B --duration 60

    # 树莓派测端到端（经 Windows Relay）
    python3 latency_probe.py --uri ws://192.168.2.49:9443 --label E2E --duration 60

    # 树莓派测连接 A（连 Windows echo_server）
    python3 latency_probe.py --uri ws://192.168.2.49:9444 --label A --duration 60
"""

import argparse
import asyncio
import json
import logging
import statistics
import time

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [probe] %(levelname)s %(message)s",
)
log = logging.getLogger("probe")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _make_observation(seq: int, device_id: str, session_id: str) -> dict:
    return {
        "type": "observation",
        "device_id": device_id,
        "session_id": session_id,
        "observation_seq": seq,
        "captured_at_ms": _now_ms(),
        "robot_state": {"joint": [0.0, 0.0, 0.0]},
        "image_encoding": "jpeg",
        "image": "<probe>",
    }


class ProbeStats:
    """收集并输出延迟统计。"""

    def __init__(self, label: str, duration: float) -> None:
        self.label = label
        self.duration = duration
        self.rtts: list[float] = []  # 毫秒
        self.timeouts = 0
        self.reconnects = 0

    def add_rtt(self, rtt_ms: float) -> None:
        self.rtts.append(rtt_ms)

    def report(self) -> str:
        if not self.rtts:
            return (
                f"===== 延迟统计 [{self.label}] =====\n"
                f"无有效样本（超时 {self.timeouts}，重连 {self.reconnects}）"
            )

        r = sorted(self.rtts)
        n = len(r)

        def pct(p: float) -> float:
            idx = min(n - 1, int(round(p / 100 * (n - 1))))
            return r[idx]

        jitter = 0.0
        if n >= 2:
            jitter = sum(abs(b - a) for a, b in zip(r, r[1:])) / (n - 1)

        total = n + self.timeouts
        lines = [
            f"===== 延迟统计 [{self.label}] =====",
            f"样本数        : {n}",
            f"平均频率      : {n / self.duration:.2f} 轮/秒",
            f"min           : {min(r):.2f} ms",
            f"avg           : {statistics.mean(r):.2f} ms",
            f"max           : {max(r):.2f} ms",
            f"p50           : {pct(50):.2f} ms",
            f"p95           : {pct(95):.2f} ms",
            f"p99           : {pct(99):.2f} ms",
            f"标准差        : {statistics.pstdev(r):.2f} ms",
            f"抖动(相邻差)  : {jitter:.2f} ms",
            f"超时次数      : {self.timeouts}",
            f"重连次数      : {self.reconnects}",
            f"超时率        : {(self.timeouts / total * 100) if total else 0.0:.2f}%",
        ]
        return "\n".join(lines)


async def _recv_loop(ws, stats: ProbeStats, pending: dict) -> None:
    """后台收包并按 observation_seq 匹配计算 RTT（interval 模式）。"""
    try:
        while True:
            raw = await ws.recv()
            recv_at = time.perf_counter()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            seq = msg.get("observation_seq")
            if seq is None:
                continue
            sent_at = pending.pop(seq, None)
            if sent_at is None:
                continue
            stats.add_rtt((recv_at - sent_at) * 1000.0)
    except websockets.ConnectionClosed:
        pass


async def _pingpong(ws, stats: ProbeStats, args, end_at: float, session_id: str) -> None:
    seq = 0
    while time.perf_counter() < end_at:
        seq += 1
        sent = time.perf_counter()
        await ws.send(json.dumps(_make_observation(seq, args.device, session_id)))
        try:
            await asyncio.wait_for(ws.recv(), timeout=args.timeout)
        except asyncio.TimeoutError:
            stats.timeouts += 1
            log.warning("[%s] seq=%s 超时", args.label, seq)
            continue
        stats.add_rtt((time.perf_counter() - sent) * 1000.0)


async def _interval(ws, stats: ProbeStats, args, end_at: float, session_id: str) -> None:
    pending: dict[int, float] = {}
    recv_task = asyncio.create_task(_recv_loop(ws, stats, pending))
    seq = 0
    try:
        while time.perf_counter() < end_at:
            seq += 1
            pending[seq] = time.perf_counter()
            await ws.send(json.dumps(_make_observation(seq, args.device, session_id)))
            await asyncio.sleep(args.interval)
    finally:
        recv_task.cancel()
        # 未回收的在途请求计为超时
        stats.timeouts += len(pending)


async def _probe(args) -> None:
    stats = ProbeStats(args.label, args.duration)
    end_at = time.perf_counter() + args.duration
    session_id = f"probe-{int(time.time())}"

    while time.perf_counter() < end_at:
        try:
            async with websockets.connect(args.uri) as ws:
                log.info("[%s] 已连接 %s", args.label, args.uri)
                if args.mode == "pingpong":
                    await _pingpong(ws, stats, args, end_at, session_id)
                else:
                    await _interval(ws, stats, args, end_at, session_id)
                break  # 正常跑完时长，跳出重连循环
        except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
            stats.reconnects += 1
            log.warning("[%s] 连接中断：%s，%.1fs 后重连", args.label, e, args.retry)
            await asyncio.sleep(args.retry)

    print(stats.report())


def main() -> None:
    parser = argparse.ArgumentParser(description="VLA 链路延迟/稳定性探测")
    parser.add_argument("--uri", required=True, help="目标 ws:// URI")
    parser.add_argument("--label", default="probe", help="统计标签")
    parser.add_argument("--device", default="probe-01", help="device_id")
    parser.add_argument("--mode", choices=["pingpong", "interval"], default="pingpong")
    parser.add_argument("--duration", type=float, default=60.0, help="总运行秒数")
    parser.add_argument("--interval", type=float, default=1.0, help="interval 模式间隔秒数")
    parser.add_argument("--timeout", type=float, default=15.0, help="单次回包超时秒数")
    parser.add_argument("--retry", type=float, default=2.0, help="重连退避秒数")
    args = parser.parse_args()
    asyncio.run(_probe(args))


if __name__ == "__main__":
    main()

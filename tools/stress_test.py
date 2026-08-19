"""多玩家并发压力测试工具。

用于验证服务器在多玩家同时连接/游戏场景下的稳定性与性能表现。
支持两种模式：
- game 模式（默认）：模拟 N 个玩家同时连接、加入并持续对局，
  通过随机合法行动推进牌局，统计连接成功率、加入耗时、消息吞吐等指标。
- connect 模式：模拟连接风暴（连接 → 加入 → 短暂停留 → 断开），
  重点考察服务器在高并发瞬时连接下的承受能力。

用法示例：
    # 30 个玩家同时进入游戏，持续 20 秒
    python -m tools.stress_test --host 127.0.0.1 --port 8888 --clients 30 --duration 20

    # 100 个连接风暴（connect 模式）
    python -m tools.stress_test --host 127.0.0.1 --port 8888 --clients 100 --mode connect

运行后输出汇总报告：成功率、耗时分布、消息吞吐、服务器侧统计等。
"""
from __future__ import annotations

import argparse
import random
import socket
import threading
import time
from typing import Any, Dict, List, Optional

from network.client import ClientError, GameClient, query_server_status

# 参与统计的类型别名
Stats = Dict[str, Any]


def _free_port() -> int:
    """获取一个空闲端口（供无参运行时本地起服务器自测）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class StressStats:
    """线程安全的压力测试统计收集器。"""

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        # 成功加入的客户端数
        self.joined: int = 0
        # 连接/加入失败数
        self.failed: int = 0
        # 加入耗时（秒）列表，用于计算平均/最值
        self.join_latencies: List[float] = []
        # 客户端累计收到的消息数（体现服务器广播吞吐）
        self.total_messages: int = 0
        # 客户端累计发送的行动数
        self.total_actions: int = 0
        # 遇到的错误消息数（服务器返回的 error 消息）
        self.server_errors: int = 0
        # 异常中断的客户端数
        self.exceptions: int = 0

    def record_join(self, latency: float) -> None:
        """记录一次成功加入及其耗时。"""
        with self._lock:
            self.joined += 1
            self.join_latencies.append(latency)

    def record_fail(self) -> None:
        """记录一次加入失败。"""
        with self._lock:
            self.failed += 1

    def record_messages(self, count: int) -> None:
        """累加收到消息数。"""
        with self._lock:
            self.total_messages += count

    def record_action(self) -> None:
        """累加发送行动数。"""
        with self._lock:
            self.total_actions += 1

    def record_server_error(self) -> None:
        """记录一条服务器错误消息。"""
        with self._lock:
            self.server_errors += 1

    def record_exception(self) -> None:
        """记录一次客户端异常。"""
        with self._lock:
            self.exceptions += 1

    def joined_count(self) -> int:
        """返回当前成功加入的客户端数（线程安全）。"""
        with self._lock:
            return self.joined

    def snapshot(self) -> Stats:
        """返回当前统计快照（线程安全）。"""
        with self._lock:
            lat = list(self.join_latencies)
            return {
                "joined": self.joined,
                "failed": self.failed,
                "join_latencies": lat,
                "total_messages": self.total_messages,
                "total_actions": self.total_actions,
                "server_errors": self.server_errors,
                "exceptions": self.exceptions,
            }


def _pick_random_action(client: GameClient) -> None:
    """根据 turn_options 从可选项中随机选择一个合法行动并发送。

    Args:
        client: 轮到此客户端行动，其 turn_options 已由服务器下发。
    """
    opts = client.turn_options or {}
    options = opts.get("options", [])
    if not options:
        return
    action = random.choice(options)
    if action == "raise":
        # 加注需指定目标金额：在 [min_raise_to, max_raise_to] 区间随机取值
        min_raise = int(opts.get("min_raise_to", 0))
        max_raise = int(opts.get("max_raise_to", min_raise))
        amount = min_raise if min_raise >= max_raise else random.randint(min_raise, max_raise)
        client.send_action("raise", amount)
    else:
        client.send_action(action)


def _run_game_client(
    host: str,
    port: int,
    index: int,
    stop_event: threading.Event,
    all_joined: threading.Event,
    stats: StressStats,
) -> None:
    """game 模式下单个客户端的运行逻辑（独立线程）。

    Args:
        host: 服务器地址。
        port: 服务器端口。
        index: 客户端序号（用于生成唯一昵称）。
        stop_event: 测试结束信号，置位后客户端退出。
        all_joined: 所有客户端加入完毕信号，房主据此开局。
        stats: 统计收集器。
    """
    name = f"bot_{index:03d}"
    client = GameClient()
    try:
        start = time.monotonic()
        ok = client.connect(host, port, name, timeout=8.0, auto_reconnect=False)
        latency = time.monotonic() - start
        if not ok:
            stats.record_fail()
            return
        stats.record_join(latency)
    except ClientError:
        stats.record_fail()
        return

    try:
        # 房主（首个加入者）等待所有人到齐后开局
        host_started = False
        while not stop_event.is_set():
            messages = client.drain_messages()
            if messages:
                stats.record_messages(len(messages))
                for msg in messages:
                    msg_type = msg.get("type", "")
                    if msg_type == "error":
                        stats.record_server_error()
                    if msg_type == "turn" and msg.get("options", {}).get("can_act"):
                        # 轮到自己行动：随机合法行动
                        _pick_random_action(client)
                        stats.record_action()
                    if msg_type == "hand_over":
                        # 本局结束：房主开始下一局，其余玩家等待
                        host_started = False
            # 房主开局：等所有人到齐且当前处于等待/结束后开新局
            if client.is_host and not host_started and all_joined.is_set():
                state = client.state
                if state is not None:
                    state_name = state.get("state_name", "")
                    players = state.get("players", [])
                    if state_name in ("WAITING", "HAND_OVER") and len(players) >= 2:
                        client.send_start()
                        stats.record_action()
                        host_started = True
            # 短暂休眠降低 CPU 占用
            time.sleep(0.02)
    except Exception:  # pylint: disable=broad-except
        stats.record_exception()
    finally:
        client.disconnect()


def _run_connect_client(
    host: str,
    port: int,
    index: int,
    stop_event: threading.Event,
    stats: StressStats,
) -> None:
    """connect 模式下单个客户端的运行逻辑（连接风暴）。

    连接 → 加入 → 停留约 0.5 秒 → 断开退出，用于考察瞬时并发承受能力。
    """
    name = f"burst_{index:04d}"
    client = GameClient()
    try:
        start = time.monotonic()
        ok = client.connect(host, port, name, timeout=5.0, auto_reconnect=False)
        latency = time.monotonic() - start
        if not ok:
            stats.record_fail()
            return
        stats.record_join(latency)
    except ClientError:
        stats.record_fail()
        return
    try:
        # 短暂停留后退出（模拟用户进入又离开）
        time.sleep(0.5)
    finally:
        client.disconnect()


def run_stress(
    host: str,
    port: int,
    clients: int,
    duration: float,
    mode: str,
) -> Stats:
    """执行压力测试并返回汇总统计。

    Args:
        host: 服务器地址。
        port: 服务器端口。
        clients: 模拟客户端数量。
        duration: 测试持续时长（秒）。
        mode: 'game' 或 'connect'。

    Returns:
        统计快照字典。
    """
    stats = StressStats()
    stop_event = threading.Event()
    all_joined = threading.Event()

    # 客户端线程包装：按模式分发到对应客户端逻辑
    def run_client(index: int) -> None:
        if mode == "connect":
            _run_connect_client(host, port, index, stop_event, stats)
        else:
            _run_game_client(host, port, index, stop_event, all_joined, stats)

    # 启动所有客户端线程（分批启动，避免瞬间创建大量线程导致系统抖动）
    threads: List[threading.Thread] = []
    for i in range(clients):
        t = threading.Thread(target=run_client, args=(i,), daemon=True)
        t.start()
        threads.append(t)

    # 等待所有客户端完成初次加入：轮询统计中 joined 数量达到 clients 即到齐
    deadline = time.time() + 30
    while time.time() < deadline:
        if stats.joined_count() >= clients:
            break
        time.sleep(0.1)
    # 无论是否全部到齐，置位开局信号让房主开始（有多少人就开多少人）
    all_joined.set()

    # 运行指定时长后结束
    time.sleep(duration)
    stop_event.set()
    # 等待客户端线程退出（最多再等 10 秒）
    for t in threads:
        t.join(timeout=10.0)

    result = stats.snapshot()
    return result


def _format_report(result: Stats, host: str, port: int, clients: int, duration: float, mode: str) -> str:
    """将统计结果格式化为可读报告文本。"""
    lat = result["join_latencies"]
    total = result["joined"] + result["failed"]
    lines: List[str] = []
    lines.append("=" * 56)
    lines.append("压力测试报告")
    lines.append("=" * 56)
    lines.append(f"目标服务器 : {host}:{port}")
    lines.append(f"测试模式   : {mode}")
    lines.append(f"模拟客户端 : {clients} 个，持续 {duration} 秒")
    lines.append("-" * 56)
    lines.append(f"成功加入   : {result['joined']}/{total}")
    lines.append(f"加入失败   : {result['failed']}")
    lines.append(f"客户端异常 : {result['exceptions']}")
    if lat:
        avg = sum(lat) / len(lat)
        lines.append(f"加入耗时   : 平均 {avg * 1000:.0f} ms，"
                     f"最小 {min(lat) * 1000:.0f} ms，最大 {max(lat) * 1000:.0f} ms")
    lines.append(f"收到消息   : {result['total_messages']} 条（广播吞吐）")
    lines.append(f"发送行动   : {result['total_actions']} 次")
    lines.append(f"服务器错误 : {result['server_errors']} 条")
    # 追加服务器侧统计（连接峰值、拒绝数等）
    try:
        st = query_server_status(host, port, timeout=5.0)
        lines.append("-" * 56)
        lines.append(f"服务器统计 : 当前连接 {st.get('connections', 0)}，"
                     f"峰值 {st.get('peak_connections', 0)}，"
                     f"累计 {st.get('total_connections', 0)}，"
                     f"拒绝 {st.get('rejected_connections', 0)}")
    except ClientError as exc:
        lines.append(f"服务器统计 : 查询失败（{exc}）")
    lines.append("=" * 56)
    return "\n".join(lines)


def main() -> int:
    """压力测试入口。

    Returns:
        进程退出码，0 表示执行完成。
    """
    parser = argparse.ArgumentParser(description="多玩家并发压力测试工具")
    parser.add_argument("--host", default="127.0.0.1", help="服务器地址")
    parser.add_argument("--port", "-p", type=int, default=8888, help="服务器端口")
    parser.add_argument("--clients", "-n", type=int, default=30, help="模拟客户端数量")
    parser.add_argument("--duration", "-d", type=float, default=20.0, help="测试时长（秒）")
    parser.add_argument(
        "--mode", choices=("game", "connect"), default="game",
        help="game=持续对局（默认），connect=连接风暴",
    )
    args = parser.parse_args()

    print(f"开始压力测试：{args.clients} 个客户端 → {args.host}:{args.port} "
          f"（模式={args.mode}，时长={args.duration}s）")
    result = run_stress(args.host, args.port, args.clients, args.duration, args.mode)
    print(_format_report(result, args.host, args.port, args.clients, args.duration, args.mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

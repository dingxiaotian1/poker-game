"""服务器部署适配功能测试。

覆盖本次服务器部署调整新增的能力：
- status 状态监控查询（status_req / status_resp 消息）
- 断线自动重连（客户端 reconnect 标记 + 服务器同名接管）
- 连接数上限保护（max_connections 拒绝超限连接）
- 心跳机制（ping/pong 应答、连接活性维持）

复用 tests/test_network.py 的服务器启动模式：后台线程 + 真实 TCP 端口。
"""
from __future__ import annotations

import socket
import threading
import time
import unittest

from network.client import ClientError, GameClient, query_server_status
from network.server import GameServer


def _free_port() -> int:
    """获取一个空闲端口供测试使用。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _drain_until(client: GameClient, msg_type: str, timeout: float = 5.0) -> dict:
    """持续轮询客户端消息队列，直到收到指定类型消息。

    Args:
        client: 游戏客户端。
        msg_type: 目标消息类型。
        timeout: 最大等待秒数。

    Returns:
        匹配的消息字典。

    Raises:
        TimeoutError: 超时未收到目标消息。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = client.poll_message(timeout=0.2)
        if msg is not None and msg.get("type") == msg_type:
            return msg
    raise TimeoutError(f"等待消息 {msg_type} 超时")


class TestServerDeploy(unittest.TestCase):
    """服务器部署适配功能集成测试。"""

    def setUp(self) -> None:
        """每个测试前启动一台全新服务器。"""
        self.port = _free_port()
        self.server = GameServer(host="127.0.0.1", port=self.port, starting_chips=1000)
        self.server_thread = threading.Thread(target=self.server.start, daemon=True)
        self.server_thread.start()
        self._wait_server_ready()

    def tearDown(self) -> None:
        """测试结束关闭服务器。"""
        try:
            self.server.stop()
        except Exception:  # pylint: disable=broad-except
            pass
        time.sleep(0.1)

    def _wait_server_ready(self, timeout: float = 3.0) -> None:
        """轮询连接服务器端口，直到可连接或超时。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=0.2)
                s.close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("服务器未在超时内就绪")

    def test_status_query_returns_monitor_info(self) -> None:
        """status 查询应返回在线人数、连接数、运行时长等监控信息。"""
        c1 = GameClient()
        try:
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Alice"))
            # 通过独立查询函数读取服务器状态（不加入房间）
            st = query_server_status("127.0.0.1", self.port, timeout=5.0)
            # 在线玩家应为 1（Alice）
            self.assertEqual(st["online_players"], 1)
            self.assertGreaterEqual(st["connections"], 1)
            self.assertGreaterEqual(st["total_connections"], 1)
            self.assertGreaterEqual(st["peak_connections"], 1)
            self.assertGreaterEqual(st["uptime_seconds"], 0)
            self.assertGreaterEqual(st["max_players"], 2)
            # 玩家列表应包含 Alice
            names = [p["name"] for p in st["players"]]
            self.assertIn("Alice", names)
        finally:
            c1.disconnect()

    def test_status_query_before_join(self) -> None:
        """未加入房间的连接也应能查询到服务器状态（监控用途）。"""
        # 直接查询，不创建游戏客户端
        st = query_server_status("127.0.0.1", self.port, timeout=5.0)
        self.assertEqual(st["online_players"], 0)
        self.assertIn("server", st)

    def test_reconnect_takeover_old_seat(self) -> None:
        """断线重连：同名玩家携带 reconnect 标记应能接管旧席位。

        场景：玩家 Alice 正常加入后，服务器侧强制断开其连接（模拟半路断网），
        Alice 的客户端自动重连并以 reconnect 标记重新加入，不应被"昵称已被使用"
        拒绝，且最终在线人数恢复为 1。
        """
        c1 = GameClient()
        try:
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Alice", auto_reconnect=True))
            self.assertEqual(c1.player_id, 1)

            # 模拟断网：直接关闭客户端的底层 socket，触发读取线程发现断开
            c1._sock.close()
            # 客户端应进入自动重连并最终收到 _reconnected
            _drain_until(c1, "_reconnected", timeout=10.0)
            # 重连后玩家应重新出现在服务器上
            st = query_server_status("127.0.0.1", self.port, timeout=5.0)
            self.assertEqual(st["online_players"], 1)
            names = [p["name"] for p in st["players"]]
            self.assertIn("Alice", names)
            # 客户端已重新加入（_joined 状态刷新）
            self.assertTrue(c1.state is not None)
        finally:
            c1.disconnect()

    def test_reconnect_flag_allows_takeover_of_dead_connection(self) -> None:
        """服务器端：同名玩家在旧连接死亡时，reconnect 标记应允许接管。"""
        c1 = GameClient()
        c2 = GameClient()
        try:
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Dup", auto_reconnect=False))
            # 手动关闭 c1 底层 socket，让服务器认为该连接已死亡（未走 leave）
            c1._sock.close()
            # 等待服务器清理旧连接（读取线程退出）
            time.sleep(0.5)
            # c2 以同名 + reconnect 标记加入，应成功接管
            ok = c2.connect("127.0.0.1", self.port, "Dup", auto_reconnect=False)
            self.assertTrue(ok, "死连接的同名玩家应能通过 reconnect 接管")
            # 服务器上只有一个玩家
            st = query_server_status("127.0.0.1", self.port, timeout=5.0)
            self.assertEqual(st["online_players"], 1)
        finally:
            c1.disconnect()
            c2.disconnect()

    def test_duplicate_name_without_reconnect_still_rejected(self) -> None:
        """同名玩家不带 reconnect 标记时仍应被拒绝（防顶替在线玩家）。"""
        c1 = GameClient()
        c2 = GameClient()
        try:
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Live", auto_reconnect=False))
            # c2 不带 reconnect 标记，应被拒绝
            ok = c2.connect("127.0.0.1", self.port, "Live", auto_reconnect=False)
            self.assertFalse(ok)
            self.assertIn("昵称", c2.last_error)
        finally:
            c1.disconnect()
            c2.disconnect()

    def test_connection_limit_rejects_excess(self) -> None:
        """连接数达到上限后，新连接应被拒绝并收到错误提示。"""
        port = _free_port()
        # 上限设为 2：允许 2 个并发连接
        server = GameServer(host="127.0.0.1", port=port, starting_chips=1000, max_connections=2)
        server_thread = threading.Thread(target=server.start, daemon=True)
        server_thread.start()
        try:
            # 等待服务器就绪
            deadline = time.time() + 3.0
            while time.time() < deadline:
                try:
                    s = socket.create_connection(("127.0.0.1", port), timeout=0.2)
                    s.close()
                    break
                except OSError:
                    time.sleep(0.05)
            # 建立 2 个连接占满上限
            socks = []
            for _ in range(2):
                socks.append(socket.create_connection(("127.0.0.1", port), timeout=2.0))
            # 第 3 个连接应被拒绝（收到错误消息后关闭）
            third = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            third.settimeout(2.0)
            try:
                data = third.recv(4096)
                self.assertTrue(data, "被拒绝的连接应收到错误消息")
                # 服务器侧应记录拒绝次数
                self.assertEqual(server.get_status()["rejected_connections"], 1)
            finally:
                third.close()
            for s in socks:
                s.close()
        finally:
            server.stop()

    def test_heartbeat_ping_pong_keeps_connection(self) -> None:
        """心跳机制：客户端 ping 应得到服务器 pong 应答。

        不等待真实心跳周期（15 秒过长），直接手动发送 ping 验证应答链路。
        """
        client = GameClient()
        try:
            self.assertTrue(client.connect("127.0.0.1", self.port, "Pinger", auto_reconnect=False))
            # 手动发送一次 ping（对应心跳线程的发送逻辑）
            client.send_ready()  # 确认发送链路可用
            client._send_raw({"type": "ping"})
            # 服务器应回复 pong（_handle_incoming 会将其入队）
            msg = client.poll_message(timeout=3.0)
            deadline = time.time() + 3.0
            found = False
            while time.time() < deadline:
                msg = client.poll_message(timeout=0.2)
                if msg is not None and msg.get("type") == "pong":
                    found = True
                    break
            self.assertTrue(found, "服务器应回复 pong 心跳应答")
        finally:
            client.disconnect()

    def test_status_contains_game_state_fields(self) -> None:
        """状态查询应包含游戏局数与状态字段（监控所需）。"""
        c1 = GameClient()
        try:
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Host"))
            st = query_server_status("127.0.0.1", self.port, timeout=5.0)
            self.assertIn("hand_number", st)
            self.assertIn("game_state", st)
            self.assertIn("host_player_id", st)
        finally:
            c1.disconnect()


if __name__ == "__main__":
    unittest.main()

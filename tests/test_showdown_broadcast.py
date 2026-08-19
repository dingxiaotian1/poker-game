"""摊牌底牌广播功能测试。

覆盖用户报告的缺陷：游戏正常结束（非弃牌）时，玩家无法查看其他玩家底牌。

根因：
- 服务器此前从不广播结构化摊牌数据（protocol.Msg.showdown 定义了却从未调用），
  底牌信息仅以事件流日志（"XX 摊牌 A♠ K♠ = 两对"）文本形式传递；
- CLI 端牌局事件区只显示最近 4 条，多位玩家摊牌时部分底牌行被截断，无法查看。

修复：
- 服务器在本局摊牌结束（showdown_revealed）时广播 showdown 消息，携带每位
  未弃牌玩家的底牌与牌型；
- CLI 端独立渲染"摊牌"区块，不受事件区条数限制，完整展示所有玩家底牌。

本测试覆盖：
- 摊牌结束：所有客户端都能收到全部未弃牌玩家的底牌与牌型；
- 弃牌结束（无人跟注）：不广播摊牌（符合规则）；
- 多人对局：弃牌玩家的底牌不被公开；
- CLI 渲染：showdown 消息正确填充摊牌缓冲，新一局开始（deal_hole）时清空。
"""
from __future__ import annotations

import socket
import threading
import time
import unittest
from types import SimpleNamespace

from network.client import GameClient
from network.server import GameServer
from ui.cli import PokerCLI


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


def _drain_state(client: GameClient, timeout: float = 3.0) -> dict:
    """轮询客户端队列，返回下一条"有行动者"的 state 快照。

    【重点注释】跳过 current_player_id 为 None 的快照（如 WAITING 阶段），
    避免把"无人行动"的旧状态当作当前行动者而误发指令。

    Args:
        client: 游戏客户端。
        timeout: 最大等待秒数。

    Returns:
        state 快照字典（current_player_id 非 None）。

    Raises:
        TimeoutError: 超时未收到符合条件的 state。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = client.poll_message(timeout=0.2)
        if msg is not None and msg.get("type") == "state":
            st = msg.get("state", {})
            if st.get("current_player_id") is not None:
                return st
    raise TimeoutError("等待有行动者的 state 超时")


class TestShowdownBroadcastNetwork(unittest.TestCase):
    """网络集成测试：服务器摊牌广播与客户端接收。"""

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

    def _join_two(self) -> tuple:
        """Alice(房主) 与 Bob 依次加入，返回两个客户端。"""
        c1 = GameClient()
        c2 = GameClient()
        self.assertTrue(c1.connect("127.0.0.1", self.port, "Alice"))
        self.assertTrue(c2.connect("127.0.0.1", self.port, "Bob"))
        return c1, c2

    def test_showdown_broadcasts_all_hole_cards(self) -> None:
        """双方全下到摊牌：两个客户端都应收到完整底牌与牌型。"""
        c1, c2 = self._join_two()
        try:
            c1.send_start()
            _drain_until(c1, "deal_hole")
            # 按 state 中的 current_player_id 依次全下（不预设行动顺序）。
            # 【重点注释】固定行动次数：双方各 all_in 一次后对局必然结束，
            # 结束时 state 的 current_player_id 为 None，无法用它判断终止，
            # 故不依赖 HAND_OVER 快照 break，用固定 2 次驱动。
            clients = {1: c1, 2: c2}
            for _ in range(2):
                st = _drain_state(c2)
                clients[st["current_player_id"]].send_action("all_in")

            # 两客户端都收到 showdown 消息
            for client in (c1, c2):
                msg = _drain_until(client, "showdown")
                results = msg.get("results", [])
                # 两位未弃牌玩家的底牌都应完整广播
                self.assertEqual(len(results), 2)
                names = {r.get("name") for r in results}
                self.assertEqual(names, {"Alice", "Bob"})
                for r in results:
                    # 每人 2 张底牌，牌型名非空
                    self.assertEqual(len(r.get("hole_cards", [])), 2)
                    self.assertTrue(r.get("hand_name"))
        finally:
            c1.disconnect()
            c2.disconnect()

    def test_uncontested_hand_no_showdown(self) -> None:
        """有人弃牌无人跟注：不广播摊牌（无需公开底牌）。"""
        c1, c2 = self._join_two()
        try:
            c1.send_start()
            _drain_until(c1, "deal_hole")
            # 首个行动者弃牌 → 本局直接结束
            st = _drain_state(c2)
            clients = {1: c1, 2: c2}
            clients[st["current_player_id"]].send_action("fold")

            # 收到 hand_over 结果摘要
            _drain_until(c1, "hand_over")
            # 但不应收到 showdown 消息（0.5 秒内无该消息）
            with self.assertRaises(TimeoutError):
                _drain_until(c1, "showdown", timeout=0.5)
        finally:
            c1.disconnect()
            c2.disconnect()

    def test_showdown_excludes_folded_players(self) -> None:
        """三人局：一人弃牌、两人摊牌，弃牌者底牌不被公开。"""
        c1 = GameClient()
        c2 = GameClient()
        c3 = GameClient()
        try:
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Alice"))
            self.assertTrue(c2.connect("127.0.0.1", self.port, "Bob"))
            self.assertTrue(c3.connect("127.0.0.1", self.port, "Cara"))
            c1.send_start()
            _drain_until(c1, "deal_hole")

            # 依次行动：轮到 Bob 时弃牌，其余人全下到摊牌。
            # 【重点注释】固定行动次数：1 次弃牌 + 2 次全下后对局必然结束，
            # 结束状态无 current_player_id，故用固定 3 次驱动。
            clients = {1: c1, 2: c2, 3: c3}
            fold_done = False
            for _ in range(3):
                st = _drain_state(c3)
                pid = st["current_player_id"]
                if pid == 2 and not fold_done:
                    c2.send_action("fold")
                    fold_done = True
                else:
                    clients[pid].send_action("all_in")

            msg = _drain_until(c1, "showdown")
            results = msg.get("results", [])
            names = {r.get("name") for r in results}
            # 弃牌的 Bob 底牌不公开，只剩 Alice/Cara
            self.assertEqual(names, {"Alice", "Cara"})
            self.assertEqual(len(results), 2)
        finally:
            c1.disconnect()
            c2.disconnect()
            c3.disconnect()


class TestCLIShowdownRendering(unittest.TestCase):
    """CLI 渲染测试：摊牌消息填充缓冲与新局清空逻辑。"""

    def _make_cli(self) -> PokerCLI:
        """构造一个使用模拟客户端的 CLI 实例。"""
        client = SimpleNamespace(
            state=None,
            player_id=1,
            hole_cards=[],
            turn_options=None,
            is_host=True,
        )
        return PokerCLI(client)  # type: ignore[arg-type]

    def test_showdown_message_fills_lines(self) -> None:
        """收到 showdown 消息：摊牌缓冲填充每位玩家底牌与牌型。"""
        cli = self._make_cli()
        cli._handle_message(
            {
                "type": "showdown",
                "results": [
                    {
                        "player_id": 1,
                        "name": "Alice",
                        "hole_cards": [{"rank": 14, "suit": "S"}, {"rank": 14, "suit": "H"}],
                        "hand_name": "一对",
                    },
                    {
                        "player_id": 2,
                        "name": "Bob",
                        "hole_cards": [{"rank": 2, "suit": "C"}, {"rank": 3, "suit": "D"}],
                        "hand_name": "高牌",
                    },
                ],
            }
        )
        # 两位玩家的底牌信息都应进入摊牌缓冲
        self.assertEqual(len(cli._showdown_lines), 2)
        self.assertIn("Alice", cli._showdown_lines[0])
        self.assertIn("一对", cli._showdown_lines[0])
        self.assertIn("Bob", cli._showdown_lines[1])

    def test_deal_hole_clears_previous_showdown(self) -> None:
        """新一局开始（deal_hole）：清空上一局摊牌展示，避免残留。"""
        cli = self._make_cli()
        # 先写入一局摊牌数据
        cli._handle_message(
            {
                "type": "showdown",
                "results": [
                    {
                        "player_id": 1,
                        "name": "Alice",
                        "hole_cards": [{"rank": 14, "suit": "S"}, {"rank": 14, "suit": "H"}],
                        "hand_name": "一对",
                    }
                ],
            }
        )
        self.assertEqual(len(cli._showdown_lines), 1)
        # 新一局发底牌：摊牌缓冲应被清空
        cli._handle_message(
            {"type": "deal_hole", "cards": [{"rank": 5, "suit": "C"}, {"rank": 9, "suit": "D"}]}
        )
        self.assertEqual(cli._showdown_lines, [])


if __name__ == "__main__":
    unittest.main()

"""网络层集成测试：服务器 + 客户端端到端。

通过在后台线程启动真实服务器、用 GameClient 连接，验证：
- 加入房间与状态广播
- 开局与底牌下发
- 行动指令端到端传递
- 玩家离开通知

为避免测试阻塞，所有客户端轮询均带超时。
"""
from __future__ import annotations

import socket
import threading
import time
import unittest

from network.client import ClientError, GameClient
from network.server import GameServer


def _free_port() -> int:
    """获取一个空闲端口供测试使用。

    通过临时绑定再释放的方式取得可用端口（存在微小竞态，但测试场景可接受）。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _drain_until(client: GameClient, msg_type: str, timeout: float = 3.0) -> dict:
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


class TestServerClientIntegration(unittest.TestCase):
    """服务器与客户端端到端集成测试。"""

    def setUp(self) -> None:
        """每个测试前启动一台全新服务器。"""
        self.port = _free_port()
        self.server = GameServer(host="127.0.0.1", port=self.port, starting_chips=1000)
        self.server_thread = threading.Thread(target=self.server.start, daemon=True)
        self.server_thread.start()
        # 等待服务器就绪：尝试连接探测
        self._wait_server_ready()

    def tearDown(self) -> None:
        """测试结束关闭服务器与所有客户端。"""
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

    def test_join_and_state_broadcast(self) -> None:
        """玩家加入后应收到 join_ok 与 state。"""
        client = GameClient()
        try:
            ok = client.connect("127.0.0.1", self.port, "Alice")
            self.assertTrue(ok)
            self.assertEqual(client.player_id, 1)
            # 客户端应已缓存状态
            self.assertIsNotNone(client.state)
            # 状态中应能看到自己
            names = [p["name"] for p in client.state["players"]]
            self.assertIn("Alice", names)
        finally:
            client.disconnect()

    def test_two_players_see_each_other(self) -> None:
        """两名玩家加入后彼此应在状态中可见。"""
        c1 = GameClient()
        c2 = GameClient()
        try:
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Alice"))
            self.assertTrue(c2.connect("127.0.0.1", self.port, "Bob"))
            # 持续轮询直到 c1 的状态中包含 Bob（c2 加入会触发状态广播）
            deadline = time.time() + 3.0
            found = False
            while time.time() < deadline:
                c1.poll_message(timeout=0.2)
                names = [p["name"] for p in c1.state.get("players", [])]
                if "Bob" in names:
                    found = True
                    break
            self.assertTrue(found, "c1 未在超时内看到 Bob 加入")
            names = [p["name"] for p in c1.state["players"]]
            self.assertIn("Alice", names)
            self.assertIn("Bob", names)
        finally:
            c1.disconnect()
            c2.disconnect()

    def test_duplicate_name_rejected(self) -> None:
        """重复昵称应被拒绝。"""
        c1 = GameClient()
        c2 = GameClient()
        try:
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Dup"))
            # 第二个同名应失败
            ok = c2.connect("127.0.0.1", self.port, "Dup")
            self.assertFalse(ok)
            self.assertIn("昵称", c2.last_error)
        finally:
            c1.disconnect()
            c2.disconnect()

    def test_start_hand_deals_hole_cards(self) -> None:
        """房主开局后双方应收到底牌。"""
        c1 = GameClient()
        c2 = GameClient()
        try:
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Host"))
            self.assertTrue(c2.connect("127.0.0.1", self.port, "Guest"))
            # 等待双方状态同步
            _drain_until(c1, "state", timeout=3.0)

            # 房主开局
            c1.send_start()
            # 双方都应收到底牌
            _drain_until(c1, "deal_hole", timeout=3.0)
            _drain_until(c2, "deal_hole", timeout=3.0)
            self.assertEqual(len(c1.hole_cards), 2)
            self.assertEqual(len(c2.hole_cards), 2)
        finally:
            c1.disconnect()
            c2.disconnect()

    def test_action_propagates(self) -> None:
        """玩家行动后状态应更新并广播。"""
        c1 = GameClient()
        c2 = GameClient()
        try:
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Host"))
            self.assertTrue(c2.connect("127.0.0.1", self.port, "Guest"))
            _drain_until(c1, "state", timeout=3.0)

            c1.send_start()
            _drain_until(c1, "deal_hole", timeout=3.0)
            _drain_until(c2, "deal_hole", timeout=3.0)

            # 房主（小盲位先行动）应收到 turn
            _drain_until(c1, "turn", timeout=3.0)
            self.assertTrue(c1.is_my_turn)

            # 房主弃牌
            c1.send_action("fold")
            # 本局应结束，c2 应收到状态更新
            _drain_until(c2, "hand_over", timeout=3.0)
        finally:
            c1.disconnect()
            c2.disconnect()

    def test_player_leave_notified(self) -> None:
        """玩家离开后另一玩家应收到 player_left 通知。"""
        c1 = GameClient()
        c2 = GameClient()
        try:
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Alice"))
            self.assertTrue(c2.connect("127.0.0.1", self.port, "Bob"))
            _drain_until(c1, "state", timeout=3.0)

            c2.disconnect()
            # c1 应收到 player_left
            _drain_until(c1, "player_left", timeout=3.0)
        finally:
            c1.disconnect()

    def test_connect_to_dead_server_raises(self) -> None:
        """连接不存在的服务器应抛出 ClientError。"""
        client = GameClient()
        port = _free_port()  # 此端口无服务器
        with self.assertRaises(ClientError):
            client.connect("127.0.0.1", port, "X", timeout=1.0)

    def test_full_hand_to_showdown(self) -> None:
        """完整跑完一局（跟注+让牌到摊牌），验证事件流与结果摘要。

        双人局流程预期（房主=小盲=庄家）：
        预翻牌：房主先行动（跟注 10）→ 对手大盲让牌；
        翻牌/转牌/河牌：每轮对手先行动（让牌）→ 房主让牌；
        最后摊牌结算，双方都收到带结果摘要的 hand_over。
        """
        c1 = GameClient()
        c2 = GameClient()
        try:
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Host"))
            self.assertTrue(c2.connect("127.0.0.1", self.port, "Guest"))
            _drain_until(c1, "state", timeout=3.0)
            _drain_until(c2, "state", timeout=3.0)

            # 房主开局，双方收到底牌
            c1.send_start()
            _drain_until(c1, "deal_hole", timeout=3.0)
            _drain_until(c2, "deal_hole", timeout=3.0)

            # 预翻牌：房主（小盲）先行动，跟注补齐到 20
            turn = _wait_turn(c1, c2)
            self.assertIs(turn, c1, "双人局预翻牌应由庄家（房主）先行动")
            turn.send_action("call")
            # 大盲享有优先权：可以让牌结束本轮
            turn = _wait_turn(c1, c2)
            self.assertIs(turn, c2, "预翻牌第二步应为大盲行动")
            turn.send_action("check")

            # 翻牌/转牌/河牌：每轮先对手（大盲位）后房主，全部让牌
            for _ in range(3):
                turn = _wait_turn(c1, c2)
                self.assertIs(turn, c2, "翻牌后应从小盲位下家（大盲）先行动")
                turn.send_action("check")
                turn = _wait_turn(c1, c2)
                self.assertIs(turn, c1, "该轮第二步应为房主行动")
                turn.send_action("check")

            # 摊牌结束：双方都应收到带结果摘要的 hand_over
            h1 = _drain_until(c1, "hand_over", timeout=3.0)
            h2 = _drain_until(c2, "hand_over", timeout=3.0)
            self.assertTrue(h1.get("summary"), "hand_over 应携带结果摘要")
            self.assertTrue(h2.get("summary"))
            self.assertIn("赢得", h1["summary"])
            self.assertIn("赢得", h2["summary"])

            # 筹码守恒：盲注 10+20=40 全部留在底池，桌面总筹码仍为 2000
            total = sum(p["chips"] for p in c1.state.get("players", []))
            self.assertEqual(total, 2000, "整局结束后桌面筹码应守恒")
        finally:
            c1.disconnect()
            c2.disconnect()

    def test_host_handover_updates_host_id(self) -> None:
        """房主离开后，状态广播中的 host_player_id 应交接给下一位玩家。"""
        c1 = GameClient()
        c2 = GameClient()
        try:
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Host"))
            self.assertTrue(c2.connect("127.0.0.1", self.port, "Guest"))
            _drain_until(c1, "state", timeout=3.0)
            _drain_until(c2, "state", timeout=3.0)
            # 初始房主是 c1，双方状态都应记录
            self.assertEqual(c1.state.get("host_player_id"), c1.player_id)
            self.assertEqual(c2.state.get("host_player_id"), c1.player_id)

            # 房主离开
            c1.disconnect()
            # c2 应看到 host_player_id 更新为自己
            deadline = time.time() + 3.0
            found = False
            while time.time() < deadline:
                c2.poll_message(timeout=0.2)
                if c2.state.get("host_player_id") == c2.player_id:
                    found = True
                    break
            self.assertTrue(found, "房主交接后 host_player_id 未更新给下一位玩家")
        finally:
            c1.disconnect()
            c2.disconnect()


def _wait_turn(c1: GameClient, c2: GameClient, timeout: float = 3.0) -> GameClient:
    """等待任一客户端收到"轮到你行动"，返回该客户端。

    丢弃途中的 state/log 等其他消息；收到有效 turn 消息即返回。

    Args:
        c1: 客户端一。
        c2: 客户端二。
        timeout: 最大等待秒数。

    Returns:
        轮到行动的客户端。

    Raises:
        TimeoutError: 超时未收到任何 turn 消息。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for c in (c1, c2):
            msg = c.poll_message(timeout=0.2)
            if msg is not None and msg.get("type") == "turn":
                if msg.get("options", {}).get("can_act"):
                    return c
    raise TimeoutError("等待 turn 消息超时")


if __name__ == "__main__":
    unittest.main()

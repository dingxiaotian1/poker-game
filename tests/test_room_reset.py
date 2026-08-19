"""房间重置功能测试。

覆盖核心重置逻辑（game.reset_room）与网络集成（服务器权限校验 + 多客户端状态同步）：
- 对局数清零：重置后 hand_number 恢复为初始设定值 0；
- 筹码恢复：每位玩家筹码恢复为房间创建时的初始配置值；
- 保留房间配置：玩家列表、昵称、玩家 ID、盲注规则不受影响；
- 不同游戏状态：等待中 / 对局进行中 / 本局结束后 / 玩家离线后；
- 权限验证：仅房间创建者（房主）可执行重置，非房主请求被拒绝。
"""
from __future__ import annotations

import socket
import threading
import time
import unittest

from core.game import Action, GameState, TexasHoldemGame
from core.player import Player
from network.client import GameClient
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


def _drain_until_state(client: GameClient, hand_number: int, timeout: float = 5.0) -> dict:
    """持续轮询消息，直到收到指定对局数的 state 快照。

    Args:
        client: 游戏客户端。
        hand_number: 期望的 state.hand_number 值。
        timeout: 最大等待秒数。

    Returns:
        匹配的 state 字典（state 消息内的 "state" 字段）。

    Raises:
        TimeoutError: 超时未收到匹配的状态快照。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = client.poll_message(timeout=0.2)
        if msg is not None and msg.get("type") == "state":
            st = msg.get("state", {})
            if st.get("hand_number") == hand_number:
                return st
    raise TimeoutError(f"等待 hand_number={hand_number} 的 state 超时")


class TestGameResetCore(unittest.TestCase):
    """核心层测试：直接操作 TexasHoldemGame 验证重置逻辑。"""

    def _make_game(self, chips: int = 1000) -> TexasHoldemGame:
        """构造一桌 2 人游戏（Alice、Bob），盲注 10/20。"""
        game = TexasHoldemGame(small_blind=10, big_blind=20)
        game.add_player(Player(1, "Alice", chips))
        game.add_player(Player(2, "Bob", chips))
        return game

    def test_reset_after_hand_over_restores_everything(self) -> None:
        """本局结束后重置：对局数清零、筹码恢复、状态回到等待中。"""
        game = self._make_game()
        # 开局：Alice 小盲(10)、Bob 大盲(20)，翻牌前先行动者为 Alice
        game.start_hand()
        self.assertEqual(game.hand_number, 1)
        # Alice 弃牌 → 无人跟注 → 本局结束（Bob 赢池），Alice/Bob 均已投入筹码
        game.player_action(1, Action.FOLD)
        self.assertEqual(game.state, GameState.HAND_OVER)
        self.assertNotEqual(game.seats.get(1).chips, 1000)

        # 执行重置
        game.reset_room(1000)

        # 对局数清零、状态复位、筹码恢复
        self.assertEqual(game.hand_number, 0)
        self.assertEqual(game.state, GameState.WAITING)
        self.assertEqual(game.seats.get(1).chips, 1000)
        self.assertEqual(game.seats.get(2).chips, 1000)

    def test_reset_during_active_hand(self) -> None:
        """对局进行中重置：中途恢复到初始状态。"""
        game = self._make_game()
        game.start_hand()
        # Alice（当前行动者）跟注，牌局仍处于翻牌前阶段（进行中）
        game.player_action(1, Action.CALL)
        self.assertEqual(game.state, GameState.PREFLOP)

        game.reset_room(1000)

        # 状态回等待、局数清零、公共牌与底池清空、筹码恢复
        self.assertEqual(game.state, GameState.WAITING)
        self.assertEqual(game.hand_number, 0)
        self.assertEqual(game.community_cards, [])
        self.assertEqual(game.pots.total, 0)
        self.assertEqual(game.seats.get(1).chips, 1000)
        self.assertEqual(game.seats.get(2).chips, 1000)

    def test_reset_keeps_players_and_rules(self) -> None:
        """重置不影响房间基本配置：玩家列表、昵称、ID、盲注规则保留。"""
        game = self._make_game()
        game.start_hand()
        game.player_action(1, Action.FOLD)
        game.reset_room(1000)

        # 玩家列表与昵称不变
        names = [p.name for p in game.seats.all()]
        ids = [p.player_id for p in game.seats.all()]
        self.assertEqual(names, ["Alice", "Bob"])
        self.assertEqual(ids, [1, 2])
        # 规则配置（盲注）不变
        self.assertEqual(game.small_blind, 10)
        self.assertEqual(game.big_blind, 20)

    def test_reset_after_offline_player_removed(self) -> None:
        """玩家离线（被移除）后重置：在座玩家筹码恢复，不残留掉线数据。"""
        game = self._make_game()
        game.start_hand()
        # Bob 掉线：从座位表移除（触发自动弃牌与流程推进）
        game.remove_player(2)
        # 执行重置：只处理在座玩家
        game.reset_room(1000)

        # 离线玩家不残留，在座玩家筹码恢复，状态回等待
        self.assertEqual(len(game.seats), 1)
        self.assertEqual(game.seats.get(1).chips, 1000)
        self.assertEqual(game.state, GameState.WAITING)
        self.assertEqual(game.hand_number, 0)

    def test_reset_rejects_negative_chips(self) -> None:
        """重置时传入非法初始筹码应抛出 ValueError（参数校验）。"""
        game = self._make_game()
        with self.assertRaises(ValueError):
            game.reset_room(-5)


class TestRoomResetNetwork(unittest.TestCase):
    """网络集成测试：服务器权限校验与多客户端状态同步。"""

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

    def test_non_host_reset_rejected(self) -> None:
        """非房主请求重置应被拒绝（权限验证），服务器状态不受影响。"""
        c1 = GameClient()
        c2 = GameClient()
        try:
            # c1 先加入成为房主，c2 后加入为普通玩家
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Alice"))
            self.assertTrue(c2.connect("127.0.0.1", self.port, "Bob"))
            self.assertTrue(c1.is_host)
            self.assertFalse(c2.is_host)

            # 非房主 Bob 请求重置
            c2.send_reset()
            msg = _drain_until(c2, "reset_fail")
            # 拒绝原因应提示权限不足（仅房间创建者可重置）
            self.assertIn("创建者", str(msg.get("reason", "")))

            # 服务器状态未被改动：对局数仍为 0，两位玩家仍在
            st = self.server.get_status()
            self.assertEqual(st["hand_number"], 0)
            self.assertEqual(st["online_players"], 2)
        finally:
            c1.disconnect()
            c2.disconnect()

    def test_host_reset_syncs_all_clients(self) -> None:
        """房主重置：所有客户端收到 reset_ok，且状态同步（局数清零、筹码恢复）。"""
        c1 = GameClient()
        c2 = GameClient()
        try:
            self.assertTrue(c1.connect("127.0.0.1", self.port, "Alice"))
            self.assertTrue(c2.connect("127.0.0.1", self.port, "Bob"))

            # 先开一局，使对局数变为 1、玩家筹码发生变动
            c1.send_start()
            _drain_until_state(c1, hand_number=1)

            # 房主执行重置
            c1.send_reset()

            # 两名客户端都应收到重置成功通知
            _drain_until(c1, "reset_ok")
            _drain_until(c2, "reset_ok")

            # 状态同步：对局数清零、两人筹码都恢复为初始值
            st1 = _drain_until_state(c1, hand_number=0)
            st2 = _drain_until_state(c2, hand_number=0)
            chips1 = {p["player_id"]: p["chips"] for p in st1["players"]}
            chips2 = {p["player_id"]: p["chips"] for p in st2["players"]}
            self.assertEqual(chips1[1], 1000)
            self.assertEqual(chips1[2], 1000)
            self.assertEqual(chips2[1], 1000)
            self.assertEqual(chips2[2], 1000)

            # 玩家列表保留（重置不踢人）
            self.assertEqual(len(st1["players"]), 2)
            self.assertEqual(len(st2["players"]), 2)
        finally:
            c1.disconnect()
            c2.disconnect()


if __name__ == "__main__":
    unittest.main()

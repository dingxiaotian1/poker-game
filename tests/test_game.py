"""游戏流程控制单元测试。

通过注入固定种子的随机源，驱动完整对局流程并验证：
- 盲注收取
- 行动轮转
- 弃牌即胜
- 全下分池
- 摊牌结算
- 筹码守恒
"""
from __future__ import annotations

import random
import unittest

from core.card import Card
from core.game import (
    DEFAULT_BIG_BLIND,
    DEFAULT_SMALL_BLIND,
    Action,
    GameError,
    GameState,
    TexasHoldemGame,
)
from core.player import Player


def _make_game(seed: int = 12345) -> TexasHoldemGame:
    """创建带固定种子的游戏实例，便于复现。

    Args:
        seed: 随机种子。

    Returns:
        TexasHoldemGame 实例。
    """
    return TexasHoldemGame(
        small_blind=DEFAULT_SMALL_BLIND,
        big_blind=DEFAULT_BIG_BLIND,
        rng=random.Random(seed),
    )


def _add_players(game: TexasHoldemGame, count: int, chips: int = 1000) -> list:
    """向游戏添加若干玩家。

    Args:
        game: 游戏实例。
        count: 玩家数。
        chips: 每人初始筹码。

    Returns:
        玩家列表。
    """
    players = []
    for i in range(1, count + 1):
        p = Player(player_id=i, name=f"P{i}", chips=chips)
        game.add_player(p)
        players.append(p)
    return players


class TestGameSetup(unittest.TestCase):
    """游戏初始化与开局条件测试。"""

    def test_cannot_start_with_less_than_two(self) -> None:
        """人数不足时不能开局。"""
        game = _make_game()
        _add_players(game, 1)
        self.assertFalse(game.can_start_hand())

    def test_cannot_start_during_hand(self) -> None:
        """游戏进行中不能重复开局。"""
        game = _make_game()
        _add_players(game, 2)
        game.start_hand()
        with self.assertRaises(GameError):
            game.start_hand()

    def test_max_players_enforced(self) -> None:
        """超过 10 人应拒绝加入。"""
        game = _make_game()
        _add_players(game, 10)
        with self.assertRaises(GameError):
            game.add_player(Player(99, "Extra", 1000))


class TestBlindsAndDeal(unittest.TestCase):
    """盲注收取与发牌测试。"""

    def test_heads_up_blinds(self) -> None:
        """双人桌：庄家为小盲，另一人为大盲。"""
        game = _make_game(seed=1)
        players = _add_players(game, 2)
        game.start_hand()

        # 庄家为 P1（首位玩家）
        self.assertEqual(game.dealer_pos, 0)
        # P1 是小盲，应已下 10
        self.assertEqual(players[0].current_bet, DEFAULT_SMALL_BLIND)
        self.assertEqual(players[0].total_bet, DEFAULT_SMALL_BLIND)
        # P2 是大盲，应已下 20
        self.assertEqual(players[1].current_bet, DEFAULT_BIG_BLIND)
        self.assertEqual(players[1].total_bet, DEFAULT_BIG_BLIND)
        # 当前最高注为大盲
        self.assertEqual(game.current_bet, DEFAULT_BIG_BLIND)
        # 状态为翻牌前
        self.assertEqual(game.state, GameState.PREFLOP)

    def test_hole_cards_dealt(self) -> None:
        """开局后每人应拿到 2 张底牌。"""
        game = _make_game(seed=2)
        players = _add_players(game, 3)
        game.start_hand()
        for p in players:
            self.assertEqual(len(p.hole_cards), 2)

    def test_pot_collects_blinds(self) -> None:
        """盲注应进入底池。"""
        game = _make_game(seed=3)
        _add_players(game, 2)
        game.start_hand()
        # 底池 = 小盲 + 大盲
        self.assertEqual(game.pots.total, DEFAULT_SMALL_BLIND + DEFAULT_BIG_BLIND)


class TestFoldUncontested(unittest.TestCase):
    """弃牌即胜场景测试。"""

    def test_fold_gives_opponent_pot(self) -> None:
        """小盲弃牌后大盲赢得底池。"""
        game = _make_game(seed=10)
        players = _add_players(game, 2)
        game.start_hand()

        # P1（小盲）先行动，选择弃牌
        self.assertEqual(game.current_pos, 0)
        game.player_action(players[0].player_id, Action.FOLD)

        # 本局应结束
        self.assertTrue(game.is_hand_over)
        # P2 赢得底池（小盲+大盲=30）
        # P2 原本扣除大盲 20，赢得 30 → 净 +10，余额 1010
        self.assertEqual(players[1].chips, 1010)
        # P1 扣除小盲 10 → 余额 990
        self.assertEqual(players[0].chips, 990)

    def test_fold_not_your_turn_raises(self) -> None:
        """非己回合行动应抛出 GameError。"""
        game = _make_game(seed=11)
        players = _add_players(game, 2)
        game.start_hand()
        # 当前是 P1 回合，P2 尝试行动应失败
        with self.assertRaises(GameError):
            game.player_action(players[1].player_id, Action.FOLD)


class TestBettingRound(unittest.TestCase):
    """下注轮流转测试。"""

    def test_call_and_check_advances_to_flop(self) -> None:
        """小盲跟注、大盲让牌后应进入翻牌阶段。"""
        game = _make_game(seed=20)
        players = _add_players(game, 2)
        game.start_hand()

        # P1（小盲）跟注：补 10 到 20
        game.player_action(players[0].player_id, Action.CALL)
        self.assertEqual(players[0].current_bet, DEFAULT_BIG_BLIND)
        # 轮到 P2
        self.assertEqual(game.current_pos, 1)
        # P2（大盲）让牌
        game.player_action(players[1].player_id, Action.CHECK)
        # 进入翻牌
        self.assertEqual(game.state, GameState.FLOP)
        # 公共牌应有 3 张
        self.assertEqual(len(game.community_cards), 3)

    def test_cannot_check_when_facing_bet(self) -> None:
        """面对加注时不能让牌。"""
        game = _make_game(seed=21)
        players = _add_players(game, 2)
        game.start_hand()
        # P1 面对大盲注，不能让牌
        with self.assertRaises(GameError):
            game.player_action(players[0].player_id, Action.CHECK)

    def test_raise_reopens_action(self) -> None:
        """加注后已行动玩家需再次行动。"""
        game = _make_game(seed=22)
        players = _add_players(game, 2)
        game.start_hand()
        # P1 跟注
        game.player_action(players[0].player_id, Action.CALL)
        # P2 加注到 60
        game.player_action(players[1].player_id, Action.RAISE, 60)
        # P1 应需再次行动（acted 被重置）
        self.assertFalse(players[0].acted)
        self.assertEqual(game.current_pos, 0)

    def test_raise_below_min_raises(self) -> None:
        """加注增量小于最小加注应抛错。"""
        game = _make_game(seed=23)
        players = _add_players(game, 2)
        game.start_hand()
        # 当前最高注 20，最小加注 20，加注到 30（增量 10）应失败
        with self.assertRaises(GameError):
            game.player_action(players[0].player_id, Action.RAISE, 30)


class TestAllInAndShowdown(unittest.TestCase):
    """全下与摊牌结算测试。"""

    def test_all_in_conserves_chips(self) -> None:
        """双方全下到摊牌，总筹码应守恒。"""
        game = _make_game(seed=30)
        players = _add_players(game, 2, chips=1000)
        total_before = sum(p.chips for p in players) + game.pots.total
        game.start_hand()

        # P1 全下（小盲位先行动）
        game.player_action(players[0].player_id, Action.ALL_IN)
        # P2 跟注全下
        game.player_action(players[1].player_id, Action.ALL_IN)

        # 双方全下后应自动跑完公共牌并摊牌
        self.assertEqual(game.state, GameState.HAND_OVER)
        # 摊牌完成，公共牌应有 5 张
        self.assertEqual(len(game.community_cards), 5)
        # 筹码守恒：双方筹码之和应等于初始总和 2000
        total_after = sum(p.chips for p in players)
        self.assertEqual(total_after, 2000)

    def test_three_player_showdown_completes(self) -> None:
        """三人全部跟注到摊牌应正常完成。"""
        game = _make_game(seed=31)
        players = _add_players(game, 3, chips=1000)
        game.start_hand()

        # 三人都过牌到摊牌：依次行动直至河牌后摊牌
        # 翻牌前：P1（UTG，pos 0）先行动
        # 注意 3 人桌：dealer=0, SB=1, BB=2, UTG=0
        # 驱动所有人 call/check 至摊牌
        max_steps = 50
        steps = 0
        while not game.is_hand_over and steps < max_steps:
            current_player = game.seats.all()[game.current_pos]
            opts = game.get_player_options(current_player.player_id)
            options = opts.get("options", [])
            if "check" in options:
                game.player_action(current_player.player_id, Action.CHECK)
            elif "call" in options:
                game.player_action(current_player.player_id, Action.CALL)
            else:
                game.player_action(current_player.player_id, Action.FOLD)
            steps += 1

        self.assertTrue(game.is_hand_over)
        # 至少跑到了河牌或提前结束
        # 筹码守恒
        self.assertEqual(sum(p.chips for p in players), 3000)


class TestHandRotation(unittest.TestCase):
    """多局之间状态流转测试。"""

    def test_can_start_next_hand_after_finish(self) -> None:
        """一局结束后应能开始下一局。"""
        game = _make_game(seed=40)
        players = _add_players(game, 2)
        game.start_hand()
        # P1 弃牌结束本局
        game.player_action(players[0].player_id, Action.FOLD)
        self.assertTrue(game.is_hand_over)
        # 应能开始下一局
        self.assertTrue(game.can_start_hand())
        game.start_hand()
        self.assertEqual(game.state, GameState.PREFLOP)
        self.assertEqual(game.hand_number, 2)

    def test_dealer_rotates(self) -> None:
        """庄家按钮应在局间移动。"""
        game = _make_game(seed=41)
        players = _add_players(game, 3)
        game.start_hand()
        first_dealer = game.dealer_pos
        # 结束本局
        # 当前行动者弃牌
        current = game.seats.all()[game.current_pos]
        game.player_action(current.player_id, Action.FOLD)
        # 继续让剩余玩家行动直到结束
        steps = 0
        while not game.is_hand_over and steps < 10:
            cur = game.seats.all()[game.current_pos]
            opts = game.get_player_options(cur.player_id)
            if "check" in opts.get("options", []):
                game.player_action(cur.player_id, Action.CHECK)
            elif "call" in opts.get("options", []):
                game.player_action(cur.player_id, Action.CALL)
            else:
                game.player_action(cur.player_id, Action.FOLD)
            steps += 1

        # 开始下一局，庄家应移动到下一位
        game.start_hand()
        self.assertNotEqual(game.dealer_pos, first_dealer)


class TestStateSnapshot(unittest.TestCase):
    """状态快照与可见性测试。"""

    def test_hole_cards_hidden_from_others(self) -> None:
        """其他玩家的底牌不应在快照中暴露。"""
        game = _make_game(seed=50)
        players = _add_players(game, 2)
        game.start_hand()

        # 以 P1 视角查看：P1 底牌可见，P2 底牌不可见
        snap = game.get_state_snapshot(viewer_id=players[0].player_id)
        snap_players = {p["player_id"]: p for p in snap["players"]}
        self.assertEqual(len(snap_players[1]["hole_cards"]), 2)
        self.assertEqual(len(snap_players[2]["hole_cards"]), 0)

    def test_hole_cards_visible_at_showdown(self) -> None:
        """摊牌阶段未弃牌玩家底牌应对所有人可见。"""
        game = _make_game(seed=51)
        players = _add_players(game, 2)
        game.start_hand()
        # 双方全下到摊牌
        game.player_action(players[0].player_id, Action.ALL_IN)
        game.player_action(players[1].player_id, Action.ALL_IN)
        self.assertEqual(game.state, GameState.HAND_OVER)

        # 以 P1 视角查看，P2 底牌在摊牌后应可见
        snap = game.get_state_snapshot(viewer_id=players[0].player_id)
        snap_players = {p["player_id"]: p for p in snap["players"]}
        self.assertEqual(len(snap_players[2]["hole_cards"]), 2)

    def test_player_options_for_current_player(self) -> None:
        """当前行动玩家应得到可执行选项。"""
        game = _make_game(seed=52)
        players = _add_players(game, 2)
        game.start_hand()
        # P1 是当前行动者，面对大盲应能 fold/call/raise/all_in
        opts = game.get_player_options(players[0].player_id)
        self.assertTrue(opts["can_act"])
        self.assertIn("fold", opts["options"])
        self.assertIn("call", opts["options"])
        self.assertIn("raise", opts["options"])


class TestPairComparisonShowdown(unittest.TestCase):
    """对子牌型摊牌胜负判定回归测试。

    覆盖用户报告的缺陷场景：双方均为对子牌型时，
    底牌为 3 与底牌为 8 不应判为平局，对 8 必须获胜。
    """

    def test_pair_three_vs_pair_eight_showdown(self) -> None:
        """完整对局摊牌：对 3 vs 对 8，对 8 玩家获胜而非平局。"""
        game = _make_game(seed=77)
        players = _add_players(game, 2)
        p1, p2 = players[0], players[1]

        # 构造摊牌前状态：双方各投入 100，底池 200
        p1.bet(100)
        p2.bet(100)
        game.pots.collect(100)
        game.pots.collect(100)
        # 注入指定底牌：P1 对 3，P2 对 8
        p1.assign_hole_cards([Card(3, "S"), Card(3, "H")])
        p2.assign_hole_cards([Card(8, "S"), Card(8, "H")])
        # 公共牌不含 3/8、无对子，双方均保持"一对"牌型
        game.community_cards = [
            Card(5, "C"), Card(6, "D"), Card(9, "H"),
            Card(11, "S"), Card(12, "C"),
        ]
        game.state = GameState.RIVER
        game._start_showdown()

        # 本局结束，P2（对 8）赢得全部底池，而不是平局
        self.assertEqual(game.state, GameState.HAND_OVER)
        self.assertIn(p2.name, game.last_result)
        self.assertIn("赢得", game.last_result)
        # 筹码结算：P2 得回 100 + 赢得 200 = 1100；P1 只剩 900
        self.assertEqual(p1.chips, 900)
        self.assertEqual(p2.chips, 1100)


class RemovePlayerFlowTest(unittest.TestCase):
    """掉线移除玩家后的行动权与流程正确性测试。

    覆盖修复前的两类缺陷：
    1. 移除玩家后座位索引错位，导致 current_pos 越界或指向错误玩家；
    2. 对手全部掉线弃牌后本局未及时结束。
    """

    def test_remove_non_acting_player_keeps_actor(self) -> None:
        """移除非当前行动者时，行动权应保持指向同一玩家。"""
        game = _make_game(seed=61)
        players = _add_players(game, 3)
        game.start_hand()
        # 开局后翻牌前首行动者（庄家位）
        actor_before = game.seats.all()[game.current_pos].player_id
        # 移除一个非行动者
        non_actor = next(p for p in players if p.player_id != actor_before)
        game.remove_player(non_actor.player_id)
        # 行动权应仍指向原行动玩家（索引已被正确修正，不越界）
        self.assertEqual(game.seats.all()[game.current_pos].player_id, actor_before)
        # 原行动玩家应仍可正常行动
        self.assertTrue(game.get_player_options(actor_before)["can_act"])

    def test_remove_acting_player_advances_turn(self) -> None:
        """移除当前行动者时，行动权应转给下一个可行动玩家。"""
        game = _make_game(seed=62)
        players = _add_players(game, 3)
        game.start_hand()
        # 找到当前行动者并移除
        actor = game.seats.all()[game.current_pos]
        remaining = [p for p in players if p.player_id != actor.player_id]
        game.remove_player(actor.player_id)
        # 剩余玩家中应有且仅有一人可行动（行动权成功移交）
        next_actor = next(
            (p for p in remaining if game.get_player_options(p.player_id)["can_act"]),
            None,
        )
        self.assertIsNotNone(next_actor, "移除行动者后应有人可行动")
        self.assertEqual(game.seats.all()[game.current_pos].player_id, next_actor.player_id)

    def test_remove_last_opponent_ends_hand(self) -> None:
        """2 人局中对方掉线弃牌，本局应立即结束并结算。"""
        game = _make_game(seed=63)
        players = _add_players(game, 2)
        game.start_hand()
        # 当前行动者为庄家（小盲），移除另一位玩家（大盲）
        actor = game.seats.all()[game.current_pos]
        opponent = next(p for p in players if p.player_id != actor.player_id)
        game.remove_player(opponent.player_id)
        # 只剩一名未弃牌玩家，本局应立即结束
        self.assertEqual(game.state, GameState.HAND_OVER)
        # 底池 = 小盲 10 + 大盲 20 = 30，全部判给幸存者
        self.assertEqual(game.last_result, f"{actor.name} 赢得 30 筹码（无人跟注）")

    def test_removed_player_chips_not_lost(self) -> None:
        """掉线玩家的投入应保留在底池中，筹码不丢失。"""
        game = _make_game(seed=64)
        players = _add_players(game, 2)
        game.start_hand()
        # 当前行动者为小盲（已投 10），对手为大盲（已投 20）
        actor = game.seats.all()[game.current_pos]
        opponent = next(p for p in players if p.player_id != actor.player_id)
        # 记录行动者当前剩余筹码（1000 - 10 = 990）
        chips_before = actor.chips
        game.remove_player(opponent.player_id)
        # 掉线者 20 筹码必须保留在底池：行动者赢得全部 30（10+20）
        self.assertEqual(game.state, GameState.HAND_OVER)
        self.assertEqual(actor.chips, chips_before + 30)


if __name__ == "__main__":
    unittest.main()

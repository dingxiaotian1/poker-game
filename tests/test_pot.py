"""底池与边池管理单元测试。"""
from __future__ import annotations

import unittest

from core.player import Player
from core.pot import Pot, PotManager


class TestPotManager(unittest.TestCase):
    """PotManager 构建边池与分配奖金的测试。"""

    def setUp(self) -> None:
        """每个测试前创建全新的底池管理器。"""
        self.pm = PotManager()

    def test_collect_and_total(self) -> None:
        """主池收集后总额应正确。"""
        self.pm.collect(50)
        self.pm.collect(30)
        self.assertEqual(self.pm.total, 80)

    def test_collect_negative_raises(self) -> None:
        """负数收集应抛出 ValueError。"""
        with self.assertRaises(ValueError):
            self.pm.collect(-5)

    def test_simple_winner_takes_all(self) -> None:
        """两人等额投注，胜者通吃。"""
        p1 = Player(1, "A", 1000)
        p2 = Player(2, "B", 1000)
        p1.total_bet = 100
        p2.total_bet = 100
        self.pm.build_side_pots([p1, p2])
        # 只有一个主池，两人均合格
        self.assertEqual(len(self.pm.pots), 1)
        self.assertEqual(self.pm.pots[0].amount, 200)
        # P1 牌力更大
        payouts = self.pm.distribute({1: (1, 14), 2: (0, 13, 12, 11, 9)})
        self.assertEqual(payouts[1], 200)
        self.assertNotIn(2, payouts)

    def test_side_pot_creation(self) -> None:
        """全下金额不同时应正确产生边池。

        场景：P1 全下 50，P2 与 P3 各跟到 100。
        - 主池：3×50 = 150，三人合格
        - 边池：2×50 = 100，仅 P2 P3 合格
        """
        p1 = Player(1, "A", 1000)
        p2 = Player(2, "B", 1000)
        p3 = Player(3, "C", 1000)
        p1.total_bet = 50
        p2.total_bet = 100
        p3.total_bet = 100

        self.pm.build_side_pots([p1, p2, p3])
        # 应有 2 个池
        self.assertEqual(len(self.pm.pots), 2)
        # 主池 150，三人合格
        self.assertEqual(self.pm.pots[0].amount, 150)
        self.assertEqual(set(self.pm.pots[0].eligible_ids), {1, 2, 3})
        # 边池 100，仅 P2 P3 合格
        self.assertEqual(self.pm.pots[1].amount, 100)
        self.assertEqual(set(self.pm.pots[1].eligible_ids), {2, 3})

    def test_side_pot_winner_distribution(self) -> None:
        """P1 主池获胜，P2 边池获胜，应分别得对应奖金。"""
        p1 = Player(1, "A", 1000)
        p2 = Player(2, "B", 1000)
        p3 = Player(3, "C", 1000)
        p1.total_bet = 50
        p2.total_bet = 100
        p3.total_bet = 100
        self.pm.build_side_pots([p1, p2, p3])

        # P1 主池牌最大，P3 边池牌最大
        payouts = self.pm.distribute({
            1: (3, 14, 5, 4, 2),   # 三条
            2: (1, 13, 12, 11, 9), # 一对
            3: (3, 13, 7, 4, 2),   # 三条（比 P1 小）
        })
        # 主池 150 给 P1
        self.assertEqual(payouts.get(1), 150)
        # 边池 100 给 P3（边池只有 P2 P3，P3 三条 > P2 一对）
        self.assertEqual(payouts.get(3), 100)
        # P2 无奖
        self.assertNotIn(2, payouts)

    def test_split_pot_on_tie(self) -> None:
        """平局时两人平分底池。"""
        p1 = Player(1, "A", 1000)
        p2 = Player(2, "B", 1000)
        p1.total_bet = 100
        p2.total_bet = 100
        self.pm.build_side_pots([p1, p2])
        # 完全相同牌力 → 平分
        payouts = self.pm.distribute({1: (2, 13, 9, 5, 3), 2: (2, 13, 9, 5, 3)})
        self.assertEqual(payouts[1], 100)
        self.assertEqual(payouts[2], 100)

    def test_non_divisible_pot_remainder(self) -> None:
        """不能整除时余数应分给前列赢家，不丢失筹码。"""
        p1 = Player(1, "A", 1000)
        p2 = Player(2, "B", 1000)
        p3 = Player(3, "C", 1000)
        # 总池 100，三人平分：33+33+33=99，余 1 给 P1
        p1.total_bet = 34
        p2.total_bet = 33
        p3.total_bet = 33
        self.pm.build_side_pots([p1, p2, p3])
        payouts = self.pm.distribute({
            1: (0, 14, 9, 7, 5),
            2: (0, 14, 9, 7, 5),
            3: (0, 14, 9, 7, 5),
        })
        # 总额应等于底池总额 100
        self.assertEqual(sum(payouts.values()), 100)

    def test_folded_player_contributes_but_ineligible(self) -> None:
        """已弃牌玩家仍贡献筹码但不合格赢。"""
        p1 = Player(1, "A", 1000)
        p2 = Player(2, "B", 1000)
        p1.total_bet = 100
        p2.total_bet = 100
        p1.folded = True  # P1 弃牌
        self.pm.build_side_pots([p1, p2])
        # 主池 200，但仅 P2 合格
        self.assertEqual(self.pm.pots[0].amount, 200)
        self.assertEqual(self.pm.pots[0].eligible_ids, [2])
        payouts = self.pm.distribute({2: (0, 13, 12, 11, 9)})
        self.assertEqual(payouts[2], 200)

    def test_reset(self) -> None:
        """reset 后底池应清空。"""
        self.pm.collect(100)
        self.pm.reset()
        self.assertEqual(self.pm.total, 0)


class TestPot(unittest.TestCase):
    """单个 Pot 类的测试。"""

    def test_pot_add(self) -> None:
        """add 应累加金额。"""
        pot = Pot(amount=10)
        pot.add(5)
        self.assertEqual(pot.amount, 15)

    def test_pot_add_negative_raises(self) -> None:
        """负数 add 应抛出 ValueError。"""
        pot = Pot(amount=10)
        with self.assertRaises(ValueError):
            pot.add(-1)

    def test_pot_serialization(self) -> None:
        """to_dict 应包含金额与合格者列表。"""
        pot = Pot(amount=200, eligible_ids=[1, 2])
        data = pot.to_dict()
        self.assertEqual(data["amount"], 200)
        self.assertEqual(data["eligible_ids"], [1, 2])


if __name__ == "__main__":
    unittest.main()

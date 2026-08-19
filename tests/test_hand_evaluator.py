"""手牌牌型评估单元测试。"""
from __future__ import annotations

import unittest

from core.card import Card
from core.hand_evaluator import (
    HAND_FOUR_KIND,
    HAND_FULL_HOUSE,
    HAND_HIGH_CARD,
    HAND_PAIR,
    HAND_ROYAL_FLUSH,
    HAND_STRAIGHT,
    HAND_STRAIGHT_FLUSH,
    HAND_THREE_KIND,
    HAND_TWO_PAIR,
    HAND_FLUSH,
    compare_hands,
    evaluate_best,
    rank_to_name,
)


def _c(rank: int, suit: str) -> Card:
    """快捷构造 Card 的辅助函数。"""
    return Card(rank, suit)


class TestHandEvaluation(unittest.TestCase):
    """各种牌型的识别与比较测试。"""

    def test_royal_flush(self) -> None:
        """皇家同花顺：A K Q J 10 同花色。"""
        cards = [_c(14, "S"), _c(13, "S"), _c(12, "S"), _c(11, "S"), _c(10, "S")]
        rank, _ = evaluate_best(cards)
        self.assertEqual(rank[0], HAND_ROYAL_FLUSH)
        self.assertEqual(rank_to_name(rank), "皇家同花顺")

    def test_straight_flush(self) -> None:
        """同花顺：5 张同花色连续。"""
        cards = [_c(9, "H"), _c(8, "H"), _c(7, "H"), _c(6, "H"), _c(5, "H")]
        rank, _ = evaluate_best(cards)
        self.assertEqual(rank[0], HAND_STRAIGHT_FLUSH)
        self.assertEqual(rank[1], 9)  # 最高牌为 9

    def test_wheel_straight(self) -> None:
        """轮转顺子 A-2-3-4-5，最高牌为 5。"""
        cards = [_c(14, "D"), _c(2, "D"), _c(3, "C"), _c(4, "H"), _c(5, "S")]
        rank, _ = evaluate_best(cards)
        self.assertEqual(rank[0], HAND_STRAIGHT)
        self.assertEqual(rank[1], 5)

    def test_four_of_a_kind(self) -> None:
        """四条。"""
        cards = [_c(9, "S"), _c(9, "H"), _c(9, "D"), _c(9, "C"), _c(3, "S")]
        rank, _ = evaluate_best(cards)
        self.assertEqual(rank[0], HAND_FOUR_KIND)
        self.assertEqual(rank[1], 9)

    def test_full_house(self) -> None:
        """葫芦：三带二。"""
        cards = [_c(7, "S"), _c(7, "H"), _c(7, "D"), _c(2, "C"), _c(2, "S")]
        rank, _ = evaluate_best(cards)
        self.assertEqual(rank[0], HAND_FULL_HOUSE)

    def test_flush(self) -> None:
        """同花但非顺子。"""
        cards = [_c(2, "C"), _c(5, "C"), _c(7, "C"), _c(9, "C"), _c(13, "C")]
        rank, _ = evaluate_best(cards)
        self.assertEqual(rank[0], HAND_FLUSH)

    def test_three_of_a_kind(self) -> None:
        """三条。"""
        cards = [_c(4, "S"), _c(4, "H"), _c(4, "D"), _c(9, "C"), _c(13, "S")]
        rank, _ = evaluate_best(cards)
        self.assertEqual(rank[0], HAND_THREE_KIND)

    def test_two_pair(self) -> None:
        """两对。"""
        cards = [_c(4, "S"), _c(4, "H"), _c(9, "D"), _c(9, "C"), _c(13, "S")]
        rank, _ = evaluate_best(cards)
        self.assertEqual(rank[0], HAND_TWO_PAIR)

    def test_one_pair(self) -> None:
        """一对。"""
        cards = [_c(4, "S"), _c(4, "H"), _c(9, "D"), _c(11, "C"), _c(13, "S")]
        rank, _ = evaluate_best(cards)
        self.assertEqual(rank[0], HAND_PAIR)

    def test_high_card(self) -> None:
        """高牌。"""
        cards = [_c(2, "S"), _c(5, "H"), _c(9, "D"), _c(11, "C"), _c(13, "S")]
        rank, _ = evaluate_best(cards)
        self.assertEqual(rank[0], HAND_HIGH_CARD)

    def test_best_of_seven(self) -> None:
        """7 张牌中应选出最佳 5 张组合。

        构造场景：底牌 2 张同花 + 公共牌含 3 张同花，应组成同花。
        """
        # 底牌: A♠ K♠，公共牌: Q♠ J♠ 9♥ 8♣ 2♠
        hole = [_c(14, "S"), _c(13, "S")]
        community = [_c(12, "S"), _c(11, "S"), _c(9, "H"), _c(8, "C"), _c(2, "S")]
        rank, best = evaluate_best(hole + community)
        # 应为同花顺（A K Q J + 2 不连续，但 A K Q J 10 需要 10；
        # 这里只有 A K Q J 同花，缺 10，所以是同花而非同花顺）
        self.assertEqual(rank[0], HAND_FLUSH)
        self.assertEqual(len(best), 5)

    def test_best_of_seven_straight_flush(self) -> None:
        """7 张牌中应能选出同花顺。"""
        # A♠ K♠ Q♠ J♠ 10♠ 在 7 张中
        cards = [_c(14, "S"), _c(13, "S"), _c(2, "H"), _c(12, "S"), _c(11, "S"), _c(3, "D"), _c(10, "S")]
        rank, _ = evaluate_best(cards)
        self.assertEqual(rank[0], HAND_ROYAL_FLUSH)


class TestHandComparison(unittest.TestCase):
    """牌型大小比较测试。"""

    def test_royal_beats_straight_flush(self) -> None:
        """皇家同花顺大于普通同花顺。"""
        royal = (HAND_ROYAL_FLUSH, 14)
        sf = (HAND_STRAIGHT_FLUSH, 9)
        self.assertGreater(compare_hands(royal, sf), 0)

    def test_four_kind_beats_full_house(self) -> None:
        """四条大于葫芦。"""
        four = (HAND_FOUR_KIND, 9, 3)
        fh = (HAND_FULL_HOUSE, 7, 2)
        self.assertGreater(compare_hands(four, fh), 0)

    def test_pair_kicker_decides_tie(self) -> None:
        """同为一对时，由踢脚牌大小决定胜负。"""
        # 两个对 K，但 A 的踢脚牌更大
        a = (HAND_PAIR, 13, 14, 9, 7, 5)
        b = (HAND_PAIR, 13, 12, 9, 7, 5)
        self.assertGreater(compare_hands(a, b), 0)

    def test_pair_three_vs_pair_eight(self) -> None:
        """底牌对 3 vs 底牌对 8：对 8 必须胜出，不得判平局。

        回归测试：历史上出现"双方均为对子时被误判平局"的问题场景，
        用于锁定对子比较算法——对子点数（而非踢脚牌）必须优先参与比较。
        """
        # 一方底牌为一对 3
        hole_a = [_c(3, "S"), _c(3, "H")]
        # 另一方底牌为一对 8
        hole_b = [_c(8, "S"), _c(8, "H")]
        # 公共牌不含 3/8，也无对子，保证双方各自保持"一对"牌型
        community = [_c(5, "C"), _c(6, "D"), _c(9, "H"), _c(11, "S"), _c(12, "C")]
        rank_a, _ = evaluate_best(hole_a + community)
        rank_b, _ = evaluate_best(hole_b + community)
        # 双方均为"一对"牌型
        self.assertEqual(rank_a[0], HAND_PAIR)
        self.assertEqual(rank_b[0], HAND_PAIR)
        # 对 8 必须严格大于对 3（而非平局）
        self.assertGreater(compare_hands(rank_b, rank_a), 0)
        self.assertEqual(compare_hands(rank_a, rank_b), -1)
        # 相同手牌仍应判平局（对照）
        self.assertEqual(compare_hands(rank_a, rank_a), 0)

    def test_pair_same_rank_kickers_decide(self) -> None:
        """对子点数相同时，再依次比较踢脚牌。"""
        # A：对 9，踢脚牌 8/6/3
        rank_a = evaluate_best(
            [_c(9, "S"), _c(9, "H"), _c(8, "D"), _c(6, "C"), _c(3, "S")]
        )[0]
        # B：对 9，踢脚牌 8/6/2（第三张踢脚更小）
        rank_b = evaluate_best(
            [_c(9, "D"), _c(9, "C"), _c(8, "S"), _c(6, "H"), _c(2, "D")]
        )[0]
        self.assertEqual(rank_a[0], HAND_PAIR)
        self.assertEqual(rank_b[0], HAND_PAIR)
        # 对子点数相同，A 的第三张踢脚 3 > B 的 2，A 胜
        self.assertGreater(compare_hands(rank_a, rank_b), 0)

    def test_equal_hands_tie(self) -> None:
        """完全相同的牌力应判平局。"""
        a = (HAND_PAIR, 13, 14, 9, 7, 5)
        b = (HAND_PAIR, 13, 14, 9, 7, 5)
        self.assertEqual(compare_hands(a, b), 0)

    def test_high_card_beats_lower(self) -> None:
        """高牌时最大点数大者胜。"""
        a = (HAND_HIGH_CARD, 14, 9, 7, 5, 2)
        b = (HAND_HIGH_CARD, 13, 9, 7, 5, 2)
        self.assertGreater(compare_hands(a, b), 0)


class TestEdgeCases(unittest.TestCase):
    """边界场景测试。"""

    def test_invalid_card_count(self) -> None:
        """牌数不在 5~7 应抛出 ValueError。"""
        # 2 张牌：过少
        with self.assertRaises(ValueError):
            evaluate_best([_c(2, "S"), _c(3, "H")])
        # 8 张牌：过多
        with self.assertRaises(ValueError):
            evaluate_best([_c(i, "S") for i in range(2, 10)])

    def test_seven_card_selects_best(self) -> None:
        """7 张含两组牌型时取最大者。

        构造：含四条 9 与一对 K，应识别为四条而非一对。
        """
        cards = [
            _c(9, "S"), _c(9, "H"), _c(9, "D"), _c(9, "C"),
            _c(13, "S"), _c(13, "H"), _c(2, "D"),
        ]
        rank, _ = evaluate_best(cards)
        self.assertEqual(rank[0], HAND_FOUR_KIND)


if __name__ == "__main__":
    unittest.main()

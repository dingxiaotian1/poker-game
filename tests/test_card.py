"""扑克牌与牌组单元测试。"""
from __future__ import annotations

import random
import unittest

from core.card import Card, Deck, RANK_DISPLAY, SUITS


class TestCard(unittest.TestCase):
    """Card 类的单元测试。"""

    def test_card_creation_valid(self) -> None:
        """合法点数与花色应能正常创建。"""
        # 创建 A♠
        card = Card(14, "S")
        self.assertEqual(card.rank, 14)
        self.assertEqual(card.suit, "S")
        self.assertEqual(card.display(), "A♠")

    def test_card_creation_invalid_rank(self) -> None:
        """非法点数应抛出 ValueError。"""
        with self.assertRaises(ValueError):
            Card(1, "S")   # 1 不在合法范围
        with self.assertRaises(ValueError):
            Card(15, "H")  # 15 超出范围

    def test_card_creation_invalid_suit(self) -> None:
        """非法花色应抛出 ValueError。"""
        with self.assertRaises(ValueError):
            Card(10, "X")  # X 不是合法花色

    def test_card_display_all_ranks(self) -> None:
        """所有点数的显示字符应正确。"""
        # 验证点数到显示字符的映射
        card = Card(11, "H")
        self.assertEqual(card.display(), "J♥")
        card = Card(10, "D")
        self.assertEqual(card.display(), "10♦")

    def test_card_equality_and_hash(self) -> None:
        """相同点数花色的牌应相等且哈希一致。"""
        a = Card(5, "C")
        b = Card(5, "C")
        c = Card(5, "D")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        # 哈希一致以支持集合去重
        self.assertEqual(hash(a), hash(b))
        self.assertEqual(len({a, b, c}), 2)

    def test_card_serialization(self) -> None:
        """to_dict / from_dict 应能无损往返。"""
        original = Card(13, "H")
        data = original.to_dict()
        restored = Card.from_dict(data)
        self.assertEqual(original, restored)


class TestDeck(unittest.TestCase):
    """Deck 类的单元测试。"""

    def test_deck_has_52_cards(self) -> None:
        """新牌组应有 52 张且无重复。"""
        deck = Deck(rng=random.Random(42))
        self.assertEqual(deck.remaining, 52)
        # 收集所有发出的牌验证无重复
        cards = deck.deal_many(52)
        self.assertEqual(len(cards), 52)
        self.assertEqual(len(set(cards)), 52)

    def test_deck_shuffle_reproducible(self) -> None:
        """相同种子的两次洗牌结果应一致。"""
        d1 = Deck(rng=random.Random(123))
        d2 = Deck(rng=random.Random(123))
        c1 = d1.deal_many(5)
        c2 = d2.deal_many(5)
        self.assertEqual(c1, c2)

    def test_deck_shuffle_changes_order(self) -> None:
        """不同种子应产生不同顺序（概率上）。"""
        d1 = Deck(rng=random.Random(1))
        d2 = Deck(rng=random.Random(2))
        c1 = d1.deal_many(10)
        c2 = d2.deal_many(10)
        self.assertNotEqual(c1, c2)

    def test_deck_deal_until_empty(self) -> None:
        """发完 52 张后再发应抛出 IndexError。"""
        deck = Deck(rng=random.Random(7))
        deck.deal_many(52)
        self.assertEqual(deck.remaining, 0)
        with self.assertRaises(IndexError):
            deck.deal()

    def test_deck_deal_many_too_many(self) -> None:
        """请求超过剩余牌数应抛出 ValueError。"""
        deck = Deck(rng=random.Random(7))
        with self.assertRaises(ValueError):
            deck.deal_many(53)

    def test_deck_deal_many_negative(self) -> None:
        """请求负数应抛出 ValueError。"""
        deck = Deck(rng=random.Random(7))
        with self.assertRaises(ValueError):
            deck.deal_many(-1)

    def test_deck_reshuffle(self) -> None:
        """shuffle 后应重新拥有 52 张。"""
        deck = Deck(rng=random.Random(7))
        deck.deal_many(30)
        self.assertEqual(deck.remaining, 22)
        deck.shuffle()
        self.assertEqual(deck.remaining, 52)

    def test_all_suits_and_ranks_present(self) -> None:
        """完整牌组应包含 4 花色 × 13 点数。"""
        deck = Deck(rng=random.Random(0))
        cards = deck.deal_many(52)
        for suit in SUITS:
            for rank in RANK_DISPLAY:
                self.assertIn(Card(rank, suit), cards)


if __name__ == "__main__":
    unittest.main()

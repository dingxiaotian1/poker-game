"""扑克牌与牌组模块。

定义单张扑克牌 Card 与一副牌 Deck，提供洗牌、发牌等基础操作。
本模块是游戏核心的最底层，不依赖任何其他业务模块，便于单元测试与复用。

设计要点：
- 点数 (rank) 用整数 2~14 表示，14 代表 A，便于直接比较大小。
- 花色 (suit) 用单字符 'S'(黑桃♠) 'H'(红心♥) 'D'(方块♦) 'C'(梅花♣) 表示，
  使用字符而非枚举是为了简化网络传输与日志输出。
- Deck 内部使用 random.Random 实例，可注入种子以便测试复现。
"""
from __future__ import annotations

import random
from typing import List, Optional, Sequence


# 【常量定义】花色与点数的合法集合，集中管理避免魔法字符
# 花色字符：S=Spade黑桃 H=Heart红心 D=Diamond方块 C=Club梅花
SUITS: Sequence[str] = ("S", "H", "D", "C")
# 点数范围：2~10 为数字牌，11=J 12=Q 13=K 14=A
RANKS: Sequence[int] = tuple(range(2, 15))

# 点数到显示字符的映射表，用于将内部整数转为人类可读文本
RANK_DISPLAY: dict[int, str] = {
    2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9", 10: "10",
    11: "J", 12: "Q", 13: "K", 14: "A",
}
# 花色字符到 Unicode 符号的映射，用于命令行美化显示
SUIT_SYMBOL: dict[str, str] = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}


class Card:
    """单张扑克牌。

    不可变对象（逻辑上），创建后点数与花色不再变化。

    Attributes:
        rank: 点数，取值 2~14，14 表示 A。
        suit: 花色字符，取值为 SUITS 之一。
    """

    __slots__ = ("rank", "suit")  # 限制属性，节省内存（牌组有52张，量较大时有益）

    def __init__(self, rank: int, suit: str) -> None:
        """初始化一张牌。

        Args:
            rank: 点数，必须在 RANKS 范围内。
            suit: 花色，必须在 SUITS 范围内。

        Raises:
            ValueError: 当点数或花色非法时抛出。
        """
        # 【参数校验】构造时即校验合法性，防止脏数据进入系统
        if rank not in RANKS:
            raise ValueError(f"非法点数: {rank}，合法范围为 2~14")
        if suit not in SUITS:
            raise ValueError(f"非法花色: {suit}，合法值为 {SUITS}")
        self.rank: int = rank
        self.suit: str = suit

    def display(self) -> str:
        """返回命令行友好的字符串表示，例如 'A♠'、'10♥'。

        Returns:
            形如 '点数+花色符号' 的字符串。
        """
        return f"{RANK_DISPLAY[self.rank]}{SUIT_SYMBOL[self.suit]}"

    def to_dict(self) -> dict:
        """序列化为字典，用于网络传输与持久化。

        Returns:
            包含 rank 与 suit 两个字段的字典。
        """
        return {"rank": self.rank, "suit": self.suit}

    @classmethod
    def from_dict(cls, data: dict) -> "Card":
        """从字典反序列化构建 Card 实例。

        Args:
            data: 包含 rank/suit 字段的字典。

        Returns:
            对应的 Card 对象。
        """
        return cls(rank=int(data["rank"]), suit=str(data["suit"]))

    def __eq__(self, other: object) -> bool:
        """相等判断：点数与花色均相同。"""
        if not isinstance(other, Card):
            return NotImplemented
        return self.rank == other.rank and self.suit == other.suit

    def __hash__(self) -> int:
        """哈希值，使 Card 可作为字典键或集合元素。"""
        return hash((self.rank, self.suit))

    def __repr__(self) -> str:
        return f"Card({self.display()})"

    def __str__(self) -> str:
        return self.display()


class Deck:
    """一副 52 张的标准扑克牌组。

    提供洗牌、发牌、剩余牌数查询等操作。每次 new_hand() 前应调用 shuffle()
    重置并洗牌。本类非线程安全，调用方需保证单线程访问或加锁。
    """

    def __init__(self, rng: Optional[random.Random] = None) -> None:
        """初始化一副完整且已洗乱的牌组。

        Args:
            rng: 可选的随机数生成器，测试时可传入固定种子的 Random 以复现结果。
        """
        # 允许注入随机源，便于单元测试中确定性发牌
        self._rng: random.Random = rng if rng is not None else random.Random()
        # 生成完整 52 张牌并立即洗乱
        self._cards: List[Card] = [Card(r, s) for r in RANKS for s in SUITS]
        self.shuffle()

    def shuffle(self) -> None:
        """重置牌组为完整 52 张并洗乱。

        每局开始前调用，确保牌组完整且顺序随机。
        """
        # 重新生成全部牌，避免上一局发完后牌组为空
        self._cards = [Card(r, s) for r in RANKS for s in SUITS]
        # Fisher-Yates 洗牌算法，random.shuffle 内部即实现此算法
        self._rng.shuffle(self._cards)

    def deal(self) -> Card:
        """从牌堆顶发一张牌。

        Returns:
            牌堆顶部的 Card。

        Raises:
            IndexError: 牌堆已空时抛出，调用方应保证发牌数量不超过 52。
        """
        if not self._cards:
            raise IndexError("牌堆已空，无法继续发牌")
        # pop() 从列表末尾取，O(1) 复杂度
        return self._cards.pop()

    def deal_many(self, count: int) -> List[Card]:
        """连续发多张牌。

        Args:
            count: 需要发出的牌数。

        Returns:
            按发出顺序排列的 Card 列表。

        Raises:
            ValueError: 当请求数量超过剩余牌数时抛出。
        """
        if count < 0:
            raise ValueError("发牌数量不能为负")
        if count > len(self._cards):
            raise ValueError(f"剩余牌数不足: 请求 {count}，实际 {len(self._cards)}")
        return [self.deal() for _ in range(count)]

    @property
    def remaining(self) -> int:
        """返回牌堆剩余牌数。"""
        return len(self._cards)

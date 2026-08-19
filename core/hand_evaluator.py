"""手牌牌型评估与比较模块。

负责从 7 张牌（2 张底牌 + 5 张公共牌）中选出最佳 5 张组合，
并给出可用于直接比较的"牌力元组"。本模块是判定胜负的核心。

设计思路：
- 对 7 张牌枚举 C(7,5)=21 种 5 张组合，逐一评估取最大牌力，保证正确性。
  组合数最多 21，性能完全可接受；这种写法比"7 张直接判定"更易读且不易出错。
- 单组 5 张的评估基于频率统计与排序实现（_evaluate_five）。
- 返回的 HandRank 是元组 (类别, 主值...)，元组天然支持字典序比较，
  因此可直接用 max/排序决定胜者。
- 类别数值越大牌型越大（高牌=0 ... 皇家同花顺=9）。
"""
from __future__ import annotations

from itertools import combinations
from typing import List, Tuple

from .card import Card


# 【牌型类别常量】数值大小代表牌型强弱，便于直接比较
HAND_HIGH_CARD: int = 0       # 高牌
HAND_PAIR: int = 1            # 一对
HAND_TWO_PAIR: int = 2        # 两对
HAND_THREE_KIND: int = 3      # 三条
HAND_STRAIGHT: int = 4        # 顺子
HAND_FLUSH: int = 5           # 同花
HAND_FULL_HOUSE: int = 6      # 葫芦（三带二）
HAND_FOUR_KIND: int = 7       # 四条
HAND_STRAIGHT_FLUSH: int = 8  # 同花顺
HAND_ROYAL_FLUSH: int = 9     # 皇家同花顺（A高同花顺）

# 类别到中文名称的映射，用于结果显示
HAND_NAMES: dict[int, str] = {
    HAND_HIGH_CARD: "高牌",
    HAND_PAIR: "一对",
    HAND_TWO_PAIR: "两对",
    HAND_THREE_KIND: "三条",
    HAND_STRAIGHT: "顺子",
    HAND_FLUSH: "同花",
    HAND_FULL_HOUSE: "葫芦",
    HAND_FOUR_KIND: "四条",
    HAND_STRAIGHT_FLUSH: "同花顺",
    HAND_ROYAL_FLUSH: "皇家同花顺",
}

# 牌力元组类型：(类别, 主值1, 主值2, ...)，长度可变但同类别内固定
HandRank = Tuple[int, ...]


def evaluate_best(cards: List[Card]) -> Tuple[HandRank, List[Card]]:
    """从 5~7 张牌中选出最佳 5 张组合并返回其牌力。

    采用枚举 C(n,5) 组合的方式保证正确性，n 最多为 7，组合数最多 21，性能可接受。
    对每个 5 张组合调用 _evaluate_five 得到牌力，取最大者。

    Args:
        cards: 参与评估的牌列表，长度必须为 5~7。

    Returns:
        二元组 (最佳牌力元组, 对应的 5 张牌列表)。

    Raises:
        ValueError: 牌数不在 5~7 范围内时抛出。
    """
    if not (5 <= len(cards) <= 7):
        raise ValueError(f"评估牌数必须为 5~7 张，当前 {len(cards)} 张")

    best_rank: HandRank = (HAND_HIGH_CARD, 0, 0, 0, 0, 0)
    best_cards: List[Card] = []

    # 枚举所有 5 张组合，逐一评估并保留最大牌力
    for combo in combinations(cards, 5):
        rank = _evaluate_five(list(combo))
        # 元组比较：先比类别，再依次比主值
        if rank > best_rank:
            best_rank = rank
            best_cards = list(combo)

    return best_rank, best_cards


def _evaluate_five(cards: List[Card]) -> HandRank:
    """评估恰好 5 张牌的牌型，返回牌力元组。

    内部函数，调用方需保证传入正好 5 张牌。

    判定顺序遵循从强到弱：先检查同花顺，再四条、葫芦...最后高牌。
    一旦命中即返回，避免重复计算。

    Args:
        cards: 正好 5 张牌。

    Returns:
        牌力元组 (类别, 比较值...)，可直接用 > 比较。
    """
    # 取出点数与花色列表，便于后续统计
    ranks: List[int] = sorted([c.rank for c in cards], reverse=True)
    suits: List[str] = [c.suit for c in cards]

    # 统计每个点数出现次数，按 (次数, 点数) 降序排列
    # 例：[A,A,K,5,5] -> [(2,14),(2,5),(1,13)]
    rank_counts: dict[int, int] = {}
    for r in ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1
    # 先按次数降序，再按点数降序，得到分组排序结果
    groups = sorted(rank_counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    counts = [g[1] for g in groups]   # 例如 [4,1] 表示四条
    group_ranks = [g[0] for g in groups]

    # 判断是否为同花：5 张花色完全相同
    is_flush: bool = len(set(suits)) == 1
    # 判断是否为顺子：5 张连续点数，或 A-2-3-4-5 的轮转顺子（A 当 1 用）
    is_straight, straight_high = _check_straight(ranks)

    # 【皇家同花顺】A 高的同花顺，是最大牌型
    if is_flush and is_straight and straight_high == 14:
        return (HAND_ROYAL_FLUSH, 14)
    # 【同花顺】同花色且连续
    if is_flush and is_straight:
        return (HAND_STRAIGHT_FLUSH, straight_high)
    # 【四条】某个点数出现 4 次
    if counts[0] == 4:
        return (HAND_FOUR_KIND, group_ranks[0], group_ranks[1])
    # 【葫芦】三加二组合
    if counts[0] == 3 and counts[1] == 2:
        return (HAND_FULL_HOUSE, group_ranks[0], group_ranks[1])
    # 【同花】5 张同花色但非顺子
    if is_flush:
        return (HAND_FLUSH, *ranks)
    # 【顺子】5 张连续点数
    if is_straight:
        return (HAND_STRAIGHT, straight_high)
    # 【三条】某个点数出现 3 次，另两张不同
    if counts[0] == 3:
        return (HAND_THREE_KIND, group_ranks[0], group_ranks[1], group_ranks[2])
    # 【两对】两个不同的对子
    if counts[0] == 2 and counts[1] == 2:
        return (HAND_TWO_PAIR, group_ranks[0], group_ranks[1], group_ranks[2])
    # 【一对】一个对子加三张散牌
    if counts[0] == 2:
        # 【重点注释】对子大小比较规则：先比对子点数，再依次比较三张踢脚牌。
        # 牌力元组必须把"对子点数"放在首位，否则踢脚牌会优先于对子点数参与
        # 比较，导致"对 3"与"对 8"这类点数不同的对子被误判为平局。
        # 此处显式取出对子点数，并对踢脚牌做一次防御性降序排序——不依赖上游
        # groups 的隐式排序结果，防止未来调整排序逻辑时引入对子比较回归。
        pair_rank = group_ranks[0]
        kickers = sorted(group_ranks[1:], reverse=True)[:3]
        return (HAND_PAIR, pair_rank, kickers[0], kickers[1], kickers[2])
    # 【高牌】以上都不满足
    return (HAND_HIGH_CARD, *ranks)


def _check_straight(sorted_ranks_desc: List[int]) -> Tuple[bool, int]:
    """判断 5 张已降序排列的点数是否构成顺子。

    需特别处理 A-2-3-4-5（轮转顺子）：A 视为 1 时构成顺子，最高牌为 5。

    Args:
        sorted_ranks_desc: 降序排列的 5 个点数。

    Returns:
        二元组 (是否为顺子, 顺子最高牌点数)。非顺子时最高牌为 0。
    """
    # 检查常规顺子：相邻点数差均为 1
    # 使用 set 去重判断，若有重复点数则不可能是顺子
    unique_ranks = set(sorted_ranks_desc)
    if len(unique_ranks) != 5:
        return False, 0

    # 常规情况：最大值与最小值之差为 4 且无重复，即连续 5 张
    if sorted_ranks_desc[0] - sorted_ranks_desc[4] == 4:
        return True, sorted_ranks_desc[0]

    # 特殊情况：A,5,4,3,2 → A 当 1 用，构成 5 高顺子
    if set(sorted_ranks_desc) == {14, 5, 4, 3, 2}:
        return True, 5

    return False, 0


def compare_hands(hand_a: HandRank, hand_b: HandRank) -> int:
    """比较两手牌力大小。

    Args:
        hand_a: 牌力元组 A。
        hand_b: 牌力元组 B。

    Returns:
        正数表示 A 更大，负数表示 B 更大，0 表示平局。
    """
    if hand_a > hand_b:
        return 1
    if hand_a < hand_b:
        return -1
    return 0


def rank_to_name(rank: HandRank) -> str:
    """根据牌力元组返回中文名称。

    Args:
        rank: 牌力元组。

    Returns:
        如 '同花顺'、'一对' 等中文字符串。
    """
    return HAND_NAMES.get(rank[0], "未知牌型")

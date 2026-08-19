"""玩家状态管理模块。

定义 Player 类，封装玩家在一局游戏中的全部状态：身份、筹码、手牌、
当前轮投注、累计投注、行动状态等。本类为纯数据+行为对象，不涉及网络与 IO。

设计要点：
- 区分 current_bet（本轮已下注）与 total_bet（本局累计下注）：
  前者用于本轮跟注/加注比较，后者用于结算边池。
- folded/all_in/acted 三个布尔标志明确刻画玩家在流程中的状态。
- 序列化/反序列化方法 to_dict/from_dict 供网络同步使用。
"""
from __future__ import annotations

from typing import List, Optional

from .card import Card


class Player:
    """德州扑克玩家。

    Attributes:
        player_id: 玩家唯一标识（连接建立时分配）。
        name: 显示昵称。
        chips: 当前剩余筹码。
        hole_cards: 底牌列表，长度 0 或 2。
        current_bet: 本轮已下注金额（每轮开始重置为 0）。
        total_bet: 本局累计投入底池的金额（用于边池计算）。
        folded: 是否已弃牌。
        all_in: 是否已全下。
        acted: 本轮是否已行动（用于判断轮次是否结束）。
        last_action: 上一次行动描述，用于 UI 展示。
    """

    def __init__(self, player_id: int, name: str, chips: int) -> None:
        """初始化玩家。

        Args:
            player_id: 玩家唯一 ID。
            name: 昵称，不可为空。
            chips: 初始筹码，必须为正。

        Raises:
            ValueError: 当昵称为空或筹码非法时抛出。
        """
        if not name or not name.strip():
            raise ValueError("玩家昵称不能为空")
        if chips < 0:
            raise ValueError(f"初始筹码不能为负: {chips}")

        self.player_id: int = player_id
        self.name: str = name.strip()
        self.chips: int = chips
        self.hole_cards: List[Card] = []

        # 本轮投注：每开始一个新的下注轮（翻牌前/翻牌/转牌/河牌）会重置为 0
        self.current_bet: int = 0
        # 本局累计投注：用于结算时计算边池，整局结束时重置
        self.total_bet: int = 0

        # 行动状态标志
        self.folded: bool = False
        self.all_in: bool = False
        self.acted: bool = False
        # 上次行动文本，例如 '跟注 50'、'加注到 100'，便于 UI 展示
        self.last_action: str = ""

    # ---------- 基础查询 ----------

    @property
    def is_active(self) -> bool:
        """玩家是否仍参与本局（未弃牌且未全下）。

        全下玩家虽然不再行动，但仍有资格争夺底池，所以"是否需行动"
        用本属性判断；"是否有资格赢池"用 can_win 判断。
        """
        return not self.folded and not self.all_in

    @property
    def can_win(self) -> bool:
        """玩家是否有资格赢取底池（未弃牌即可，全下也算）。"""
        return not self.folded

    @property
    def has_hole_cards(self) -> bool:
        """玩家是否已拿到底牌。"""
        return len(self.hole_cards) == 2

    # ---------- 状态变更 ----------

    def reset_for_new_hand(self) -> None:
        """新的一局开始时重置玩家手牌与投注状态。

        注意：不重置 chips，筹码跨局保留。
        """
        self.hole_cards = []
        self.current_bet = 0
        self.total_bet = 0
        self.folded = False
        self.all_in = False
        self.acted = False
        self.last_action = ""

    def reset_for_new_round(self) -> None:
        """新一轮下注（翻牌/转牌/河牌）开始时重置本轮投注与行动标记。

        全下与弃牌状态保留，因为它们跨越整局。
        """
        self.current_bet = 0
        # 全下玩家无需再行动，acted 保持 True 避免被要求行动
        if not self.all_in:
            self.acted = False
        self.last_action = ""

    def assign_hole_cards(self, cards: List[Card]) -> None:
        """发放底牌给玩家。

        Args:
            cards: 恰好 2 张牌。

        Raises:
            ValueError: 牌数不为 2 时抛出。
        """
        if len(cards) != 2:
            raise ValueError(f"底牌必须为 2 张，当前 {len(cards)} 张")
        self.hole_cards = list(cards)

    def bet(self, amount: int) -> int:
        """玩家下注（盲注、跟注、加注统一走此方法）。

        自动处理筹码不足时转为全下：若请求金额超过剩余筹码，则仅投入剩余筹码
        并标记 all_in。

        Args:
            amount: 期望投入的筹码数，必须 > 0。

        Returns:
            实际投入的筹码数（可能小于请求值，当筹码不足时）。

        Raises:
            ValueError: 当 amount 非正或玩家已全下/弃牌时抛出。
        """
        if amount <= 0:
            raise ValueError(f"下注金额必须为正: {amount}")
        if self.folded:
            raise ValueError("已弃牌，无法下注")
        if self.all_in:
            raise ValueError("已全下，无法继续下注")

        # 【全下处理】请求金额超过剩余筹码时，仅投入全部剩余筹码
        actual = min(amount, self.chips)
        self.chips -= actual
        self.current_bet += actual
        self.total_bet += actual
        # 投入全部筹码后标记全下
        if self.chips == 0:
            self.all_in = True
        return actual

    def fold(self) -> None:
        """玩家弃牌。"""
        if self.folded:
            raise ValueError("已经弃牌，不可重复操作")
        self.folded = True
        self.acted = True
        self.last_action = "弃牌"

    def mark_acted(self, action_desc: str) -> None:
        """记录玩家已完成本轮行动。

        Args:
            action_desc: 行动描述文本，用于 UI 展示。
        """
        self.acted = True
        self.last_action = action_desc

    # ---------- 序列化 ----------

    def to_dict(self, include_hole_cards: bool = False) -> dict:
        """序列化为字典用于网络传输。

        Args:
            include_hole_cards: 是否包含底牌。只有发给本人或摊牌阶段才应传 True，
                                避免泄露其他玩家手牌。

        Returns:
            玩家状态字典。
        """
        return {
            "player_id": self.player_id,
            "name": self.name,
            "chips": self.chips,
            "hole_cards": [c.to_dict() for c in self.hole_cards] if include_hole_cards else [],
            "card_count": len(self.hole_cards),
            "current_bet": self.current_bet,
            "total_bet": self.total_bet,
            "folded": self.folded,
            "all_in": self.all_in,
            "acted": self.acted,
            "last_action": self.last_action,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Player":
        """从字典反序列化玩家状态。

        仅恢复基本字段，底牌需通过单独的 deal 消息下发以保证可见性控制。

        Args:
            data: 玩家状态字典。

        Returns:
            Player 实例。
        """
        player = cls(
            player_id=int(data["player_id"]),
            name=str(data["name"]),
            chips=int(data["chips"]),
        )
        player.current_bet = int(data.get("current_bet", 0))
        player.total_bet = int(data.get("total_bet", 0))
        player.folded = bool(data.get("folded", False))
        player.all_in = bool(data.get("all_in", False))
        player.acted = bool(data.get("acted", False))
        player.last_action = str(data.get("last_action", ""))
        # 若字典中包含底牌数据则一并恢复（摊牌阶段）
        if data.get("hole_cards"):
            player.hole_cards = [Card.from_dict(c) for c in data["hole_cards"]]
        return player

    def __repr__(self) -> str:
        return f"Player(id={self.player_id}, name={self.name}, chips={self.chips})"


# ---------- 桌位管理工具 ----------

class PlayerSeat:
    """玩家座位表，维护按顺序排列的玩家列表。

    德州扑克需要按顺时针轮流行动，因此需要一个稳定顺序的玩家列表，
    并支持"从某个位置开始找下一个未弃牌玩家"等操作。
    """

    def __init__(self, players: Optional[List[Player]] = None) -> None:
        """初始化座位表。

        Args:
            players: 初始玩家列表，可为空。
        """
        self._players: List[Player] = list(players) if players else []

    def add(self, player: Player) -> None:
        """添加玩家到座位表末尾。

        Args:
            player: 要加入的玩家。

        Raises:
            ValueError: 玩家 ID 已存在时抛出。
        """
        if any(p.player_id == player.player_id for p in self._players):
            raise ValueError(f"玩家 ID 已存在: {player.player_id}")
        self._players.append(player)

    def remove(self, player_id: int) -> Optional[Player]:
        """移除指定 ID 的玩家。

        Args:
            player_id: 要移除的玩家 ID。

        Returns:
            被移除的 Player，若不存在返回 None。
        """
        for i, p in enumerate(self._players):
            if p.player_id == player_id:
                return self._players.pop(i)
        return None

    def get(self, player_id: int) -> Optional[Player]:
        """按 ID 查找玩家。"""
        for p in self._players:
            if p.player_id == player_id:
                return p
        return None

    def all(self) -> List[Player]:
        """返回所有玩家列表（按座位顺序）。"""
        return list(self._players)

    def active(self) -> List[Player]:
        """返回所有未弃牌的玩家（含全下者），按座位顺序。"""
        return [p for p in self._players if p.can_win]

    def actionable(self) -> List[Player]:
        """返回当前仍可行动的玩家（未弃牌且未全下）。"""
        return [p for p in self._players if p.is_active]

    def __len__(self) -> int:
        return len(self._players)

    def __iter__(self):
        return iter(self._players)

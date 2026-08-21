"""德州扑克游戏流程控制模块。

实现一局完整德州扑克的状态机：发牌 → 翻牌前 → 翻牌 → 转牌 → 河牌 → 摊牌。
本模块是服务端的权威逻辑核心，所有规则判定、投注校验、胜负分配都在此完成。
客户端只负责显示与发送行动指令，绝不自行计算结果，以保证数据一致性。

状态机：
    WAITING ──start_hand──> PREFLOP ──round_end──> FLOP ──round_end──> TURN
        └──> RIVER ──round_end──> SHOWDOWN ──> HAND_OVER ──(下一局)──> WAITING

设计要点：
- 采用"行动标记 + 投注对齐"判定下注轮是否结束，自然处理大盲注优先权。
- 加注会重置其他活跃玩家的 acted 标记，强制其再次行动（面对加注）。
- 全下分池由 PotManager 在摊牌前构建。
"""
from __future__ import annotations

import random
from enum import IntEnum
from typing import Dict, List, Optional, Tuple

from .card import Card, Deck
from .hand_evaluator import evaluate_best, rank_to_name
from .player import Player, PlayerSeat
from .pot import PotManager


# ---------- 行动类型常量 ----------

class Action(IntEnum):
    """玩家行动类型枚举。"""

    FOLD = 1      # 弃牌
    CHECK = 2     # 让牌（本轮无人加注时可用）
    CALL = 3      # 跟注到当前最高注
    RAISE = 4     # 加注到指定金额
    ALL_IN = 5    # 全下全部剩余筹码


# ---------- 游戏阶段 ----------

class GameState(IntEnum):
    """游戏状态枚举，用于状态机流转。"""

    WAITING = 0     # 等待开局（人数不足或上一局结束）
    PREFLOP = 1     # 翻牌前下注轮（已发底牌）
    FLOP = 2        # 翻牌下注轮（已发3张公共牌）
    TURN = 3        # 转牌下注轮（已发第4张公共牌）
    RIVER = 4       # 河牌下注轮（已发第5张公共牌）
    SHOWDOWN = 5    # 摊牌
    HAND_OVER = 6   # 本局结束，准备下一局


# 默认规则常量，集中管理便于调整
DEFAULT_STARTING_CHIPS: int = 1000   # 初始筹码
DEFAULT_SMALL_BLIND: int = 10        # 小盲注
DEFAULT_BIG_BLIND: int = 20          # 大盲注
MIN_PLAYERS: int = 2                 # 最少开局人数
MAX_PLAYERS: int = 10                # 最多同桌人数


def _fmt_chips(amount: int) -> str:
    """将筹码数值格式化为带货币单位的显示文本。

    Args:
        amount: 筹码数值（整数）。

    Returns:
        形如 "$50" 的字符串。美元符号紧贴数值是国际惯例写法，
        符号本身即充当单位与数值之间的分隔，保持界面文本紧凑。
        全项目筹码日志统一经此格式化，保证单位风格一致。
    """
    return f"${amount}"


class GameError(Exception):
    """游戏规则违规异常，携带可读的错误信息供 UI 展示。"""


class TexasHoldemGame:
    """德州扑克单桌游戏控制器。

    服务器持有一个本类实例，管理本桌全部玩家与流程。
    所有状态变更方法均同步执行，调用方（服务器）负责加锁与消息广播。

    Attributes:
        seats: 玩家座位表。
        deck: 牌堆。
        pots: 底池管理器。
        community_cards: 公共牌列表。
        state: 当前游戏状态。
        dealer_pos: 庄家按钮位置（座位索引）。
        current_pos: 当前行动玩家位置。
        current_bet: 本轮最高投注额。
        min_raise: 最小加注增量。
        small_blind / big_blind: 盲注金额。
        hand_number: 当前是第几局。
    """

    def __init__(
        self,
        small_blind: int = DEFAULT_SMALL_BLIND,
        big_blind: int = DEFAULT_BIG_BLIND,
        rng: Optional[random.Random] = None,
    ) -> None:
        """初始化一桌游戏。

        Args:
            small_blind: 小盲注金额。
            big_blind: 大盲注金额。
            rng: 可选随机源，测试时注入固定种子可复现发牌。
        """
        if small_blind <= 0 or big_blind <= 0:
            raise ValueError("盲注金额必须为正")
        if small_blind >= big_blind:
            raise ValueError("大盲注必须大于小盲注")

        self.small_blind: int = small_blind
        self.big_blind: int = big_blind
        self._rng: random.Random = rng if rng is not None else random.Random()

        self.seats: PlayerSeat = PlayerSeat()
        self.deck: Deck = Deck(rng=self._rng)
        self.pots: PotManager = PotManager()

        self.community_cards: List[Card] = []
        self.state: GameState = GameState.WAITING

        # 庄家位置，每局结束后顺时针移动；初始 -1 表示尚未指定，
        # 首局 start_hand 时通过 _next_pos_with_chips 推进到 0
        self.dealer_pos: int = -1
        self.current_pos: int = 0         # 当前行动玩家位置
        self.current_bet: int = 0         # 本轮最高投注
        self.min_raise: int = big_blind   # 最小加注增量，初始等于大盲注

        self.hand_number: int = 0
        # 本局日志，记录每个关键事件文本，供 UI 回显
        self.log: List[str] = []
        # 本局已移除（掉线）但仍投入过筹码的玩家列表：
        # 掉线玩家从座位表移除后，其投入需在结算分层时以"额外贡献者"参与，
        # 否则底池重建会丢失这些筹码（见 PotManager.build_side_pots 注释）
        self._removed_players: List[Player] = []
        # 本局最终结果摘要（如"Alice 赢得 30 筹码"），摊牌/未跟注结束时生成，
        # 供 UI 在本局结束时醒目展示
        self.last_result: str = ""
        # 摊牌是否已发生：摊牌后未弃牌玩家底牌对所有人可见，直至下一局重置
        self.showdown_revealed: bool = False

    # ---------- 玩家管理 ----------

    def add_player(self, player: Player) -> None:
        """添加玩家入座。

        Args:
            player: 要加入的玩家。

        Raises:
            GameError: 桌满或游戏进行中时抛出。
        """
        if len(self.seats) >= MAX_PLAYERS:
            raise GameError(f"桌子已满（最多 {MAX_PLAYERS} 人）")
        if self.state not in (GameState.WAITING, GameState.HAND_OVER):
            raise GameError("游戏进行中，暂无法加入")
        self.seats.add(player)

    def remove_player(self, player_id: int) -> Optional[Player]:
        """移除玩家。游戏进行中移除会触发该玩家弃牌，并正确推进行动权。

        实现要点：
        1. 先移除玩家，再基于更新后的座位表修正行动位置，避免索引错位。
        2. 掉线者若是当前行动者，行动权转给下一个可行动者。
        3. 掉线弃牌后若只剩一名未弃牌玩家，立即结束本局，避免流程停滞。

        Args:
            player_id: 要移除的玩家 ID。

        Returns:
            被移除的 Player，若不存在返回 None。
        """
        player = self.seats.get(player_id)
        if player is None:
            return None

        # 记录移除前的关键信息（在座位表中的位置、是否为当前行动者、是否下注阶段）
        pos = self._pos_of(player_id)
        in_betting = self.state in (
            GameState.PREFLOP, GameState.FLOP, GameState.TURN, GameState.RIVER
        )
        was_acting = self.current_pos == pos

        # 先移除玩家，保证座位表顺序正确（后续索引判断基于新列表）
        removed = self.seats.remove(player_id)

        # 【重点注释】立即记录已移除玩家：其本局投入仍需参与结算分层。
        # 必须放在任何可能触发 _end_hand_uncontested 的调用之前，否则
        # 结算时底池会丢失该玩家的筹码。
        self._removed_players.append(removed)

        if in_betting and not removed.folded:
            # 掉线玩家自动弃牌，保证流程可继续推进
            removed.fold()
            removed.folded = True
            removed.last_action = "离线弃牌"
            self._log(f"{removed.name} 掉线，自动弃牌")

            # 【重点注释】修正 current_pos：
            # 移除位置 pos 之后的玩家索引整体左移 1，因此：
            # - 掉线者是当前行动者：行动权应转给其下家。移除后原 pos+1 玩家
            #   现在位于 pos，故从 pos 开始找下一个可行动者（_next_actionable_pos
            #   从 start 的下一位起找，传 pos-1 即可覆盖 pos 位置本身）。
            # - 掉线者不是行动者但在行动者之前：行动者索引需左移 1。
            # 若不做此修正，掉线后 current_pos 会越界或指向错误玩家，导致
            # 行动权错乱甚至流程停滞。
            if was_acting:
                nxt = self._next_actionable_pos(pos - 1)
                if nxt == -1:
                    # 其余玩家全部全下，无可行动者：直接结束本轮自动跑牌
                    self._end_betting_round()
                else:
                    self.current_pos = nxt
            elif pos < self.current_pos:
                self.current_pos -= 1

            # 掉线弃牌后若只剩一名未弃牌玩家，立即结束本局（无需再行动）
            if self.state in (
                GameState.PREFLOP, GameState.FLOP, GameState.TURN, GameState.RIVER
            ) and self._remaining_players() <= 1:
                self._end_hand_uncontested()

        return removed

    def _pos_of(self, player_id: int) -> int:
        """返回玩家 ID 对应的座位索引，找不到返回 -1。"""
        for i, p in enumerate(self.seats.all()):
            if p.player_id == player_id:
                return i
        return -1

    # ---------- 开局 ----------

    def can_start_hand(self) -> bool:
        """判断是否满足开局条件：状态为 WAITING/HAND_OVER 且有筹码的玩家 >= 2。"""
        if self.state not in (GameState.WAITING, GameState.HAND_OVER):
            return False
        ready = [p for p in self.seats.all() if p.chips > 0]
        return len(ready) >= MIN_PLAYERS

    def start_hand(self) -> None:
        """开始新一局：洗牌、移动庄家、收盲注、发底牌、进入翻牌前。

        Raises:
            GameError: 不满足开局条件时抛出。
        """
        if not self.can_start_hand():
            raise GameError("当前无法开局：人数不足或游戏进行中")

        self.hand_number += 1
        self.log = []
        self.last_result = ""
        self._removed_players = []
        self.showdown_revealed = False
        self.community_cards = []
        self.pots.reset()
        self.deck.shuffle()

        # 重置所有玩家本局状态
        for p in self.seats.all():
            p.reset_for_new_hand()

        # 移动庄家按钮：找到下一个有筹码的玩家
        self.dealer_pos = self._next_pos_with_chips(self.dealer_pos)
        self._log(f"──── 第 {self.hand_number} 局开始，庄家: {self.seats.all()[self.dealer_pos].name} ────")

        # 收取盲注
        sb_pos, bb_pos = self._get_blind_positions()
        self._post_blind(sb_pos, self.small_blind, "小盲注")
        self._post_blind(bb_pos, self.big_blind, "大盲注")

        # 设置本轮投注基线：大盲注即当前最高注
        self.current_bet = self.big_blind
        self.min_raise = self.big_blind

        # 发底牌：每人 2 张，按座位顺序
        for p in self.seats.all():
            if p.chips > 0 or p.current_bet > 0:
                p.assign_hole_cards(self.deck.deal_many(2))

        # 翻牌前首个行动者：大盲注之后第一个可行动玩家
        self.current_pos = self._next_actionable_pos(bb_pos)
        self.state = GameState.PREFLOP

    def _post_blind(self, pos: int, amount: int, label: str) -> None:
        """收取盲注。

        Args:
            pos: 玩家座位索引。
            amount: 盲注金额。
            label: 盲注名称（用于日志）。
        """
        player = self.seats.all()[pos]
        # 实际投入可能小于盲注（筹码不足时全下）
        actual = player.bet(amount)
        self.pots.collect(actual)
        self._log(f"{player.name} 下 {label} {_fmt_chips(actual)}")

    def _get_blind_positions(self) -> Tuple[int, int]:
        """计算小盲注与大盲注位置。

        Heads-up（2人）时庄家为小盲注；3人及以上时庄家下家为小盲注，再下家为大盲注。

        Returns:
            (小盲注位置, 大盲注位置)。
        """
        # 统计有筹码的玩家数，用于判断是否为 heads-up
        ready = [i for i, p in enumerate(self.seats.all()) if p.chips > 0]
        n = len(ready)
        if n == 2:
            # heads-up: 庄家即小盲
            sb = self.dealer_pos
            bb = self._next_pos_with_chips(sb)
        else:
            sb = self._next_pos_with_chips(self.dealer_pos)
            bb = self._next_pos_with_chips(sb)
        return sb, bb

    def _next_pos_with_chips(self, start: int) -> int:
        """从 start 的下一位起（不含 start 本身）找下一个有筹码的玩家位置。

        采用"不含起点"的语义，便于顺时针推进庄家与盲注位置：
        - 庄家轮转：传入当前庄家位置，返回下一位有筹码者。
        - 盲注推进：大盲注 = 下一位(小盲注)。

        Args:
            start: 起始位置（不含）。

        Returns:
            下一个 chips>0 的玩家位置。

        Raises:
            GameError: 没有任何其他玩家有筹码时抛出。
        """
        n = len(self.seats)
        # offset 从 1 开始，跳过 start 本身，实现"下一位"语义
        for offset in range(1, n + 1):
            pos = (start + offset) % n
            if self.seats.all()[pos].chips > 0:
                return pos
        raise GameError("没有其他玩家有筹码")

    def _next_actionable_pos(self, start: int) -> int:
        """从 start 的下一位起找下一个可行动（未弃牌、未全下）的玩家位置。

        Returns:
            下一个可行动玩家位置，找不到返回 -1。
        """
        n = len(self.seats)
        for offset in range(1, n + 1):
            pos = (start + offset) % n
            player = self.seats.all()[pos]
            if player.is_active:
                return pos
        return -1

    # ---------- 玩家行动 ----------

    def player_action(self, player_id: int, action: Action, amount: int = 0) -> None:
        """处理玩家行动。

        所有规则校验在此完成，校验失败抛出 GameError，由调用方转为错误消息回显。

        Args:
            player_id: 行动玩家 ID。
            action: 行动类型。
            amount: 加注目标金额（仅 RAISE 使用，表示"加注到 amount"）。

        Raises:
            GameError: 不是该玩家回合、行动非法、金额不合法等。
        """
        # 【回合校验】必须是当前行动玩家
        if self._pos_of(player_id) != self.current_pos:
            raise GameError("现在不是你的回合")
        if self.state not in (GameState.PREFLOP, GameState.FLOP, GameState.TURN, GameState.RIVER):
            raise GameError("当前不是下注阶段")

        player = self.seats.get(player_id)
        if player is None:
            raise GameError("玩家不存在")
        if not player.is_active:
            raise GameError("你已无法行动（已弃牌或全下）")

        # 分发到具体行动处理
        if action == Action.FOLD:
            self._do_fold(player)
        elif action == Action.CHECK:
            self._do_check(player)
        elif action == Action.CALL:
            self._do_call(player)
        elif action == Action.RAISE:
            self._do_raise(player, amount)
        elif action == Action.ALL_IN:
            self._do_all_in(player)
        else:
            raise GameError(f"未知行动类型: {action}")

        # 行动后检查是否只剩一名未弃牌玩家 → 直接结束
        if self._remaining_players() <= 1:
            self._end_hand_uncontested()
            return

        # 推进到下一个行动者或结束本轮
        self._advance_after_action()

    def _remaining_players(self) -> int:
        """返回未弃牌玩家数。"""
        return len([p for p in self.seats.all() if not p.folded])

    def _do_fold(self, player: Player) -> None:
        """处理弃牌。"""
        player.fold()
        self._log(f"{player.name} 弃牌")

    def _do_check(self, player: Player) -> None:
        """处理让牌。仅当玩家本轮投注已达当前最高注时可用。"""
        if player.current_bet < self.current_bet:
            raise GameError("当前有人加注，无法让牌，必须跟注/加注/弃牌")
        player.mark_acted("让牌")
        self._log(f"{player.name} 让牌")

    def _do_call(self, player: Player) -> None:
        """处理跟注：补齐到当前最高注。"""
        need = self.current_bet - player.current_bet
        if need <= 0:
            # 已达最高注，等价于让牌
            player.mark_acted("让牌")
            self._log(f"{player.name} 让牌")
            return
        actual = player.bet(need)
        self.pots.collect(actual)
        # 若跟注金额不足（全下），描述为全下
        if player.all_in:
            player.mark_acted(f"全下 {actual}")
            self._log(f"{player.name} 全下 {_fmt_chips(actual)}")
        else:
            player.mark_acted(f"跟注 {actual}")
            self._log(f"{player.name} 跟注 {_fmt_chips(actual)}")

    def _do_raise(self, player: Player, raise_to: int) -> None:
        """处理加注到指定金额。

        Args:
            player: 加注玩家。
            raise_to: 加注后的本轮总投注额（不是增量）。

        Raises:
            GameError: 加注金额不合法时抛出。
        """
        if raise_to <= self.current_bet:
            raise GameError(f"加注金额必须高于当前最高注 {self.current_bet}")
        # 玩家需补足的筹码 = 目标金额 - 已下注
        need = raise_to - player.current_bet
        if need > player.chips:
            # 筹码不足时自动转为全下
            self._do_all_in(player)
            return

        # 【最小加注校验】加注增量不得小于最小加注（除非全下，已在上面处理）
        increment = raise_to - self.current_bet
        if increment < self.min_raise:
            raise GameError(
                f"加注增量 {increment} 小于最小加注 {self.min_raise}"
            )

        player.bet(need)
        self.pots.collect(need)
        old_bet = self.current_bet
        self.current_bet = player.current_bet
        # 更新最小加注为本次加注增量
        self.min_raise = increment
        player.mark_acted(f"加注到 {self.current_bet}")
        self._log(f"{player.name} 加注到 {_fmt_chips(self.current_bet)}")

        # 【重新开启行动】加注后，其他可行动玩家需要再次行动
        for p in self.seats.all():
            if p is not player and p.is_active:
                p.acted = False

    def _do_all_in(self, player: Player) -> None:
        """处理全下：投入全部剩余筹码。"""
        if player.chips <= 0:
            raise GameError("没有筹码可全下")
        # 全下金额 = 当前筹码（投入后 current_bet 增加 chips）
        all_in_total = player.current_bet + player.chips
        # 计算相对当前最高注的增量
        increment = all_in_total - self.current_bet
        actual = player.bet(player.chips)
        self.pots.collect(actual)

        if increment > 0:
            # 全下构成加注：判断是否达到最小加注
            if increment >= self.min_raise:
                # 完整加注：更新最高注与最小加注，重开行动
                self.current_bet = player.current_bet
                self.min_raise = increment
                for p in self.seats.all():
                    if p is not player and p.is_active:
                        p.acted = False
            else:
                # 全下金额不足最小加注：仍更新最高注但不重开行动
                if player.current_bet > self.current_bet:
                    self.current_bet = player.current_bet
        player.mark_acted(f"全下 {actual}")
        self._log(f"{player.name} 全下 {_fmt_chips(actual)}（总投注 {_fmt_chips(player.current_bet)}）")

    def _advance_after_action(self) -> None:
        """一次行动完成后，推进流程：找到下一个行动者或结束本轮。"""
        # 若所有可行动玩家都已行动且投注对齐，本轮结束
        if self._is_round_complete():
            self._end_betting_round()
            return

        # 找下一个未行动的可行动玩家
        nxt = self._next_actionable_pos(self.current_pos)
        if nxt == -1:
            # 没有可行动玩家（可能全员全下），结束本轮
            self._end_betting_round()
            return
        self.current_pos = nxt

    def _is_round_complete(self) -> bool:
        """判断本轮下注是否结束。

        结束条件：所有可行动玩家均已行动 且 其 current_bet 均等于本轮最高注。
        大盲注优先权由 acted 标记自然处理：BB 在翻牌前即使投注对齐也 acted=False，
        必须等其主动 check/raise 后才算结束。
        """
        actionable = [p for p in self.seats.all() if p.is_active]
        if not actionable:
            return True
        for p in actionable:
            if not p.acted:
                return False
            if p.current_bet != self.current_bet:
                return False
        return True

    # ---------- 轮次切换 ----------

    def _end_betting_round(self) -> None:
        """结束当前下注轮：收集本轮投注到累计，重置本轮状态，进入下一阶段。"""
        # 本轮投注已通过 collect 实时入池；这里只需重置玩家本轮投注与行动标记
        for p in self.seats.all():
            p.reset_for_new_round()
        self.current_bet = 0
        self.min_raise = self.big_blind

        # 根据当前状态进入下一阶段
        if self.state == GameState.PREFLOP:
            self._deal_community(3, "翻牌")
            self.state = GameState.FLOP
        elif self.state == GameState.FLOP:
            self._deal_community(1, "转牌")
            self.state = GameState.TURN
        elif self.state == GameState.TURN:
            self._deal_community(1, "河牌")
            self.state = GameState.RIVER
        elif self.state == GameState.RIVER:
            self._start_showdown()
            return

        # 若所有人都已全下（无可行动玩家），自动跑完剩余公共牌
        if not [p for p in self.seats.all() if p.is_active]:
            self._end_betting_round()
            return

        # 翻牌后首个行动者：庄家之后第一个可行动玩家
        self.current_pos = self._next_actionable_pos(self.dealer_pos)
        if self.current_pos == -1:
            self._end_betting_round()

    def _deal_community(self, count: int, label: str) -> None:
        """发出公共牌。

        Args:
            count: 发牌张数。
            label: 阶段名称（用于日志）。
        """
        cards = self.deck.deal_many(count)
        self.community_cards.extend(cards)
        display = " ".join(c.display() for c in cards)
        self._log(f"{label}: {display}")

    # ---------- 摊牌与结算 ----------

    def _start_showdown(self) -> None:
        """进入摊牌阶段：评估所有未弃牌玩家手牌并分配底池。"""
        self.state = GameState.SHOWDOWN
        # 标记摊牌已发生：使未弃牌玩家底牌对所有人可见
        self.showdown_revealed = True
        # 构建分层底池（主池+边池）；掉线移除玩家的投入作为额外贡献者参与
        self.pots.build_side_pots(self.seats.all(), self._removed_players)

        # 评估每位未弃牌玩家的最佳手牌
        hand_ranks: Dict[int, tuple] = {}
        eval_info: List[str] = []
        for p in self.seats.all():
            if p.folded:
                continue
            cards = p.hole_cards + self.community_cards
            rank, best5 = evaluate_best(cards)
            hand_ranks[p.player_id] = rank
            eval_info.append(
                f"{p.name}: {' '.join(c.display() for c in p.hole_cards)} → "
                f"{rank_to_name(rank)}（{' '.join(c.display() for c in best5)}）"
            )
            self._log(
                f"{p.name} 摊牌 {' '.join(c.display() for c in p.hole_cards)} = {rank_to_name(rank)}"
            )

        # 分配奖金
        payouts = self.pots.distribute(hand_ranks)
        for pid, amount in payouts.items():
            player = self.seats.get(pid)
            if player:
                player.chips += amount
                self._log(f"{player.name} 赢得 {_fmt_chips(amount)}")

        # 生成本局结果摘要，供 UI 醒目展示
        winner_lines = [
            f"{self.seats.get(pid).name} 赢得 {_fmt_chips(amount)}"
            for pid, amount in payouts.items()
            if amount > 0 and self.seats.get(pid) is not None
        ]
        # 无赢家（如所有奖金为 0）时给出兜底文案
        self.last_result = "，".join(winner_lines) if winner_lines else "本局无人赢得底池"

        self.state = GameState.HAND_OVER

    def _end_hand_uncontested(self) -> None:
        """其他人都弃牌，仅剩一人时直接把底池判给该玩家（不摊牌）。"""
        # 构建底池（无摊牌信息，distribute 会平分给唯一合格者）；
        # 掉线移除玩家的投入作为额外贡献者参与，避免筹码丢失
        self.pots.build_side_pots(self.seats.all(), self._removed_players)
        # 【重点注释】兜底：全员离席（座位表为空）时没有赢家，
        # 本局直接作废回到等待状态，避免对空列表迭代崩溃。
        winner = next((p for p in self.seats.all() if not p.folded), None)
        if winner is None:
            self.state = GameState.WAITING
            return
        payouts = self.pots.distribute({})
        for pid, amount in payouts.items():
            p = self.seats.get(pid)
            if p:
                p.chips += amount
        self._log(f"{winner.name} 未被跟注，赢得 {_fmt_chips(self.pots.total)}")
        # 生成本局结果摘要（未摊牌场景）
        self.last_result = f"{winner.name} 赢得 {_fmt_chips(self.pots.total)}（无人跟注）"
        self.state = GameState.HAND_OVER

    # ---------- 状态查询 ----------

    def get_state_snapshot(self, viewer_id: Optional[int] = None) -> dict:
        """生成供广播/客户端渲染的状态快照。

        底牌可见性控制：仅 viewer 本人可见自己的底牌；摊牌阶段所有未弃牌玩家底牌可见。

        Args:
            viewer_id: 查看者玩家 ID，None 表示观察者（看不到任何底牌）。

        Returns:
            状态字典，包含阶段、玩家列表、公共牌、底池、当前行动者等。
        """
        # 底牌可见性：本人始终可见；摊牌发生后未弃牌玩家对所有人可见
        players_data = []
        for p in self.seats.all():
            # 决定是否对该查看者暴露底牌
            show = (p.player_id == viewer_id) or (self.showdown_revealed and not p.folded)
            players_data.append(p.to_dict(include_hole_cards=show))

        return {
            "state": int(self.state),
            "state_name": self.state.name,
            "hand_number": self.hand_number,
            "community_cards": [c.to_dict() for c in self.community_cards],
            "pot": self.pots.to_dict(),
            "current_bet": self.current_bet,
            "min_raise": self.min_raise,
            # 庄家位置：未开局时为 -1，避免客户端越界
            "dealer_pos": self.dealer_pos if 0 <= self.dealer_pos < len(self.seats) else -1,
            "current_pos": self.current_pos,
            "current_player_id": (
                self.seats.all()[self.current_pos].player_id
                if 0 <= self.current_pos < len(self.seats) and self.state in
                (GameState.PREFLOP, GameState.FLOP, GameState.TURN, GameState.RIVER)
                else None
            ),
            "players": players_data,
            "small_blind": self.small_blind,
            "big_blind": self.big_blind,
            "log": list(self.log[-20:]),  # 仅返回最近 20 条日志
        }

    def get_player_options(self, player_id: int) -> dict:
        """获取某玩家当前可执行的行动及参数范围，供 UI 提示。

        Args:
            player_id: 玩家 ID。

        Returns:
            包含可行动列表、跟注金额、最小/最大加注的字典。
        """
        player = self.seats.get(player_id)
        if player is None:
            return {"can_act": False, "reason": "玩家不存在"}
        if self._pos_of(player_id) != self.current_pos:
            return {"can_act": False, "reason": "不是你的回合"}
        if not player.is_active:
            return {"can_act": False, "reason": "已弃牌或全下"}
        if self.state not in (GameState.PREFLOP, GameState.FLOP, GameState.TURN, GameState.RIVER):
            return {"can_act": False, "reason": "当前非下注阶段"}

        options: List[str] = ["fold"]
        call_need = self.current_bet - player.current_bet
        # 让牌：仅当已投注达到最高注
        if call_need <= 0:
            options.append("check")
        else:
            options.append("call")
        # 加注：仅当玩家筹码足以超过当前最高注
        min_raise_to = self.current_bet + self.min_raise
        max_raise_to = player.current_bet + player.chips  # 全下即最大加注
        if max_raise_to > self.current_bet:
            options.append("raise")
            options.append("all_in")
        elif player.chips > 0:
            # 筹码不足以合法加注但仍可全下
            options.append("all_in")

        return {
            "can_act": True,
            "options": options,
            "call_amount": max(call_need, 0),
            "min_raise_to": min_raise_to,
            "max_raise_to": max_raise_to,
            "current_bet": self.current_bet,
            "player_current_bet": player.current_bet,
            "player_chips": player.chips,
        }

    # ---------- 房间重置 ----------

    def reset_room(self, starting_chips: int) -> None:
        """重置整个房间到"刚创建"的初始状态（由房主触发）。

        具体行为：
        - 对局计数 hand_number 清零，恢复初始设定值；
        - 每位在座玩家筹码恢复为房间创建时的初始配置值 starting_chips；
        - 清空本局相关数据（公共牌、底池、投注、手牌、行动标志、摊牌信息）；
        - 游戏状态回到 WAITING，庄家/行动位复位。

        【重点注释】不影响房间基本配置：
        - 玩家列表、昵称、玩家 ID 全部保留（不新增/移除任何玩家）；
        - 盲注、初始筹码等规则常量（self.small_blind / big_blind）不变。

        适用于任何游戏状态（等待中 / 对局进行中 / 本局结束 / 玩家离线后），
        重置后由房主重新开局即可继续对局。

        Args:
            starting_chips: 房间创建时的初始筹码值，用于恢复每位玩家。

        Raises:
            ValueError: starting_chips 为负时抛出。
        """
        if starting_chips < 0:
            raise ValueError(f"初始筹码不能为负: {starting_chips}")

        # 1. 对局计数清零：恢复至初始设定值 0
        self.hand_number = 0
        # 2. 清空本局数据：日志、结果摘要、移除记录、摊牌可见性
        self.log = []
        self.last_result = ""
        self._removed_players = []
        self.showdown_revealed = False
        # 3. 清空牌桌数据：公共牌、底池、下注状态
        self.community_cards = []
        self.pots.reset()
        self.state = GameState.WAITING
        self.current_bet = 0
        # 【重点注释】最小加注增量复位为大盲注（对应开局前的初始值）
        self.min_raise = self.big_blind
        # 庄家与行动位复位：未开局时庄家为 -1（与 __init__ 初始一致）
        self.dealer_pos = -1
        self.current_pos = 0
        # 4. 每位玩家：筹码恢复初始值，手牌/投注/行动标志清零
        for p in self.seats.all():
            # 直接覆盖 chips（reset_for_new_hand 不重置筹码，需单独赋值）
            p.chips = starting_chips
            p.reset_for_new_hand()
        # 5. 写一条重置日志，供服务器广播给所有客户端
        self._log("──── 房间已重置：对局数清零，所有玩家筹码恢复初始值 ────")

    def _log(self, message: str) -> None:
        """记录一条游戏日志。"""
        self.log.append(message)

    @property
    def is_hand_over(self) -> bool:
        """本局是否结束。"""
        return self.state == GameState.HAND_OVER

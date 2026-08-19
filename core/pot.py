"""底池与边池管理模块。

负责维护主底池与因玩家全下产生的边池，并在摊牌后按牌力分配奖金给各池的合格赢家。

核心概念：
- 主底池 (main pot)：所有玩家投入的筹码汇总。
- 边池 (side pot)：当某玩家全下金额小于其他玩家时，超出部分形成独立边池，
  只有投入达到该边池门槛的玩家才有资格争夺。
- 本实现采用"按 total_bet 分层"的算法：将所有未弃牌玩家按累计投入排序，
  逐层切分底池，每层形成一个 Pot，并记录合格参与者。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .player import Player


class Pot:
    """单个底池（主池或边池）。

    Attributes:
        amount: 该池累积的总筹码。
        eligible_ids: 有资格争夺该池的玩家 ID 列表。
    """

    def __init__(self, amount: int = 0, eligible_ids: Optional[List[int]] = None) -> None:
        """初始化底池。

        Args:
            amount: 初始金额。
            eligible_ids: 合格玩家 ID 列表。
        """
        self.amount: int = amount
        # 使用 list 保持顺序，eligible_ids 不可为 None 时赋空列表
        self.eligible_ids: List[int] = list(eligible_ids) if eligible_ids else []

    def add(self, amount: int) -> None:
        """向池中追加筹码。

        Args:
            amount: 追加金额，必须非负。

        Raises:
            ValueError: 金额为负时抛出。
        """
        if amount < 0:
            raise ValueError(f"底池追加金额不能为负: {amount}")
        self.amount += amount

    def to_dict(self) -> dict:
        """序列化为字典。"""
        return {"amount": self.amount, "eligible_ids": list(self.eligible_ids)}


class PotManager:
    """底池管理器：构建分层底池并分配奖金。

    工作流程：
        1. collect_bets() 收集所有玩家本轮投注到主池。
       （由 Game 在每轮结束时调用 bet_chip 移交）
        2. build_side_pots() 在摊牌前根据各玩家 total_bet 切分边池。
        3. distribute() 根据各玩家牌力分配每个池的奖金。
    """

    def __init__(self) -> None:
        """初始化空的底池管理器。"""
        # 主池累计金额
        self.main_amount: int = 0
        # 已构建的分层底池列表（摊牌时填充）
        self.pots: List[Pot] = []

    def collect(self, amount: int) -> None:
        """向主池追加筹码。

        Args:
            amount: 追加金额，必须非负。
        """
        if amount < 0:
            raise ValueError(f"主池追加金额不能为负: {amount}")
        self.main_amount += amount

    def reset(self) -> None:
        """新局开始时清空所有底池。"""
        self.main_amount = 0
        self.pots = []

    @property
    def total(self) -> int:
        """返回底池总额（主池+所有边池，未构建边池时即主池金额）。"""
        if self.pots:
            return sum(p.amount for p in self.pots)
        return self.main_amount

    def build_side_pots(
        self,
        players: List[Player],
        extra_contributors: Optional[List[Player]] = None,
    ) -> List[Pot]:
        """根据玩家累计投注构建分层底池（主池+边池）。

        算法说明：
        - 取所有 total_bet > 0 的玩家（含已弃牌者，他们的投入进入池中但不合格赢）。
        - 按 total_bet 升序排序，逐层处理：每层门槛 = 当前最小 total_bet，
          所有 total_bet >= 门槛的玩家向该层贡献 (门槛 - 上一层门槛) 筹码，
          且这些玩家中"未弃牌"的成为该层合格赢家候选。
        - 已处理玩家从待处理列表中移除。

        【重点注释】extra_contributors 参数：
        掉线玩家从座位表移除后，其本局投入（total_bet）已进入主池，但无法再
        通过 players 列表参与分层。若不处理，这些筹码会在重建分层时被丢弃，
        导致底池总额减少、筹码凭空丢失。因此允许额外传入已移除玩家的引用，
        让其投入按原层级参与分配；其 folded 已为 True，自动失去赢取资格。

        Args:
            players: 当前在桌的玩家列表。
            extra_contributors: 本局已移除（掉线）但仍有投入的玩家列表，可空。

        Returns:
            构建好的 Pot 列表（至少包含主池）。
        """
        # 清空旧分层
        self.pots = []

        # 收集所有有投入的玩家（含额外贡献者），按 total_bet 升序
        contributors = [p for p in players if p.total_bet > 0]
        if extra_contributors:
            contributors.extend(p for p in extra_contributors if p.total_bet > 0)
        contributors.sort(key=lambda p: p.total_bet)

        # 上一层的门槛，初始为 0
        previous_level = 0
        remaining = list(contributors)

        # 逐层切分，直到所有玩家投入都被分配
        while remaining:
            # 当前层门槛 = 剩余玩家中最小的 total_bet
            current_level = remaining[0].total_bet
            # 该层每个玩家贡献的差额
            layer_delta = current_level - previous_level

            # 计算该层总筹码：所有 total_bet >= current_level 的玩家各贡献 layer_delta
            layer_amount = 0
            eligible_ids: List[int] = []
            for p in contributors:
                if p.total_bet >= current_level:
                    # 该玩家在此层贡献 layer_delta
                    layer_amount += layer_delta
                    # 仅未弃牌玩家有资格争夺此池
                    if not p.folded:
                        eligible_ids.append(p.player_id)

            # 仅当该层有筹码时才创建池（避免空池）
            if layer_amount > 0:
                self.pots.append(Pot(amount=layer_amount, eligible_ids=eligible_ids))

            # 移除已处理到顶的玩家（total_bet == current_level 的）
            remaining = [p for p in remaining if p.total_bet > current_level]
            previous_level = current_level

        # 若没有任何投入（理论上不会发生），保留一个空主池以避免后续除零
        if not self.pots:
            self.pots.append(Pot(amount=0, eligible_ids=[]))
            return self.pots

        # 【重点注释】合并"无人认领"的死池，保证筹码守恒：
        # 掉线玩家若投入超过其他玩家，超出部分所在的层只有其一人贡献，
        # 而该玩家已弃牌（掉线自动弃牌），该层没有任何合格赢家，形成死池。
        # 若直接跳过这些筹码，会在 distribute 时凭空丢失（底池缩水）。
        # 处理方式：将死池金额并入主池（列表首项 = 最低层，
        # 所有未弃牌玩家均合格），由在场玩家共同争夺。
        dead_amount = 0
        live_pots: List[Pot] = []
        for pot in self.pots:
            if pot.amount > 0 and not pot.eligible_ids:
                # 该层无人有资格赢取：累计为死池金额
                dead_amount += pot.amount
            else:
                live_pots.append(pot)
        if dead_amount > 0 and live_pots:
            # 主池为最低层（首项），把死池筹码并入其中
            live_pots[0].amount += dead_amount
            self.pots = live_pots

        return self.pots

    def distribute(self, hand_ranks: Dict[int, tuple]) -> Dict[int, int]:
        """按牌力分配每个底池的奖金给赢家。

        对每个池：在合格玩家中找最大牌力，所有并列第一的玩家平分该池。
        除法向下取整的余数按座位顺序分给前列玩家（处理不能整除的情况）。

        Args:
            hand_ranks: 玩家 ID -> 牌力元组的映射。已弃牌玩家不应出现在此映射中。

        Returns:
            玩家 ID -> 赢得筹码数的映射。
        """
        payouts: Dict[int, int] = {}

        for pot in self.pots:
            if pot.amount <= 0 or not pot.eligible_ids:
                continue

            # 收集合格玩家中参与摊牌的牌力（部分玩家可能未到摊牌即赢，则直接平分）
            # hand_ranks 中存在的才参与比较；若均不在（所有人都未摊牌的极端情况），
            # 则按合格列表平分。
            contenders = [
                (pid, hand_ranks[pid]) for pid in pot.eligible_ids if pid in hand_ranks
            ]

            if contenders:
                # 找最大牌力
                best_rank = max(rank for _, rank in contenders)
                # 所有达到最大牌力的玩家平分
                winners = [pid for pid, rank in contenders if rank == best_rank]
            else:
                # 无摊牌信息（如所有人弃牌只剩一人）则合格玩家平分
                winners = list(pot.eligible_ids)

            if not winners:
                continue

            # 【整除处理】不能整除的余数按顺序分给前几位赢家，避免筹码丢失
            share = pot.amount // len(winners)
            remainder = pot.amount - share * len(winners)
            for i, pid in enumerate(winners):
                extra = 1 if i < remainder else 0
                payouts[pid] = payouts.get(pid, 0) + share + extra

        return payouts

    def to_dict(self) -> dict:
        """序列化为字典。"""
        if self.pots:
            return {"pots": [p.to_dict() for p in self.pots], "total": self.total}
        return {"pots": [Pot(self.main_amount).to_dict()], "total": self.main_amount}

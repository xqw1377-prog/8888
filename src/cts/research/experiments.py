"""已登记的有限实验。E1 的均线只在本模块，不改 T1 默认信号。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from cts.errors import CtsError
from cts.ledger.models import Side
from cts.money import D


@dataclass(frozen=True)
class ExperimentRow:
    experiment_id: str
    change: str
    status: str
    reason: str


class ExperimentRegistry:
    def __init__(self, limit: int = 12) -> None:
        self.limit = limit
        self.rows: list[ExperimentRow] = []

    def register(self, experiment_id: str, change: str, status: str, reason: str) -> ExperimentRow:
        if any(row.experiment_id == experiment_id for row in self.rows):
            raise CtsError("同一实验编号不能重复登记", reason_code="IDEMPOTENCY_CONFLICT")
        if len(self.rows) >= self.limit:
            raise CtsError("试验配置超过 12 个上限", reason_code="EXPERIMENT_BUDGET")
        row = ExperimentRow(experiment_id, change, status, reason)
        self.rows.append(row)
        return row


def ema200_status(closes: list[Decimal]) -> ExperimentRow:
    if len(closes) < 1000:
        return ExperimentRow("E1", "EMA200 方向过滤", "NOT_TESTABLE", "需要至少 1000 根已收盘预热，当前样本不足")
    return ExperimentRow("E1", "EMA200 方向过滤", "RAN", "过滤已计算，不能单独当作升级依据")


def e2_status() -> ExperimentRow:
    return ExperimentRow(
        "E2",
        "TTL 60 秒改为 300 秒",
        "NOT_TESTABLE",
        "缺少信号后的报价、盘口和延迟记录，不能只改下一根开盘价",
    )


def breakeven_stop(
    *,
    side: Side,
    entry: Decimal,
    initial_stop: Decimal,
    mark: Decimal,
    cost_per_unit: Decimal,
    current_stop: Decimal,
) -> Decimal:
    distance = abs(D(entry) - D(initial_stop))
    current = D(current_stop)
    if side == Side.LONG:
        if D(mark) < D(entry) + distance * 2:
            return current
        target = D(entry) + D(cost_per_unit)
        return max(current, target)
    if D(mark) > D(entry) - distance * 2:
        return current
    target = D(entry) - D(cost_per_unit)
    return min(current, target)

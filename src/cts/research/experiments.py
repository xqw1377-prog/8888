"""已登记的有限实验。E1 的均线只在本模块，不改 T1 默认信号。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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


def ema200(closes: list[Decimal]) -> Optional[Decimal]:
    if len(closes) < 1000:
        return None
    weight = D(2) / D(201)
    value = sum(closes[:200], D("0")) / D(200)
    for price in closes[200:]:
        value = price * weight + value * (D(1) - weight)
    return value


def ema200_status(closes: list[Decimal]) -> ExperimentRow:
    value = ema200(closes)
    if value is None:
        return ExperimentRow("E1", "EMA200 方向过滤", "NOT_TESTABLE", "需要至少 1000 根已收盘预热，当前样本不足")
    return ExperimentRow("E1", "EMA200 方向过滤", "RAN", f"EMA200={format(value, 'f')}，过滤结果不能单独当作升级依据")


def e1_allows(side: Side, close: Decimal, ema: Decimal) -> bool:
    if side == Side.LONG:
        return D(close) > D(ema)
    return D(close) < D(ema)


@dataclass(frozen=True)
class BookQuote:
    event_time: datetime
    available_at: datetime
    bid: Decimal
    ask: Decimal


def e2_status() -> ExperimentRow:
    return ExperimentRow(
        "E2",
        "TTL 60 秒改为 300 秒",
        "NOT_TESTABLE",
        "缺少信号后的报价、盘口和延迟记录，不能只改下一根开盘价",
    )


def evaluate_e2(signal_available: datetime, quotes: list[BookQuote]) -> ExperimentRow:
    if not quotes:
        return e2_status()

    def fillable(ttl_ms: int) -> bool:
        limit = signal_available + timedelta(milliseconds=ttl_ms)
        for quote in quotes:
            if quote.available_at < signal_available or quote.available_at > limit:
                continue
            mid = (D(quote.bid) + D(quote.ask)) / 2
            if mid <= 0:
                continue
            spread_bps = (D(quote.ask) - D(quote.bid)) / mid * D(10000)
            if spread_bps <= D(10):
                return True
        return False

    within_60 = fillable(60_000)
    within_300 = fillable(300_000)
    return ExperimentRow("E2", "TTL 60 秒改为 300 秒", "RAN", f"60秒可成交={within_60};300秒可成交={within_300}")


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


def run_e3_stops(
    *,
    side: Side,
    entry: Decimal,
    initial_stop: Decimal,
    marks: list[Decimal],
    cost_per_unit: Decimal,
) -> list[Decimal]:
    stop = D(initial_stop)
    path: list[Decimal] = []
    for mark in marks:
        stop = breakeven_stop(
            side=side,
            entry=entry,
            initial_stop=initial_stop,
            mark=mark,
            cost_per_unit=cost_per_unit,
            current_stop=stop,
        )
        path.append(stop)
    return path

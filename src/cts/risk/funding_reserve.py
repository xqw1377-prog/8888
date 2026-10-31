"""未来 24 小时资金费预留：取已登记压力情景集合的最大值。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from cts.ledger.models import Side
from cts.money import D, require_positive


@dataclass(frozen=True)
class SettlementPoint:
    at: datetime
    rate: Decimal
    mark_price: Decimal


@dataclass(frozen=True)
class FundingScenario:
    scenario_id: str
    settlements: tuple[SettlementPoint, ...]


def position_stress_value(qty: Decimal, multiplier: Decimal, mark_price: Decimal) -> Decimal:
    return abs(D(qty)) * require_positive(D(multiplier), "multiplier") * require_positive(D(mark_price), "mark_price")


def scenario_reserve(
    *,
    side: Side,
    qty: Decimal,
    multiplier: Decimal,
    settlements: Sequence[SettlementPoint],
) -> Decimal:
    direction = D("1") if side == Side.LONG else D("-1")
    total = D("0")
    for point in settlements:
        v = position_stress_value(qty, multiplier, point.mark_price)
        payment_rate = direction * D(point.rate)
        total += v * max(D("0"), payment_rate)
    return total


def funding_reserve(
    *,
    side: Side,
    qty: Decimal,
    multiplier: Decimal,
    scenarios: Sequence[FundingScenario],
) -> Decimal:
    if not scenarios:
        raise ValueError("资金费压力情景未登记，拒绝新风险")
    return max(scenario_reserve(side=side, qty=qty, multiplier=multiplier, settlements=s.settlements) for s in scenarios)

"""名义仓位取各上限最小值；不能为了用满杠杆而放大数量。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from cts.ledger.models import Side
from cts.money import D, floor_to_lot, require_positive
from cts.risk.parameters import INITIAL_MARGIN_LEVERAGE


@dataclass(frozen=True)
class SizedPosition:
    side: Side
    qty: Decimal
    notional: Decimal
    risk_amount: Decimal
    stress_loss_ratio: Decimal
    binding_constraint: str
    effective_notional_capacity: Decimal


def size_position(
    *,
    side: Side,
    equity: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    multiplier: Decimal,
    lot_size: Decimal,
    per_symbol_risk_pct: Decimal = Decimal("0.0020"),
    portfolio_risk_pct: Decimal = Decimal("0.0040"),
    used_portfolio_risk: Decimal = Decimal("0"),
    per_symbol_notional_pct: Decimal = Decimal("0.75"),
    used_symbol_notional: Decimal = Decimal("0"),
    total_abs_notional_pct: Decimal = Decimal("1.50"),
    used_abs_notional: Decimal = Decimal("0"),
    leverage: Decimal = INITIAL_MARGIN_LEVERAGE,
    remaining_margin_budget: Decimal,
    fee_rate_one_way: Decimal,
    stress_slippage: Decimal,
    funding_reserve_ratio: Decimal,
    liquidity_cap: Optional[Decimal] = None,
) -> Optional[SizedPosition]:
    equity = require_positive(D(equity), "equity")
    entry_price = require_positive(D(entry_price), "entry_price")
    stop_price = require_positive(D(stop_price), "stop_price")
    multiplier = require_positive(D(multiplier), "multiplier")
    lot_size = require_positive(D(lot_size), "lot_size")
    stop_distance = abs(entry_price - stop_price) / entry_price
    if stop_distance <= 0:
        return None
    stress = stop_distance + (D(fee_rate_one_way) * 2) + D(stress_slippage) + D(funding_reserve_ratio)
    if stress <= 0:
        return None

    risk_budget = equity * D(per_symbol_risk_pct)
    portfolio_remaining = (equity * D(portfolio_risk_pct)) - D(used_portfolio_risk)
    risk_amount = min(risk_budget, portfolio_remaining)
    if risk_amount <= 0:
        return None

    by_risk = risk_amount / stress
    by_symbol = (equity * D(per_symbol_notional_pct)) - D(used_symbol_notional)
    by_total = (equity * D(total_abs_notional_pct)) - D(used_abs_notional)
    by_margin = D(remaining_margin_budget) * D(leverage)
    caps = {
        "RISK": by_risk,
        "SYMBOL_NOTIONAL": by_symbol,
        "TOTAL_ABS_NOTIONAL": by_total,
        "MARGIN_LEVERAGE": by_margin,
    }
    if liquidity_cap is not None:
        caps["LIQUIDITY"] = D(liquidity_cap)
    if any(v <= 0 for v in caps.values()):
        return None

    binding = min(caps, key=lambda k: caps[k])
    n = caps[binding]
    qty = floor_to_lot(n / (entry_price * multiplier), lot_size)
    if qty <= 0:
        return None
    notional = qty * entry_price * multiplier

    # 取整后再验全部约束
    if notional > n:
        return None
    actual_risk = notional * stress
    if actual_risk > risk_amount:
        qty = floor_to_lot((risk_amount / stress) / (entry_price * multiplier), lot_size)
        if qty <= 0:
            return None
        notional = qty * entry_price * multiplier
        binding = "RISK"

    return SizedPosition(
        side=side,
        qty=qty,
        notional=notional,
        risk_amount=actual_risk,
        stress_loss_ratio=stress,
        binding_constraint=binding,
        effective_notional_capacity=n,
    )

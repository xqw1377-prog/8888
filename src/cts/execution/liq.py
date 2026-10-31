"""逐仓线性强平价模型。这是本地公式，不是交易所已验证实现。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from cts.ledger.models import Side
from cts.money import D


@dataclass(frozen=True)
class LiqAssessment:
    status: str
    model_price: Optional[Decimal]
    venue_price: Optional[Decimal]
    reason_code: Optional[str]
    tau: Optional[Decimal]
    buffer_ok: Optional[bool]
    evidence_layer: str = "LOCAL_INJECTION"


def model_liq_price(
    *,
    side: Side,
    avg_price: Decimal,
    qty: Decimal,
    multiplier: Decimal,
    isolated_margin: Decimal,
    mmr: Decimal,
    mm_deduction: Decimal = Decimal("0"),
    fee_rate: Decimal = Decimal("0"),
) -> Decimal:
    q = D(qty) * D(multiplier)
    if q <= 0:
        raise ValueError("qty")
    avg = D(avg_price)
    isolated = D(isolated_margin)
    deduction = D(mm_deduction)
    mmr = D(mmr)
    fee = D(fee_rate)
    if side == Side.LONG:
        numerator = isolated - avg * q + deduction
        denominator = q * (mmr - Decimal("1") + fee)
    else:
        numerator = isolated + avg * q + deduction
        denominator = q * (Decimal("1") + mmr + fee)
    if denominator == 0:
        raise ValueError("denominator")
    return numerator / denominator


def classify_liq_quote(raw: Optional[str]) -> tuple[str, Optional[Decimal]]:
    if raw is None or str(raw).strip() == "":
        return "UNKNOWN", None
    try:
        price = D(str(raw).strip())
    except Exception:
        return "UNSUPPORTED", None
    if price <= 0:
        return "OUT_OF_RANGE", None
    return "KNOWN", price


def assess_liquidation(
    *,
    side: Side,
    mark: Decimal,
    stop: Decimal,
    model_price: Optional[Decimal],
    venue_raw: Optional[str],
    buffer_b: Optional[Decimal],
    q_bound: Decimal = Decimal("0"),
    stop_ratio: Decimal = Decimal("0.20"),
    buffer_ratio: Decimal = Decimal("0.25"),
    distance_multiple: Decimal = Decimal("3"),
) -> LiqAssessment:
    status, venue_price = classify_liq_quote(venue_raw)
    if status != "KNOWN" or model_price is None or venue_price is None:
        return LiqAssessment(status, model_price, venue_price, "LIQ_PRICE_UNVERIFIED", None, False)
    mark = D(mark)
    stop = D(stop)
    if mark <= 0:
        return LiqAssessment("UNKNOWN", model_price, venue_price, "LIQ_PRICE_UNVERIFIED", None, False)
    gap_stop = abs(mark - stop)
    gap_buffer = abs(stop - model_price)
    tau = min(D(stop_ratio) * gap_stop, D(buffer_ratio) * gap_buffer)
    if D(q_bound) > tau:
        return LiqAssessment("INDETERMINATE", model_price, venue_price, "LIQ_TOLERANCE_INDETERMINATE", tau, False)
    if abs(model_price - venue_price) > tau:
        return LiqAssessment("KNOWN", model_price, venue_price, "LIQ_MODEL_MISMATCH", tau, False)
    if buffer_b is None:
        return LiqAssessment("KNOWN", model_price, venue_price, "LIQUIDATION_BUFFER_UNREGISTERED", tau, False)
    d_stop = gap_stop / mark
    d_liq = abs(mark - model_price) / mark
    gap_ratio = abs(stop - model_price) / mark
    buffer_ok = d_liq >= D(distance_multiple) * d_stop and gap_ratio >= D(buffer_b)
    if side == Side.LONG and not (model_price < stop < mark):
        buffer_ok = False
    if side == Side.SHORT and not (mark < stop < model_price):
        buffer_ok = False
    reason = None if buffer_ok else "LIQ_BUFFER_INSUFFICIENT"
    return LiqAssessment("KNOWN", model_price, venue_price, reason, tau, buffer_ok)

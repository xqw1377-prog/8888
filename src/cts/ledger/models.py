"""账本实体：复式分录、仓位子账、净值快照。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from cts.clock import require_utc
from cts.errors import LedgerImbalanceError, UnitValidationError
from cts.money import D, require_non_negative


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class AccountBucket(str, Enum):
    WALLET = "WALLET"
    ISOLATED_MARGIN = "ISOLATED_MARGIN"
    EXTERNAL = "EXTERNAL"
    REALIZED_PNL = "REALIZED_PNL"
    FEE = "FEE"
    FUNDING = "FUNDING"


class LedgerEventType(str, Enum):
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"
    MARGIN_LOCK = "MARGIN_LOCK"
    MARGIN_RELEASE = "MARGIN_RELEASE"
    FILL = "FILL"
    FEE = "FEE"
    FUNDING = "FUNDING"
    LIQUIDATION = "LIQUIDATION"
    ADL = "ADL"
    REVERSAL = "REVERSAL"


INCOME_BUCKETS = {
    AccountBucket.EXTERNAL,
    AccountBucket.REALIZED_PNL,
    AccountBucket.FEE,
    AccountBucket.FUNDING,
}


@dataclass(frozen=True)
class JournalLine:
    asset: str
    bucket: AccountBucket
    debit: Decimal
    credit: Decimal
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "debit", D(self.debit))
        object.__setattr__(self, "credit", D(self.credit))
        if self.debit < 0 or self.credit < 0:
            raise UnitValidationError("分录借贷不能为负")
        if (self.debit == 0 and self.credit == 0) or (self.debit > 0 and self.credit > 0):
            raise UnitValidationError("分录必须恰好一侧有金额")
        if not self.asset:
            raise UnitValidationError("分录资产不能为空")

    @property
    def signed(self) -> Decimal:
        return self.debit - self.credit


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    account_id: str
    venue: str
    event_type: LedgerEventType
    event_time: datetime
    received_at: datetime
    asset: str
    external_id: str
    source: str
    lines: tuple[JournalLine, ...]
    symbol: Optional[str] = None
    position_id: Optional[str] = None
    metadata: dict[str, str] = field(default_factory=dict)
    reversed_event_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", require_utc(self.event_time, "event_time"))
        object.__setattr__(self, "received_at", require_utc(self.received_at, "received_at"))
        object.__setattr__(self, "lines", tuple(self.lines))
        object.__setattr__(self, "metadata", dict(self.metadata))
        if not self.event_id or not self.account_id or not self.external_id:
            raise UnitValidationError("event_id/account_id/external_id 不能为空")
        _assert_balanced(self.lines, self.asset, self.event_type)


def _assert_balanced(lines: tuple[JournalLine, ...], asset: str, event_type: LedgerEventType) -> None:
    if not lines:
        if event_type == LedgerEventType.FILL:
            return
        raise LedgerImbalanceError("事件至少需要两条分录")
    debit = Decimal("0")
    credit = Decimal("0")
    for line in lines:
        if line.asset != asset:
            raise LedgerImbalanceError("第一阶段单事件只允许单一结算资产")
        debit += line.debit
        credit += line.credit
    if debit != credit:
        raise LedgerImbalanceError(f"借贷不平衡: debit={debit} credit={credit}")


@dataclass
class PositionState:
    account_id: str
    symbol: str
    multiplier: Decimal
    side: Optional[Side] = None
    qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    isolated_margin: Decimal = Decimal("0")
    realized_pnl_cum: Decimal = Decimal("0")
    funding_cum: Decimal = Decimal("0")
    version: int = 0

    def __post_init__(self) -> None:
        self.multiplier = D(self.multiplier)
        self.qty = D(self.qty)
        self.avg_price = D(self.avg_price)
        self.isolated_margin = D(self.isolated_margin)
        self.realized_pnl_cum = D(self.realized_pnl_cum)
        self.funding_cum = D(self.funding_cum)
        require_non_negative(self.qty, "qty")
        require_non_negative(self.isolated_margin, "isolated_margin")

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        mark = D(mark_price)
        if self.qty == 0 or self.side is None:
            return Decimal("0")
        delta = mark - self.avg_price
        signed = delta if self.side == Side.LONG else -delta
        return signed * self.qty * self.multiplier


@dataclass(frozen=True)
class EquitySnapshot:
    account_id: str
    as_of: datetime
    asset: str
    wallet: Decimal
    isolated_margin: Decimal
    reserved_budget: Decimal
    unrealized_pnl: Decimal
    equity: Decimal
    shares: Decimal
    unit_nav: Decimal
    high_water_mark: Decimal
    freshness: str
    venue_equity: Optional[Decimal] = None
    valuation_source: str = "MODEL"


@dataclass(frozen=True)
class DeltaAttribution:
    realized_pnl: Decimal
    fees: Decimal
    funding: Decimal
    margin_transfers: Decimal
    price_revaluation: Decimal
    transfers: Decimal
    unexplained_residual: Decimal
    event_ids: tuple[str, ...]
    input_version: str
    valuation_basis: str


@dataclass(frozen=True)
class ReconcileResult:
    account_id: str
    as_of: datetime
    consistent: bool
    tolerance: Decimal
    model_equity: Decimal
    venue_equity: Optional[Decimal]
    attribution: DeltaAttribution
    conclusion: str
    reason_code: Optional[str] = None

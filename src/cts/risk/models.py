"""P0 04A 风险内核实体。生产参数默认 PROPOSED；实盘 HOLD。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Optional

from cts.ledger.models import Side
from cts.money import D


class RuntimeMode(str, Enum):
    SIMULATION = "SIMULATION"
    PRODUCTION = "PRODUCTION"


class SystemState(str, Enum):
    RUNNING = "RUNNING"
    REDUCED = "REDUCED"
    RECONCILING = "RECONCILING"
    PAUSED = "PAUSED"
    FAULT = "FAULT"


STATE_SEVERITY = {
    SystemState.FAULT: 50,
    SystemState.PAUSED: 40,
    SystemState.RECONCILING: 30,
    SystemState.REDUCED: 20,
    SystemState.RUNNING: 10,
}


class ReservationStatus(str, Enum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    UNKNOWN = "UNKNOWN"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    RELEASED = "RELEASED"


class ParameterStatus(str, Enum):
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    RETIRED = "RETIRED"


class IntentKind(str, Enum):
    INCREASE_RISK = "INCREASE_RISK"
    REDUCE_ONLY = "REDUCE_ONLY"


NEW_RISK_BLOCKING_STATES = {
    SystemState.PAUSED,
    SystemState.FAULT,
    SystemState.RECONCILING,
}

# 试验合同：快照与恢复审批超过该窗口即失效。不是已批准的生产参数。
RECOVERY_FRESHNESS_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class ParameterBundle:
    version: str
    status: ParameterStatus
    values: dict[str, str]
    config_hash: str
    approved_by: Optional[str] = None

    def decimal(self, key: str) -> Decimal:
        return D(self.values[key])


@dataclass(frozen=True)
class ReserveRequest:
    symbol: str
    side: Side
    signal_key: str
    entry_price: Decimal
    stop_price: Decimal
    multiplier: Decimal
    lot_size: Decimal
    fee_rate_one_way: Decimal = Decimal("0")
    stress_slippage: Decimal = Decimal("0")
    funding_reserve_ratio: Decimal = Decimal("0")
    expected_equity_version: Optional[int] = None
    expected_risk_version: Optional[int] = None
    qty: Optional[Decimal] = None  # 平仓预留必须显式给出精确数量
    max_new_margin: Optional[Decimal] = None  # 本笔新增保证金上限；空则沿用参数合同


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    account_id: str
    symbol: str
    side: Side
    signal_key: str
    qty: Decimal
    filled_qty: Decimal
    notional: Decimal
    risk_amount: Decimal
    stress_loss_ratio: Decimal
    status: ReservationStatus
    risk_version: int
    equity_version: int
    account_state_version: int
    config_hash: str
    unknown_since: Optional[datetime]
    created_at: datetime
    leverage: Decimal

    @property
    def unfilled_qty(self) -> Decimal:
        return self.qty - self.filled_qty

    @property
    def pending_risk(self) -> Decimal:
        if self.status in (ReservationStatus.RELEASED, ReservationStatus.FILLED):
            return Decimal("0")
        if self.qty == 0:
            return Decimal("0")
        return (self.unfilled_qty / self.qty) * self.risk_amount

    @property
    def pending_notional(self) -> Decimal:
        if self.status in (ReservationStatus.RELEASED, ReservationStatus.FILLED):
            return Decimal("0")
        if self.qty == 0:
            return Decimal("0")
        return (self.unfilled_qty / self.qty) * self.notional


@dataclass(frozen=True)
class PositionOccupancy:
    symbol: str
    side: Optional[Side]
    qty: Decimal
    notional: Decimal
    risk_amount: Decimal
    isolated_margin: Decimal
    position_version: int


@dataclass(frozen=True)
class Blocker:
    blocker_id: str
    reason_code: str
    severity: SystemState
    created_at: datetime
    cleared_at: Optional[datetime]
    details: str


@dataclass(frozen=True)
class RecoverySnapshot:
    snapshot_version: int
    blocker_set_version: int
    risk_version: int
    account_state_version: int
    equity_version: int
    config_hash: str
    blocker_ids: tuple[str, ...]
    system_state: SystemState
    created_at: datetime


@dataclass(frozen=True)
class RecoveryAttempt:
    attempt_id: str
    snapshot: RecoverySnapshot
    result: Optional[str]
    reason_code: Optional[str]


@dataclass(frozen=True)
class OccupancyView:
    equity: Decimal
    equity_version: int
    risk_version: int
    account_state_version: int
    blocker_set_version: int
    config_hash: str
    system_state: SystemState
    position_risk: Decimal
    pending_risk: Decimal
    total_risk: Decimal
    position_abs_notional: Decimal
    pending_abs_notional: Decimal
    total_abs_notional: Decimal
    isolated_margin: Decimal
    pending_margin: Decimal
    reservations: tuple[Reservation, ...]
    positions: tuple[PositionOccupancy, ...]
    blockers: tuple[Blocker, ...]


@dataclass(frozen=True)
class TraceEvent:
    seq: int
    at: datetime
    action: str
    payload: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExitAuthorization:
    allowed: bool
    reason_code: Optional[str]
    symbol: str
    qty: Decimal
    position_version: int

"""P0 01 数据合同实体。金额与价格一律 Decimal。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from cts.checksum import sha256_hex
from cts.clock import require_utc
from cts.errors import UnitValidationError
from cts.money import D, require_non_negative, require_positive


SCHEMA_VERSION = "0.3.3"


def _ts(value: datetime, name: str) -> datetime:
    return require_utc(value, name)


class DataGrade(str, Enum):
    SIGNAL_RESEARCH_ONLY = "SIGNAL_RESEARCH_ONLY"
    EXECUTION_PNL_VALIDATED = "EXECUTION_PNL_VALIDATED"
    LIQUIDATION_REPLAY_VALIDATED = "LIQUIDATION_REPLAY_VALIDATED"


def _common_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "venue": record["venue"],
        "symbol": record["symbol"],
        "event_time": record["event_time"],
        "received_at": record["received_at"],
        "source_id": record["source_id"],
        "schema_version": record["schema_version"],
    }


@dataclass(frozen=True)
class Candle:
    venue: str
    symbol: str
    interval: str
    event_time: datetime
    received_at: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_id: str
    is_closed: bool
    schema_version: str = SCHEMA_VERSION
    checksum: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", _ts(self.event_time, "event_time"))
        object.__setattr__(self, "received_at", _ts(self.received_at, "received_at"))
        object.__setattr__(self, "available_at", _ts(self.available_at, "available_at"))
        object.__setattr__(self, "open", D(self.open))
        object.__setattr__(self, "high", D(self.high))
        object.__setattr__(self, "low", D(self.low))
        object.__setattr__(self, "close", D(self.close))
        object.__setattr__(self, "volume", D(self.volume))
        self.validate_units()
        if not self.checksum:
            object.__setattr__(self, "checksum", sha256_hex(self.payload()))

    def identity(self) -> tuple[Any, ...]:
        return (self.venue, self.symbol, self.interval, self.event_time, self.source_id)

    def payload(self) -> dict[str, Any]:
        return {
            **_common_payload(self.__dict__),
            "interval": self.interval,
            "available_at": self.available_at,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "is_closed": self.is_closed,
        }

    def validate_units(self) -> None:
        for name in ("open", "high", "low", "close"):
            require_positive(getattr(self, name), name)
        require_non_negative(self.volume, "volume")
        if self.high < self.low:
            raise UnitValidationError("high 不能低于 low")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise UnitValidationError("K 线实体必须落在 high/low 之内")
        if self.available_at < self.event_time:
            raise UnitValidationError("available_at 不能早于 event_time")
        if not self.venue or not self.symbol or not self.interval:
            raise UnitValidationError("venue/symbol/interval 不能为空")

    def with_checksum(self) -> "Candle":
        return replace(self, checksum=sha256_hex(self.payload()))


@dataclass(frozen=True)
class MarkSample:
    venue: str
    symbol: str
    event_time: datetime
    received_at: datetime
    available_at: datetime
    mark_price: Decimal
    index_price: Optional[Decimal]
    source_id: str
    schema_version: str = SCHEMA_VERSION
    checksum: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", _ts(self.event_time, "event_time"))
        object.__setattr__(self, "received_at", _ts(self.received_at, "received_at"))
        object.__setattr__(self, "available_at", _ts(self.available_at, "available_at"))
        object.__setattr__(self, "mark_price", D(self.mark_price))
        if self.index_price is not None:
            object.__setattr__(self, "index_price", D(self.index_price))
        self.validate_units()
        if not self.checksum:
            object.__setattr__(self, "checksum", sha256_hex(self.payload()))

    def identity(self) -> tuple[Any, ...]:
        return (self.venue, self.symbol, "mark", self.event_time, self.source_id)

    def payload(self) -> dict[str, Any]:
        return {
            **_common_payload(self.__dict__),
            "available_at": self.available_at,
            "mark_price": self.mark_price,
            "index_price": self.index_price,
        }

    def validate_units(self) -> None:
        require_positive(self.mark_price, "mark_price")
        if self.index_price is not None:
            require_positive(self.index_price, "index_price")
        if self.available_at < self.event_time:
            raise UnitValidationError("available_at 不能早于 event_time")


@dataclass(frozen=True)
class FundingSettlement:
    venue: str
    symbol: str
    event_time: datetime
    received_at: datetime
    available_at: datetime
    settlement_id: str
    funding_rate: Decimal
    payment: Decimal
    interval_minutes: Optional[Decimal]
    source_id: str
    schema_version: str = SCHEMA_VERSION
    checksum: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", _ts(self.event_time, "event_time"))
        object.__setattr__(self, "received_at", _ts(self.received_at, "received_at"))
        object.__setattr__(self, "available_at", _ts(self.available_at, "available_at"))
        object.__setattr__(self, "funding_rate", D(self.funding_rate))
        object.__setattr__(self, "payment", D(self.payment))
        if self.interval_minutes is not None:
            object.__setattr__(self, "interval_minutes", D(self.interval_minutes))
        self.validate_units()
        if not self.checksum:
            object.__setattr__(self, "checksum", sha256_hex(self.payload()))

    def identity(self) -> tuple[Any, ...]:
        return (self.venue, self.symbol, "funding", self.settlement_id, self.source_id)

    def payload(self) -> dict[str, Any]:
        return {
            **_common_payload(self.__dict__),
            "available_at": self.available_at,
            "settlement_id": self.settlement_id,
            "funding_rate": self.funding_rate,
            "payment": self.payment,
            "interval_minutes": self.interval_minutes,
        }

    def validate_units(self) -> None:
        if not self.settlement_id:
            raise UnitValidationError("settlement_id 不能为空")
        if self.interval_minutes is not None:
            require_positive(self.interval_minutes, "interval_minutes")


@dataclass(frozen=True)
class RiskTierSnapshot:
    venue: str
    symbol: str
    tier_index: int
    notional_min: Decimal
    notional_max: Optional[Decimal]
    initial_margin_rate: Decimal
    maintenance_margin_rate: Decimal
    mm_deduction: Decimal
    effective_from: datetime
    effective_to: Optional[datetime]
    source_id: str
    raw_fields: Mapping[str, str]
    schema_version: str = SCHEMA_VERSION
    checksum: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "notional_min", D(self.notional_min))
        object.__setattr__(self, "initial_margin_rate", D(self.initial_margin_rate))
        object.__setattr__(self, "maintenance_margin_rate", D(self.maintenance_margin_rate))
        object.__setattr__(self, "mm_deduction", D(self.mm_deduction))
        if self.notional_max is not None:
            object.__setattr__(self, "notional_max", D(self.notional_max))
        object.__setattr__(self, "effective_from", _ts(self.effective_from, "effective_from"))
        if self.effective_to is not None:
            object.__setattr__(self, "effective_to", _ts(self.effective_to, "effective_to"))
        object.__setattr__(self, "raw_fields", dict(self.raw_fields))
        self.validate_units()
        if not self.checksum:
            object.__setattr__(self, "checksum", sha256_hex(self.payload()))

    def identity(self) -> tuple[Any, ...]:
        return (self.venue, self.symbol, "tier", self.tier_index, self.effective_from, self.source_id)

    def payload(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "tier_index": self.tier_index,
            "notional_min": self.notional_min,
            "notional_max": self.notional_max,
            "initial_margin_rate": self.initial_margin_rate,
            "maintenance_margin_rate": self.maintenance_margin_rate,
            "mm_deduction": self.mm_deduction,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "source_id": self.source_id,
            "schema_version": self.schema_version,
            "raw_fields": dict(self.raw_fields),
        }

    def validate_units(self) -> None:
        require_non_negative(self.notional_min, "notional_min")
        if self.notional_max is not None and self.notional_max <= self.notional_min:
            raise UnitValidationError("notional_max 必须大于 notional_min")
        require_positive(self.initial_margin_rate, "initial_margin_rate")
        require_positive(self.maintenance_margin_rate, "maintenance_margin_rate")
        require_non_negative(self.mm_deduction, "mm_deduction")
        if self.maintenance_margin_rate > self.initial_margin_rate:
            raise UnitValidationError("维持保证金率不能高于初始保证金率")
        required = {
            "notional_min",
            "notional_max",
            "initial_margin_rate",
            "maintenance_margin_rate",
            "mm_deduction",
        }
        missing = required - set(self.raw_fields)
        if missing:
            raise UnitValidationError(f"风险档位原始字段缺失: {sorted(missing)}")


@dataclass(frozen=True)
class InstrumentRule:
    venue: str
    symbol: str
    contract_multiplier: Decimal
    lot_size: Decimal
    tick_size: Decimal
    quote_asset: str
    settle_asset: str
    funding_interval_minutes: Optional[Decimal]
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    rule_snapshot_id: str
    source_id: str
    captured_at: datetime
    effective_from: datetime
    effective_to: Optional[datetime]
    raw_artifact_hash: str
    parsed_rule_hash: str
    schema_version: str = SCHEMA_VERSION
    checksum: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "contract_multiplier", D(self.contract_multiplier))
        object.__setattr__(self, "lot_size", D(self.lot_size))
        object.__setattr__(self, "tick_size", D(self.tick_size))
        object.__setattr__(self, "maker_fee_rate", D(self.maker_fee_rate))
        object.__setattr__(self, "taker_fee_rate", D(self.taker_fee_rate))
        if self.funding_interval_minutes is not None:
            object.__setattr__(self, "funding_interval_minutes", D(self.funding_interval_minutes))
        object.__setattr__(self, "captured_at", _ts(self.captured_at, "captured_at"))
        object.__setattr__(self, "effective_from", _ts(self.effective_from, "effective_from"))
        if self.effective_to is not None:
            object.__setattr__(self, "effective_to", _ts(self.effective_to, "effective_to"))
        self.validate_units()
        if not self.checksum:
            object.__setattr__(self, "checksum", sha256_hex(self.payload()))

    def identity(self) -> tuple[Any, ...]:
        return (self.venue, self.symbol, "rule", self.rule_snapshot_id, self.source_id)

    def payload(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "contract_multiplier": self.contract_multiplier,
            "lot_size": self.lot_size,
            "tick_size": self.tick_size,
            "quote_asset": self.quote_asset,
            "settle_asset": self.settle_asset,
            "funding_interval_minutes": self.funding_interval_minutes,
            "maker_fee_rate": self.maker_fee_rate,
            "taker_fee_rate": self.taker_fee_rate,
            "rule_snapshot_id": self.rule_snapshot_id,
            "source_id": self.source_id,
            "captured_at": self.captured_at,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "raw_artifact_hash": self.raw_artifact_hash,
            "parsed_rule_hash": self.parsed_rule_hash,
            "schema_version": self.schema_version,
        }

    def validate_units(self) -> None:
        require_positive(self.contract_multiplier, "contract_multiplier")
        require_positive(self.lot_size, "lot_size")
        require_positive(self.tick_size, "tick_size")
        require_non_negative(self.maker_fee_rate, "maker_fee_rate")
        require_non_negative(self.taker_fee_rate, "taker_fee_rate")
        if self.quote_asset != "USDT" or self.settle_asset != "USDT":
            raise UnitValidationError("V0.3.3 仅接受 USDT 本位线性合约")
        if not self.rule_snapshot_id or not self.raw_artifact_hash or not self.parsed_rule_hash:
            raise UnitValidationError("规则快照哈希不能为空")
        if self.funding_interval_minutes is None:
            # 字段缺失不能默认 8 小时
            pass


@dataclass(frozen=True)
class MarketDataset:
    venue: str
    symbol: str
    interval: str
    start: datetime
    end: datetime
    source: str
    rule_snapshot_id: str
    contract_multiplier: Decimal
    expected_sampling_interval: timedelta
    candles: Sequence[Candle] = field(default_factory=tuple)
    marks: Sequence[MarkSample] = field(default_factory=tuple)
    fundings: Sequence[FundingSettlement] = field(default_factory=tuple)
    risk_tiers: Sequence[RiskTierSnapshot] = field(default_factory=tuple)
    checksum: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _ts(self.start, "start"))
        object.__setattr__(self, "end", _ts(self.end, "end"))
        object.__setattr__(self, "contract_multiplier", D(self.contract_multiplier))
        object.__setattr__(self, "candles", tuple(self.candles))
        object.__setattr__(self, "marks", tuple(self.marks))
        object.__setattr__(self, "fundings", tuple(self.fundings))
        object.__setattr__(self, "risk_tiers", tuple(self.risk_tiers))
        if self.end < self.start:
            raise UnitValidationError("数据集 end 不能早于 start")
        if not self.checksum:
            object.__setattr__(self, "checksum", self.compute_checksum())

    def compute_checksum(self) -> str:
        return sha256_hex(
            {
                "venue": self.venue,
                "symbol": self.symbol,
                "interval": self.interval,
                "start": self.start,
                "end": self.end,
                "source": self.source,
                "rule_snapshot_id": self.rule_snapshot_id,
                "contract_multiplier": self.contract_multiplier,
                "expected_sampling_interval": str(self.expected_sampling_interval),
                "candle_checksums": [c.checksum for c in self.candles],
                "mark_checksums": [m.checksum for m in self.marks],
                "funding_checksums": [f.checksum for f in self.fundings],
                "tier_checksums": [t.checksum for t in self.risk_tiers],
            }
        )

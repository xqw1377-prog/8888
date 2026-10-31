from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable

from cts.clock import UTC
from cts.data.models import Candle, InstrumentRule, MarkSample, RiskTierSnapshot
from cts.money import D

ORIGIN = datetime(2024, 1, 1, tzinfo=UTC)
INTERVAL = timedelta(hours=4)


def t_close(i: int) -> datetime:
    return ORIGIN + INTERVAL * i


def candle(
    i: int,
    *,
    open_: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
    symbol: str = "BTCUSDT",
    venue: str = "SIM",
    delay_seconds: int = 1,
    source_id: str = "sim",
    is_closed: bool = True,
) -> Candle:
    event = t_close(i)
    received = event + timedelta(seconds=delay_seconds)
    return Candle(
        venue=venue,
        symbol=symbol,
        interval="4h",
        event_time=event,
        received_at=received,
        available_at=received,
        open=D(open_),
        high=D(high),
        low=D(low),
        close=D(close),
        volume=D("10"),
        source_id=source_id,
        is_closed=is_closed,
    )


def mark(
    i: int,
    price: str,
    *,
    symbol: str = "BTCUSDT",
    venue: str = "SIM",
    source_id: str = "sim",
) -> MarkSample:
    event = t_close(i)
    received = event + timedelta(seconds=1)
    return MarkSample(
        venue=venue,
        symbol=symbol,
        event_time=event,
        received_at=received,
        available_at=received,
        mark_price=D(price),
        index_price=D(price),
        source_id=source_id,
    )


def range_bars(n: int, **kwargs) -> list[Candle]:
    return [candle(i, **kwargs) for i in range(n)]


def breakout_series(warmup: int = 130) -> list[Candle]:
    bars = range_bars(warmup)
    bars.append(candle(warmup, open_="101", high="120", low="101", close="118"))
    bars.append(candle(warmup + 1, open_="118", high="119", low="117", close="118.5"))
    return bars


def usdt_linear_rule() -> InstrumentRule:
    now = ORIGIN
    return InstrumentRule(
        venue="SIM",
        symbol="BTCUSDT",
        contract_multiplier=D("1"),
        lot_size=D("0.001"),
        tick_size=D("0.1"),
        quote_asset="USDT",
        settle_asset="USDT",
        funding_interval_minutes=D("480"),
        maker_fee_rate=D("0.0002"),
        taker_fee_rate=D("0.00055"),
        rule_snapshot_id="rule-sim-1",
        source_id="sim",
        captured_at=now,
        effective_from=now,
        effective_to=None,
        raw_artifact_hash="a" * 64,
        parsed_rule_hash="b" * 64,
    )


def tier() -> RiskTierSnapshot:
    return RiskTierSnapshot(
        venue="SIM",
        symbol="BTCUSDT",
        tier_index=1,
        notional_min=D("0"),
        notional_max=D("1000000"),
        initial_margin_rate=D("0.5"),
        maintenance_margin_rate=D("0.01"),
        mm_deduction=D("0"),
        effective_from=ORIGIN,
        effective_to=None,
        source_id="sim",
        raw_fields={
            "notional_min": "0",
            "notional_max": "1000000",
            "initial_margin_rate": "0.5",
            "maintenance_margin_rate": "0.01",
            "mm_deduction": "0",
        },
    )


def hashes(items: Iterable) -> list[str]:
    return [i.checksum for i in items]


def dec(value: str) -> Decimal:
    return D(value)

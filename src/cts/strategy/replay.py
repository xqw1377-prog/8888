"""确定性事件回放：信号之后成交，成本入账。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional, Sequence

from cts.data.models import Candle, MarkSample
from cts.ledger.journal import Ledger
from cts.ledger.models import Side
from cts.money import D
from cts.strategy.sizing import SizedPosition, size_position
from cts.strategy.t1 import (
    OpenPosition,
    SignalIntent,
    T1Params,
    apply_fill_to_state,
    evaluate_exit,
    evaluate_signal,
    next_open_fill_price,
)


@dataclass
class ReplayConfig:
    params: T1Params
    multiplier: Decimal = Decimal("1")
    lot_size: Decimal = Decimal("0.001")
    fee_rate: Decimal = Decimal("0.0005")
    adverse_cost: Decimal = Decimal("0.0005")
    stress_slippage: Decimal = Decimal("0.001")
    funding_reserve_ratio: Decimal = Decimal("0")
    leverage: Decimal = Decimal("2")
    remaining_margin_budget_pct: Decimal = Decimal("0.50")


@dataclass
class ReplayResult:
    signals: list[SignalIntent]
    fills: int
    exits: int
    skipped_no_fill: int


def mark_at(marks: Sequence[MarkSample], as_of: datetime, symbol: str) -> Optional[Decimal]:
    eligible = [m for m in marks if m.symbol == symbol and m.available_at <= as_of]
    if not eligible:
        return None
    return max(eligible, key=lambda m: (m.event_time, m.available_at)).mark_price


def replay_t1(
    *,
    ledger: Ledger,
    symbol: str,
    candles: Sequence[Candle],
    marks: Sequence[MarkSample],
    config: ReplayConfig,
    venue: str,
) -> ReplayResult:
    closed = [c for c in candles if c.is_closed and c.symbol == symbol]
    closed = sorted(closed, key=lambda c: c.event_time)
    position: Optional[OpenPosition] = None
    last_exit: Optional[datetime] = None
    pending: Optional[SignalIntent] = None
    signals: list[SignalIntent] = []
    fills = 0
    exits = 0
    skipped = 0

    for i, bar in enumerate(closed):
        hist = closed[: i + 1]
        m = mark_at(marks, bar.available_at, symbol)
        if position is not None and m is not None:
            position, decision = evaluate_exit(position, hist, mark_price=m, params=config.params)
            if decision is not None:
                ledger.apply_fill(
                    symbol=symbol,
                    side=Side.LONG if position.side == Side.SHORT else Side.SHORT,
                    qty=position.qty,
                    price=m if decision.reason == "STOP" else (bar.close),
                    multiplier=config.multiplier,
                    event_time=bar.event_time,
                    received_at=bar.available_at,
                    external_id=f"exit:{symbol}:{bar.event_time.isoformat()}:{decision.reason}",
                    venue=venue,
                    venue_trade_id=f"exit-{fills}-{exits}",
                    reduce_only=True,
                )
                notional = position.qty * (m if decision.reason == "STOP" else bar.close) * config.multiplier
                ledger.post_fee(
                    amount=notional * config.fee_rate,
                    event_time=bar.event_time,
                    received_at=bar.available_at,
                    external_id=f"fee-exit:{symbol}:{bar.event_time.isoformat()}",
                    venue=venue,
                    symbol=symbol,
                )
                last_exit = bar.event_time
                position = None
                pending = None
                exits += 1
                continue

        if pending is not None:
            fill_px = next_open_fill_price(pending.event_time, hist, pending.side, config.adverse_cost)
            if fill_px is not None and bar.event_time > pending.event_time:
                snap = ledger.equity_snapshot(as_of=bar.available_at, mark_prices={symbol: m or fill_px})
                stop = (
                    fill_px - config.params.stop_multiple * pending.atr
                    if pending.side == Side.LONG
                    else fill_px + config.params.stop_multiple * pending.atr
                )
                sized: Optional[SizedPosition] = size_position(
                    side=pending.side,
                    equity=snap.equity,
                    entry_price=fill_px,
                    stop_price=stop,
                    multiplier=config.multiplier,
                    lot_size=config.lot_size,
                    remaining_margin_budget=snap.equity * config.remaining_margin_budget_pct,
                    fee_rate_one_way=config.fee_rate,
                    stress_slippage=config.stress_slippage,
                    funding_reserve_ratio=config.funding_reserve_ratio,
                )
                if sized is None:
                    skipped += 1
                    pending = None
                else:
                    ledger.apply_fill(
                        symbol=symbol,
                        side=pending.side,
                        qty=sized.qty,
                        price=fill_px,
                        multiplier=config.multiplier,
                        event_time=bar.event_time,
                        received_at=bar.available_at,
                        external_id=f"entry:{symbol}:{pending.event_time.isoformat()}",
                        venue=venue,
                        venue_trade_id=f"entry-{fills}",
                    )
                    ledger.lock_margin(
                        amount=sized.notional / config.leverage,
                        symbol=symbol,
                        event_time=bar.event_time,
                        received_at=bar.available_at,
                        external_id=f"im:{symbol}:{pending.event_time.isoformat()}",
                        venue=venue,
                    )
                    ledger.post_fee(
                        amount=sized.notional * config.fee_rate,
                        event_time=bar.event_time,
                        received_at=bar.available_at,
                        external_id=f"fee-entry:{symbol}:{pending.event_time.isoformat()}",
                        venue=venue,
                        symbol=symbol,
                    )
                    position = apply_fill_to_state(signal=pending, fill_price=fill_px, qty=sized.qty, params=config.params)
                    fills += 1
                    pending = None

        if position is None and pending is None:
            intent = evaluate_signal(
                hist,
                params=config.params,
                has_position=False,
                has_pending_entry=False,
                last_exit_event_time=last_exit,
            )
            if intent is not None:
                signals.append(intent)
                pending = intent

    return ReplayResult(signals=signals, fills=fills, exits=exits, skipped_no_fill=skipped)

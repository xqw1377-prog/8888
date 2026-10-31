"""确定性事件回放：信号之后成交，成本入账。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from cts.clock import UTC
from cts.data.models import Candle, MarkSample
from cts.errors import LookaheadError, UnitValidationError
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
    stop_breached,
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


def bar_open_time(bar: Candle) -> datetime:
    spec = bar.interval.strip().lower()
    if len(spec) < 2 or not spec[:-1].isdigit():
        raise UnitValidationError(f"无法解析 K 线周期: {bar.interval}")
    count = int(spec[:-1])
    unit = spec[-1]
    if unit == "h":
        delta = timedelta(hours=count)
    elif unit == "m":
        delta = timedelta(minutes=count)
    elif unit == "d":
        delta = timedelta(days=count)
    else:
        raise UnitValidationError(f"无法解析 K 线周期: {bar.interval}")
    return bar.event_time - delta


def executable_time(entry_time: datetime, available_at: datetime) -> datetime:
    """成交只能记在开仓之后，且不能早于触发数据的 available_at。"""
    return max(entry_time, available_at)


def causal_entry_time(signal: SignalIntent, bar: Candle) -> datetime:
    """开仓不得早于信号可得，也不得早于下一根开盘。"""
    return max(bar_open_time(bar), signal.available_at)


def mark_can_fill(mk: MarkSample) -> bool:
    """标记价只能在它自己的事件时间成交，且当时必须已经可得。晚到价格不能挪到以后使用。"""
    return mk.available_at <= mk.event_time


def _mark_key(mk: MarkSample) -> tuple:
    return (mk.symbol, mk.source_id, mk.event_time.isoformat(), format(mk.mark_price, "f"), mk.available_at.isoformat())


def next_available_stop(position: OpenPosition, marks: Sequence[MarkSample], consumed: set[tuple], as_of: datetime):
    visible = [
        mk
        for mk in marks
        if mk.symbol == position.symbol
        and _mark_key(mk) not in consumed
        and mk.available_at <= as_of
        and mark_can_fill(mk)
        and mk.event_time > position.entry_event_time
    ]
    visible.sort(key=lambda mk: (mk.event_time, mk.available_at))
    for mk in visible:
        consumed.add(_mark_key(mk))
        if stop_breached(position, mk.mark_price):
            return mk
    return None


def mark_at(marks: Sequence[MarkSample], as_of: datetime, symbol: str) -> Optional[Decimal]:
    eligible = [m for m in marks if m.symbol == symbol and m.available_at <= as_of and mark_can_fill(m)]
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
    consumed_marks: set[tuple] = set()
    prior = ledger.events()
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    last_event_time = max((event.event_time for event in prior), default=epoch)
    last_received = max((event.received_at for event in prior), default=epoch)

    def stamp(when: datetime) -> datetime:
        nonlocal last_event_time
        if when < last_event_time:
            raise LookaheadError(f"回放成交时间倒序: {when.isoformat()} < {last_event_time.isoformat()}")
        last_event_time = when
        return when

    def observe(when: datetime) -> datetime:
        nonlocal last_received
        seen = when if when >= last_received else last_received
        if seen < last_event_time:
            seen = last_event_time
        last_received = seen
        return seen

    def book_stop(held: OpenPosition, breached: MarkSample) -> None:
        nonlocal exits, last_exit, position, pending
        if not mark_can_fill(breached):
            raise LookaheadError("晚到标记价不能作为新成交的价格")
        booked = stamp(breached.event_time)
        seen = observe(booked)
        ledger.apply_fill(
            symbol=symbol,
            side=Side.LONG if held.side == Side.SHORT else Side.SHORT,
            qty=held.qty,
            price=breached.mark_price,
            multiplier=config.multiplier,
            event_time=booked,
            received_at=seen,
            external_id=f"exit:{symbol}:{booked.isoformat()}:STOP:{breached.source_id}",
            venue=venue,
            venue_trade_id=f"exit-{fills}-{exits}",
            reduce_only=True,
        )
        notional = held.qty * breached.mark_price * config.multiplier
        ledger.post_fee(
            amount=notional * config.fee_rate,
            event_time=booked,
            received_at=seen,
            external_id=f"fee-exit:{symbol}:{booked.isoformat()}:{breached.source_id}",
            venue=venue,
            symbol=symbol,
        )
        last_exit = booked
        position = None
        pending = None
        exits += 1

    for i, bar in enumerate(closed):
        hist = closed[: i + 1]
        m = mark_at(marks, bar.available_at, symbol)
        if position is not None:
            breached = next_available_stop(position, marks, consumed_marks, bar.available_at)
            if breached is not None:
                book_stop(position, breached)
                continue
        if position is not None:
            position, decision = evaluate_exit(position, hist, mark_price=bar.close, params=config.params)
            if decision is not None:
                booked = stamp(executable_time(position.entry_event_time, bar.available_at))
                seen = observe(booked)
                exit_price = bar.close
                ledger.apply_fill(
                    symbol=symbol,
                    side=Side.LONG if position.side == Side.SHORT else Side.SHORT,
                    qty=position.qty,
                    price=exit_price,
                    multiplier=config.multiplier,
                    event_time=booked,
                    received_at=seen,
                    external_id=f"exit:{symbol}:{booked.isoformat()}:{decision.reason}",
                    venue=venue,
                    venue_trade_id=f"exit-{fills}-{exits}",
                    reduce_only=True,
                )
                notional = position.qty * exit_price * config.multiplier
                ledger.post_fee(
                    amount=notional * config.fee_rate,
                    event_time=booked,
                    received_at=seen,
                    external_id=f"fee-exit:{symbol}:{booked.isoformat()}",
                    venue=venue,
                    symbol=symbol,
                )
                last_exit = booked
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
                    opened = stamp(causal_entry_time(pending, bar))
                    observed = observe(bar.available_at)
                    ledger.apply_fill(
                        symbol=symbol,
                        side=pending.side,
                        qty=sized.qty,
                        price=fill_px,
                        multiplier=config.multiplier,
                        event_time=opened,
                        received_at=observed,
                        external_id=f"entry:{symbol}:{pending.event_time.isoformat()}",
                        venue=venue,
                        venue_trade_id=f"entry-{fills}",
                    )
                    ledger.lock_margin(
                        amount=sized.notional / config.leverage,
                        symbol=symbol,
                        event_time=opened,
                        received_at=observed,
                        external_id=f"im:{symbol}:{pending.event_time.isoformat()}",
                        venue=venue,
                    )
                    ledger.post_fee(
                        amount=sized.notional * config.fee_rate,
                        event_time=opened,
                        received_at=observed,
                        external_id=f"fee-entry:{symbol}:{pending.event_time.isoformat()}",
                        venue=venue,
                        symbol=symbol,
                    )
                    position = apply_fill_to_state(
                        signal=pending,
                        fill_price=fill_px,
                        qty=sized.qty,
                        params=config.params,
                        entry_time=opened,
                    )
                    fills += 1
                    pending = None
                    breached = next_available_stop(position, marks, consumed_marks, bar.available_at)
                    if breached is not None:
                        book_stop(position, breached)
                        continue

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

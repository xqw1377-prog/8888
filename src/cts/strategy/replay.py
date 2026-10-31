"""确定性事件回放：信号之后成交，成本入账。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from cts.clock import UTC
from cts.data.models import Candle, DataGrade, FundingSettlement, MarkSample
from cts.errors import LookaheadError, UnitValidationError
from cts.execution.liq import model_liq_price
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
    grade: DataGrade = DataGrade.SIGNAL_RESEARCH_ONLY
    funding_posted: int = 0


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


EXECUTABLE_QUOTE = "EXECUTABLE_QUOTE"
RESEARCH_APPROXIMATE = "RESEARCH_APPROXIMATE"


def classify_quote(price_time: datetime, price_available: datetime, fill_time: datetime) -> str:
    """可执行报价必须与成交时刻是同一笔价格证据。对不上的只能作为研究近似。"""
    if price_time == fill_time and price_available <= fill_time:
        return EXECUTABLE_QUOTE
    return RESEARCH_APPROXIMATE


def _mark_key(mk: MarkSample) -> tuple:
    return (mk.symbol, mk.source_id, mk.event_time.isoformat(), format(mk.mark_price, "f"), mk.available_at.isoformat())


def next_available_stop(position: OpenPosition, marks: Sequence[MarkSample], consumed: set[tuple], as_of: datetime):
    visible = [
        mk
        for mk in marks
        if mk.symbol == position.symbol
        and _mark_key(mk) not in consumed
        and mk.available_at <= as_of
        and mk.event_time > position.entry_event_time
    ]
    visible.sort(key=lambda mk: (mk.event_time, mk.available_at))
    for mk in visible:
        consumed.add(_mark_key(mk))
        if stop_breached(position, mk.mark_price):
            return mk
    return None


def latest_mark(marks: Sequence[MarkSample], as_of: datetime, symbol: str) -> Optional[MarkSample]:
    eligible = [m for m in marks if m.symbol == symbol and m.available_at <= as_of]
    if not eligible:
        return None
    return max(eligible, key=lambda m: (m.event_time, m.available_at))


def mark_at(marks: Sequence[MarkSample], as_of: datetime, symbol: str) -> Optional[Decimal]:
    sample = latest_mark(marks, as_of, symbol)
    if sample is None:
        return None
    return sample.mark_price


def replay_t1(
    *,
    ledger: Ledger,
    symbol: str,
    candles: Sequence[Candle],
    marks: Sequence[MarkSample],
    config: ReplayConfig,
    venue: str,
    fundings: Sequence[FundingSettlement] = (),
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
    funding_posted = 0
    consumed_marks: set[tuple] = set()
    posted_funding: set[str] = set()
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
        booked = stamp(executable_time(held.entry_event_time, breached.available_at))
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
            price_source="MARK",
            quote_grade=classify_quote(breached.event_time, breached.available_at, booked),
            price_event_time=breached.event_time,
            annotations={"exit_reason": "STOP", "direction": held.side.value},
        )
        notional = held.qty * breached.mark_price * config.multiplier
        book_exit_costs(notional, booked, seen, f"{symbol}:{booked.isoformat()}:{breached.source_id}")
        last_exit = booked
        position = None
        pending = None
        exits += 1
        close_hold(booked)
        book_late_funding(decision_as_of)

    def book_exit_costs(notional: Decimal, booked: datetime, seen: datetime, tag: str) -> None:
        fee = notional * config.fee_rate
        slip = notional * config.adverse_cost
        if fee > 0:
            ledger.post_fee(
                amount=fee,
                event_time=booked,
                received_at=seen,
                external_id=f"fee-exit:{tag}",
                venue=venue,
                symbol=symbol,
            )
        if slip > 0:
            ledger.post_fee(
                amount=slip,
                event_time=booked,
                received_at=seen,
                external_id=f"slip-exit:{tag}",
                venue=venue,
                symbol=symbol,
            )

    def book_funding_until(as_of: datetime, until: Optional[datetime]) -> None:
        nonlocal funding_posted
        if position is None:
            return
        due = [
            item
            for item in fundings
            if item.symbol == symbol
            and item.settlement_id not in posted_funding
            and item.payment != 0
            and item.available_at <= as_of
            and item.event_time > position.entry_event_time
        ]
        due.sort(key=lambda item: (item.available_at, item.event_time, item.settlement_id))
        for item in due:
            if item.settlement_id in posted_funding:
                continue
            when = executable_time(position.entry_event_time, item.available_at)
            if until is not None and (when > until or item.event_time > until):
                continue
            booked = stamp(when)
            seen = observe(booked)
            ledger.post_funding(
                payment=item.payment,
                event_time=booked,
                received_at=seen,
                external_id=f"funding:{symbol}:{item.settlement_id}",
                venue=venue,
                symbol=symbol,
                from_isolated=True,
            )
            posted_funding.add(item.settlement_id)
            funding_posted += 1

    holds: list[dict[str, datetime | None]] = []
    decision_as_of = candles[0].event_time if candles else epoch

    def open_hold(entry: datetime) -> None:
        holds.append({"entry": entry, "exit": None})

    def close_hold(exit_at: datetime) -> None:
        if holds and holds[-1]["exit"] is None:
            holds[-1]["exit"] = exit_at

    def book_late_funding(as_of: datetime) -> None:
        nonlocal funding_posted
        for hold in holds:
            exit_at = hold["exit"]
            entry = hold["entry"]
            if exit_at is None or entry is None:
                continue
            due = [
                item
                for item in fundings
                if item.symbol == symbol
                and item.settlement_id not in posted_funding
                and item.payment != 0
                and entry < item.event_time <= exit_at
                and item.available_at > exit_at
                and item.available_at <= as_of
            ]
            due.sort(key=lambda item: (item.available_at, item.event_time, item.settlement_id))
            for item in due:
                if item.settlement_id in posted_funding:
                    continue
                booked = stamp(item.available_at)
                seen = observe(booked)
                ledger.post_funding(
                    payment=item.payment,
                    event_time=booked,
                    received_at=seen,
                    external_id=f"funding:{symbol}:{item.settlement_id}",
                    venue=venue,
                    symbol=symbol,
                    from_isolated=True,
                )
                posted_funding.add(item.settlement_id)
                funding_posted += 1

    for i, bar in enumerate(closed):
        decision_as_of = bar.available_at
        book_late_funding(bar.available_at)
        hist = closed[: i + 1]
        sample = latest_mark(marks, bar.available_at, symbol)
        m = sample.mark_price if sample is not None else None
        if position is not None:
            breached = next_available_stop(position, marks, consumed_marks, bar.available_at)
            stop_time = executable_time(position.entry_event_time, breached.available_at) if breached is not None else bar.available_at
            book_funding_until(bar.available_at, stop_time)
            if breached is not None:
                book_stop(position, breached)
                continue
        if position is not None:
            mark_for_stop = m if sample is not None and sample.event_time > position.entry_event_time else None
            position, decision = evaluate_exit(position, hist, mark_price=mark_for_stop, params=config.params)
            if decision is not None and decision.reason == "STOP" and sample is not None:
                book_stop(position, sample)
                continue
            if decision is not None and decision.reason == "TREND":
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
                    price_source="CLOSE",
                    quote_grade=classify_quote(bar.event_time, bar.available_at, booked),
                    price_event_time=bar.event_time,
                    annotations={"exit_reason": "TREND", "direction": position.side.value},
                )
                notional = position.qty * exit_price * config.multiplier
                book_exit_costs(notional, booked, seen, f"{symbol}:{booked.isoformat()}:TREND")
                last_exit = booked
                close_hold(booked)
                book_late_funding(bar.available_at)
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
                        price_source="NEXT_OPEN",
                        quote_grade=classify_quote(bar_open_time(bar), bar.available_at, opened),
                        price_event_time=bar_open_time(bar),
                        annotations={
                            "signal_time": pending.event_time.isoformat(),
                            "risk_decision": "已按权益和止损距离定仓",
                            "direction": pending.side.value,
                            "protection": f"研究止损 {format(stop, 'f')}，无交易所保护单证据",
                            "liq_distance": _liq_distance(
                                side=pending.side,
                                price=fill_px,
                                qty=sized.qty,
                                margin=sized.notional / config.leverage,
                                mark=m or fill_px,
                                multiplier=config.multiplier,
                                fee_rate=config.fee_rate,
                            ),
                        },
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
                    open_hold(position.entry_event_time)
                    fills += 1
                    pending = None
                    breached = next_available_stop(position, marks, consumed_marks, bar.available_at)
                    stop_time = (
                        executable_time(position.entry_event_time, breached.available_at)
                        if breached is not None
                        else bar.available_at
                    )
                    book_funding_until(bar.available_at, stop_time)
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

    return ReplayResult(
        signals=signals,
        fills=fills,
        exits=exits,
        skipped_no_fill=skipped,
        grade=DataGrade.SIGNAL_RESEARCH_ONLY,
        funding_posted=funding_posted,
    )


def _liq_distance(
    *,
    side: Side,
    price: Decimal,
    qty: Decimal,
    margin: Decimal,
    mark: Decimal,
    multiplier: Decimal,
    fee_rate: Decimal,
) -> str:
    if margin <= 0 or mark <= 0:
        return "暂无证据"
    try:
        liq = model_liq_price(
            side=side,
            avg_price=price,
            qty=qty,
            multiplier=multiplier,
            isolated_margin=margin,
            mmr=Decimal("0.005"),
            fee_rate=fee_rate,
        )
    except (ValueError, ArithmeticError):
        return "暂无证据"
    return f"本地公式 {format(abs(mark - liq) / mark, 'f')}，不是交易所读回"

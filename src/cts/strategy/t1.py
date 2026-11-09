"""T1 纯函数：信号、退出、成交后状态。成交不得假定为信号收盘价。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Optional, Sequence

from cts.checksum import sha256_hex
from cts.data.models import Candle
from cts.ledger.models import Side
from cts.money import D
from cts.strategy.indicators import atr_sma, window_high, window_low


@dataclass(frozen=True)
class T1Params:
    entry_lookback: int = 40
    exit_lookback: int = 20
    atr_period: int = 20
    stop_multiple: Decimal = Decimal("2.5")
    take_multiple: Decimal = Decimal("5")
    warmup_bars: int = 120
    version: str = "T1-E0"


def t1_params_binding_hash(params: "T1Params") -> str:
    """全字段绑定：任一策略参数（含 take_multiple、stop_multiple、回看期）变化都必须改变哈希。"""
    from cts.checksum import sha256_hex

    return sha256_hex({
        "version": params.version,
        "entry_lookback": str(params.entry_lookback),
        "exit_lookback": str(params.exit_lookback),
        "atr_period": str(params.attr_period) if hasattr(params, "attr_period") else str(params.atr_period),
        "stop_multiple": format(params.stop_multiple, "f"),
        "take_multiple": format(params.take_multiple, "f"),
        "warmup_bars": str(params.warmup_bars),
    })


@dataclass(frozen=True)
class SignalIntent:
    symbol: str
    side: Side
    event_time: datetime
    available_at: datetime
    close: Decimal
    entry_band: Decimal
    atr: Decimal
    input_hash: str
    strategy_version: str


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    side: Side
    qty: Decimal
    avg_price: Decimal
    signal_atr: Decimal
    stop: Decimal
    extreme: Decimal
    entry_event_time: datetime
    last_exit_event_time: Optional[datetime] = None


@dataclass(frozen=True)
class ExitDecision:
    reason: str
    side: Side
    stop: Decimal
    trend_level: Decimal
    mark_price: Decimal
    close: Optional[Decimal]


def evaluate_signal(
    candles: Sequence[Candle],
    *,
    params: T1Params,
    has_position: bool,
    has_pending_entry: bool,
    last_exit_event_time: Optional[datetime] = None,
) -> Optional[SignalIntent]:
    if has_position or has_pending_entry:
        return None
    if len(candles) <= params.warmup_bars:
        return None
    if not all(c.is_closed for c in candles):
        return None
    current = candles[-1]
    prior = candles[:-1]
    if last_exit_event_time is not None and current.event_time <= last_exit_event_time:
        return None
    if len(prior) < max(params.entry_lookback, params.exit_lookback, params.atr_period):
        return None
    u40 = window_high(prior[-params.entry_lookback :])
    d40 = window_low(prior[-params.entry_lookback :])
    atr = atr_sma(candles, params.atr_period)
    side: Optional[Side] = None
    band = u40
    if current.close > u40:
        side = Side.LONG
        band = u40
    elif current.close < d40:
        side = Side.SHORT
        band = d40
    if side is None:
        return None
    payload = {
        "symbol": current.symbol,
        "event_time": current.event_time,
        "side": side.value,
        "close": current.close,
        "u40": u40,
        "d40": d40,
        "atr": atr,
        "params": params.version,
        "params_binding": t1_params_binding_hash(params),
        "candle_checksum": current.checksum,
        "prior_checksums": [c.checksum for c in prior[-params.entry_lookback :]],
    }
    return SignalIntent(
        symbol=current.symbol,
        side=side,
        event_time=current.event_time,
        available_at=current.available_at,
        close=current.close,
        entry_band=band,
        atr=atr,
        input_hash=sha256_hex(payload),
        strategy_version=params.version,
    )


def stop_breached(position: OpenPosition, mark_price: Decimal) -> bool:
    mark = D(mark_price)
    if position.side == Side.LONG:
        return mark <= position.stop
    return mark >= position.stop


def initial_stop(side: Side, fill_price: Decimal, signal_atr: Decimal, params: T1Params) -> Decimal:
    offset = D(params.stop_multiple) * D(signal_atr)
    if side == Side.LONG:
        return D(fill_price) - offset
    return D(fill_price) + offset


def take_profit_price(side: Side, fill_price: Decimal, signal_atr: Decimal, params: T1Params) -> Decimal:
    """止盈是入场价加减 5 倍 ATR，等于初始止损距离的两倍，不跟随移动。"""
    offset = D(params.take_multiple) * D(signal_atr)
    if side == Side.LONG:
        return D(fill_price) + offset
    return D(fill_price) - offset


def trail_stop(position: OpenPosition, candles: Sequence[Candle], params: T1Params) -> Decimal:
    atr = atr_sma(candles, params.atr_period)
    offset = D(params.stop_multiple) * atr
    if position.side == Side.LONG:
        candidate = position.extreme - offset
        return max(position.stop, candidate)
    candidate = position.extreme + offset
    return min(position.stop, candidate)


def evaluate_exit(
    position: OpenPosition,
    candles: Sequence[Candle],
    *,
    mark_price: Optional[Decimal],
    params: T1Params,
) -> tuple[OpenPosition, Optional[ExitDecision]]:
    if not candles or not all(c.is_closed for c in candles):
        return position, None
    current = candles[-1]
    prior = candles[:-1]
    since_entry = [c for c in candles if c.event_time > position.entry_event_time]
    if since_entry:
        if position.side == Side.LONG:
            extreme = max(position.extreme, max(c.close for c in since_entry))
        else:
            extreme = min(position.extreme, min(c.close for c in since_entry))
    else:
        extreme = position.extreme
    updated = replace(position, extreme=extreme)
    updated = replace(updated, stop=trail_stop(updated, candles, params))

    mark = D(mark_price) if mark_price is not None else None
    target = take_profit_price(position.side, position.avg_price, position.signal_atr, params)
    if mark is not None and position.signal_atr > 0 and position.side == Side.LONG and mark >= target and mark > updated.stop:
        return updated, ExitDecision("TAKE_PROFIT", updated.side, updated.stop, target, mark, current.close)
    if mark is not None and position.signal_atr > 0 and position.side == Side.SHORT and mark <= target and mark < updated.stop:
        return updated, ExitDecision("TAKE_PROFIT", updated.side, updated.stop, target, mark, current.close)
    if mark is not None and position.side == Side.LONG and mark <= updated.stop:
        return updated, ExitDecision("STOP", updated.side, updated.stop, Decimal("0"), mark, current.close)
    if mark is not None and position.side == Side.SHORT and mark >= updated.stop:
        return updated, ExitDecision("STOP", updated.side, updated.stop, Decimal("0"), mark, current.close)

    if len(prior) >= params.exit_lookback:
        d20 = window_low(prior[-params.exit_lookback :])
        u20 = window_high(prior[-params.exit_lookback :])
        recorded_mark = mark if mark is not None else current.close
        if position.side == Side.LONG and current.close < d20:
            return updated, ExitDecision("TREND", updated.side, updated.stop, d20, recorded_mark, current.close)
        if position.side == Side.SHORT and current.close > u20:
            return updated, ExitDecision("TREND", updated.side, updated.stop, u20, recorded_mark, current.close)
    return updated, None


def apply_fill_to_state(
    *,
    signal: SignalIntent,
    fill_price: Decimal,
    qty: Decimal,
    params: T1Params,
    entry_time: Optional[datetime] = None,
) -> OpenPosition:
    stop = initial_stop(signal.side, fill_price, signal.atr, params)
    return OpenPosition(
        symbol=signal.symbol,
        side=signal.side,
        qty=D(qty),
        avg_price=D(fill_price),
        signal_atr=signal.atr,
        stop=stop,
        extreme=D(fill_price),
        entry_event_time=entry_time or signal.event_time,
    )


def next_open_fill_price(signal_close_time: datetime, subsequent: Sequence[Candle], side: Side, adverse_cost: Decimal) -> Optional[Decimal]:
    nxt = next((c for c in subsequent if c.event_time > signal_close_time and c.is_closed), None)
    if nxt is None:
        return None
    cost = D(adverse_cost)
    if side == Side.LONG:
        return nxt.open * (Decimal("1") + cost)
    return nxt.open * (Decimal("1") - cost)

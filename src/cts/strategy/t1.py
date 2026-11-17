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


EXIT_MODE_FIXED = "fixed"
EXIT_MODE_TRAIL = "trail"
EXIT_FACT_TRIGGERED = "TRIGGERED"
EXIT_FACT_SUBMITTED = "SUBMITTED"
EXIT_FACT_FILLED = "FILLED"
EXIT_FACT_FLAT = "FLAT"


@dataclass(frozen=True)
class T1Params:
    entry_lookback: int = 40
    exit_lookback: int = 20
    atr_period: int = 20
    stop_multiple: Decimal = Decimal("2.5")
    take_multiple: Decimal = Decimal("5")
    warmup_bars: int = 120
    version: str = "T1-E0"
    exit_mode: str = EXIT_MODE_TRAIL


def t1_params_binding_hash(params: "T1Params") -> str:
    """全字段绑定：退出模式和止盈止损倍数一样，变了就必须变哈希。"""
    from cts.checksum import sha256_hex

    return sha256_hex({
        "version": params.version,
        "entry_lookback": str(params.entry_lookback),
        "exit_lookback": str(params.exit_lookback),
        "atr_period": str(params.attr_period) if hasattr(params, "attr_period") else str(params.atr_period),
        "stop_multiple": format(params.stop_multiple, "f"),
        "take_multiple": format(params.take_multiple, "f"),
        "warmup_bars": str(params.warmup_bars),
        "exit_mode": str(getattr(params, "exit_mode", EXIT_MODE_TRAIL)),
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
    take_profit: Optional[Decimal] = None


@dataclass(frozen=True)
class ExitDecision:
    reason: str
    side: Side
    stop: Decimal
    trend_level: Decimal
    mark_price: Decimal
    close: Optional[Decimal]


@dataclass(frozen=True)
class ExitTrigger:
    """只描述触发。下单、成交、账本归零由调用方另记。"""

    reason: str
    mode: str
    side: Side
    mark_price: Decimal
    persisted_stop: Decimal
    persisted_take_profit: Decimal
    compared_stop: Decimal
    trend_level: Decimal
    close: Optional[Decimal]
    degraded: bool
    trail_computed: bool
    evidence_id: str

    def evidence(self) -> dict[str, str]:
        return {
            "fact": EXIT_FACT_TRIGGERED,
            "reason": self.reason,
            "mode": self.mode,
            "side": self.side.value,
            "mark": format(self.mark_price, "f"),
            "persisted_stop": format(self.persisted_stop, "f"),
            "persisted_take_profit": format(self.persisted_take_profit, "f"),
            "compared_stop": format(self.compared_stop, "f"),
            "trend_level": format(self.trend_level, "f"),
            "degraded": "1" if self.degraded else "0",
            "trail_computed": "1" if self.trail_computed else "0",
            "evidence_id": self.evidence_id,
        }


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


def _exit_mode(params: T1Params) -> str:
    mode = str(getattr(params, "exit_mode", EXIT_MODE_TRAIL) or EXIT_MODE_TRAIL)
    if mode not in (EXIT_MODE_FIXED, EXIT_MODE_TRAIL):
        raise ValueError(f"未知退出模式 {mode}")
    return mode


def _persisted_target(position: OpenPosition, params: T1Params) -> Decimal:
    if position.take_profit is not None and position.take_profit > 0:
        return D(position.take_profit)
    if position.signal_atr > 0 and position.avg_price > 0:
        return take_profit_price(position.side, position.avg_price, position.signal_atr, params)
    return D("0")


def _barrier_reason(side: Side, mark: Decimal, stop: Decimal, target: Decimal) -> Optional[str]:
    """同一标记价上止损优先于止盈。两条边界在价格两侧时，一次报价只会碰到一边。"""
    if stop > 0 and side == Side.LONG and mark <= stop:
        return "STOP"
    if stop > 0 and side == Side.SHORT and mark >= stop:
        return "STOP"
    if target > 0 and side == Side.LONG and mark >= target:
        return "TAKE_PROFIT"
    if target > 0 and side == Side.SHORT and mark <= target:
        return "TAKE_PROFIT"
    return None


def _trigger(
    *,
    reason: str,
    mode: str,
    position: OpenPosition,
    mark: Decimal,
    persisted_stop: Decimal,
    persisted_target: Decimal,
    compared_stop: Decimal,
    trend_level: Decimal,
    close: Optional[Decimal],
    degraded: bool,
    trail_computed: bool,
) -> ExitTrigger:
    payload = {
        "reason": reason,
        "mode": mode,
        "side": position.side.value,
        "mark": format(mark, "f"),
        "persisted_stop": format(persisted_stop, "f"),
        "persisted_take_profit": format(persisted_target, "f"),
        "compared_stop": format(compared_stop, "f"),
        "trend_level": format(trend_level, "f"),
        "degraded": "1" if degraded else "0",
        "trail_computed": "1" if trail_computed else "0",
    }
    return ExitTrigger(
        reason=reason,
        mode=mode,
        side=position.side,
        mark_price=mark,
        persisted_stop=persisted_stop,
        persisted_take_profit=persisted_target,
        compared_stop=compared_stop,
        trend_level=trend_level,
        close=close,
        degraded=degraded,
        trail_computed=trail_computed,
        evidence_id=sha256_hex(payload),
    )


def decide_exit(
    position: OpenPosition,
    candles: Optional[Sequence[Candle]],
    *,
    mark_price: Optional[Decimal],
    params: T1Params,
) -> tuple[OpenPosition, Optional[ExitTrigger]]:
    """唯一退出核。固定模式不计算移动止损；跟踪模式才更新止损并允许通道退出。"""
    mode = _exit_mode(params)
    mark = D(mark_price) if mark_price is not None else None
    persisted_stop = D(position.stop)
    persisted_target = _persisted_target(position, params)
    bars_ready = bool(candles) and all(c.is_closed for c in candles or ())
    if mode == EXIT_MODE_FIXED or not bars_ready:
        if mark is None:
            return position, None
        reason = _barrier_reason(position.side, mark, persisted_stop, persisted_target)
        if reason is None:
            return position, None
        return position, _trigger(
            reason=reason,
            mode=mode,
            position=position,
            mark=mark,
            persisted_stop=persisted_stop,
            persisted_target=persisted_target,
            compared_stop=persisted_stop,
            trend_level=persisted_target if reason == "TAKE_PROFIT" else D("0"),
            close=candles[-1].close if bars_ready else None,
            degraded=not bars_ready,
            trail_computed=False,
        )

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
    updated = replace(position, extreme=extreme, stop=trail_stop(replace(position, extreme=extreme), candles, params))
    if mark is not None and position.signal_atr > 0 and position.side == Side.LONG and mark >= persisted_target and mark > updated.stop:
        reason = "TAKE_PROFIT"
    elif mark is not None and position.signal_atr > 0 and position.side == Side.SHORT and mark <= persisted_target and mark < updated.stop:
        reason = "TAKE_PROFIT"
    elif mark is not None and position.side == Side.LONG and mark <= updated.stop:
        reason = "STOP"
    elif mark is not None and position.side == Side.SHORT and mark >= updated.stop:
        reason = "STOP"
    else:
        reason = None
    trend_level = persisted_target if reason == "TAKE_PROFIT" else D("0")
    if reason is None and len(prior) >= params.exit_lookback:
        d20 = window_low(prior[-params.exit_lookback :])
        u20 = window_high(prior[-params.exit_lookback :])
        if position.side == Side.LONG and current.close < d20:
            reason = "TREND"
            trend_level = d20
        elif position.side == Side.SHORT and current.close > u20:
            reason = "TREND"
            trend_level = u20
    if reason is None:
        return updated, None
    recorded_mark = mark if mark is not None else current.close
    return updated, _trigger(
        reason=reason,
        mode=mode,
        position=position,
        mark=recorded_mark,
        persisted_stop=persisted_stop,
        persisted_target=persisted_target,
        compared_stop=updated.stop,
        trend_level=trend_level,
        close=current.close,
        degraded=False,
        trail_computed=True,
    )


def evaluate_exit(
    position: OpenPosition,
    candles: Sequence[Candle],
    *,
    mark_price: Optional[Decimal],
    params: T1Params,
) -> tuple[OpenPosition, Optional[ExitDecision]]:
    updated, trigger = decide_exit(position, candles, mark_price=mark_price, params=params)
    if trigger is None:
        return updated, None
    return updated, ExitDecision(
        trigger.reason,
        trigger.side,
        trigger.compared_stop,
        trigger.trend_level,
        trigger.mark_price,
        trigger.close,
    )


def apply_fill_to_state(
    *,
    signal: SignalIntent,
    fill_price: Decimal,
    qty: Decimal,
    params: T1Params,
    entry_time: Optional[datetime] = None,
) -> OpenPosition:
    stop = initial_stop(signal.side, fill_price, signal.atr, params)
    target = take_profit_price(signal.side, fill_price, signal.atr, params)
    return OpenPosition(
        symbol=signal.symbol,
        side=signal.side,
        qty=D(qty),
        avg_price=D(fill_price),
        signal_atr=signal.atr,
        stop=stop,
        extreme=D(fill_price),
        entry_event_time=entry_time or signal.event_time,
        take_profit=target,
    )


def next_open_fill_price(signal_close_time: datetime, subsequent: Sequence[Candle], side: Side, adverse_cost: Decimal) -> Optional[Decimal]:
    nxt = next((c for c in subsequent if c.event_time > signal_close_time and c.is_closed), None)
    if nxt is None:
        return None
    cost = D(adverse_cost)
    if side == Side.LONG:
        return nxt.open * (Decimal("1") + cost)
    return nxt.open * (Decimal("1") - cost)

"""EXIT-KERNEL-R1：固定模式不经过移动止损，回放和运行读同一个核。"""

from __future__ import annotations

from decimal import Decimal as D

from cts.ledger.models import Side
from cts.strategy.replay import next_mark_exit
from cts.strategy.t1 import EXIT_MODE_FIXED, EXIT_MODE_TRAIL, OpenPosition, T1Params, decide_exit
from tests.helpers import candle, mark, t_close


def _params(mode: str) -> T1Params:
    return T1Params(
        entry_lookback=5,
        exit_lookback=3,
        atr_period=3,
        warmup_bars=6,
        stop_multiple=D("8"),
        take_multiple=D("4"),
        exit_mode=mode,
        version="exit-kernel",
    )


def _position(side: Side, stop: str, target: str, extreme: str) -> OpenPosition:
    return OpenPosition(
        symbol="BTCUSDT",
        side=side,
        qty=D("1"),
        avg_price=D("100"),
        signal_atr=D("1"),
        stop=D(stop),
        extreme=D(extreme),
        entry_event_time=t_close(0),
        take_profit=D(target),
    )


def _bars(close: str) -> list:
    return [candle(i, open_=close, high=str(D(close) + 1), low=str(D(close) - 1), close=close) for i in range(1, 8)]


def test_fixed_counterexamples_ignore_a_trail_that_would_mask_the_target() -> None:
    long_pos = _position(Side.LONG, "92", "104", "100")
    updated, trigger = decide_exit(long_pos, _bars("130"), mark_price=D("105"), params=_params(EXIT_MODE_FIXED))
    assert trigger is not None and trigger.reason == "TAKE_PROFIT"
    assert trigger.trail_computed is False
    assert updated.stop == D("92")

    short_pos = _position(Side.SHORT, "108", "96", "100")
    updated, trigger = decide_exit(short_pos, _bars("70"), mark_price=D("95"), params=_params(EXIT_MODE_FIXED))
    assert trigger is not None and trigger.reason == "TAKE_PROFIT"
    assert updated.stop == D("108")


def test_gap_through_either_barrier_still_triggers() -> None:
    params = _params(EXIT_MODE_FIXED)
    long_pos = _position(Side.LONG, "92", "104", "100")
    _, stopped = decide_exit(long_pos, None, mark_price=D("70"), params=params)
    _, target = decide_exit(long_pos, None, mark_price=D("150"), params=params)
    assert stopped is not None and stopped.reason == "STOP" and stopped.degraded is True
    assert target is not None and target.reason == "TAKE_PROFIT"

    short_pos = _position(Side.SHORT, "108", "96", "100")
    _, stopped = decide_exit(short_pos, None, mark_price=D("130"), params=params)
    _, target = decide_exit(short_pos, None, mark_price=D("70"), params=params)
    assert stopped is not None and stopped.reason == "STOP"
    assert target is not None and target.reason == "TAKE_PROFIT"


def test_replay_mark_exit_matches_the_kernel() -> None:
    params = _params(EXIT_MODE_FIXED)
    position = _position(Side.LONG, "92", "104", "100")
    sample = mark(3, "105")
    _, reason, evidence = next_mark_exit(position, [sample], set(), sample.available_at, params)
    _, trigger = decide_exit(position, None, mark_price=sample.mark_price, params=params)
    assert trigger is not None
    assert reason == trigger.reason == "TAKE_PROFIT"
    assert evidence["fact"] == "TRIGGERED"
    assert evidence["evidence_id"] == trigger.evidence_id
    assert sample.event_time > position.entry_event_time


def test_trail_mode_still_uses_the_trailed_stop() -> None:
    position = _position(Side.LONG, "50", "400", "100")
    updated, trigger = decide_exit(position, _bars("130"), mark_price=D("100"), params=_params(EXIT_MODE_TRAIL))
    assert trigger is not None and trigger.reason == "STOP"
    assert trigger.trail_computed is True
    assert updated.stop > position.stop

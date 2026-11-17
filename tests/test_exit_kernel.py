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


def test_mark_sweep_run_and_degraded_agree_in_fixed_mode() -> None:
    """同一标记价上,运行路径(有K线)与降级路径(无K线)必须给出同一决策。"""
    params = _params(EXIT_MODE_FIXED)
    for side, stop, target, sweep in (
        (Side.LONG, "92", "104", [str(x) for x in range(80, 121, 2)]),
        (Side.SHORT, "108", "96", [str(x) for x in range(120, 79, -2)]),
    ):
        position = _position(side, stop, target, "100")
        for m in sweep:
            run_updated, run_trigger = decide_exit(position, _bars("100"), mark_price=D(m), params=params)
            deg_updated, deg_trigger = decide_exit(position, None, mark_price=D(m), params=params)
            assert (run_trigger is None) == (deg_trigger is None), m
            if run_trigger is not None:
                assert run_trigger.reason == deg_trigger.reason, m
                # 证据哈希按设计携带来源(运行/降级):决策一致,provenance 必须可区分
                assert run_trigger.degraded is False and deg_trigger.degraded is True, m
                assert run_trigger.evidence_id != deg_trigger.evidence_id, m
                assert run_trigger.compared_stop == deg_trigger.compared_stop == D(stop), m
            assert run_updated.stop == deg_updated.stop == D(stop), m


def test_trail_mode_replay_mark_scan_matches_the_degraded_branch() -> None:
    """跟踪模式下,回放的逐标记扫描与内核降级分支(读已持久化跟踪位)必须一致。"""
    params = _params(EXIT_MODE_TRAIL)
    position = _position(Side.LONG, "95", "104", "100")  # 已持久化的跟踪位 95
    for m in ("90", "94", "95", "96", "103", "104", "110"):
        sample = mark(3, m)
        scan_hit, scan_reason, _ = next_mark_exit(position, [sample], set(), sample.available_at, params)
        _, kernel_trigger = decide_exit(position, None, mark_price=D(m), params=params)
        assert (scan_hit is not None) == (kernel_trigger is not None), m
        if kernel_trigger is not None:
            assert scan_reason == kernel_trigger.reason, m


def test_trend_only_fires_in_trail_mode() -> None:
    """通道退出只属于跟踪模式;固定模式即使通道破位也不触发(演示配置语义)。"""
    closes = ["99", "98", "97", "96", "95", "94", "88"]
    falling = [
        candle(i + 1, open_="99", high="100", low=str(D(c) - 1), close=c)
        for i, c in enumerate(closes)
    ]
    position = _position(Side.LONG, "50", "140", "100")
    _, fixed_trigger = decide_exit(position, falling, mark_price=D("93"), params=_params(EXIT_MODE_FIXED))
    assert fixed_trigger is None, "固定模式不得因通道破位或移动止损触发"
    _, trail_trigger = decide_exit(position, falling, mark_price=D("93"), params=_params(EXIT_MODE_TRAIL))
    assert trail_trigger is not None and trail_trigger.reason == "TREND"
    assert trail_trigger.trend_level > 0


def test_evidence_id_is_deterministic_and_mark_sensitive() -> None:
    params = _params(EXIT_MODE_FIXED)
    position = _position(Side.LONG, "92", "104", "100")
    _, a = decide_exit(position, None, mark_price=D("105"), params=params)
    _, b = decide_exit(position, None, mark_price=D("105"), params=params)
    _, c = decide_exit(position, None, mark_price=D("104.1"), params=params)
    assert a.evidence_id == b.evidence_id
    assert a.evidence_id != c.evidence_id

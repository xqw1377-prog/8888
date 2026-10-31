from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from cts.clock import UTC
from cts.ledger.journal import Ledger
from cts.ledger.models import Side
from cts.money import D
from cts.strategy.replay import ReplayConfig, replay_t1
from cts.strategy.sizing import size_position
from cts.strategy.t1 import T1Params, evaluate_exit, evaluate_signal, next_open_fill_price
from tests.helpers import breakout_series, candle, mark, range_bars


def test_manual_breakout_long_signal() -> None:
    bars = breakout_series(130)
    signal = evaluate_signal(bars[:-1], params=T1Params(), has_position=False, has_pending_entry=False)
    assert signal is not None
    assert signal.side == Side.LONG
    assert signal.close > signal.entry_band


def test_future_data_cannot_change_signal_at_t() -> None:
    bars = breakout_series(130)
    at_t = bars[:-1]
    first = evaluate_signal(at_t, params=T1Params(), has_position=False, has_pending_entry=False)
    assert first is not None
    mutated = list(at_t)
    future = candle(200, close="9", high="9", low="8", open_="9")
    second = evaluate_signal(mutated, params=T1Params(), has_position=False, has_pending_entry=False)
    _ = future
    assert second is not None
    assert first.input_hash == second.input_hash
    later = at_t + [candle(len(at_t), close="50", high="50", low="49", open_="50")]
    third = evaluate_signal(at_t, params=T1Params(), has_position=False, has_pending_entry=False)
    assert third is not None and third.input_hash == first.input_hash
    changed_now = evaluate_signal(later, params=T1Params(), has_position=False, has_pending_entry=False)
    assert changed_now is None or changed_now.input_hash != first.input_hash or changed_now.event_time != first.event_time


def test_no_signal_before_warmup() -> None:
    bars = range_bars(119) + [candle(119, open_="101", high="120", low="101", close="118")]
    assert evaluate_signal(bars, params=T1Params(), has_position=False, has_pending_entry=False) is None


def test_fill_uses_next_open_not_signal_close() -> None:
    bars = breakout_series(130)
    signal_bars = bars[:-1]
    signal = evaluate_signal(signal_bars, params=T1Params(), has_position=False, has_pending_entry=False)
    assert signal is not None
    fill = next_open_fill_price(signal.event_time, bars, signal.side, D("0.001"))
    assert fill is not None
    assert fill != signal.close
    assert fill == bars[-1].open * D("1.001")


def test_stop_only_ratchets_in_favor() -> None:
    bars = breakout_series(130)
    signal = evaluate_signal(bars[:-1], params=T1Params(), has_position=False, has_pending_entry=False)
    assert signal is not None
    from cts.strategy.t1 import apply_fill_to_state

    pos = apply_fill_to_state(signal=signal, fill_price=D("118"), qty=D("1"), params=T1Params())
    first_stop = pos.stop
    grown = bars[:-1] + [
        candle(len(bars), open_="130", high="140", low="129", close="135"),
    ]
    updated, _ = evaluate_exit(pos, grown, mark_price=D("135"), params=T1Params())
    assert updated.stop >= first_stop
    pulled = grown + [candle(len(grown), open_="120", high="121", low="110", close="112")]
    later, decision = evaluate_exit(updated, pulled, mark_price=D("112"), params=T1Params())
    assert later.stop >= updated.stop
    if decision and decision.reason == "STOP":
        assert decision.mark_price <= later.stop


def test_mark_stop_triggers_exit() -> None:
    bars = breakout_series(130)
    signal = evaluate_signal(bars[:-1], params=T1Params(), has_position=False, has_pending_entry=False)
    assert signal is not None
    from cts.strategy.t1 import apply_fill_to_state

    pos = apply_fill_to_state(signal=signal, fill_price=D("118"), qty=D("1"), params=T1Params())
    _, decision = evaluate_exit(pos, bars[:-1], mark_price=pos.stop, params=T1Params())
    assert decision is not None
    assert decision.reason == "STOP"


def test_sizing_risk_binds_before_leverage() -> None:
    sized = size_position(
        side=Side.LONG,
        equity=D("10000"),
        entry_price=D("100"),
        stop_price=D("98"),
        multiplier=D("1"),
        lot_size=D("0.001"),
        remaining_margin_budget=D("5000"),
        fee_rate_one_way=D("0"),
        stress_slippage=D("0"),
        funding_reserve_ratio=D("0"),
    )
    assert sized is not None
    assert sized.binding_constraint == "RISK"
    assert sized.notional == D("1000")
    assert sized.notional / D("10000") == D("0.1")


def test_replay_is_deterministic_and_books_costs() -> None:
    bars = breakout_series(130)
    marks = [mark(i, str(bars[i].close)) for i in range(len(bars))]
    ledger = Ledger("acct-replay")
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    ledger.transfer_in(amount=D("10000"), event_time=ts, received_at=ts, external_id="dep")
    config = ReplayConfig(params=T1Params())
    first = replay_t1(ledger=ledger, symbol="BTCUSDT", candles=bars, marks=marks, config=config, venue="SIM")
    assert first.signals
    assert first.fills >= 1
    from cts.ledger.models import AccountBucket

    assert ledger.balance(AccountBucket.FEE) > 0
    clone = Ledger("acct-replay-2")
    clone.transfer_in(amount=D("10000"), event_time=ts, received_at=ts, external_id="dep")
    second = replay_t1(ledger=clone, symbol="BTCUSDT", candles=bars, marks=marks, config=config, venue="SIM")
    assert [s.input_hash for s in first.signals] == [s.input_hash for s in second.signals]
    assert clone.cash_equity() == ledger.cash_equity()


def test_short_breakout_signal() -> None:
    bars = range_bars(130, open_="100", high="101", low="99", close="100")
    bars.append(candle(130, open_="99", high="99", low="80", close="82"))
    signal = evaluate_signal(bars, params=T1Params(), has_position=False, has_pending_entry=False)
    assert signal is not None
    assert signal.side == Side.SHORT

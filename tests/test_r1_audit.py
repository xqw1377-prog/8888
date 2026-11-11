"""R1：对照独立审计的最小复现。通过这些测试也不等于签发 PASS。"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from cts.data.models import DataGrade, FundingSettlement, MarkSample
from cts.data.store import DataStore
from cts.errors import ConflictError, LedgerImbalanceError, NewRiskBlockedError, RecoveryStaleError
from cts.ledger.journal import Ledger
from cts.ledger.models import AccountBucket, LedgerEventType, Side
from cts.money import D
from cts.risk.models import SystemState
from cts.strategy.replay import ReplayConfig, replay_t1
from cts.strategy.t1 import T1Params, apply_fill_to_state, evaluate_exit, evaluate_signal
from tests.helpers import ORIGIN, breakout_series, candle, mark, t_close
from tests.test_ledger import TS, funded
from tests.test_risk_p004a import NOW, bound_evidence, make_engine, req


def test_close_rejects_without_zeroing_position() -> None:
    ledger = funded()
    ledger.lock_margin(amount=D("5"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    ledger.apply_fill(
        symbol="BTCUSDT",
        side=Side.LONG,
        qty=D("1"),
        price=D("100"),
        multiplier=D("1"),
        event_time=TS,
        received_at=TS,
        external_id="f1",
        venue="SIM",
        venue_trade_id="t1",
    )
    equity_before = ledger.cash_equity()
    with pytest.raises(LedgerImbalanceError):
        ledger.apply_fill(
            symbol="BTCUSDT",
            side=Side.SHORT,
            qty=D("1"),
            price=D("80"),
            multiplier=D("1"),
            event_time=TS,
            received_at=TS,
            external_id="f2",
            venue="SIM",
            venue_trade_id="t2",
            reduce_only=True,
        )
    assert ledger.position("BTCUSDT").qty == D("1")
    assert ledger.position("BTCUSDT").side == Side.LONG
    assert ledger.cash_equity() == equity_before


def test_funding_reject_does_not_reduce_equity() -> None:
    ledger = funded()
    ledger.lock_margin(amount=D("100"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-btc", venue="SIM")
    ledger.lock_margin(amount=D("100"), symbol="ETHUSDT", event_time=TS, received_at=TS, external_id="im-eth", venue="SIM")
    before = ledger.cash_equity()
    with pytest.raises(LedgerImbalanceError):
        ledger.post_funding(
            payment=D("-150"),
            event_time=TS,
            received_at=TS,
            external_id="fund-1",
            venue="SIM",
            symbol="BTCUSDT",
            from_isolated=True,
        )
    assert ledger.cash_equity() == before
    assert ledger.position("BTCUSDT").isolated_margin == D("100")
    assert ledger.position("BTCUSDT").funding_cum == D("0")


def test_changed_funding_amount_is_conflict() -> None:
    ledger = funded()
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    ledger.post_funding(
        payment=D("-1"),
        event_time=TS,
        received_at=TS,
        external_id="fund-1",
        venue="SIM",
        symbol="BTCUSDT",
        from_isolated=True,
    )
    with pytest.raises(ConflictError):
        ledger.post_funding(
            payment=D("-999"),
            event_time=TS,
            received_at=TS,
            external_id="fund-1",
            venue="SIM",
            symbol="BTCUSDT",
            from_isolated=True,
        )
    assert ledger.position("BTCUSDT").funding_cum == D("-1")
    assert ledger.cash_equity() == D("9999")


def test_forged_checksum_does_not_hide_new_content() -> None:
    store = DataStore()
    original = candle(0)
    store.put(original)
    forged = candle(0, close="101", high="102", low="99", open_="100")
    object.__setattr__(forged, "checksum", original.checksum)
    with pytest.raises(ConflictError):
        store.put(forged)
    from dataclasses import replace

    with pytest.raises(ConflictError):
        replace(original, close=D("101"), high=D("102"), checksum=original.checksum)


def test_candle_aligned_marks_are_not_liquidation_validated() -> None:
    from cts.data.gaps import classify_gaps

    bars = [candle(i) for i in range(4)]
    marks = [mark(i, "100") for i in range(4)]
    report = classify_gaps(
        candles=bars,
        marks=marks,
        expected_sampling_interval=timedelta(hours=4),
        range_start=t_close(0),
        range_end=t_close(3),
    )
    assert report.grade == DataGrade.SIGNAL_RESEARCH_ONLY
    assert report.grade != DataGrade.EXECUTION_PNL_VALIDATED
    assert report.grade != DataGrade.LIQUIDATION_REPLAY_VALIDATED


def test_fault_recovery_does_not_clear_blockers(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine.add_blocker("PROTECTION_OVERDUE", SystemState.FAULT, now=NOW)
    snap = engine.create_recovery_snapshot(now=NOW)
    with pytest.raises(NewRiskBlockedError) as err:
        engine.apply_recovery(snap, now=NOW)
    assert err.value.reason_code == "FAULT_REQUIRES_INVESTIGATION"
    occ = engine.occupancy()
    assert occ.system_state == SystemState.FAULT
    assert {b.reason_code for b in occ.blockers} == {"PROTECTION_OVERDUE"}
    row = engine.store._conn.execute(
        "SELECT result, reason_code FROM recovery_attempts WHERE account_id=?",
        ("acct-1",),
    ).fetchone()
    assert row["result"] == "FAILED"
    assert row["reason_code"] == "FAULT_REQUIRES_INVESTIGATION"


def test_stale_recovery_failure_is_persisted(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine.add_blocker("DAILY_LOSS", SystemState.PAUSED, now=NOW)
    engine.clear_blocker("DAILY_LOSS", resolution_evidence_id=bound_evidence(engine, "DAILY_LOSS"), now=NOW)
    snap = engine.create_recovery_snapshot(now=NOW)
    approval = engine.approve_recovery(snap, approved_by="owner", now=NOW)
    engine.add_blocker("UNKNOWN_ORDER", SystemState.PAUSED, now=NOW)
    with pytest.raises(RecoveryStaleError):
        engine.apply_recovery(snap, approval_id=approval, now=NOW)
    row = engine.store._conn.execute(
        "SELECT result, reason_code FROM recovery_attempts WHERE account_id=?",
        ("acct-1",),
    ).fetchone()
    assert row["result"] == "FAILED"
    assert row["reason_code"] == "RECOVERY_SNAPSHOT_STALE"
    assert engine.occupancy().system_state == SystemState.PAUSED


def test_unknown_since_is_not_reset_and_cancel_cannot_release(tmp_path) -> None:
    engine = make_engine(tmp_path)
    reserved = engine.reserve([req("BTCUSDT", "sig-unknown")], now=NOW)[0]
    first = engine.mark_unknown(reserved.reservation_id, now=NOW)
    later = NOW + timedelta(minutes=5)
    second = engine.mark_unknown(reserved.reservation_id, now=later)
    assert second.unknown_since == first.unknown_since == NOW
    pending = engine.occupancy().pending_risk
    engine.request_cancel(reserved.reservation_id, now=later)
    kept = next(r for r in engine.occupancy().reservations if r.reservation_id == reserved.reservation_id)
    assert kept.status.value == "UNKNOWN"
    with pytest.raises(NewRiskBlockedError):
        engine.confirm_cancel(reserved.reservation_id, D("0"), now=later)
    assert engine.occupancy().pending_risk == pending


def test_late_cumulative_fill_after_release_updates_occupancy(tmp_path) -> None:
    engine = make_engine(tmp_path)
    reserved = engine.reserve([req("BTCUSDT", "sig-late")], now=NOW)[0]
    engine.confirm_cancel(reserved.reservation_id, reserved.qty * D("0.4"), now=NOW)
    assert engine.occupancy().positions[0].qty == reserved.qty * D("0.4")
    engine.confirm_cancel(reserved.reservation_id, reserved.qty * D("0.6"), now=NOW)
    occ = engine.occupancy()
    assert occ.positions[0].qty == reserved.qty * D("0.6")
    assert occ.position_risk == reserved.risk_amount * D("0.6")
    assert occ.pending_risk == D("0")


def test_joint_scale_does_not_reject_the_batch(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine.sync_position(
        symbol="BTCUSDT",
        side=Side.LONG,
        qty=D("1"),
        notional=D("100"),
        risk_amount=D("30"),
        isolated_margin=D("50"),
        now=NOW,
    )
    created = engine.reserve([req("BTCUSDT", "sig-btc"), req("ETHUSDT", "sig-eth")], now=NOW)
    assert len(created) == 2
    total_new = sum((row.risk_amount for row in created), start=D("0"))
    assert total_new <= D("10.0000001")
    assert all(row.risk_amount < D("10") for row in created)
    assert engine.occupancy().total_risk <= D("40.0000001")


def test_paused_recovery_still_reaches_running(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine.add_blocker("DAILY_LOSS", SystemState.PAUSED, now=NOW)
    blocked = engine.create_recovery_snapshot(now=NOW)
    with pytest.raises(NewRiskBlockedError) as err:
        engine.apply_recovery(blocked, now=NOW)
    assert err.value.reason_code == "RECOVERY_APPROVAL_REQUIRED"
    assert engine.occupancy().system_state == SystemState.PAUSED
    assert {b.reason_code for b in engine.occupancy().blockers} == {"DAILY_LOSS"}
    state = engine.clear_blocker("DAILY_LOSS", resolution_evidence_id=bound_evidence(engine, "DAILY_LOSS"), now=NOW)
    assert state == SystemState.PAUSED
    assert engine.occupancy().blockers == ()
    snap = engine.create_recovery_snapshot(now=NOW)
    approval = engine.approve_recovery(snap, approved_by="owner", now=NOW)
    applied = engine.apply_recovery(snap, approval_id=approval, now=NOW)
    assert applied.result == "APPLIED"
    assert engine.occupancy().system_state == SystemState.RUNNING
    assert engine.occupancy().blockers == ()


def test_intrabar_stop_is_not_ignored_after_recovery() -> None:
    bars = breakout_series(130)
    bars.append(candle(132, open_="118", high="119", low="117", close="118"))
    marks = [mark(i, format(bars[i].close, "f")) for i in range(len(bars) - 1)]
    breach_at = t_close(131) + timedelta(hours=1)
    marks.append(
        MarkSample(
            venue="SIM",
            symbol="BTCUSDT",
            event_time=breach_at,
            received_at=breach_at,
            available_at=breach_at,
            mark_price=D("110"),
            index_price=D("110"),
            source_id="breach",
        )
    )
    marks.append(mark(132, "118"))
    ledger = Ledger("acct-replay")
    ledger.transfer_in(amount=D("10000"), event_time=ORIGIN, received_at=ORIGIN, external_id="dep")
    result = replay_t1(
        ledger=ledger,
        symbol="BTCUSDT",
        candles=bars,
        marks=marks,
        config=ReplayConfig(params=T1Params()),
        venue="SIM",
    )
    assert result.fills == 1
    assert result.exits == 1


def test_trail_does_not_use_signal_bar_extreme() -> None:
    bars = breakout_series(130)
    signal = evaluate_signal(bars[:-1], params=T1Params(), has_position=False, has_pending_entry=False)
    assert signal is not None
    pos = apply_fill_to_state(signal=signal, fill_price=signal.close, qty=D("1"), params=T1Params())
    updated, _ = evaluate_exit(pos, bars[:-1], mark_price=signal.close, params=T1Params())
    assert updated.extreme == signal.close
    assert updated.stop == pos.stop
    assert signal.entry_band < bars[-2].high


def test_fill_id_quantity_conflict_is_rejected(tmp_path) -> None:
    engine = make_engine(tmp_path)
    reserved = engine.reserve([req("BTCUSDT", "sig-fill")], now=NOW)[0]
    engine.apply_fill(reserved.reservation_id, reserved.qty * D("0.1"), fill_id="fill-1", now=NOW)
    with pytest.raises(ConflictError):
        engine.apply_fill(reserved.reservation_id, reserved.qty * D("0.9"), fill_id="fill-1", now=NOW)
    occ = engine.occupancy()
    assert occ.positions[0].qty == reserved.qty * D("0.1")


def test_entry_bar_stop_before_close_exits() -> None:
    bars = breakout_series(130)
    breach_at = t_close(130) + timedelta(hours=1)
    marks = [mark(i, format(bars[i].close, "f")) for i in range(len(bars))]
    marks.append(
        MarkSample(
            venue="SIM",
            symbol="BTCUSDT",
            event_time=breach_at,
            received_at=breach_at,
            available_at=breach_at,
            mark_price=D("110"),
            index_price=D("110"),
            source_id="entry-breach",
        )
    )
    ledger = Ledger("acct-entry-stop")
    ledger.transfer_in(amount=D("10000"), event_time=ORIGIN, received_at=ORIGIN, external_id="dep")
    result = replay_t1(
        ledger=ledger,
        symbol="BTCUSDT",
        candles=bars,
        marks=marks,
        config=ReplayConfig(params=T1Params()),
        venue="SIM",
    )
    assert result.fills == 1
    assert result.exits == 1
    fills = [event for event in ledger.events() if event.event_type == LedgerEventType.FILL]
    opened = next(event for event in fills if event.metadata.get("note") == "open")
    closed = next(event for event in fills if event.metadata.get("note") == "reduce")
    assert result.grade == DataGrade.SIGNAL_RESEARCH_ONLY
    assert opened.event_time == t_close(130) + timedelta(seconds=1)
    assert opened.event_time > t_close(130)
    assert opened.metadata["price_source"] == "NEXT_OPEN"
    assert opened.metadata["quote_grade"] == "RESEARCH_APPROXIMATE"
    assert closed.event_time == breach_at
    assert closed.metadata["price"] == "110"
    assert closed.metadata["price_source"] == "MARK"
    assert closed.metadata["quote_grade"] == "EXECUTABLE_QUOTE"
    assert opened.event_time < closed.event_time < t_close(131)
    assert all(fills[i].event_time <= fills[i + 1].event_time for i in range(len(fills) - 1))


def test_mark_is_not_used_before_available_at() -> None:
    bars = breakout_series(130)
    breach_at = t_close(130) + timedelta(hours=1)
    late = t_close(131) + timedelta(hours=2)
    marks = [mark(i, format(bars[i].close, "f")) for i in range(len(bars))]
    marks.append(
        MarkSample(
            venue="SIM",
            symbol="BTCUSDT",
            event_time=breach_at,
            received_at=late,
            available_at=late,
            mark_price=D("110"),
            index_price=D("110"),
            source_id="late-breach",
        )
    )
    ledger = Ledger("acct-late-mark")
    ledger.transfer_in(amount=D("10000"), event_time=ORIGIN, received_at=ORIGIN, external_id="dep")
    result = replay_t1(
        ledger=ledger,
        symbol="BTCUSDT",
        candles=bars,
        marks=marks,
        config=ReplayConfig(params=T1Params()),
        venue="SIM",
    )
    assert result.fills == 1
    assert result.exits == 0


def test_trail_uses_close_not_high() -> None:
    bars = breakout_series(130)
    signal = evaluate_signal(bars[:-1], params=T1Params(), has_position=False, has_pending_entry=False)
    assert signal is not None
    pos = apply_fill_to_state(signal=signal, fill_price=signal.close, qty=D("1"), params=T1Params())
    later = list(bars[:-1]) + [candle(200, open_=format(signal.close, "f"), high="500", low=format(signal.close, "f"), close=format(signal.close, "f"))]
    updated, _ = evaluate_exit(pos, later, mark_price=signal.close, params=T1Params())
    assert updated.extreme == signal.close
    assert updated.stop == pos.stop


def test_unknown_evidence_and_approver_cannot_resume(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine.register_recovery_approver("owner", now=NOW)
    engine.add_blocker("DAILY_LOSS", SystemState.PAUSED, now=NOW)
    with pytest.raises(NewRiskBlockedError) as missing:
        engine.clear_blocker("DAILY_LOSS", resolution_evidence_id="does-not-exist", now=NOW)
    assert missing.value.reason_code == "RESOLUTION_EVIDENCE_INVALID"
    assert {b.reason_code for b in engine.occupancy().blockers} == {"DAILY_LOSS"}
    snap = engine.create_recovery_snapshot(now=NOW)
    with pytest.raises(NewRiskBlockedError) as stranger:
        engine.approve_recovery(snap, approved_by="stranger", now=NOW)
    assert stranger.value.reason_code == "RECOVERY_APPROVER_UNKNOWN"
    assert engine.occupancy().system_state == SystemState.PAUSED


def test_recovery_approval_expires_after_100_days(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine.add_blocker("DAILY_LOSS", SystemState.PAUSED, now=NOW)
    engine.clear_blocker("DAILY_LOSS", resolution_evidence_id=bound_evidence(engine, "DAILY_LOSS"), now=NOW)
    snap = engine.create_recovery_snapshot(now=NOW)
    approval = engine.approve_recovery(snap, approved_by="owner", now=NOW)
    later = NOW + timedelta(days=100)
    with pytest.raises(RecoveryStaleError) as err:
        engine.apply_recovery(snap, approval_id=approval, now=later)
    assert err.value.reason_code == "RECOVERY_APPROVAL_EXPIRED"
    assert engine.occupancy().system_state == SystemState.PAUSED


def test_fill_id_cannot_attach_to_another_reservation(tmp_path) -> None:
    engine = make_engine(tmp_path)
    btc, eth = engine.reserve([req("BTCUSDT", "sig-btc"), req("ETHUSDT", "sig-eth")], now=NOW)
    qty = btc.qty * D("0.1")
    engine.apply_fill(btc.reservation_id, qty, fill_id="fill-btc", now=NOW)
    with pytest.raises(ConflictError):
        engine.apply_fill(eth.reservation_id, qty, fill_id="fill-btc", now=NOW)
    reservations = {row.symbol: row for row in engine.occupancy().reservations}
    assert reservations["BTCUSDT"].filled_qty == qty
    assert reservations["ETHUSDT"].filled_qty == D("0")


def test_late_mark_cannot_backdate_exit() -> None:
    bars = breakout_series(130)
    bars.append(candle(132, open_="118.5", high="119", low="117", close="118"))
    breach_at = t_close(130) + timedelta(hours=1)
    visible = bars[-1].available_at
    marks = [mark(i, format(bars[i].close, "f")) for i in range(len(bars) - 1)]
    marks.append(
        MarkSample(
            venue="SIM",
            symbol="BTCUSDT",
            event_time=breach_at,
            received_at=visible,
            available_at=visible,
            mark_price=D("110"),
            index_price=D("110"),
            source_id="late-backfill",
        )
    )
    ledger = Ledger("acct-backfill")
    ledger.transfer_in(amount=D("10000"), event_time=ORIGIN, received_at=ORIGIN, external_id="dep")
    result = replay_t1(
        ledger=ledger,
        symbol="BTCUSDT",
        candles=bars,
        marks=marks,
        config=ReplayConfig(params=T1Params()),
        venue="SIM",
    )
    fills = [event for event in ledger.events() if event.event_type == LedgerEventType.FILL]
    opened = next(event for event in fills if event.metadata.get("note") == "open")
    closed = next(event for event in fills if event.metadata.get("note") == "reduce")
    assert result.fills == 1
    assert result.exits == 1
    assert result.grade == DataGrade.SIGNAL_RESEARCH_ONLY
    assert opened.event_time == t_close(130) + timedelta(seconds=1)
    assert opened.metadata["quote_grade"] == "RESEARCH_APPROXIMATE"
    assert closed.event_time == visible
    assert closed.event_time > breach_at
    assert closed.metadata["price"] == "110"
    assert closed.metadata["price_source"] == "MARK"
    assert closed.metadata["quote_grade"] == "RESEARCH_APPROXIMATE"
    assert all(event.event_time <= event.received_at for event in fills)
    assert all(fills[i].event_time <= fills[i + 1].event_time for i in range(len(fills) - 1))


def test_one_millisecond_mark_delay_still_stops() -> None:
    bars = breakout_series(130)
    breach_at = t_close(130) + timedelta(hours=1)
    visible = breach_at + timedelta(milliseconds=1)
    marks = [mark(i, format(bars[i].close, "f")) for i in range(len(bars))]
    marks.append(
        MarkSample(
            venue="SIM",
            symbol="BTCUSDT",
            event_time=breach_at,
            received_at=visible,
            available_at=visible,
            mark_price=D("110"),
            index_price=D("110"),
            source_id="delay-1ms",
        )
    )
    ledger = Ledger("acct-delay-1ms")
    ledger.transfer_in(amount=D("10000"), event_time=ORIGIN, received_at=ORIGIN, external_id="dep")
    result = replay_t1(
        ledger=ledger,
        symbol="BTCUSDT",
        candles=bars,
        marks=marks,
        config=ReplayConfig(params=T1Params()),
        venue="SIM",
    )
    fills = [event for event in ledger.events() if event.event_type == LedgerEventType.FILL]
    closed = next(event for event in fills if event.metadata.get("note") == "reduce")
    assert result.exits == 1
    assert result.grade == DataGrade.SIGNAL_RESEARCH_ONLY
    assert closed.event_time == visible
    assert closed.metadata["price"] == "110"
    assert closed.metadata["price_source"] == "MARK"
    assert closed.metadata["quote_grade"] == "RESEARCH_APPROXIMATE"


def test_stop_uses_mark_not_close() -> None:
    bars = breakout_series(130)
    bars.append(candle(132, open_="118", high="118", low="90", close="100"))
    marks = [mark(i, "118") for i in range(len(bars))]
    ledger = Ledger("acct-mark-stop")
    ledger.transfer_in(amount=D("10000"), event_time=ORIGIN, received_at=ORIGIN, external_id="dep")
    result = replay_t1(
        ledger=ledger,
        symbol="BTCUSDT",
        candles=bars,
        marks=marks,
        config=ReplayConfig(params=T1Params()),
        venue="SIM",
    )
    assert result.fills == 1
    assert result.exits == 0
    assert result.grade == DataGrade.SIGNAL_RESEARCH_ONLY
    assert all("STOP" not in event.external_id for event in ledger.events())


def test_evidence_cannot_clear_a_later_fault_instance(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine.register_recovery_approver("owner", now=NOW)
    early = engine.record_resolution_evidence("DAILY_LOSS", statement="before-fault", recorded_by="owner", now=NOW)
    later = NOW + timedelta(hours=1)
    engine.add_blocker("DAILY_LOSS", SystemState.PAUSED, now=later)
    with pytest.raises(NewRiskBlockedError) as err:
        engine.clear_blocker("DAILY_LOSS", resolution_evidence_id=early, now=later)
    assert err.value.reason_code == "RESOLUTION_EVIDENCE_INVALID"
    assert {b.reason_code for b in engine.occupancy().blockers} == {"DAILY_LOSS"}
    engine.add_blocker("DRAWDOWN_6", SystemState.PAUSED, now=NOW)
    first = engine.record_resolution_evidence("DRAWDOWN_6", statement="instance-1", recorded_by="owner", now=NOW)
    second = engine.record_resolution_evidence("DRAWDOWN_6", statement="instance-1b", recorded_by="owner", now=NOW)
    engine.clear_blocker("DRAWDOWN_6", resolution_evidence_id=second, now=NOW)
    engine.add_blocker("DRAWDOWN_6", SystemState.PAUSED, now=later)
    with pytest.raises(NewRiskBlockedError) as err:
        engine.clear_blocker("DRAWDOWN_6", resolution_evidence_id=first, now=later)
    assert err.value.reason_code == "RESOLUTION_EVIDENCE_INVALID"
    assert "DRAWDOWN_6" in {b.reason_code for b in engine.occupancy().blockers}


def test_forged_snapshot_time_cannot_renew_approval(tmp_path) -> None:
    engine = make_engine(tmp_path)
    old = NOW - timedelta(days=100)
    engine.add_blocker("DAILY_LOSS", SystemState.PAUSED, now=old)
    engine.clear_blocker("DAILY_LOSS", resolution_evidence_id=bound_evidence(engine, "DAILY_LOSS", now=old), now=old)
    snap = engine.create_recovery_snapshot(now=old)
    forged = replace(snap, created_at=NOW)
    with pytest.raises(RecoveryStaleError) as err:
        engine.approve_recovery(forged, approved_by="owner", now=NOW)
    assert err.value.reason_code == "RECOVERY_APPROVAL_EXPIRED"
    assert engine.occupancy().system_state == SystemState.PAUSED


def _settlement(settlement_id: str, event_time, available_at, payment: str) -> FundingSettlement:
    return FundingSettlement(
        venue="SIM",
        symbol="BTCUSDT",
        event_time=event_time,
        received_at=available_at,
        available_at=available_at,
        settlement_id=settlement_id,
        funding_rate=D("0.0001"),
        payment=D(payment),
        interval_minutes=D("480"),
        source_id=settlement_id,
    )


def test_replay_posts_available_funding_and_exit_slippage_separately() -> None:
    bars = breakout_series(130)
    breach_at = t_close(130) + timedelta(hours=1)
    held_at = t_close(130) + timedelta(minutes=30)
    marks = [mark(i, format(bars[i].close, "f")) for i in range(len(bars))]
    marks.append(
        MarkSample(
            venue="SIM",
            symbol="BTCUSDT",
            event_time=breach_at,
            received_at=breach_at,
            available_at=breach_at,
            mark_price=D("110"),
            index_price=D("110"),
            source_id="entry-breach",
        )
    )
    fundings = [
        _settlement("set-before", t_close(100), t_close(100), "-1"),
        _settlement("set-held", held_at, held_at, "-0.5"),
        _settlement("set-held", held_at, held_at, "-0.5"),
        _settlement("set-late", breach_at + timedelta(minutes=30), bars[-1].available_at + timedelta(hours=2), "-9"),
    ]
    ledger = Ledger("acct-funding")
    ledger.transfer_in(amount=D("10000"), event_time=ORIGIN, received_at=ORIGIN, external_id="dep")
    result = replay_t1(
        ledger=ledger,
        symbol="BTCUSDT",
        candles=bars,
        marks=marks,
        config=ReplayConfig(params=T1Params()),
        venue="SIM",
        fundings=fundings,
    )
    assert result.grade == DataGrade.SIGNAL_RESEARCH_ONLY
    assert result.funding_posted == 1
    funding_events = [event for event in ledger.events() if event.event_type == LedgerEventType.FUNDING]
    assert len(funding_events) == 1
    assert funding_events[0].metadata["payment"] == "-0.5"
    assert funding_events[0].event_time == held_at
    assert ledger.position("BTCUSDT").funding_cum == D("-0.5")
    assert ledger.balance(AccountBucket.FUNDING) == D("0.5")
    fills = [event for event in ledger.events() if event.event_type == LedgerEventType.FILL]
    closed = next(event for event in fills if event.metadata.get("note") == "reduce")
    assert closed.metadata["price"] == "110"
    assert closed.metadata["price_source"] == "MARK"
    notional = D(closed.metadata["qty"]) * D(closed.metadata["price"])
    slips = [event for event in ledger.events() if event.external_id.startswith("slip-exit:")]
    exit_fees = [event for event in ledger.events() if event.external_id.startswith("fee-exit:")]
    assert len(slips) == 1
    assert len(exit_fees) == 1
    assert sum(line.debit for line in slips[0].lines if line.bucket == AccountBucket.FEE) == notional * D("0.0005")
    assert sum(line.debit for line in exit_fees[0].lines if line.bucket == AccountBucket.FEE) == notional * D("0.0005")
    assert all(not event.external_id.startswith("slip-entry:") for event in ledger.events())


def test_funding_visible_after_exit_is_booked_without_backdating() -> None:
    bars = breakout_series(130)
    breach_at = t_close(130) + timedelta(hours=1)
    settled_at = t_close(130) + timedelta(minutes=30)
    visible_at = t_close(130) + timedelta(hours=2)
    marks = [mark(i, format(bars[i].close, "f")) for i in range(len(bars))]
    marks.append(
        MarkSample(
            venue="SIM",
            symbol="BTCUSDT",
            event_time=breach_at,
            received_at=breach_at,
            available_at=breach_at,
            mark_price=D("110"),
            index_price=D("110"),
            source_id="entry-breach",
        )
    )
    ledger = Ledger("acct-late-funding")
    ledger.transfer_in(amount=D("10000"), event_time=ORIGIN, received_at=ORIGIN, external_id="dep")
    result = replay_t1(
        ledger=ledger,
        symbol="BTCUSDT",
        candles=bars,
        marks=marks,
        config=ReplayConfig(params=T1Params()),
        venue="SIM",
        fundings=[_settlement("set-after-exit", settled_at, visible_at, "-0.5")],
    )
    assert result.grade == DataGrade.SIGNAL_RESEARCH_ONLY
    assert result.funding_posted == 1
    funding = next(event for event in ledger.events() if event.event_type == LedgerEventType.FUNDING)
    assert funding.event_time == visible_at
    assert funding.event_time > breach_at
    assert funding.metadata["payment"] == "-0.5"
    closed = next(event for event in ledger.events() if event.metadata.get("note") == "reduce")
    assert closed.metadata["price"] == "110"


def _tp_replay(bars, marks, params=None):
    ledger = Ledger("acct-tp")
    ledger.transfer_in(amount=D("10000"), event_time=ORIGIN, received_at=ORIGIN, external_id="dep")
    result = replay_t1(
        ledger=ledger,
        symbol="BTCUSDT",
        candles=bars,
        marks=marks,
        config=ReplayConfig(params=params or T1Params()),
        venue="SIM",
    )
    return ledger, result


def test_take_profit_is_booked_as_exit_and_releases_margin() -> None:
    bars = breakout_series(130)
    bars.append(candle(132, open_="118.5", high="130", low="118", close="129"))
    marks = [mark(132, "135")]
    ledger, result = _tp_replay(bars, marks)
    assert result.fills == 1
    assert result.exits == 1
    assert result.exit_reasons == {"TAKE_PROFIT": 1}
    pos = ledger.position("BTCUSDT")
    assert pos.qty == 0 and pos.side is None
    # 清仓后逐仓保证金必须全部划回钱包
    assert pos.isolated_margin == 0
    assert ledger.balance(AccountBucket.ISOLATED_MARGIN) == 0
    kinds = [e.event_type for e in ledger.events()]
    assert LedgerEventType.MARGIN_RELEASE in kinds
    exits = [e for e in ledger.events() if e.metadata.get("exit_reason")]
    assert len(exits) == result.exits
    assert exits[0].metadata["exit_reason"] == "TAKE_PROFIT"
    assert exits[0].metadata["price_source"] == "MARK"
    assert exits[0].metadata["quote_grade"] == "RESEARCH_APPROXIMATE"


def test_tp_and_stop_event_order_is_respected() -> None:
    tail = [
        candle(132, open_="118.5", high="130", low="118", close="129"),
        candle(133, open_="129", high="130", low="117", close="118"),
    ]
    bars = breakout_series(130) + tail
    _, tp_first = _tp_replay(bars, [mark(132, "135"), mark(133, "105")])
    assert tp_first.exit_reasons == {"TAKE_PROFIT": 1}
    _, stop_first = _tp_replay(bars, [mark(132, "105"), mark(133, "135")])
    assert stop_first.exit_reasons == {"STOP": 1}


def test_same_mark_tp_and_stop_resolves_conservatively_to_stop() -> None:
    # 先把 extreme 抬到 150,移动止损(150-2.5*ATR≈138.75)越过固定止盈(118.06+14.5≈132.56);
    # 唯一一笔标记价 135 同时满足两个条件,先后不可辨,必须保守取止损。
    bars = breakout_series(130)
    bars.append(candle(132, open_="118.5", high="126", low="118", close="126"))
    bars.append(candle(133, open_="126", high="150", low="126", close="150"))
    bars.append(candle(134, open_="150", high="150", low="130", close="140"))
    marks = [mark(134, "135")]
    ledger, result = _tp_replay(bars, marks)
    assert result.exit_reasons == {"STOP": 1}
    exits = [e for e in ledger.events() if e.metadata.get("exit_reason")]
    assert exits[0].metadata["price"] == "135"

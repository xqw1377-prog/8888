from __future__ import annotations

import threading
from datetime import datetime
from decimal import Decimal
from time import monotonic, sleep
from typing import Optional

import pytest

from cts.clock import UTC
from cts.errors import (
    NewRiskBlockedError,
    ParameterAdmissionError,
    RecoveryStaleError,
    StaleDataError,
    UnitValidationError,
)
from cts.ledger.models import Side
from cts.money import D
from cts.risk.engine import RiskEngine
from cts.risk.models import ReserveRequest, ReservationStatus, RuntimeMode, SystemState
from cts.risk.parameters import mark_approved, proposed_bundle

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def bound_evidence(engine, reason: str, now: datetime = NOW) -> str:
    engine.register_recovery_approver("owner", now=now)
    return engine.record_resolution_evidence(reason, statement=f"resolved:{reason}", recorded_by="owner", now=now)


def req(symbol: str, signal: str, *, equity_version: Optional[int] = None, risk_version: Optional[int] = None) -> ReserveRequest:
    return ReserveRequest(
        symbol=symbol,
        side=Side.LONG,
        signal_key=signal,
        entry_price=D("100"),
        stop_price=D("98"),
        multiplier=D("1"),
        lot_size=D("0.001"),
        expected_equity_version=equity_version,
        expected_risk_version=risk_version,
    )


def make_engine(tmp_path, *, equity: str = "10000", mode: RuntimeMode = RuntimeMode.SIMULATION) -> RiskEngine:
    path = tmp_path if str(tmp_path).endswith(".db") else tmp_path / "risk.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return RiskEngine.create(
        str(path),
        "acct-1",
        equity=D(equity),
        bundle=proposed_bundle(),
        mode=mode,
        now=NOW,
    )


def test_proposed_params_can_simulate_but_not_enter_production(tmp_path) -> None:
    sim = make_engine(tmp_path / "sim.db")
    created = sim.reserve([req("BTCUSDT", "s-btc-1")], now=NOW)
    assert created[0].status == ReservationStatus.PENDING
    prod = make_engine(tmp_path / "prod.db", mode=RuntimeMode.PRODUCTION)
    with pytest.raises(ParameterAdmissionError) as err:
        prod.reserve([req("BTCUSDT", "s-btc-prod")], now=NOW)
    assert err.value.reason_code == "PARAM_NOT_APPROVED"


def test_production_requires_approved_hash(tmp_path) -> None:
    engine = make_engine(tmp_path, mode=RuntimeMode.PRODUCTION)
    approved = mark_approved(proposed_bundle(), approved_by="owner")
    engine.install_parameters(approved, now=NOW)
    created = engine.reserve([req("ETHUSDT", "s-eth-ok")], now=NOW)
    assert created[0].qty > 0
    tampered = mark_approved(proposed_bundle(), approved_by="owner", version="other")
    engine.install_parameters(tampered, now=NOW)
    engine.store._conn.execute(
        "UPDATE account_state SET config_hash=? WHERE account_id=?",
        ("0" * 64, "acct-1"),
    )
    with pytest.raises(ParameterAdmissionError) as err:
        engine.reserve([req("ETHUSDT", "s-eth-bad")], now=NOW)
    assert err.value.reason_code == "PARAM_HASH_MISMATCH"


def test_clearing_one_blocker_does_not_release_others(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine.add_blocker("DAILY_LOSS", SystemState.PAUSED, now=NOW)
    engine.add_blocker("DRAWDOWN_6", SystemState.PAUSED, now=NOW)
    assert engine.occupancy().system_state == SystemState.PAUSED
    state = engine.clear_blocker("DAILY_LOSS", resolution_evidence_id=bound_evidence(engine, "DAILY_LOSS"), now=NOW)
    assert state == SystemState.PAUSED
    codes = {b.reason_code for b in engine.occupancy().blockers}
    assert codes == {"DRAWDOWN_6"}
    with pytest.raises(NewRiskBlockedError):
        engine.reserve([req("BTCUSDT", "blocked")], now=NOW)


def test_fault_is_stricter_than_paused(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine.add_blocker("DAILY_LOSS", SystemState.PAUSED, now=NOW)
    engine.add_blocker("PROTECTION_OVERDUE", SystemState.FAULT, now=NOW)
    assert engine.occupancy().system_state == SystemState.FAULT
    with pytest.raises(NewRiskBlockedError) as err:
        engine.clear_blocker("PROTECTION_OVERDUE", resolution_evidence_id="ev-fault", now=NOW)
    assert err.value.reason_code == "FAULT_REQUIRES_INVESTIGATION"
    engine.clear_blocker("DAILY_LOSS", resolution_evidence_id=bound_evidence(engine, "DAILY_LOSS"), now=NOW)
    assert engine.occupancy().system_state == SystemState.FAULT


def test_exit_allowed_when_paused_or_model_fails_but_cannot_flip(tmp_path) -> None:
    engine = make_engine(tmp_path)
    reserved = engine.reserve([req("BTCUSDT", "s-exit")], now=NOW)[0]
    engine.apply_fill(reserved.reservation_id, reserved.qty, now=NOW)
    engine.add_blocker("DAILY_LOSS", SystemState.PAUSED, now=NOW)
    auth = engine.authorize_exit(
        symbol="BTCUSDT",
        qty=reserved.qty,
        liq_validation_failed=True,
        param_admission_failed=True,
        now=NOW,
    )
    assert auth.allowed is True
    with pytest.raises(UnitValidationError):
        engine.apply_fill(reserved.reservation_id, D("0.001"), now=NOW)
    with pytest.raises(Exception) as err:
        engine.authorize_exit(symbol="BTCUSDT", qty=reserved.qty + D("1"), now=NOW)
    assert getattr(err.value, "reason_code", "") == "EXIT_WOULD_FLIP"


def test_recovery_cas_fails_after_new_blocker(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine.add_blocker("DAILY_LOSS", SystemState.PAUSED, now=NOW)
    engine.clear_blocker("DAILY_LOSS", resolution_evidence_id=bound_evidence(engine, "DAILY_LOSS"), now=NOW)
    snap = engine.create_recovery_snapshot(now=NOW)
    approval = engine.approve_recovery(snap, approved_by="owner", now=NOW)
    engine.add_blocker("UNKNOWN_ORDER", SystemState.PAUSED, now=NOW)
    with pytest.raises(RecoveryStaleError):
        engine.apply_recovery(snap, approval_id=approval, now=NOW)
    codes = {b.reason_code for b in engine.occupancy().blockers}
    assert codes == {"UNKNOWN_ORDER"}
    assert engine.occupancy().system_state == SystemState.PAUSED
    with pytest.raises(NewRiskBlockedError):
        engine.reserve([req("BTCUSDT", "after-stale")], now=NOW)


def test_concurrent_btc_eth_cannot_jointly_exceed(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine.sync_position(
        symbol="BTCUSDT",
        side=Side.LONG,
        qty=D("0.1"),
        notional=D("500"),
        risk_amount=D("10"),
        isolated_margin=D("250"),
        now=NOW,
    )
    errors: list[str] = []
    created: list[str] = []

    def worker(symbol: str, key: str) -> None:
        try:
            rows = engine.reserve([req(symbol, key)], now=NOW)
            created.extend(r.reservation_id for r in rows)
        except Exception as exc:
            errors.append(f"{symbol}:{type(exc).__name__}")

    t1 = threading.Thread(target=worker, args=("BTCUSDT", "sig-btc"))
    t2 = threading.Thread(target=worker, args=("ETHUSDT", "sig-eth"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    occ = engine.occupancy()
    assert occ.total_risk <= D("40") + D("0.0000001")
    assert occ.total_risk >= D("10")
    assert len(created) + len(errors) == 2


def test_crash_after_reserve_does_not_lose_occupancy(tmp_path) -> None:
    timeline: list[tuple[float, str]] = []
    path = str(tmp_path / "risk.db")
    engine = RiskEngine.create(path, "acct-1", equity=D("10000"), bundle=proposed_bundle(), now=NOW)
    timeline.append((monotonic(), "reserve"))
    reserved = engine.reserve([req("BTCUSDT", "sig-crash")], now=NOW)[0]
    pending = engine.occupancy().pending_risk
    engine.close()
    timeline.append((monotonic(), "crash_close"))
    restarted = RiskEngine.reopen(path, "acct-1", now=NOW)
    timeline.append((monotonic(), "reopen"))
    occ = restarted.occupancy()
    assert occ.system_state == SystemState.RECONCILING
    assert occ.pending_risk == pending
    assert occ.reservations[0].reservation_id == reserved.reservation_id
    with pytest.raises(NewRiskBlockedError):
        restarted.reserve([req("ETHUSDT", "sig-after-restart")], now=NOW)
    assert timeline[0][0] <= timeline[1][0] <= timeline[2][0]


def test_cancel_and_partial_fill_do_not_double_release(tmp_path) -> None:
    timeline: list[tuple[float, str]] = []
    engine = make_engine(tmp_path)
    reserved = engine.reserve([req("BTCUSDT", "sig-race")], now=NOW)[0]
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def fill() -> None:
        barrier.wait()
        timeline.append((monotonic(), "fill_start"))
        engine.apply_fill(reserved.reservation_id, reserved.qty * D("0.4"), fill_id="fill-race", now=NOW)
        timeline.append((monotonic(), "fill_done"))

    def cancel() -> None:
        barrier.wait()
        timeline.append((monotonic(), "cancel_start"))
        try:
            engine.confirm_cancel(reserved.reservation_id, reserved.qty * D("0.4"), now=NOW)
        except Exception as exc:
            errors.append(type(exc).__name__)
        timeline.append((monotonic(), "cancel_done"))

    t1 = threading.Thread(target=fill)
    t2 = threading.Thread(target=cancel)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    occ = engine.occupancy()
    res = next(r for r in occ.reservations if r.reservation_id == reserved.reservation_id)
    assert res.filled_qty == reserved.qty * D("0.4")
    assert res.pending_risk == D("0")
    assert occ.positions[0].qty == reserved.qty * D("0.4")
    assert res.status in (ReservationStatus.RELEASED, ReservationStatus.PARTIAL)
    assert not errors or errors == []
    assert [name for _, name in sorted(timeline)]


def test_unknown_and_ttl_do_not_release(tmp_path) -> None:
    engine = make_engine(tmp_path)
    reserved = engine.reserve([req("BTCUSDT", "sig-unknown")], now=NOW)[0]
    engine.mark_unknown(reserved.reservation_id, now=NOW)
    before = engine.occupancy().pending_risk
    engine.expire_ttl(reserved.reservation_id, now=NOW)
    assert engine.occupancy().pending_risk == before
    with pytest.raises(NewRiskBlockedError):
        engine.confirm_cancel(reserved.reservation_id, D("0"), now=NOW)
    late = engine.apply_fill(reserved.reservation_id, reserved.qty * D("0.3"), now=NOW)
    assert late.filled_qty == reserved.qty * D("0.3")
    assert engine.occupancy().positions[0].qty == reserved.qty * D("0.3")


def test_stale_equity_version_cannot_approve_new_size(tmp_path) -> None:
    engine = make_engine(tmp_path)
    snap = engine.occupancy()
    engine.set_equity(D("9000"), reason="funding_and_unrealized_loss", now=NOW)
    with pytest.raises(StaleDataError):
        engine.reserve([req("BTCUSDT", "old-budget", equity_version=snap.equity_version)], now=NOW)
    created = engine.reserve([req("ETHUSDT", "new-budget", equity_version=2)], now=NOW)
    assert created[0].risk_amount <= D("9000") * D("0.0020") + D("0.0000001")


def test_recovery_and_blocker_leave_no_entry_window(tmp_path) -> None:
    timeline: list[tuple[float, str, str]] = []
    engine = make_engine(tmp_path)
    engine.add_blocker("DAILY_LOSS", SystemState.PAUSED, now=NOW)
    snap = engine.create_recovery_snapshot(now=NOW)
    barrier = threading.Barrier(3)
    results: list[str] = []

    def recover() -> None:
        barrier.wait()
        timeline.append((monotonic(), "recover", "start"))
        try:
            engine.apply_recovery(snap, now=NOW)
            results.append("recovered")
        except (RecoveryStaleError, NewRiskBlockedError):
            results.append("stale")
        timeline.append((monotonic(), "recover", "done"))

    def block() -> None:
        barrier.wait()
        timeline.append((monotonic(), "block", "start"))
        engine.add_blocker("NEW_FILL", SystemState.PAUSED, details="concurrent", now=NOW)
        results.append("blocked")
        timeline.append((monotonic(), "block", "done"))

    def enter() -> None:
        barrier.wait()
        sleep(0.02)
        timeline.append((monotonic(), "enter", "start"))
        try:
            engine.reserve([req("BTCUSDT", "window")], now=NOW)
            results.append("reserved")
        except Exception as exc:
            results.append(type(exc).__name__)
        timeline.append((monotonic(), "enter", "done"))

    threads = [threading.Thread(target=fn) for fn in (recover, block, enter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    occ = engine.occupancy()
    assert "reserved" not in results
    if "blocked" in results:
        assert occ.system_state in (SystemState.PAUSED, SystemState.RECONCILING, SystemState.FAULT)
        assert any(b.reason_code in {"NEW_FILL", "DAILY_LOSS"} for b in occ.blockers)
    assert occ.pending_risk == D("0")


def test_signal_reserve_partial_fill_cancel_remaining_quota_trace(tmp_path) -> None:
    engine = make_engine(tmp_path)
    before = engine.occupancy()
    reserved = engine.reserve([req("BTCUSDT", "acct|SIM|T1|BTCUSDT|2026-10-31T00:00:00+00:00|LONG")], now=NOW)[0]
    mid = engine.apply_fill(reserved.reservation_id, reserved.qty * D("0.4"), now=NOW)
    engine.request_cancel(reserved.reservation_id, now=NOW)
    done = engine.confirm_cancel(reserved.reservation_id, mid.filled_qty, now=NOW)
    after = engine.occupancy()
    assert done.status == ReservationStatus.RELEASED
    assert done.filled_qty == reserved.qty * D("0.4")
    assert after.pending_risk == D("0")
    assert after.position_risk == reserved.risk_amount * D("0.4")
    released = reserved.risk_amount * D("0.6")
    assert after.total_risk == before.total_risk + reserved.risk_amount * D("0.4")
    actions = [e.action for e in engine.traces()]
    assert actions[-4:] == ["RESERVE", "FILL", "CANCEL_REQUEST", "CANCEL_CONFIRM"]
    fill_event = next(e for e in engine.traces() if e.action == "FILL")
    cancel_event = next(e for e in engine.traces() if e.action == "CANCEL_CONFIRM")
    assert fill_event.payload["filled_qty"] == format(reserved.qty * D("0.4"), "f")
    assert cancel_event.payload["released_qty"] == format(reserved.qty * D("0.6"), "f")
    assert released > 0


def test_opposite_equal_qty_fill_reduces_position(tmp_path) -> None:
    """平仓通道最小实现：等量反向成交按比例减仓，超量仍拒绝翻仓。"""
    engine = make_engine(tmp_path)
    long_res = engine.reserve([req("BTCUSDT", "s-close-1")], now=NOW)[0]
    engine.apply_fill(long_res.reservation_id, long_res.qty, now=NOW)
    assert engine.occupancy().positions[0].qty == long_res.qty

    short = engine.reserve(
        [ReserveRequest(
            symbol="BTCUSDT", side=Side.SHORT, signal_key="s-close-2",
            entry_price=D("100"), stop_price=D("102"), multiplier=D("1"), lot_size=D("0.001"),
        )],
        now=NOW,
    )[0]
    engine.apply_fill(short.reservation_id, long_res.qty, now=NOW)   # 等量平仓
    view = engine.occupancy()
    assert view.positions[0].qty == 0
    assert view.positions[0].side is None
    assert view.isolated_margin == 0                                 # 数量归零时保证金一并释放

    over = engine.reserve(
        [ReserveRequest(
            symbol="BTCUSDT", side=Side.LONG, signal_key="s-close-3",
            entry_price=D("100"), stop_price=D("98"), multiplier=D("1"), lot_size=D("0.001"),
        )],
        now=NOW,
    )[0]
    long2 = engine.reserve([req("BTCUSDT", "s-close-4")], now=NOW)[0]
    engine.apply_fill(long2.reservation_id, long2.qty, now=NOW)
    from cts.errors import UnitValidationError as _UVE
    import pytest as _pytest
    with _pytest.raises(_UVE):
        engine.apply_fill(over.reservation_id, long2.qty + D("0.001"), now=NOW)

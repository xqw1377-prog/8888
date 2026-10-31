"""P0 05 本地故障注入。通过这些测试也不等于交易所或实盘 PASS。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from cts.errors import CtsError, ExitRejectedError
from cts.execution.executor import Executor, SimClock
from cts.execution.liq import assess_liquidation, classify_liq_quote, model_liq_price
from cts.execution.live import LiveExchangeAdapter
from cts.execution.venue import SimVenue, VenueScript
from cts.ledger.journal import Ledger
from cts.ledger.models import LedgerEventType, Side
from cts.money import D
from cts.risk.engine import RiskEngine
from cts.risk.models import RuntimeMode, SystemState
from cts.risk.parameters import mark_approved, proposed_bundle

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=__import__("cts.clock", fromlist=["UTC"]).UTC)


def world(tmp_path, script: VenueScript | None = None, boot_id: str = "boot-1", elapsed: int = 0):
    root = tmp_path
    root.mkdir(parents=True, exist_ok=True)
    risk = RiskEngine.create(str(tmp_path / "risk.db"), "acct", equity=D("10000"), now=NOW)
    ledger = Ledger("acct", path=str(tmp_path / "ledger.db"))
    ledger.transfer_in(amount=D("10000"), event_time=NOW, received_at=NOW, external_id="dep")
    venue = SimVenue(script or VenueScript())
    clock = SimClock(boot_id=boot_id, elapsed_ms=elapsed)
    executor = Executor.open(
        str(tmp_path / "exec.db"),
        account_id="acct",
        risk=risk,
        ledger=ledger,
        venue=venue,
        clock=clock,
        now=NOW,
        buffer_b=D("0.05"),
        token="lease-1",
    )
    executor.observe_config()
    return executor


def test_liq_quote_blank_is_not_zero() -> None:
    status, price = classify_liq_quote("")
    assert status == "UNKNOWN"
    assert price is None
    status, price = classify_liq_quote("0")
    assert status == "OUT_OF_RANGE"
    assert price is None


def test_model_liq_matches_isolated_formula() -> None:
    price = model_liq_price(
        side=Side.LONG,
        avg_price=D("100"),
        qty=D("1"),
        multiplier=D("1"),
        isolated_margin=D("50"),
        mmr=D("0.005"),
        fee_rate=D("0.0005"),
    )
    assert price == (D("50") - D("100")) / (D("0.005") - D("1") + D("0.0005"))


def test_duplicate_signal_submits_once(tmp_path) -> None:
    ex = world(tmp_path)
    first = ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    second = ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    assert first["client_order_id"] == second["client_order_id"]
    assert ex.venue.submit_count == 2  # entry + protection
    assert sum(1 for order in ex.venue.orders.values() if order["kind"] == "ENTRY") == 1


def test_timeout_recovers_by_query_without_a_second_buy(tmp_path) -> None:
    ex = world(tmp_path, VenueScript(submit_outcome="TIMEOUT"))
    intent = ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    assert intent["state"] == "UNKNOWN"
    assert ex.ledger.position("BTCUSDT").qty == 0
    since = intent["unknown_since_ms"]
    ex.reconcile_open_orders()
    qty = ex.ledger.position("BTCUSDT").qty
    assert qty > 0
    ex.reconcile_open_orders()
    assert ex.ledger.position("BTCUSDT").qty == qty
    blocked = ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-2", mark=D("100"))
    assert blocked["state"] == "RISK_REJECTED"
    assert ex.risk.occupancy().system_state == SystemState.PAUSED
    assert intent["unknown_since_ms"] == since


def test_partial_cancel_keeps_fill_and_late_cumulative(tmp_path) -> None:
    ex = world(tmp_path, VenueScript(fill_ratio=D("0.4"), protect_delay_ms=10000))
    intent = ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    started = ex.state["protections"]["BTCUSDT"]["uncovered_since_ms"]
    ex.cancel_entry("sig-1")
    assert ex.ledger.position("BTCUSDT").qty == D(intent["qty"]) * D("0.4")
    ex.clock.advance(1000)
    ex.venue.set_cumulative(intent["client_order_id"], D(intent["qty"]) * D("0.6"))
    ex.reconcile_open_orders()
    assert ex.ledger.position("BTCUSDT").qty == D(intent["qty"]) * D("0.6")
    assert ex.state["protections"]["BTCUSDT"]["uncovered_since_ms"] == started
    ex.clock.advance(4000)
    ex.poll()
    assert ex.risk.occupancy().system_state == SystemState.FAULT
    assert any(b.reason_code == "PROTECTION_OVERDUE" for b in ex.risk.occupancy().blockers)


def test_protection_fault_at_five_seconds_and_late_confirm_does_not_flip(tmp_path) -> None:
    ex = world(tmp_path, VenueScript(protect_delay_ms=8000))
    ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    ex.clock.advance(5000)
    ex.poll()
    assert ex.risk.occupancy().system_state == SystemState.FAULT
    ex.clock.advance(4000)
    ex.reconcile_open_orders()
    assert ex.ledger.position("BTCUSDT").side == Side.LONG
    assert ex.risk.occupancy().system_state == SystemState.FAULT


def test_pause_keeps_protection_and_flatten_is_separate(tmp_path) -> None:
    ex = world(tmp_path)
    ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    qty = ex.ledger.position("BTCUSDT").qty
    ex.pause_new_entries()
    assert ex.ledger.position("BTCUSDT").qty == qty
    assert ex.state["flatten_requested"] is False
    assert all(ex.venue.orders[cid]["kind"] != "PROTECTION" for cid in ex.venue.canceled)
    result = ex.request_flatten("BTCUSDT", qty)
    assert result["confirmed"] is True
    assert D(result["fill_price"]) != D(result["trigger_price"])
    assert ex.ledger.position("BTCUSDT").qty == 0
    with pytest.raises(ExitRejectedError):
        ex.request_flatten("BTCUSDT", qty)


def test_lost_lease_cannot_submit(tmp_path) -> None:
    ex = world(tmp_path)
    other = Executor.open(
        str(tmp_path / "exec.db"),
        account_id="acct",
        risk=ex.risk,
        ledger=ex.ledger,
        venue=ex.venue,
        clock=SimClock(),
        now=NOW,
        token="someone-else",
        buffer_b=D("0.05"),
    )
    with pytest.raises(CtsError) as err:
        other.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-x", mark=D("100"))
    assert err.value.reason_code == "LEASE_LOST"


def test_restart_does_not_reset_unknown_clock(tmp_path) -> None:
    ex = world(tmp_path, VenueScript(submit_outcome="TIMEOUT"))
    intent = ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    since = intent["unknown_since_ms"]
    ex.close()
    restarted = Executor.open(
        str(tmp_path / "exec.db"),
        account_id="acct",
        risk=RiskEngine.reopen(str(tmp_path / "risk.db"), "acct", now=NOW),
        ledger=Ledger("acct", path=str(tmp_path / "ledger.db")),
        venue=SimVenue(VenueScript(submit_outcome="TIMEOUT")),
        clock=SimClock(boot_id="boot-1", elapsed_ms=0),
        now=NOW,
        token="lease-1",
        buffer_b=D("0.05"),
    )
    assert restarted.clock.elapsed_ms == since
    assert restarted.state["intents"]["sig-1"]["unknown_since_ms"] == since
    restarted.clock.advance(120000)
    restarted.poll()
    assert restarted.risk.occupancy().system_state == SystemState.FAULT


def test_new_boot_without_downtime_bound_faults(tmp_path) -> None:
    ex = world(tmp_path, VenueScript(submit_outcome="TIMEOUT"))
    intent = ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    since = intent["unknown_since_ms"]
    ex.close()
    restarted = Executor.open(
        str(tmp_path / "exec.db"),
        account_id="acct",
        risk=RiskEngine.reopen(str(tmp_path / "risk.db"), "acct", now=NOW),
        ledger=Ledger("acct", path=str(tmp_path / "ledger.db")),
        venue=SimVenue(),
        clock=SimClock(boot_id="boot-2"),
        now=NOW,
        token="lease-1",
        buffer_b=D("0.05"),
    )
    assert restarted.state["clock_fault"] is True
    assert restarted.state["intents"]["sig-1"]["unknown_since_ms"] == since
    assert restarted.risk.occupancy().system_state == SystemState.FAULT


def test_config_expiry_and_amr_drift(tmp_path) -> None:
    ex = world(tmp_path)
    ex.clock.advance(60001)
    ex.poll()
    assert any(b.reason_code == "CONFIG_STALE" for b in ex.risk.occupancy().blockers)
    fresh = world(tmp_path / "amr")
    fresh.venue.amr = True
    fresh.observe_config()
    assertion = fresh.state["assertions"][-1]
    assert assertion["reason_code"] == "AMR_DRIFT"
    assert assertion["blocked_at_ms"] - assertion["detected_at_ms"] <= 60000
    assert fresh.risk.occupancy().system_state == SystemState.PAUSED
    fresh.venue.margin_mode = "CROSS"
    fresh.venue.amr = False
    fresh.observe_config()
    assert any(b.reason_code == "ACCOUNT_MODE_DRIFT" for b in fresh.risk.occupancy().blockers)


def test_pre_submit_config_older_than_five_seconds_is_rejected(tmp_path) -> None:
    ex = world(tmp_path)
    ex.clock.advance(5001)
    with pytest.raises(CtsError) as err:
        ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    assert err.value.reason_code == "STALE_DATA"


def test_blank_or_far_liq_quote_rejects_entry(tmp_path) -> None:
    blank = world(tmp_path / "blank", VenueScript(liq_mode="EMPTY"))
    rejected = blank.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    assert rejected["reason"] == "LIQ_PRICE_UNVERIFIED"
    assert rejected["liq_status"] == "UNKNOWN"
    assert blank.venue.submit_count == 0
    far = world(tmp_path / "far", VenueScript(liq_mode="FIXED", liq_fixed="10"))
    rejected = far.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    assert rejected["reason"] == "LIQ_MODEL_MISMATCH"
    wide = world(tmp_path / "wide")
    rejected = wide.submit_entry(
        symbol="BTCUSDT",
        side=Side.LONG,
        entry_price=D("100"),
        stop_price=D("98"),
        signal_key="sig-1",
        mark=D("100"),
        q_bound=D("1"),
    )
    assert rejected["reason"] == "LIQ_TOLERANCE_INDETERMINATE"
    wide.risk.sync_position(symbol="BTCUSDT", side=Side.LONG, qty=D("1"), notional=D("100"), risk_amount=D("2"), isolated_margin=D("50"), now=NOW)
    wide.ledger.apply_fill(
        symbol="BTCUSDT",
        side=Side.LONG,
        qty=D("1"),
        price=D("100"),
        multiplier=D("1"),
        event_time=NOW,
        received_at=NOW,
        external_id="manual",
        venue="SIM",
        venue_trade_id="manual",
    )
    wide.state["protections"]["BTCUSDT"] = {
        "required_qty": "1",
        "confirmed_qty": "1",
        "coverage": "COVERED",
        "uncovered_since_ms": None,
        "side": "LONG",
        "stop": "98",
        "mark": "100",
        "avg_price": "100",
        "client_order_id": "",
        "binding_mode_declared": "FULL",
        "binding_mode_observed": "FULL",
    }
    wide.state["intents_by_symbol"]["BTCUSDT"] = {"mark": "100", "stop": "98", "side": "LONG", "avg_price": "100"}
    wide.ledger.lock_margin(amount=D("50"), symbol="BTCUSDT", event_time=NOW, received_at=NOW, external_id="im-manual", venue="SIM")
    result = wide.request_flatten("BTCUSDT", D("1"))
    assert result["confirmed"] is True


def test_funding_interval_change_blocks_within_deadline(tmp_path) -> None:
    ex = world(tmp_path)
    ex.observe_funding_interval(D("480"))
    ex.observe_funding_interval(D("60"))
    assert ex.state["funding_state"] == "CHANGING"
    assert ex.state["funding_recomputed_ms"] - ex.state["funding_change_ms"] <= 60000
    blocked = ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    assert blocked["state"] == "RISK_REJECTED"
    ex.clock.advance(60001)
    ex.poll()
    assert ex.state["funding_state"] == "STALE"


def test_external_adl_is_idempotent_and_unknown_qty_does_not_rebuild(tmp_path) -> None:
    ex = world(tmp_path)
    ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    held = ex.ledger.position("BTCUSDT").qty
    ex.venue.inject_external(external_id="adl-1", symbol="BTCUSDT", kind="ADL", qty=format(held / 2, "f"), price="100")
    assert ex.pull_external() == 1
    assert ex.pull_external() == 0
    assert ex.ledger.position("BTCUSDT").qty == held / 2
    assert any(event.event_type == LedgerEventType.ADL for event in ex.ledger.events())
    assert ex.risk.occupancy().system_state == SystemState.FAULT
    ex.venue.inject_external(external_id="adl-unknown", symbol="BTCUSDT", kind="ADL", qty=None, price="100")
    before = ex.ledger.position("BTCUSDT").qty
    ex.pull_external()
    assert ex.ledger.position("BTCUSDT").qty == before
    assert any(b.reason_code == "POSITION_QTY_UNKNOWN" for b in ex.risk.occupancy().blockers)


def test_mark_gap_cannot_upgrade_liquidation_grade(tmp_path) -> None:
    ex = world(tmp_path)
    assert ex.note_mark_gap(overlaps_exposure=True) == "SIGNAL_RESEARCH_ONLY"
    assert ex.state["data_grade"] == "SIGNAL_RESEARCH_ONLY"


def test_rate_limit_stops_new_entries(tmp_path) -> None:
    ex = world(tmp_path, VenueScript(rate_limited=True))
    rejected = ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    assert rejected["state"] == "RISK_REJECTED"
    assert ex.state["backoff_ms"] == "1000"
    assert sum(1 for order in ex.venue.orders.values() if order["kind"] == "ENTRY") == 0


def test_restore_blocks_orders_until_reconciliation(tmp_path) -> None:
    ex = world(tmp_path)
    ex.mark_restored()
    with pytest.raises(CtsError) as err:
        ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    assert err.value.reason_code == "RECONCILIATION_REQUIRED"
    ex.acknowledge_restore()
    opened = ex.submit_entry(symbol="BTCUSDT", side=Side.LONG, entry_price=D("100"), stop_price=D("98"), signal_key="sig-1", mark=D("100"))
    assert opened["state"] == "FILLED"


def test_live_adapter_refuses_without_sending(tmp_path) -> None:
    live = LiveExchangeAdapter()
    with pytest.raises(CtsError) as err:
        live.submit(symbol="BTCUSDT")
    assert err.value.reason_code == "LIVE_TRADING_HOLD"
    assert live.calls == 1
    bundle = proposed_bundle()
    risk = RiskEngine.create(str(tmp_path / "risk.db"), "acct", equity=D("10000"), now=NOW)
    with pytest.raises(CtsError) as proposed:
        Executor.open(
            str(tmp_path / "exec.db"),
            account_id="acct",
            risk=risk,
            ledger=Ledger("acct"),
            venue=SimVenue(),
            clock=SimClock(),
            now=NOW,
            mode=RuntimeMode.PRODUCTION,
            bundle=bundle,
            bundle_hash=bundle.config_hash,
        )
    assert proposed.value.reason_code == "PARAM_NOT_APPROVED"
    approved = mark_approved(bundle, approved_by="owner")
    with pytest.raises(CtsError) as held:
        Executor.open(
            str(tmp_path / "exec2.db"),
            account_id="acct",
            risk=risk,
            ledger=Ledger("acct"),
            venue=SimVenue(),
            clock=SimClock(),
            now=NOW,
            mode=RuntimeMode.PRODUCTION,
            bundle=approved,
            bundle_hash=approved.config_hash,
        )
    assert held.value.reason_code == "LIVE_TRADING_HOLD"


def test_buffer_assessment_uses_distance_not_the_tolerance_ratio() -> None:
    model = D("90")
    assessment = assess_liquidation(
        side=Side.LONG,
        mark=D("100"),
        stop=D("98"),
        model_price=model,
        venue_raw="90",
        buffer_b=D("0.05"),
    )
    assert assessment.buffer_ok is True
    tight = assess_liquidation(
        side=Side.LONG,
        mark=D("100"),
        stop=D("98"),
        model_price=D("97.9"),
        venue_raw="97.9",
        buffer_b=D("0.05"),
    )
    assert tight.buffer_ok is False

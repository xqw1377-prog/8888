"""杠杆只读合同：研究上限 3 倍，生产保持 2 倍，保证金与合同是同一个数。"""

from __future__ import annotations

from datetime import datetime

import pytest

from cts.clock import UTC
from cts.errors import ParameterAdmissionError
from cts.ledger.journal import Ledger
from cts.ledger.models import Side
from cts.money import D
from cts.risk.models import ParameterBundle, ParameterStatus, RuntimeMode
from cts.risk.parameters import PROPOSED_VALUES, bundle_hash
from cts.strategy.replay import ReplayConfig, replay_t1
from cts.strategy.t1 import T1Params
from tests.test_execution import world
from tests.test_risk_p004a import NOW, make_engine, req

ENTRY = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def leverage_bundle(value: str, *, approved: bool = False) -> ParameterBundle:
    values = dict(PROPOSED_VALUES)
    values["leverage"] = value
    status = ParameterStatus.APPROVED if approved else ParameterStatus.PROPOSED
    version = "risk-lev-approved" if approved else "risk-lev"
    return ParameterBundle(
        version=version,
        status=status,
        values=values,
        config_hash=bundle_hash(values, version, status),
        approved_by="owner" if approved else None,
    )


def test_research_fill_margin_uses_contract_leverage(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine.install_parameters(leverage_bundle("3"), now=NOW)
    reserved = engine.reserve([req("BTCUSDT", "s-lev-3")], now=NOW)[0]
    assert engine.occupancy().pending_margin == reserved.notional / D("3")
    engine.apply_fill(reserved.reservation_id, reserved.qty, now=NOW)
    view = engine.occupancy()
    assert view.pending_margin == D("0")
    assert view.isolated_margin == reserved.notional / D("3")
    assert view.positions[0].isolated_margin == reserved.notional / D("3")


def test_production_cannot_raise_leverage_above_baseline(tmp_path) -> None:
    engine = make_engine(tmp_path / "prod.db", mode=RuntimeMode.PRODUCTION)
    engine.install_parameters(leverage_bundle("2", approved=True), now=NOW)
    opened = engine.reserve([req("BTCUSDT", "s-lev-prod")], now=NOW)[0]
    assert opened.notional > 0
    engine.install_parameters(leverage_bundle("3", approved=True), now=NOW)
    with pytest.raises(ParameterAdmissionError) as err:
        engine.reserve([req("ETHUSDT", "s-lev-raised")], now=NOW)
    assert err.value.reason_code == "LEVERAGE_CONTRACT_MISMATCH"
    assert engine.contract_leverage() == D("3")


def test_leverage_above_research_cap_is_rejected(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine.install_parameters(leverage_bundle("4"), now=NOW)
    with pytest.raises(ParameterAdmissionError) as err:
        engine.reserve([req("BTCUSDT", "s-lev-4")], now=NOW)
    assert err.value.reason_code == "LEVERAGE_CONTRACT_MISMATCH"


def test_executor_isolated_margin_follows_the_same_contract(tmp_path) -> None:
    executor = world(tmp_path)
    executor.risk.install_parameters(leverage_bundle("3"), now=ENTRY)
    intent = executor.submit_entry(
        symbol="BTCUSDT",
        side=Side.LONG,
        entry_price=D("100"),
        stop_price=D("98"),
        signal_key="sig-lev",
        mark=D("100"),
    )
    assert "isolated" in intent
    assert D(intent["isolated"]) == D(intent["qty"]) * D("100") / D("3")


def test_replay_rejects_leverage_above_research_cap(tmp_path) -> None:
    ledger = Ledger("acct", path=str(tmp_path / "ledger.db"))
    config = ReplayConfig(params=T1Params(), leverage=D("4"))
    with pytest.raises(ParameterAdmissionError) as err:
        replay_t1(ledger=ledger, symbol="BTCUSDT", candles=(), marks=(), config=config, venue="SIM")
    assert err.value.reason_code == "LEVERAGE_CONTRACT_MISMATCH"

from datetime import datetime
from decimal import Decimal

import pytest

from cts.clock import UTC
from cts.errors import CtsError
from cts.ledger.models import Side
from cts.money import D
from cts.ops.drill import run_local_drill
from cts.research.experiments import ExperimentRegistry, breakeven_stop
from cts.research.report import build_e0_report, register_first_round
from cts.shadow.session import ShadowSession
from tests.helpers import breakout_series


def test_e0_report_can_say_evidence_is_insufficient() -> None:
    bars = breakout_series(130)
    report = build_e0_report(candles=bars, marks=())
    assert report.split_index == int(len(bars) * Decimal("0.8"))
    assert report.conclusion in ("证据不足", "样本外未显示优势", "样本外区间下界大于零，仍不是未来盈利保证")
    assert "策略已验证" not in report.conclusion
    assert report.data_grade == "SIGNAL_RESEARCH_ONLY"
    assert report.rule_snapshot_id == "UNREGISTERED"
    assert report.cash_benchmark == "0"
    assert report.known_limits[-1] == "本报告不授予实盘权限"


def test_registered_experiments_stop_at_twelve_and_e2_is_not_testable() -> None:
    registry = register_first_round([D("1")])
    assert registry.rows[1].status == "NOT_TESTABLE"
    assert registry.rows[2].status == "NOT_TESTABLE"
    extra = ExperimentRegistry()
    for index in range(12):
        extra.register(f"X{index}", "change", "REGISTERED", "kept")
    with pytest.raises(CtsError) as err:
        extra.register("X12", "change", "REGISTERED", "over")
    assert err.value.reason_code == "EXPERIMENT_BUDGET"


def test_breakeven_stop_only_tightens_after_two_r() -> None:
    unchanged = breakeven_stop(side=Side.LONG, entry=D("100"), initial_stop=D("90"), mark=D("119"), cost_per_unit=D("1"), current_stop=D("90"))
    assert unchanged == D("90")
    tightened = breakeven_stop(side=Side.LONG, entry=D("100"), initial_stop=D("90"), mark=D("120"), cost_per_unit=D("1"), current_stop=D("90"))
    assert tightened == D("101")
    kept = breakeven_stop(side=Side.LONG, entry=D("100"), initial_stop=D("90"), mark=D("130"), cost_per_unit=D("1"), current_stop=D("110"))
    assert kept == D("110")


def test_shadow_records_unfillable_signal_without_live_orders() -> None:
    session = ShadowSession()
    session.observe(breakout_series(130))
    assert session.live_attempts == 0
    assert session.decisions[-1].fillable is False
    assert session.decisions[-1].reason == "NO_BOOK"


def test_local_drill_does_not_approve_micro_live() -> None:
    report = run_local_drill()
    assert report["live_authorization"] == "HOLD"
    assert report["micro_live_approved"] is False
    assert report["conclusion"] == "本地故障注入不能替代生产执行证据"
    layers = {item["id"]: item for item in report["items"]}
    assert layers["LIVE_HOLD"]["result"] == "PASS"
    assert layers["VENUE_TEST"]["result"] == "UNVERIFIED"
    assert layers["MICRO_LIVE_OBSERVATION"]["layer"] == "UNVERIFIED"
    assert datetime.now(UTC).tzinfo is not None

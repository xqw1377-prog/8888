from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from cts.data.gaps import classify_gaps
from cts.data.models import DataGrade, MarketDataset
from cts.data.report import validate_dataset
from cts.data.store import DataStore
from cts.errors import ConflictError, LookaheadError, UnitValidationError
from cts.money import D
from tests.helpers import ORIGIN, candle, mark, range_bars, t_close, tier, usdt_linear_rule


def test_put_is_idempotent() -> None:
    store = DataStore()
    a = candle(0)
    b = candle(0)
    assert a.checksum == b.checksum
    store.put(a)
    again = store.put(b)
    assert again.checksum == a.checksum
    assert len(store.all()) == 1


def test_same_identity_different_payload_rejected() -> None:
    store = DataStore()
    store.put(candle(0, close="100"))
    with pytest.raises(ConflictError):
        store.put(candle(0, close="101", open_="100", high="101", low="99"))


def test_reject_float_money() -> None:
    with pytest.raises(TypeError):
        D(1.25)  # type: ignore[arg-type]


def test_invalid_ohlc_rejected() -> None:
    with pytest.raises(UnitValidationError):
        candle(0, open_="100", high="99", low="98", close="99")


def test_research_cannot_use_unavailable_data() -> None:
    store = DataStore()
    bar = candle(1, delay_seconds=30)
    store.put(bar)
    as_of = t_close(1) + timedelta(seconds=10)
    assert store.available_at(as_of) == []
    with pytest.raises(LookaheadError):
        store.require_no_lookahead(bar, as_of)
    later = t_close(1) + timedelta(seconds=30)
    assert len(store.available_at(later)) == 1


def test_future_bar_mutation_does_not_change_past_view() -> None:
    store = DataStore()
    past = candle(0)
    store.put(past)
    as_of = past.available_at
    visible = store.available_at(as_of)
    future = candle(5, close="999", high="1000", low="998", open_="999")
    store.put(future)
    assert [c.checksum for c in store.available_at(as_of)] == [c.checksum for c in visible]


def test_gap_and_mark_missing_with_exposure_blocks_liq_grade() -> None:
    bars = range_bars(3)
    marks = [mark(0, "100"), mark(2, "100")]
    report = classify_gaps(
        candles=bars,
        marks=marks,
        expected_sampling_interval=timedelta(hours=4),
        range_start=t_close(0),
        range_end=t_close(2),
        exposure_windows=((t_close(1), t_close(2)),),
    )
    assert report.mark_price_missing_ratio > 0
    assert report.exposure_overlap is True
    assert report.grade == DataGrade.SIGNAL_RESEARCH_ONLY


def test_dataset_report_has_stable_hash() -> None:
    bars = range_bars(4)
    marks = [mark(i, "100") for i in range(4)]
    dataset = MarketDataset(
        venue="SIM",
        symbol="BTCUSDT",
        interval="4h",
        start=t_close(0),
        end=t_close(3),
        source="sim",
        rule_snapshot_id="rule-sim-1",
        contract_multiplier=Decimal("1"),
        expected_sampling_interval=timedelta(hours=4),
        candles=bars,
        marks=marks,
        fundings=(),
        risk_tiers=(tier(),),
    )
    a = validate_dataset(dataset, generated_at=ORIGIN)
    b = validate_dataset(dataset, generated_at=ORIGIN)
    assert a.report_checksum == b.report_checksum
    assert a.dataset_checksum == dataset.checksum
    assert a.grade == DataGrade.LIQUIDATION_REPLAY_VALIDATED
    assert a.accepted is True


def test_funding_interval_missing_is_not_defaulted() -> None:
    rule = usdt_linear_rule()
    raw = rule.__dict__.copy()
    raw["funding_interval_minutes"] = None
    raw["checksum"] = ""
    from cts.data.models import InstrumentRule

    missing = InstrumentRule(**raw)
    assert missing.funding_interval_minutes is None
    assert missing.funding_interval_minutes != D("480")


def test_risk_tier_requires_raw_field_mapping() -> None:
    with pytest.raises(UnitValidationError):
        tier().raw_fields  # sanity
        from cts.data.models import RiskTierSnapshot

        RiskTierSnapshot(
            venue="SIM",
            symbol="BTCUSDT",
            tier_index=1,
            notional_min=D("0"),
            notional_max=D("1"),
            initial_margin_rate=D("0.5"),
            maintenance_margin_rate=D("0.01"),
            mm_deduction=D("0"),
            effective_from=ORIGIN,
            effective_to=None,
            source_id="sim",
            raw_fields={"notional_min": "0"},
        )


def test_missing_candle_is_not_forward_filled() -> None:
    bars = [candle(0), candle(2)]
    report = classify_gaps(
        candles=bars,
        marks=[mark(0, "100"), mark(2, "100")],
        expected_sampling_interval=timedelta(hours=4),
        range_start=t_close(0),
        range_end=t_close(2),
    )
    assert any(gap.kind == "CANDLE" for gap in report.missing_intervals)
    assert report.grade != DataGrade.LIQUIDATION_REPLAY_VALIDATED

"""规范哈希的时间编码与历史校验和。"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cts.checksum import canonical_json, legacy_sha256_hex, sha256_hex, use_legacy_hash_timezone
from cts.clock import UTC
from cts.data.models import MarketDataset
from cts.data.report import validate_dataset
from cts.data.store import DataStore
from cts.errors import ConflictError
from tests.helpers import ORIGIN, candle, mark, tier

SHANGHAI = timezone(timedelta(hours=8))


def test_same_instant_uses_utc_text() -> None:
    utc = datetime(2024, 1, 1, tzinfo=UTC)
    plus8 = utc.astimezone(timezone(timedelta(hours=8)))
    assert sha256_hex({"t": utc}) == sha256_hex({"t": plus8})
    text = canonical_json({"t": plus8})
    assert "2024-01-01T00:00:00+00:00" in text
    assert "+08:00" not in text


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError):
        sha256_hex({"t": datetime(2024, 1, 1)})


def _read_under_utc_process(loaded_checksum: str, zone):
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        fresh = candle(0)
        with use_legacy_hash_timezone(zone):
            return fresh, replace(fresh, checksum=loaded_checksum)
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_shanghai_legacy_hash_reads_in_utc_process() -> None:
    fresh = candle(0)
    historical = legacy_sha256_hex(fresh.payload(), timezone=SHANGHAI)
    assert historical != legacy_sha256_hex(fresh.payload(), timezone=UTC)
    original = fresh.checksum
    _source, loaded = _read_under_utc_process(historical, SHANGHAI)
    assert loaded.checksum == historical
    assert loaded.checksum != original

    store = DataStore()
    with use_legacy_hash_timezone(SHANGHAI):
        stored = store.put(loaded)
        again = store.put(fresh)
    assert stored.checksum == historical
    assert again is stored


def test_unknown_legacy_timezone_keeps_original_hash() -> None:
    fresh = candle(0)
    original = fresh.checksum
    historical = legacy_sha256_hex(fresh.payload(), timezone=SHANGHAI)
    with pytest.raises(ConflictError) as exc:
        replace(fresh, checksum=historical)
    assert exc.value.reason_code == "EVIDENCE_INSUFFICIENT"
    assert fresh.checksum == original


def test_legacy_checksum_loads_without_being_rewritten() -> None:
    fresh = candle(0)
    historical = legacy_sha256_hex(fresh.payload(), timezone=SHANGHAI)
    with use_legacy_hash_timezone(SHANGHAI):
        loaded = replace(fresh, checksum=historical)
        assert loaded.checksum == historical
        store = DataStore()
        stored = store.put(loaded)
        again = store.put(fresh)
    assert stored.checksum == historical
    assert again is stored
    assert again.checksum == historical


def test_wrong_checksum_is_still_rejected() -> None:
    fresh = candle(0)
    original = fresh.checksum
    with use_legacy_hash_timezone(SHANGHAI):
        with pytest.raises(ConflictError) as exc:
            replace(fresh, checksum="0" * 64)
    assert exc.value.reason_code == "IDEMPOTENCY_CONFLICT"
    assert fresh.checksum == original


def _dataset(*bars: object) -> MarketDataset:
    return MarketDataset(
        venue="SIM",
        symbol="BTCUSDT",
        interval="4h",
        start=ORIGIN,
        end=ORIGIN,
        source="sim",
        rule_snapshot_id="rule-sim-1",
        contract_multiplier=Decimal("1"),
        expected_sampling_interval=timedelta(hours=4),
        candles=bars,
        marks=(mark(0, "100"),),
        fundings=(),
        risk_tiers=(tier(),),
    )


def test_validate_dataset_uses_canonical_content_and_keeps_hashes() -> None:
    fresh = candle(0)
    historical = legacy_sha256_hex(fresh.payload(), timezone=SHANGHAI)
    with use_legacy_hash_timezone(SHANGHAI):
        old = replace(fresh, checksum=historical)
        report = validate_dataset(_dataset(fresh, old), generated_at=ORIGIN)
    assert old.checksum == historical
    assert fresh.checksum != historical
    assert report.accepted is True


def test_validate_dataset_rejects_different_content_without_rewriting_hashes() -> None:
    first = candle(0)
    second = candle(0, open_="100", high="102", low="99", close="101")
    first_hash = first.checksum
    second_hash = second.checksum
    with pytest.raises(ConflictError) as exc:
        validate_dataset(_dataset(first, second), generated_at=ORIGIN)
    assert "异容" in str(exc.value)
    assert first.checksum == first_hash
    assert second.checksum == second_hash

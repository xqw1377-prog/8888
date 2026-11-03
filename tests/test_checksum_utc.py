"""规范哈希的时间编码与历史校验和。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from cts.checksum import canonical_json, checksum_matches, legacy_sha256_hex, sha256_hex
from cts.clock import UTC
from cts.data.store import DataStore
from cts.errors import ConflictError
from tests.helpers import candle


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


def test_legacy_checksum_loads_without_being_rewritten() -> None:
    fresh = candle(0)
    historical = legacy_sha256_hex(fresh.payload())
    loaded = replace(fresh, checksum=historical)
    assert loaded.checksum == historical
    assert checksum_matches(fresh.payload(), historical)

    store = DataStore()
    stored = store.put(loaded)
    assert stored.checksum == historical
    again = store.put(fresh)
    assert again is stored
    assert again.checksum == historical


def test_wrong_checksum_is_still_rejected() -> None:
    fresh = candle(0)
    with pytest.raises(ConflictError):
        replace(fresh, checksum="0" * 64)

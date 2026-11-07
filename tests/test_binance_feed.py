"""币安模拟盘行情接入：白名单、配额、幂等、拒重定向。全部使用假传输器。"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cts.data.binance_feed import BinanceDataFeed, DataPullAuthorization
from cts.data.models import DataGrade
from cts.data.store import DataStore
from cts.errors import CtsError

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)


class FakeResponse:
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, headers: dict) -> FakeResponse:
        self.calls.append((method, url))
        return self.response


def kline_rows() -> list:
    return [
        [
            1792800000000, "100", "110", "99", "105", "12.5",
            1792814399999, "1312.5", 100, "6", "6.5", "0",
        ],
        [
            int((NOW - timedelta(minutes=1)).timestamp() * 1000), "105", "106", "104", "105.5", "3",
            int((NOW + timedelta(hours=1)).timestamp() * 1000), "316.5", 30, "1", "1.5", "0",
        ],
    ]


def auth(**overrides) -> DataPullAuthorization:
    values = dict(granted_by="owner", expires_at=LATER, max_requests=10)
    values.update(overrides)
    return DataPullAuthorization(**values)


def feed(opener: FakeOpener, authorization: DataPullAuthorization | None = None) -> BinanceDataFeed:
    return BinanceDataFeed(
        DataStore(),
        authorization or auth(),
        opener=opener,
        now=lambda: NOW,
    )


def test_candles_parse_close_bar_only_and_stay_idempotent() -> None:
    rows = kline_rows()
    # 第二根未收盘：收盘时间晚于 NOW，不入库
    assert datetime.fromtimestamp(rows[1][6] / 1000, tz=timezone.utc) > NOW
    store_feed = feed(FakeOpener(FakeResponse(200, rows)))
    candles = store_feed.fetch_candles("BTCUSDT", "4h", 500)
    assert len(candles) == 1
    candle = candles[0]
    assert candle.symbol == "BTCUSDT" and candle.interval == "4h"
    assert candle.open == Decimal("100") and candle.close == Decimal("105")
    assert candle.volume == Decimal("12.5")
    assert candle.is_closed is True
    assert candle.available_at == datetime.fromtimestamp(1792814399999 / 1000, tz=timezone.utc)

    again = store_feed.fetch_candles("BTCUSDT", "4h", 500)
    assert len(again) == 1
    assert store_feed.store.get(candle.identity()) is not None
    assert store_feed.requests_used == 2


def test_mark_sample_and_funding_settlements_stored() -> None:
    mark_feed = feed(FakeOpener(FakeResponse(200, {
        "symbol": "ETHUSDT", "markPrice": "2501.5", "indexPrice": "2500.9",
        "lastFundingRate": "0.0001", "time": int(NOW.timestamp() * 1000),
    })))
    sample = mark_feed.fetch_mark("ETHUSDT")
    assert sample.mark_price == Decimal("2501.5")
    assert sample.index_price == Decimal("2500.9")
    assert mark_feed.store.get(sample.identity()) is not None

    funding_feed = feed(FakeOpener(FakeResponse(200, [
        {"symbol": "BTCUSDT", "fundingTime": 1792800000000, "fundingRate": "0.00010"},
        {"symbol": "BTCUSDT", "fundingTime": 1792836000000, "fundingRate": "-0.00005"},
    ])))
    settlements = funding_feed.fetch_funding("BTCUSDT", 100)
    assert len(settlements) == 2
    assert settlements[1].funding_rate == Decimal("-0.00005")
    assert funding_feed.store.get(settlements[0].identity()) is not None


def test_expired_or_exhausted_authorization_sends_nothing() -> None:
    opener = FakeOpener(FakeResponse(200, kline_rows()))
    expired = feed(opener, auth(expires_at=NOW))
    with pytest.raises(CtsError) as exc:
        expired.fetch_candles("BTCUSDT", "4h", 10)
    assert exc.value.reason_code == "LIVE_GRANT_EXPIRED"
    assert opener.calls == []

    quota = feed(opener, auth(max_requests=1))
    quota.fetch_candles("BTCUSDT", "4h", 10)
    with pytest.raises(CtsError) as exc2:
        quota.fetch_candles("BTCUSDT", "4h", 10)
    assert exc2.value.reason_code == "LIVE_GRANT_EXPIRED"
    assert len(opener.calls) == 1


def test_redirect_and_non_200_are_refused() -> None:
    redirected = feed(FakeOpener(FakeResponse(302, [])))
    with pytest.raises(CtsError) as exc:
        redirected.fetch_candles("BTCUSDT", "4h", 10)
    assert exc.value.reason_code == "LIVE_VENUE_UNVERIFIED"

    errored = feed(FakeOpener(FakeResponse(418, {"code": -1000})))
    with pytest.raises(CtsError) as exc2:
        errored.fetch_candles("BTCUSDT", "4h", 10)
    assert exc2.value.reason_code == "RECONCILIATION_FAILED"


def test_symbol_interval_and_limit_whitelists() -> None:
    opener = FakeOpener(FakeResponse(200, []))
    target = feed(opener)
    for call in (
        lambda: target.fetch_candles("SOLUSDT", "4h", 10),
        lambda: target.fetch_candles("BTCUSDT", "8h", 10),
        lambda: target.fetch_candles("BTCUSDT", "4h", 1001),
        lambda: target.fetch_candles("BTCUSDT", "4h", 0),
        lambda: target.fetch_mark("SOLUSDT"),
        lambda: target.fetch_funding("SOLUSDT", 10),
    ):
        with pytest.raises(CtsError) as exc:
            call()
        assert exc.value.reason_code == "LIVE_VENUE_UNVERIFIED"
    assert opener.calls == []


def test_authorization_requires_testnet_and_positive_quota() -> None:
    with pytest.raises(CtsError):
        DataPullAuthorization(granted_by="owner", expires_at=LATER, max_requests=5, network="MAINNET")
    with pytest.raises(CtsError):
        DataPullAuthorization(granted_by="owner", expires_at=LATER, max_requests=0)


def test_request_url_pins_demo_host() -> None:
    opener = FakeOpener(FakeResponse(200, kline_rows()))
    target = feed(opener)
    target.fetch_candles("BTCUSDT", "4h", 10)
    method, url = opener.calls[0]
    assert method == "GET"
    assert url.startswith("https://demo-fapi.binance.com/fapi/v1/klines?")
    assert "symbol=BTCUSDT" in url and "interval=4h" in url

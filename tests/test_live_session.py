"""实盘通道。默认适配器仍拒绝；授权会话只在限额和账户模式通过后签名。"""

from datetime import datetime, timedelta, timezone

import pytest

from cts import LIVE_TRADING_AUTHORIZATION
from cts.errors import CtsError
from cts.execution.live import (
    LIVE_GRANT_STATEMENT,
    MAINNET_BASE,
    TESTNET_BASE,
    LiveExchangeAdapter,
    LiveGrant,
    LiveSession,
    VenueCredentials,
)
from cts.ledger.models import Side
from cts.money import D


NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
SECRET = "test-secret-not-a-credential"


class RecordingTransport:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, str, dict[str, str]]] = []

    def __call__(self, method: str, path: str, query: str, headers: dict[str, str]):
        self.calls.append((method, path, query, headers))
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


def grant(**overrides) -> LiveGrant:
    payload = {
        "granted_by": "owner",
        "statement": LIVE_GRANT_STATEMENT,
        "network": "MAINNET",
        "account_id": "acct",
        "max_notional": D("100"),
        "expires_at": NOW + timedelta(minutes=10),
        "symbols": ("BTCUSDT", "ETHUSDT"),
    }
    payload.update(overrides)
    return LiveGrant(**payload)


def healthy(symbol: str = "BTCUSDT") -> list:
    return [
        {"dualSidePosition": False},
        {"multiAssetsMargin": False},
        [{"symbol": symbol, "marginType": "isolated"}],
        {"orderId": 1, "status": "NEW", "clientOrderId": "cid-1"},
    ]


def test_default_adapter_still_refuses_without_a_request() -> None:
    assert LIVE_TRADING_AUTHORIZATION == "HOLD"
    live = LiveExchangeAdapter()
    with pytest.raises(CtsError) as err:
        live.submit(symbol="BTCUSDT")
    assert err.value.reason_code == "LIVE_TRADING_HOLD"
    assert live.calls == 1


def test_missing_or_expired_grant_does_not_touch_the_transport() -> None:
    transport = RecordingTransport(healthy())
    credentials = VenueCredentials("key-id", SECRET)
    with pytest.raises(CtsError) as missing:
        LiveSession(grant(statement="请下单"), credentials, transport, now=NOW)
    assert missing.value.reason_code == "LIVE_TRADING_HOLD"
    with pytest.raises(CtsError) as expired:
        LiveSession(grant(expires_at=NOW - timedelta(seconds=1)), credentials, transport, now=NOW)
    assert expired.value.reason_code == "LIVE_GRANT_EXPIRED"
    assert transport.calls == []


def test_cross_margin_account_blocks_before_the_order() -> None:
    transport = RecordingTransport(
        [
            {"dualSidePosition": False},
            {"multiAssetsMargin": False},
            [{"symbol": "BTCUSDT", "marginType": "cross"}],
        ]
    )
    session = LiveSession(grant(), VenueCredentials("key-id", SECRET), transport, now=NOW)
    with pytest.raises(CtsError) as err:
        session.submit(
            symbol="BTCUSDT",
            side=Side.LONG,
            qty=D("0.01"),
            price=D("100"),
            client_order_id="cid-1",
            notional=D("1"),
            now=NOW,
        )
    assert err.value.reason_code == "ACCOUNT_MODE_DRIFT"
    assert session.orders_sent == 0
    assert [call[1] for call in transport.calls] == [
        "/fapi/v1/positionSide/dual",
        "/fapi/v1/multiAssetsMargin",
        "/fapi/v2/positionRisk",
    ]


def test_authorized_limit_order_is_signed_without_exposing_the_secret() -> None:
    transport = RecordingTransport(healthy())
    session = LiveSession(grant(), VenueCredentials("key-id", SECRET), transport, now=NOW)
    assert session.base_url == MAINNET_BASE
    demo = LiveSession(grant(network="TESTNET"), VenueCredentials("key-id", SECRET), RecordingTransport(healthy()), now=NOW)
    assert demo.base_url == TESTNET_BASE
    assert demo.base_url == "https://demo-fapi.binance.com"
    assert SECRET not in repr(session)
    body = session.submit(
        symbol="BTCUSDT",
        side=Side.LONG,
        qty=D("0.01"),
        price=D("100"),
        client_order_id="cid-1",
        notional=D("1"),
        now=NOW,
    )
    assert body["status"] == "NEW"
    assert session.orders_sent == 1
    method, path, query, headers = transport.calls[-1]
    assert method == "POST"
    assert path == "/fapi/v1/order"
    assert "symbol=BTCUSDT" in query
    assert "reduceOnly=false" in query
    assert "signature=" in query
    assert SECRET not in query
    assert headers["X-MBX-APIKEY"] == "key-id"
    assert SECRET not in headers["X-MBX-APIKEY"]


def test_notional_above_the_grant_is_refused_before_preflight() -> None:
    transport = RecordingTransport(healthy())
    session = LiveSession(grant(max_notional=D("10")), VenueCredentials("key-id", SECRET), transport, now=NOW)
    with pytest.raises(CtsError) as err:
        session.submit(
            symbol="BTCUSDT",
            side=Side.LONG,
            qty=D("1"),
            price=D("100"),
            client_order_id="cid-1",
            notional=D("100"),
            now=NOW,
        )
    assert err.value.reason_code == "RISK_LIMIT"
    assert transport.calls == []


def test_env_credentials_are_not_read_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CTS_BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("CTS_BINANCE_API_SECRET", raising=False)
    from cts.execution.live import credentials_from_env

    with pytest.raises(CtsError) as err:
        credentials_from_env()
    assert err.value.reason_code == "CREDENTIALS_MISSING"


def test_other_venues_are_refused_before_any_request() -> None:
    transport = RecordingTransport(healthy())
    with pytest.raises(CtsError) as err:
        LiveSession(grant(venue="OTHER"), VenueCredentials("key-id", SECRET), transport, now=NOW)
    assert err.value.reason_code == "LIVE_VENUE_UNVERIFIED"
    assert transport.calls == []


def test_blank_credentials_are_rejected() -> None:
    with pytest.raises(CtsError) as err:
        VenueCredentials("", SECRET)
    assert err.value.reason_code == "CREDENTIALS_MISSING"

"""实盘通道。S1-A：不该发的请求不能到达传输层。签名只出现在已准入的模拟盘开仓。"""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
import os
import threading

import pytest

from cts import LIVE_TRADING_AUTHORIZATION
from cts.errors import CtsError
from cts.execution.orders import LiveOrder, OrderStore
from cts.execution.live import (
    LIVE_GRANT_STATEMENT,
    TESTNET_BASE,
    LiveExchangeAdapter,
    LiveGrant,
    LiveSession,
    TransportResult,
    VenueCredentials,
    open_testnet_transport,
)
from cts.ledger.models import Side
from cts.money import D
from cts.risk.models import SystemState
from tests.test_risk_p004a import make_engine, req


NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
SECRET = "test-secret-not-a-credential"


class RecordingOpener:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str]):
        self.calls.append((method, url, headers))
        if not self.responses:
            raise AssertionError("unexpected request")
        body = self.responses.pop(0)
        if method == "POST":
            body = complete_order_ack(url, body)
        return body


def transport(responses: list) -> tuple[RecordingOpener, object]:
    opener = RecordingOpener(responses)
    return opener, open_testnet_transport(opener)


def ensure_order_db(path) -> str:
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        OrderStore(str(target)).close()
    return str(target)


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


def complete_order_ack(url: str, body):
    """正常回执补上本次请求里的订单身份。已经写明的字段保持原样。"""
    if not isinstance(body, dict) or "status" not in body:
        return body
    query = parse_qs(urlsplit(url).query)

    def pick(*names: str) -> str | None:
        for name in names:
            values = query.get(name)
            if values:
                return values[0]
        return None

    filled = dict(body)
    defaults = {
        "clientOrderId": pick("newClientOrderId", "origClientOrderId"),
        "symbol": pick("symbol"),
        "side": pick("side"),
        "origQty": pick("quantity"),
    }
    for key, value in defaults.items():
        if key not in filled and value is not None:
            filled[key] = value
    return filled


def healthy(symbol: str = "BTCUSDT") -> list:
    return [
        {"dualSidePosition": False},
        {"multiAssetsMargin": False},
        [{"symbol": symbol, "marginType": "isolated"}],
        {"orderId": 1, "status": "NEW", "executedQty": "0", "symbol": symbol, "side": "BUY"},
    ]


def test_default_adapter_still_refuses_without_a_request() -> None:
    assert LIVE_TRADING_AUTHORIZATION == "HOLD"
    live = LiveExchangeAdapter()
    with pytest.raises(CtsError) as err:
        live.submit(symbol="BTCUSDT")
    assert err.value.reason_code == "LIVE_TRADING_HOLD"
    assert live.calls == 1


def test_missing_or_expired_grant_does_not_touch_the_transport() -> None:
    opener, pinned = transport(healthy())
    credentials = VenueCredentials("key-id", SECRET)
    with pytest.raises(CtsError) as missing:
        LiveSession(grant(statement="请下单"), credentials, pinned, now=NOW)
    assert missing.value.reason_code == "LIVE_TRADING_HOLD"
    with pytest.raises(CtsError) as expired:
        LiveSession(grant(expires_at=NOW - timedelta(seconds=1)), credentials, pinned, now=NOW)
    assert expired.value.reason_code == "LIVE_GRANT_EXPIRED"
    assert opener.calls == []


def test_mainnet_grant_produces_no_write_request() -> None:
    opener, pinned = transport(healthy())
    with pytest.raises(CtsError) as err:
        LiveSession(grant(network="MAINNET"), VenueCredentials("key-id", SECRET), pinned, now=NOW)
    assert err.value.reason_code == "LIVE_VENUE_UNVERIFIED"
    assert opener.calls == []
    with pytest.raises(CtsError) as unbound:
        from cts.execution.live import urllib_transport

        urllib_transport("https://fapi.binance.com")
    assert unbound.value.reason_code == "LIVE_VENUE_UNVERIFIED"


def test_testnet_rejects_a_transport_that_is_not_bound_to_the_demo_host() -> None:
    calls: list = []

    class Foreign:
        base_url = "https://fapi.binance.com"

        def __call__(self, method: str, path: str, query: str, headers: dict[str, str]):
            calls.append(path)

    with pytest.raises(CtsError) as err:
        LiveSession(grant(network="TESTNET"), VenueCredentials("key-id", SECRET), Foreign(), now=NOW)
    assert err.value.reason_code == "LIVE_VENUE_UNVERIFIED"
    assert calls == []


def test_redirect_is_not_followed(tmp_path) -> None:
    engine, reservation, session, opener = admitted(tmp_path, [])

    def redirect(method: str, url: str, headers: dict[str, str]):
        opener.calls.append((method, url, headers))
        return TransportResult(status=302, location="https://fapi.binance.com/fapi/v1/order")

    session._transport = open_testnet_transport(redirect)
    with pytest.raises(CtsError) as err:
        submit(session, engine, reservation.reservation_id)
    assert err.value.reason_code == "LIVE_VENUE_UNVERIFIED"
    assert len(opener.calls) == 1
    host = opener.calls[0][1].split("/", 3)[2]
    assert host == "demo-fapi.binance.com"
    assert "/fapi/v1/order" not in opener.calls[0][1]


def test_claimed_notional_must_match_qty_price_and_contract_unit(tmp_path) -> None:
    engine, reservation, session, opener = admitted(tmp_path, healthy())
    with pytest.raises(CtsError) as mismatch:
        submit(session, engine, reservation.reservation_id, qty=D("10000"), price=D("100"), notional=D("1"))
    assert mismatch.value.reason_code == "UNIT_INVALID"
    with pytest.raises(CtsError) as unit:
        submit(
            session,
            engine,
            reservation.reservation_id,
            qty=D("1"),
            price=D("50"),
            notional=D("50"),
            contract_unit=D("2"),
        )
    assert unit.value.reason_code == "UNIT_INVALID"
    assert opener.calls == []


def test_cumulative_budget_approves_at_most_one_of_two_sixties(tmp_path) -> None:
    engine = make_engine(tmp_path, equity="100000")
    first = engine.reserve([req("BTCUSDT", "s-budget-btc")], now=NOW)[0]
    second = engine.reserve([req("ETHUSDT", "s-budget-eth")], now=NOW)[0]
    opener, pinned = transport(healthy() + healthy())
    session = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        pinned,
        now=NOW,
        path=ensure_order_db(tmp_path / "cap-orders.db"),
    )
    submit(session, engine, first.reservation_id, symbol="BTCUSDT", qty=D("0.6"), price=D("100"), notional=D("60"), client_order_id="cid-btc")
    with pytest.raises(CtsError) as err:
        submit(session, engine, second.reservation_id, symbol="ETHUSDT", qty=D("0.6"), price=D("100"), notional=D("60"), client_order_id="cid-eth")
    assert err.value.reason_code == "RISK_LIMIT"
    posts = [call for call in opener.calls if "/fapi/v1/order" in call[1] and call[0] == "POST"]
    assert len(posts) == 1

    engine_b = make_engine(tmp_path / "concurrent.db", equity="100000")
    left = engine_b.reserve([req("BTCUSDT", "s-race-btc")], now=NOW)[0]
    right = engine_b.reserve([req("ETHUSDT", "s-race-eth")], now=NOW)[0]
    both = [
        {"dualSidePosition": False},
        {"multiAssetsMargin": False},
        [
            {"symbol": "BTCUSDT", "marginType": "isolated"},
            {"symbol": "ETHUSDT", "marginType": "isolated"},
        ],
        {"orderId": 1, "status": "NEW", "executedQty": "0"},
    ]
    opener_b, pinned_b = transport(both + both)
    raced = LiveSession(
        grant(network="TESTNET", account_id=engine_b.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        pinned_b,
        now=NOW,
        path=ensure_order_db(tmp_path / "race-orders.db"),
    )
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def race(reservation_id: str, symbol: str, client_order_id: str) -> None:
        barrier.wait()
        try:
            submit(
                raced,
                engine_b,
                reservation_id,
                symbol=symbol,
                qty=D("0.6"),
                price=D("100"),
                notional=D("60"),
                client_order_id=client_order_id,
            )
        except CtsError as exc:
            errors.append(exc.reason_code)

    threads = [
        threading.Thread(target=race, args=(left.reservation_id, "BTCUSDT", "cid-left")),
        threading.Thread(target=race, args=(right.reservation_id, "ETHUSDT", "cid-right")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    posts = [call for call in opener_b.calls if call[0] == "POST" and "/fapi/v1/order" in call[1]]
    assert posts and len(posts) == 1
    assert errors == ["RISK_LIMIT"]


def test_invalid_order_fields_never_reach_the_transport(tmp_path) -> None:
    engine, reservation, session, opener = admitted(tmp_path, healthy())
    samples = [
        {"qty": D("NaN")},
        {"qty": D("Infinity")},
        {"qty": D("0")},
        {"qty": D("-1")},
        {"price": D("0")},
        {"side": "BUY"},
        {"client_order_id": "  "},
    ]
    for sample in samples:
        with pytest.raises(CtsError) as err:
            submit(session, engine, reservation.reservation_id, **sample)
        assert err.value.reason_code == "UNIT_INVALID"
    assert opener.calls == []


def test_missing_account_mode_fields_stop_before_the_order(tmp_path) -> None:
    cases = [
        [{}],
        [{"dualSidePosition": False}, {}],
        [{"dualSidePosition": False}, {"multiAssetsMargin": False}, [{"symbol": "BTCUSDT"}]],
    ]
    for responses in cases:
        engine, reservation, session, opener = admitted(tmp_path / str(len(responses)), responses)
        with pytest.raises(CtsError) as err:
            submit(session, engine, reservation.reservation_id)
        assert err.value.reason_code == "ACCOUNT_MODE_DRIFT"
        assert not any(call[0] == "POST" for call in opener.calls)


def test_pause_fault_or_missing_approval_cannot_open(tmp_path) -> None:
    engine, reservation, session, opener = admitted(tmp_path / "pause.db", healthy())
    engine.add_blocker("MANUAL_PAUSE", SystemState.PAUSED, "暂停", now=NOW)
    with pytest.raises(CtsError) as paused:
        submit(session, engine, reservation.reservation_id)
    assert paused.value.reason_code == "MANUAL_PAUSE"
    assert opener.calls == []

    faulted, fault_reservation, fault_session, fault_opener = admitted(tmp_path / "fault.db", healthy())
    faulted.add_blocker("FAULT_REQUIRES_INVESTIGATION", SystemState.FAULT, "故障", now=NOW)
    with pytest.raises(CtsError) as fault:
        submit(fault_session, faulted, fault_reservation.reservation_id)
    assert fault.value.reason_code == "FAULT_REQUIRES_INVESTIGATION"
    assert fault_opener.calls == []

    bare, _, bare_session, bare_opener = admitted(tmp_path / "bare.db", healthy())
    with pytest.raises(CtsError) as missing:
        submit(bare_session, bare, "missing-approval")
    assert missing.value.reason_code == "NEW_RISK_BLOCKED"
    assert bare_opener.calls == []

    class PretendRisk:
        account_id = bare.account_id

        def admit_live_open(self, *args, **kwargs):
            return None

    with pytest.raises(CtsError) as pretend:
        submit(bare_session, PretendRisk(), reservation.reservation_id)
    assert pretend.value.reason_code == "NEW_RISK_BLOCKED"
    assert bare_opener.calls == []


def test_cross_margin_account_blocks_before_the_order(tmp_path) -> None:
    engine, reservation, session, opener = admitted(
        tmp_path,
        [
            {"dualSidePosition": False},
            {"multiAssetsMargin": False},
            [{"symbol": "BTCUSDT", "marginType": "cross"}],
        ],
    )
    with pytest.raises(CtsError) as err:
        submit(session, engine, reservation.reservation_id)
    assert err.value.reason_code == "ACCOUNT_MODE_DRIFT"
    assert session.orders_sent == 0
    assert [call[1].split("?", 1)[0].removeprefix(TESTNET_BASE) for call in opener.calls] == [
        "/fapi/v1/positionSide/dual",
        "/fapi/v1/multiAssetsMargin",
        "/fapi/v2/positionRisk",
    ]


def test_admitted_demo_limit_order_is_signed_without_exposing_the_secret(tmp_path) -> None:
    engine, reservation, session, opener = admitted(tmp_path, healthy())
    assert session.base_url == TESTNET_BASE
    assert SECRET not in repr(session)
    body = submit(session, engine, reservation.reservation_id)
    assert body["status"] == "NEW"
    assert session.orders_sent == 1
    method, url, headers = opener.calls[-1]
    assert method == "POST"
    assert url.startswith(f"{TESTNET_BASE}/fapi/v1/order?")
    assert "symbol=BTCUSDT" in url
    assert "reduceOnly=false" in url
    assert "signature=" in url
    assert SECRET not in url
    assert headers["X-MBX-APIKEY"] == "key-id"
    assert SECRET not in headers["X-MBX-APIKEY"]
    assert all(call[1].startswith(TESTNET_BASE) for call in opener.calls)


def test_notional_above_the_grant_is_refused_before_preflight(tmp_path) -> None:
    engine, reservation, session, opener = admitted(tmp_path, healthy(), max_notional=D("10"))
    with pytest.raises(CtsError) as err:
        submit(session, engine, reservation.reservation_id, qty=D("1"), price=D("100"), notional=D("100"))
    assert err.value.reason_code == "RISK_LIMIT"
    assert opener.calls == []


def test_unknown_order_id_does_not_reach_the_transport(tmp_path) -> None:
    _, _, session, opener = admitted(tmp_path, healthy())
    with pytest.raises(CtsError) as query:
        session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    with pytest.raises(CtsError) as cancel:
        session.cancel(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert query.value.reason_code == "RECONCILIATION_FAILED"
    assert cancel.value.reason_code == "RECONCILIATION_FAILED"
    assert opener.calls == []


def test_order_intent_is_stored_before_the_post(tmp_path) -> None:
    from cts.execution.orders import OrderStore

    path = tmp_path / "orders.db"
    engine = make_engine(tmp_path / "risk.db", equity="100000")
    reservation = engine.reserve([req("BTCUSDT", "s-intent")], now=NOW)[0]
    seen: list[tuple[str, str, bool]] = []
    responses = healthy()

    def opener(method: str, url: str, headers: dict[str, str]):
        order = OrderStore(str(path)).get("cid-1")
        assert order is not None
        seen.append((method, order.status, order.dispatch_started))
        if not responses:
            raise AssertionError("unexpected request")
        body = responses.pop(0)
        if method == "POST":
            body = complete_order_ack(url, body)
        return body

    session = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        open_testnet_transport(opener),
        now=NOW,
        path=ensure_order_db(path),
    )
    body = submit(session, engine, reservation.reservation_id)
    assert body["status"] == "NEW"
    assert seen[0] == ("GET", "INTENT", False)
    assert seen[-1][0] == "POST" and seen[-1][1:] == ("INTENT", True)
    assert session.get_order("cid-1").status == "NEW"
    again = submit(session, engine, reservation.reservation_id)
    assert again["status"] == "NEW"
    assert [row[0] for row in seen].count("POST") == 1
    with pytest.raises(CtsError) as changed:
        submit(session, engine, reservation.reservation_id, qty=D("0.02"), price=D("100"), notional=D("2"), client_order_id="cid-1")
    assert changed.value.reason_code == "IDEMPOTENCY_CONFLICT"


def test_lost_submit_response_keeps_the_budget_until_query(tmp_path) -> None:
    path = tmp_path / "orders.db"
    engine = make_engine(tmp_path / "risk.db", equity="100000")
    reservation = engine.reserve([req("BTCUSDT", "s-unknown")], now=NOW)[0]
    other = engine.reserve([req("ETHUSDT", "s-blocked")], now=NOW)[0]
    calls: list[str] = []
    preflight = healthy()[:3]

    def opener(method: str, url: str, headers: dict[str, str]):
        calls.append(method)
        if method == "POST":
            raise TimeoutError("response lost")
        return preflight.pop(0)

    session = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        open_testnet_transport(opener),
        now=NOW,
        path=ensure_order_db(path),
    )
    with pytest.raises(CtsError) as lost:
        submit(session, engine, reservation.reservation_id, qty=D("0.6"), price=D("100"), notional=D("60"))
    assert lost.value.reason_code == "UNKNOWN_ORDER"
    assert session.orders_sent == 0
    assert session.reserved_notional == D("60")
    assert session.get_order("cid-1").status == "UNKNOWN"
    with pytest.raises(CtsError) as blocked:
        submit(
            session,
            engine,
            other.reservation_id,
            symbol="ETHUSDT",
            qty=D("0.1"),
            price=D("100"),
            notional=D("10"),
            client_order_id="cid-2",
        )
    assert blocked.value.reason_code == "UNKNOWN_ORDER"
    assert calls.count("POST") == 1

    restored = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        open_testnet_transport(opener),
        now=NOW,
        path=ensure_order_db(path),
    )
    assert restored.get_order("cid-1").status == "UNKNOWN"
    assert restored.reserved_notional == D("60")

    def reconcile(method: str, url: str, headers: dict[str, str]):
        calls.append(method + ":reconcile")
        return {"status": "NEW", "executedQty": "0", "origQty": "0.6", "clientOrderId": "cid-1", "symbol": "BTCUSDT", "side": "BUY"}

    restored._transport = open_testnet_transport(reconcile)
    body = restored.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert body["status"] == "NEW"
    assert restored.get_order("cid-1").status == "NEW"
    assert restored.reserved_notional == D("60")


def test_partial_fill_releases_only_the_unfilled_notional(tmp_path) -> None:
    path = tmp_path / "orders.db"
    engine = make_engine(tmp_path / "risk.db", equity="100000")
    first = engine.reserve([req("BTCUSDT", "s-partial")], now=NOW)[0]
    second = engine.reserve([req("ETHUSDT", "s-next")], now=NOW)[0]
    script = healthy() + [
        {"status": "PARTIALLY_FILLED", "executedQty": "0.2", "origQty": "0.6", "clientOrderId": "cid-1", "symbol": "BTCUSDT", "side": "BUY"},
        {"status": "CANCELED", "executedQty": "0.2", "origQty": "0.6", "clientOrderId": "cid-1", "symbol": "BTCUSDT", "side": "BUY"},
    ] + [
        {"dualSidePosition": False},
        {"multiAssetsMargin": False},
        [
            {"symbol": "BTCUSDT", "marginType": "isolated"},
            {"symbol": "ETHUSDT", "marginType": "isolated"},
        ],
        {"status": "NEW", "executedQty": "0", "origQty": "0.6", "clientOrderId": "cid-2", "symbol": "ETHUSDT", "side": "BUY"},
    ]
    opener, pinned = transport(script)
    session = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        pinned,
        now=NOW,
        path=ensure_order_db(path),
    )
    submit(session, engine, first.reservation_id, qty=D("0.6"), price=D("100"), notional=D("60"))
    session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert session.get_order("cid-1").executed_qty == D("0.2")
    assert session.reserved_notional == D("60")
    session.cancel(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert session.get_order("cid-1").status == "CANCELED"
    assert session.reserved_notional == D("20")
    submit(
        session,
        engine,
        second.reservation_id,
        symbol="ETHUSDT",
        qty=D("0.6"),
        price=D("100"),
        notional=D("60"),
        client_order_id="cid-2",
    )
    assert session.reserved_notional == D("80")


def test_expired_grant_blocks_new_risk_but_keeps_query_and_cancel(tmp_path) -> None:
    path = tmp_path / "orders.db"
    engine = make_engine(tmp_path / "risk.db", equity="100000")
    reservation = engine.reserve([req("BTCUSDT", "s-expire")], now=NOW)[0]
    later = NOW + timedelta(minutes=11)
    opener, pinned = transport(
        healthy()
        + [
            {"status": "NEW", "executedQty": "0", "origQty": "0.01", "clientOrderId": "cid-1", "symbol": "BTCUSDT", "side": "BUY"},
            {"status": "CANCELED", "executedQty": "0", "origQty": "0.01", "clientOrderId": "cid-1", "symbol": "BTCUSDT", "side": "BUY"},
        ]
    )
    session = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, expires_at=NOW + timedelta(minutes=10)),
        VenueCredentials("key-id", SECRET),
        pinned,
        now=NOW,
        path=ensure_order_db(path),
    )
    submit(session, engine, reservation.reservation_id)
    before = len(opener.calls)
    queried = session.query(symbol="BTCUSDT", client_order_id="cid-1", now=later)
    canceled = session.cancel(symbol="BTCUSDT", client_order_id="cid-1", now=later)
    assert queried["status"] == "NEW"
    assert canceled["status"] == "CANCELED"
    assert session.reserved_notional == D("0")
    with pytest.raises(CtsError) as expired:
        submit(session, engine, reservation.reservation_id, client_order_id="cid-2", now=later)
    assert expired.value.reason_code == "LIVE_GRANT_EXPIRED"
    assert [call[0] for call in opener.calls[before:]] == ["GET", "DELETE"]


def test_restart_promotes_a_dispatched_intent_to_unknown(tmp_path) -> None:
    from cts.execution.orders import LiveOrder, OrderStore

    path = tmp_path / "orders.db"
    engine = make_engine(tmp_path / "risk.db", equity="100000")
    store = OrderStore(str(path))
    draft = LiveOrder(
        client_order_id="cid-crash",
        account_id=engine.account_id,
        reservation_id="res-1",
        symbol="BTCUSDT",
        side="LONG",
        qty=D("0.6"),
        price=D("100"),
        notional=D("60"),
        status="INTENT",
        executed_qty=D("0"),
        dispatch_started=True,
        body={},
    )
    store.insert_intent(
        LiveOrder(
            client_order_id=draft.client_order_id,
            account_id=draft.account_id,
            reservation_id=draft.reservation_id,
            symbol=draft.symbol,
            side=draft.side,
            qty=draft.qty,
            price=draft.price,
            notional=draft.notional,
            status="INTENT",
            executed_qty=D("0"),
            dispatch_started=False,
            body={},
        ),
        NOW,
    )
    store.save(draft, NOW)
    opener, pinned = transport([])
    session = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        pinned,
        now=NOW,
        path=ensure_order_db(path),
    )
    assert session.get_order("cid-crash").status == "UNKNOWN"
    assert session.reserved_notional == D("60")
    assert opener.calls == []


def admitted(path, responses: list, **overrides):
    engine = make_engine(path, equity="100000")
    reservation = engine.reserve([req("BTCUSDT", "s-open")], now=NOW)[0]
    opener, pinned = transport(responses)
    fields = {"network": "TESTNET", "account_id": engine.account_id, "max_notional": D("100000")}
    fields.update(overrides)
    order_path = path.with_name(path.name + ".orders") if str(path).endswith(".db") else path / "orders.db"
    session = LiveSession(
        grant(**fields),
        VenueCredentials("key-id", SECRET),
        pinned,
        now=NOW,
        path=ensure_order_db(order_path),
    )
    return engine, reservation, session, opener


def submit(session: LiveSession, engine, reservation_id: str, **overrides):
    payload = {
        "symbol": "BTCUSDT",
        "side": Side.LONG,
        "qty": D("0.01"),
        "price": D("100"),
        "client_order_id": "cid-1",
        "notional": D("1"),
        "now": NOW,
        "risk": engine,
        "reservation_id": reservation_id,
    }
    payload.update(overrides)
    return session.submit(**payload)


def test_env_credentials_are_not_read_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CTS_BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("CTS_BINANCE_API_SECRET", raising=False)
    from cts.execution.live import credentials_from_env

    with pytest.raises(CtsError) as err:
        credentials_from_env()
    assert err.value.reason_code == "CREDENTIALS_MISSING"


def test_other_venues_are_refused_before_any_request() -> None:
    opener, pinned = transport(healthy())
    with pytest.raises(CtsError) as err:
        LiveSession(grant(venue="OTHER"), VenueCredentials("key-id", SECRET), pinned, now=NOW)
    assert err.value.reason_code == "LIVE_VENUE_UNVERIFIED"
    assert opener.calls == []


def test_venue_wallet_replaces_the_local_ledger_balance() -> None:
    from cts.console.app import ConsoleState, apply_venue_wallet
    from cts.execution.live import parse_usdt_wallet
    from cts.ledger.journal import Ledger
    from tests.test_ledger import TS

    body = {
        "totalMarginBalance": "4999.65538680",
        "assets": [
            {
                "asset": "USDT",
                "walletBalance": "4999.65538680",
                "availableBalance": "4999.65538680",
                "unrealizedProfit": "0.00000000",
                "marginBalance": "4999.65538680",
                "initialMargin": "0.00000000",
            }
        ],
    }
    venue = parse_usdt_wallet(body)
    ledger = Ledger("acct")
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep")
    state = ConsoleState.from_ledger(ledger)
    assert state.books["source"] == "本地账本"
    assert state.books["wallet"] == "10000"
    apply_venue_wallet(state, venue, as_of="2026-10-31T00:00:00+00:00")
    apply_venue_wallet(state, venue, as_of="2026-10-31T00:01:00+00:00")
    assert state.equity == "4999.65538680"
    assert state.books["wallet"] == "4999.65538680"
    assert state.books["source"] == "币安模拟盘"
    assert state.books["gap"] == format(D("4999.65538680") - D("10000"), "f")


def test_contract_unit_cannot_discount_local_occupancy(tmp_path) -> None:
    engine, reservation, session, opener = admitted(tmp_path, healthy())
    with pytest.raises(CtsError) as err:
        submit(
            session,
            engine,
            reservation.reservation_id,
            qty=D("0.6"),
            price=D("100"),
            notional=D("0.6"),
            contract_unit=D("0.01"),
        )
    assert err.value.reason_code == "UNIT_INVALID"
    assert opener.calls == []


def test_one_reservation_cannot_fund_two_orders(tmp_path) -> None:
    engine, reservation, session, opener = admitted(tmp_path, healthy())
    assert reservation.notional == reservation.qty * D("100")
    submit(
        session,
        engine,
        reservation.reservation_id,
        qty=reservation.qty,
        price=D("100"),
        notional=reservation.notional,
        client_order_id="cid-full",
    )
    with pytest.raises(CtsError) as err:
        submit(
            session,
            engine,
            reservation.reservation_id,
            qty=reservation.qty,
            price=D("100"),
            notional=reservation.notional,
            client_order_id="cid-again",
        )
    assert err.value.reason_code == "RISK_LIMIT"
    posts = [call for call in opener.calls if call[0] == "POST"]
    assert len(posts) == 1


def test_changed_price_or_equity_invalidates_the_reservation(tmp_path) -> None:
    engine, reservation, session, opener = admitted(tmp_path, healthy())
    with pytest.raises(CtsError) as price:
        submit(
            session,
            engine,
            reservation.reservation_id,
            qty=D("0.01"),
            price=D("90"),
            notional=D("0.9"),
            client_order_id="cid-price",
        )
    assert price.value.reason_code == "NEW_RISK_BLOCKED"
    engine.set_equity(D("50000"), reason="权益下降", now=NOW)
    with pytest.raises(CtsError) as equity:
        submit(session, engine, reservation.reservation_id, client_order_id="cid-equity")
    assert equity.value.reason_code == "STALE_DATA"
    assert opener.calls == []


def test_same_client_order_posts_once(tmp_path) -> None:
    engine = make_engine(tmp_path, equity="100000")
    reservation = engine.reserve([req("BTCUSDT", "s-once")], now=NOW)[0]

    class Opener:
        def __init__(self) -> None:
            self.posts = 0

        def __call__(self, method: str, url: str, headers: dict[str, str]):
            if method == "POST":
                self.posts += 1
                return complete_order_ack(url, {"orderId": 1, "status": "NEW", "executedQty": "0"})
            if "positionSide" in url:
                return {"dualSidePosition": False}
            if "multiAssetsMargin" in url:
                return {"multiAssetsMargin": False}
            return [{"symbol": "BTCUSDT", "marginType": "isolated"}]

    opener = Opener()
    session = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        open_testnet_transport(opener),
        now=NOW,
        path=ensure_order_db(tmp_path / "once.db"),
    )
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def race() -> None:
        barrier.wait()
        try:
            submit(session, engine, reservation.reservation_id, client_order_id="cid-same")
        except CtsError as exc:
            errors.append(exc.reason_code)

    threads = [threading.Thread(target=race), threading.Thread(target=race)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert opener.posts == 1
    assert set(errors) <= {"UNKNOWN_ORDER"}


def test_two_sessions_share_the_account_cap(tmp_path) -> None:
    engine = make_engine(tmp_path, equity="100000")
    left = engine.reserve([req("BTCUSDT", "s-share-btc")], now=NOW)[0]
    right = engine.reserve([req("ETHUSDT", "s-share-eth")], now=NOW)[0]
    path = tmp_path / "shared-orders.db"

    class Opener:
        def __init__(self) -> None:
            self.posts = 0

        def __call__(self, method: str, url: str, headers: dict[str, str]):
            if method == "POST":
                self.posts += 1
                return complete_order_ack(url, {"orderId": 1, "status": "NEW", "executedQty": "0"})
            if "positionSide" in url:
                return {"dualSidePosition": False}
            if "multiAssetsMargin" in url:
                return {"multiAssetsMargin": False}
            return [
                {"symbol": "BTCUSDT", "marginType": "isolated"},
                {"symbol": "ETHUSDT", "marginType": "isolated"},
            ]

    opener = Opener()
    transport_pin = open_testnet_transport(opener)
    credentials = VenueCredentials("key-id", SECRET)
    first = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        credentials,
        transport_pin,
        now=NOW,
        path=ensure_order_db(path),
    )
    second = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        credentials,
        transport_pin,
        now=NOW,
        path=ensure_order_db(path),
    )
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def race(session: LiveSession, reservation_id: str, symbol: str, client_order_id: str) -> None:
        barrier.wait()
        try:
            submit(
                session,
                engine,
                reservation_id,
                symbol=symbol,
                qty=D("0.6"),
                price=D("100"),
                notional=D("60"),
                client_order_id=client_order_id,
            )
        except CtsError as exc:
            errors.append(exc.reason_code)

    threads = [
        threading.Thread(target=race, args=(first, left.reservation_id, "BTCUSDT", "cid-share-btc")),
        threading.Thread(target=race, args=(second, right.reservation_id, "ETHUSDT", "cid-share-eth")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert opener.posts == 1
    assert errors == ["RISK_LIMIT"]
    assert first.reserved_notional <= D("100")
    assert second.reserved_notional <= D("100")


def test_pause_during_preflight_blocks_the_post(tmp_path) -> None:
    engine = make_engine(tmp_path, equity="100000")
    reservation = engine.reserve([req("BTCUSDT", "s-pause")], now=NOW)[0]
    posts: list[str] = []

    def opener(method: str, url: str, headers: dict[str, str]):
        if method == "GET" and "positionSide" in url:
            engine.add_blocker("MANUAL_PAUSE", SystemState.PAUSED, "预检期间暂停", now=NOW)
            return {"dualSidePosition": False}
        if method == "POST":
            posts.append(url)
            return complete_order_ack(url, {"orderId": 1, "status": "NEW", "executedQty": "0"})
        if "multiAssetsMargin" in url:
            return {"multiAssetsMargin": False}
        return [{"symbol": "BTCUSDT", "marginType": "isolated"}]

    session = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        open_testnet_transport(opener),
        now=NOW,
        path=ensure_order_db(tmp_path / "pause.db"),
    )
    with pytest.raises(CtsError) as err:
        submit(session, engine, reservation.reservation_id)
    assert err.value.reason_code == "MANUAL_PAUSE"
    assert posts == []


def test_confirmed_fill_cannot_decrease_and_reaches_risk(tmp_path) -> None:
    engine = make_engine(tmp_path, equity="100000")
    reservation = engine.reserve([req("BTCUSDT", "s-fill")], now=NOW)[0]
    responses = healthy()
    responses[-1] = {"status": "PARTIALLY_FILLED", "executedQty": "0.4", "origQty": "0.6", "clientOrderId": "cid-1", "symbol": "BTCUSDT", "side": "BUY"}
    opener, pinned = transport(responses)
    session = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        pinned,
        now=NOW,
        path=ensure_order_db(tmp_path / "fills.db"),
    )
    submit(session, engine, reservation.reservation_id, qty=D("0.6"), price=D("100"), notional=D("60"))
    stored = next(item for item in engine.occupancy().reservations if item.reservation_id == reservation.reservation_id)
    assert stored.filled_qty == D("0.4")
    session._transport = open_testnet_transport(
        lambda method, url, headers: {
            "status": "PARTIALLY_FILLED",
            "executedQty": "0.2",
            "origQty": "0.6",
            "clientOrderId": "cid-1",
            "symbol": "BTCUSDT",
            "side": "BUY",
        }
    )
    with pytest.raises(CtsError) as err:
        session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert err.value.reason_code == "RECONCILIATION_FAILED"
    assert session.get_order("cid-1").executed_qty == D("0.4")
    stored = next(item for item in engine.occupancy().reservations if item.reservation_id == reservation.reservation_id)
    assert stored.filled_qty == D("0.4")


def test_cancel_missing_fill_fields_does_not_release_budget(tmp_path) -> None:
    engine, reservation, session, _opener = admitted(tmp_path, healthy())
    submit(session, engine, reservation.reservation_id)
    session._transport = open_testnet_transport(lambda method, url, headers: {"status": "CANCELED"})
    with pytest.raises(CtsError) as err:
        session.cancel(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert err.value.reason_code == "UNKNOWN_ORDER"
    assert session.get_order("cid-1").status == "UNKNOWN"
    assert session.reserved_notional == D("1")


def test_unknown_order_blocks_new_risk_until_it_is_known(tmp_path) -> None:
    engine = make_engine(tmp_path, equity="100000")
    reservation = engine.reserve([req("BTCUSDT", "s-hold")], now=NOW)[0]
    preflight = healthy()[:3]

    def opener(method: str, url: str, headers: dict[str, str]):
        if method == "POST":
            raise TimeoutError("response lost")
        return preflight.pop(0)

    session = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        open_testnet_transport(opener),
        now=NOW,
        path=ensure_order_db(tmp_path / "unknown-risk.db"),
    )
    with pytest.raises(CtsError):
        submit(session, engine, reservation.reservation_id, qty=D("0.6"), price=D("100"), notional=D("60"))
    with pytest.raises(CtsError) as blocked:
        engine.reserve([req("ETHUSDT", "s-after-unknown")], now=NOW)
    assert blocked.value.reason_code == "UNKNOWN_ORDER"
    session._transport = open_testnet_transport(
        lambda method, url, headers: {"status": "NEW", "executedQty": "0", "origQty": "0.6", "clientOrderId": "cid-1", "symbol": "BTCUSDT", "side": "BUY"}
    )
    session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW, risk=engine)
    created = engine.reserve([req("ETHUSDT", "s-after-known")], now=NOW)
    assert created[0].qty > 0


def test_missing_order_db_refuses_open_without_creating_one(tmp_path) -> None:
    import tempfile
    from pathlib import Path

    opener, pinned = transport([])
    credentials = VenueCredentials("key-id", SECRET)
    before = set(Path(tempfile.gettempdir()).glob("cts-orders-*"))
    with pytest.raises(CtsError) as missing:
        LiveSession(grant(network="TESTNET", account_id="acct-no-path"), credentials, pinned, now=NOW)
    assert missing.value.reason_code == "RECONCILIATION_FAILED"
    absent = tmp_path / "absent.db"
    with pytest.raises(CtsError) as lost:
        LiveSession(grant(network="TESTNET", account_id="acct-lost"), credentials, pinned, now=NOW, path=str(absent))
    assert lost.value.reason_code == "RECONCILIATION_FAILED"
    assert absent.exists() is False
    assert set(Path(tempfile.gettempdir()).glob("cts-orders-*")) == before
    assert opener.calls == []


def test_new_process_reopens_the_original_order_db(tmp_path) -> None:
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    path = ensure_order_db(tmp_path / "original.db")
    account = "acct-cross-process"
    _opener, pinned = transport([])
    first = LiveSession(grant(network="TESTNET", account_id=account), VenueCredentials("key-id", SECRET), pinned, now=NOW, path=path)
    first.orders.insert_intent(
        LiveOrder(
            client_order_id="cid-keep",
            account_id=account,
            reservation_id="res-keep",
            symbol="BTCUSDT",
            side="LONG",
            qty=D("0.6"),
            price=D("100"),
            notional=D("60"),
            status="INTENT",
            executed_qty=D("0"),
            dispatch_started=False,
            body={},
        ),
        NOW,
    )
    first.orders.close()
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    script = """
import json, sys
from datetime import datetime, timedelta, timezone
from cts.execution.live import LIVE_GRANT_STATEMENT, LiveGrant, LiveSession, VenueCredentials, open_testnet_transport
from cts.money import D
NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
def opener(method, url, headers):
    raise AssertionError("recovery must not send")
session = LiveSession(
    LiveGrant(granted_by="owner", statement=LIVE_GRANT_STATEMENT, network="TESTNET", account_id=sys.argv[2], max_notional=D("100"), expires_at=NOW + timedelta(minutes=10), symbols=("BTCUSDT", "ETHUSDT")),
    VenueCredentials("key-id", "test-secret-not-a-credential"),
    open_testnet_transport(opener),
    now=NOW,
    path=sys.argv[1],
)
order = session.get_order("cid-keep")
json.dump({"status": order.status, "reserved": format(session.reserved_notional, "f")}, sys.stdout)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, path, account],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"status": "INTENT", "reserved": "60"}


def test_pause_commits_only_before_or_after_the_send_lock(tmp_path) -> None:
    engine = make_engine(tmp_path, equity="100000")
    reservation = engine.reserve([req("BTCUSDT", "s-atomic")], now=NOW)[0]
    posts: list[str] = []
    blocked = threading.Event()
    entered = threading.Event()

    class Probe:
        def __init__(self, inner) -> None:
            self.inner = inner

        def __enter__(self):
            if not self.inner.acquire(blocking=False):
                blocked.set()
                self.inner.acquire()
            return self

        def __exit__(self, *args) -> None:
            self.inner.release()

    engine.store._lock = Probe(engine.store._lock)

    def opener(method: str, url: str, headers: dict[str, str]):
        if method == "POST":
            posts.append(url)
            entered.set()
            assert blocked.wait(2)
            return complete_order_ack(url, {"orderId": 1, "status": "NEW", "executedQty": "0"})
        if "positionSide" in url:
            return {"dualSidePosition": False}
        if "multiAssetsMargin" in url:
            return {"multiAssetsMargin": False}
        return [{"symbol": "BTCUSDT", "marginType": "isolated"}]

    def pause_after_send_holds_the_lock() -> None:
        assert entered.wait(2)
        engine.add_blocker("MANUAL_PAUSE", SystemState.PAUSED, "发送锁内暂停", now=NOW)

    session = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        open_testnet_transport(opener),
        now=NOW,
        path=ensure_order_db(tmp_path / "atomic.db"),
    )
    paused = threading.Thread(target=pause_after_send_holds_the_lock)
    paused.start()
    submit(session, engine, reservation.reservation_id)
    paused.join(2)
    assert posts
    assert paused.is_alive() is False
    assert len(posts) == 1


def test_wrong_symbol_response_keeps_the_budget(tmp_path) -> None:
    engine, reservation, session, _opener = admitted(tmp_path, healthy())
    submit(session, engine, reservation.reservation_id)
    reserved = session.reserved_notional
    session._transport = open_testnet_transport(
        lambda method, url, headers: {"symbol": "ETHUSDT", "status": "CANCELED", "executedQty": "0", "clientOrderId": "cid-1"}
    )
    with pytest.raises(CtsError) as err:
        session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert err.value.reason_code == "UNKNOWN_ORDER"
    assert session.get_order("cid-1").status == "UNKNOWN"
    assert session.get_order("cid-1").symbol == "BTCUSDT"
    assert session.reserved_notional == reserved


def test_rejected_with_a_fill_does_not_release_the_budget(tmp_path) -> None:
    engine, reservation, session, _opener = admitted(tmp_path, healthy())
    submit(session, engine, reservation.reservation_id, qty=D("0.6"), price=D("100"), notional=D("60"))
    session._transport = open_testnet_transport(
        lambda method, url, headers: {"status": "REJECTED", "executedQty": "0.4", "origQty": "0.6", "clientOrderId": "cid-1", "symbol": "BTCUSDT", "side": "BUY"}
    )
    with pytest.raises(CtsError) as err:
        session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW, risk=engine)
    assert err.value.reason_code == "RECONCILIATION_FAILED"
    order = session.get_order("cid-1")
    assert order.status == "UNKNOWN"
    assert order.executed_qty == D("0.4")
    assert session.reserved_notional == D("60")
    stored = next(item for item in engine.occupancy().reservations if item.reservation_id == reservation.reservation_id)
    assert stored.filled_qty == D("0.4")


def test_interrupted_cancel_reopens_as_unknown(tmp_path) -> None:
    engine, reservation, session, _opener = admitted(tmp_path, healthy())
    submit(session, engine, reservation.reservation_id)
    session._save_order(session.get_order("cid-1"), status="CANCELING", dispatch_started=True, now=NOW)
    _restored_opener, pinned = transport([])
    restored = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id),
        VenueCredentials("key-id", SECRET),
        pinned,
        now=NOW,
        path=session.orders.path,
    )
    assert restored.get_order("cid-1").status == "UNKNOWN"
    assert restored.reserved_notional == D("1")


def test_failed_wallet_read_keeps_the_local_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    from cts.console.app import ConsoleApp, ConsoleState, apply_venue_wallet
    from cts.execution.live import parse_usdt_wallet
    from cts.ledger.journal import Ledger
    from tests.test_ledger import TS

    body = {
        "totalMarginBalance": "4999.65538680",
        "assets": [
            {
                "asset": "USDT",
                "walletBalance": "4999.65538680",
                "availableBalance": "4999.65538680",
                "unrealizedProfit": "0.00000000",
                "marginBalance": "4999.65538680",
                "initialMargin": "0.00000000",
            }
        ],
    }
    venue = parse_usdt_wallet(body)
    ledger = Ledger("acct")
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep")
    state = ConsoleState.from_ledger(ledger)
    apply_venue_wallet(state, venue, as_of="2026-10-31T00:00:00+00:00")
    local = state.books["local_equity"]
    def unreadable() -> dict:
        raise TimeoutError("read failed")

    monkeypatch.setattr("cts.execution.live.read_demo_wallet", unreadable)
    ConsoleApp("token", state, watch_demo=True).refresh_demo_wallet()
    assert state.books["local_equity"] == local
    assert state.books["available"] == "暂无证据"
    assert state.metrics["data_freshness"] == "币安模拟盘读取失败"
    apply_venue_wallet(state, venue, as_of="2026-10-31T00:02:00+00:00")
    assert state.books["local_equity"] == local
    assert state.books["available"] == venue["available"]
    assert state.books["gap"] == format(D(venue["equity"]) - D(local), "f")
    assert "00:02:00" in state.metrics["data_freshness"]


def _filled(engine, reservation_id):
    return next(item.filled_qty for item in engine.occupancy().reservations if item.reservation_id == reservation_id)


def _identified(status: str, executed: str, qty: str, client: str = "cid-1", symbol: str = "BTCUSDT") -> dict:
    return {
        "status": status,
        "executedQty": executed,
        "origQty": qty,
        "clientOrderId": client,
        "symbol": symbol,
        "side": "BUY",
    }


def test_query_without_risk_does_not_skip_projection(tmp_path) -> None:
    engine, reservation, session, _opener = admitted(tmp_path, healthy())
    submit(session, engine, reservation.reservation_id, qty=D("0.6"), price=D("100"), notional=D("60"))
    session._risk = None
    session._transport = open_testnet_transport(lambda method, url, headers: _identified("FILLED", "0.6", "0.6"))
    with pytest.raises(CtsError) as missing:
        session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert missing.value.reason_code == "RECONCILIATION_FAILED"
    assert session.get_order("cid-1").status != "FILLED"
    assert _filled(engine, reservation.reservation_id) == D("0")
    session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW, risk=engine)
    assert session.get_order("cid-1").status == "FILLED"
    assert _filled(engine, reservation.reservation_id) == D("0.6")
    session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW, risk=engine)
    assert _filled(engine, reservation.reservation_id) == D("0.6")


def test_filled_order_without_a_cursor_backfills_once(tmp_path) -> None:
    engine, reservation, session, _opener = admitted(tmp_path, healthy())
    submit(session, engine, reservation.reservation_id, qty=D("0.6"), price=D("100"), notional=D("60"))
    current = session.get_order("cid-1")
    session.orders.save(
        LiveOrder(
            client_order_id=current.client_order_id,
            account_id=current.account_id,
            reservation_id=current.reservation_id,
            symbol=current.symbol,
            side=current.side,
            qty=current.qty,
            price=current.price,
            notional=current.notional,
            status="FILLED",
            executed_qty=D("0.6"),
            dispatch_started=True,
            body=current.body,
        ),
        NOW,
    )
    session._risk = None
    session._transport = open_testnet_transport(lambda method, url, headers: _identified("FILLED", "0.6", "0.6"))
    with pytest.raises(CtsError) as missing:
        session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert missing.value.reason_code == "RECONCILIATION_FAILED"
    assert session.get_order("cid-1").status == "FILLED"
    assert _filled(engine, reservation.reservation_id) == D("0")
    session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW, risk=engine)
    assert _filled(engine, reservation.reservation_id) == D("0.6")
    session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW, risk=engine)
    assert _filled(engine, reservation.reservation_id) == D("0.6")


def test_interrupted_cumulative_is_not_applied_twice(tmp_path) -> None:
    engine = make_engine(tmp_path, equity="100000")
    reservation = engine.reserve([req("BTCUSDT", "s-cursor")], now=NOW)[0]
    opener, pinned = transport(healthy())
    path = ensure_order_db(tmp_path / "cursor.db")
    session = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        pinned,
        now=NOW,
        path=path,
    )
    submit(session, engine, reservation.reservation_id, qty=D("0.6"), price=D("100"), notional=D("60"))
    session._transport = open_testnet_transport(lambda method, url, headers: _identified("PARTIALLY_FILLED", "0.2", "0.6"))
    session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW, risk=engine)
    assert _filled(engine, reservation.reservation_id) == D("0.2")
    current = session.get_order("cid-1")
    session.orders.save(
        LiveOrder(
            client_order_id=current.client_order_id,
            account_id=current.account_id,
            reservation_id=current.reservation_id,
            symbol=current.symbol,
            side=current.side,
            qty=current.qty,
            price=current.price,
            notional=current.notional,
            status="NEW",
            executed_qty=D("0"),
            dispatch_started=True,
            body=current.body,
        ),
        NOW,
    )
    restored = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        open_testnet_transport(lambda method, url, headers: _identified("PARTIALLY_FILLED", "0.4", "0.6")),
        now=NOW,
        path=path,
        risk=engine,
    )
    restored.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert restored.get_order("cid-1").executed_qty == D("0.4")
    assert _filled(engine, reservation.reservation_id) == D("0.4")


def test_concurrent_queries_project_once(tmp_path) -> None:
    engine, reservation, session, _opener = admitted(tmp_path, healthy())
    submit(session, engine, reservation.reservation_id, qty=D("0.6"), price=D("100"), notional=D("60"))
    session._transport = open_testnet_transport(lambda method, url, headers: _identified("PARTIALLY_FILLED", "0.4", "0.6"))
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def race() -> None:
        barrier.wait()
        try:
            session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW, risk=engine)
        except CtsError as exc:
            errors.append(exc.reason_code)

    threads = [threading.Thread(target=race), threading.Thread(target=race)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert _filled(engine, reservation.reservation_id) == D("0.4")
    assert not errors


def test_query_interruption_blocks_new_risk(tmp_path) -> None:
    engine = make_engine(tmp_path, equity="100000")
    reservation = engine.reserve([req("BTCUSDT", "s-query-hold")], now=NOW)[0]
    seen: list[str] = []

    def opener(method: str, url: str, headers: dict[str, str]):
        if method == "GET" and "/fapi/v1/order" in url:
            try:
                engine.reserve([req("ETHUSDT", "s-during-query")], now=NOW)
            except CtsError as exc:
                seen.append(exc.reason_code)
            raise TimeoutError("query lost")
        if "positionSide" in url:
            return {"dualSidePosition": False}
        if "multiAssetsMargin" in url:
            return {"multiAssetsMargin": False}
        if method == "POST":
            return complete_order_ack(url, {"orderId": 1, "status": "NEW", "executedQty": "0"})
        return [{"symbol": "BTCUSDT", "marginType": "isolated"}]

    path = ensure_order_db(tmp_path / "query-hold.db")
    session = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        open_testnet_transport(opener),
        now=NOW,
        path=path,
    )
    submit(session, engine, reservation.reservation_id)
    with pytest.raises(CtsError):
        session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert seen == ["UNKNOWN_ORDER"]
    session._save_order(session.get_order("cid-1"), status="QUERYING", dispatch_started=True, now=NOW)
    restored = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        open_testnet_transport(lambda method, url, headers: {"dualSidePosition": False}),
        now=NOW,
        path=path,
    )
    assert restored.get_order("cid-1").status == "UNKNOWN"
    with pytest.raises(CtsError) as blocked:
        engine.reserve([req("ETHUSDT", "s-after-restart")], now=NOW)
    assert blocked.value.reason_code == "UNKNOWN_ORDER"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", "ETHUSDT"),
        ("side", "SELL"),
        ("clientOrderId", "cid-other"),
        ("origQty", "1"),
    ],
)
def test_new_rejects_an_identity_conflict(tmp_path, field: str, value: str) -> None:
    engine, reservation, session, _opener = admitted(tmp_path, healthy())
    submit(session, engine, reservation.reservation_id)
    body = {
        "status": "NEW",
        "executedQty": "0",
        "origQty": "0.01",
        "clientOrderId": "cid-1",
        "symbol": "BTCUSDT",
        "side": "BUY",
    }
    body[field] = value
    session._transport = open_testnet_transport(lambda method, url, headers: body)
    with pytest.raises(CtsError) as err:
        session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert err.value.reason_code == "UNKNOWN_ORDER"
    stored = session.get_order("cid-1")
    assert stored.status == "UNKNOWN"
    assert stored.executed_qty == D("0")
    assert session.reserved_notional == D("1")
    with pytest.raises(CtsError) as blocked:
        engine.reserve([req("ETHUSDT", f"s-conflict-{field}")], now=NOW)
    assert blocked.value.reason_code == "UNKNOWN_ORDER"


def test_new_without_identity_is_not_accepted(tmp_path) -> None:
    engine, reservation, session, _opener = admitted(tmp_path, healthy())
    submit(session, engine, reservation.reservation_id)
    session._transport = open_testnet_transport(lambda method, url, headers: {"status": "NEW", "executedQty": "0"})
    with pytest.raises(CtsError) as err:
        session.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert err.value.reason_code == "UNKNOWN_ORDER"
    assert session.get_order("cid-1").status == "UNKNOWN"
    assert session.reserved_notional == D("1")


def test_one_query_cannot_clear_another_orders_hold(tmp_path) -> None:
    from cts.risk.models import ReservationStatus

    engine, reservation, session, _opener = admitted(tmp_path, healthy() + healthy())
    submit(session, engine, reservation.reservation_id, qty=D("0.2"), price=D("100"), notional=D("20"), client_order_id="cid-a")
    submit(session, engine, reservation.reservation_id, qty=D("0.2"), price=D("100"), notional=D("20"), client_order_id="cid-b")
    entered = threading.Event()
    release = threading.Event()

    def opener(method: str, url: str, headers: dict[str, str]):
        if "origClientOrderId=cid-a" in url:
            entered.set()
            assert release.wait(2)
        return complete_order_ack(
            url,
            {"status": "NEW", "executedQty": "0", "origQty": "0.2", "side": "BUY"},
        )

    session._transport = open_testnet_transport(opener)
    worker = threading.Thread(target=lambda: session.query(symbol="BTCUSDT", client_order_id="cid-a", now=NOW))
    worker.start()
    assert entered.wait(2)
    session.query(symbol="BTCUSDT", client_order_id="cid-b", now=NOW)
    assert session.get_order("cid-a").status == "QUERYING"
    assert session.get_order("cid-b").status == "NEW"
    stored = next(item for item in engine.occupancy().reservations if item.reservation_id == reservation.reservation_id)
    assert stored.status == ReservationStatus.UNKNOWN
    with pytest.raises(CtsError) as blocked:
        engine.reserve([req("ETHUSDT", "s-while-querying")], now=NOW)
    assert blocked.value.reason_code == "UNKNOWN_ORDER"
    release.set()
    worker.join(2)
    assert worker.is_alive() is False
    assert session.get_order("cid-a").status == "NEW"
    created = engine.reserve([req("ETHUSDT", "s-after-both")], now=NOW)
    assert created[0].qty > 0


def _ack_new(qty: str = "0.01", client: str = "cid-1", symbol: str = "BTCUSDT") -> dict:
    return _identified("NEW", "0", qty, client, symbol)


def _open_peer(session: LiveSession, engine, transport) -> LiveSession:
    return LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100000")),
        VenueCredentials("key-id", SECRET),
        transport,
        now=NOW,
        path=session.orders.path,
        risk=engine,
    )


def test_other_session_keeps_an_inflight_hold(tmp_path) -> None:
    engine, reservation, session_a, _opener = admitted(tmp_path, healthy())
    submit(session_a, engine, reservation.reservation_id)
    entered = threading.Event()
    release = threading.Event()

    def blocked(method: str, url: str, headers: dict[str, str]):
        entered.set()
        assert release.wait(2)
        return _ack_new()

    session_a._transport = open_testnet_transport(blocked)
    session_b = _open_peer(session_a, engine, open_testnet_transport(lambda method, url, headers: _ack_new()))
    worker = threading.Thread(target=lambda: session_a.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW))
    worker.start()
    assert entered.wait(2)
    session_b.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert engine.live_hold_count(reservation.reservation_id) == 1
    with pytest.raises(CtsError) as blocked_risk:
        engine.reserve([req("ETHUSDT", "s-peer-query")], now=NOW)
    assert blocked_risk.value.reason_code == "UNKNOWN_ORDER"
    release.set()
    worker.join(2)
    assert worker.is_alive() is False
    assert session_a.get_order("cid-1").status == "NEW"
    assert engine.live_hold_count(reservation.reservation_id) == 0
    created = engine.reserve([req("ETHUSDT", "s-peer-query-done")], now=NOW)
    assert created[0].qty > 0


def test_other_session_cancel_keeps_an_inflight_query(tmp_path) -> None:
    engine, reservation, session_a, _opener = admitted(tmp_path, healthy())
    submit(session_a, engine, reservation.reservation_id)
    entered = threading.Event()
    release = threading.Event()

    def blocked(method: str, url: str, headers: dict[str, str]):
        entered.set()
        assert release.wait(2)
        return _ack_new()

    session_a._transport = open_testnet_transport(blocked)
    session_b = _open_peer(
        session_a,
        engine,
        open_testnet_transport(lambda method, url, headers: _identified("CANCELED", "0", "0.01")),
    )
    worker = threading.Thread(target=lambda: session_a.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW))
    worker.start()
    assert entered.wait(2)
    session_b.cancel(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert engine.live_hold_count(reservation.reservation_id) == 1
    with pytest.raises(CtsError) as blocked_risk:
        engine.reserve([req("ETHUSDT", "s-peer-cancel")], now=NOW)
    assert blocked_risk.value.reason_code == "UNKNOWN_ORDER"
    release.set()
    worker.join(2)
    assert worker.is_alive() is False


def test_recovered_owner_hold_clears_after_the_order_is_confirmed(tmp_path) -> None:
    engine, reservation, session_a, _opener = admitted(tmp_path, healthy())
    submit(session_a, engine, reservation.reservation_id)
    entered = threading.Event()
    release = threading.Event()

    def blocked(method: str, url: str, headers: dict[str, str]):
        entered.set()
        assert release.wait(2)
        return _ack_new()

    session_a._transport = open_testnet_transport(blocked)
    session_b = _open_peer(session_a, engine, open_testnet_transport(lambda method, url, headers: _ack_new()))
    worker = threading.Thread(target=lambda: session_a.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW))
    worker.start()
    assert entered.wait(2)
    assert session_a._owner_lock_fd is not None
    os.close(session_a._owner_lock_fd)
    session_a._owner_lock_fd = None
    session_b.query(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert engine.live_hold_count(reservation.reservation_id) == 0
    evidence = engine.store._conn.execute(
        "SELECT recovery_evidence FROM live_hold_owners WHERE owner_id=?",
        (session_a._owner_id,),
    ).fetchone()
    assert evidence["recovery_evidence"] == "所有者锁已释放"
    created = engine.reserve([req("ETHUSDT", "s-owner-recovered")], now=NOW)
    assert created[0].qty > 0
    release.set()
    worker.join(2)
    assert worker.is_alive() is False


def test_cancel_identity_must_be_present(tmp_path) -> None:
    engine, reservation, session, _opener = admitted(tmp_path, healthy())
    submit(session, engine, reservation.reservation_id, qty=D("0.6"), price=D("100"), notional=D("60"))
    session._transport = open_testnet_transport(lambda method, url, headers: {"status": "CANCELED", "executedQty": "0"})
    with pytest.raises(CtsError) as err:
        session.cancel(symbol="BTCUSDT", client_order_id="cid-1", now=NOW)
    assert err.value.reason_code == "UNKNOWN_ORDER"
    assert session.reserved_notional == D("60")
    assert session.get_order("cid-1").status == "UNKNOWN"


def test_cancel_releases_the_risk_reservation(tmp_path) -> None:
    from cts.risk.models import ReservationStatus

    engine, reservation, session, _opener = admitted(tmp_path, healthy())
    submit(
        session,
        engine,
        reservation.reservation_id,
        qty=reservation.qty,
        price=D("100"),
        notional=reservation.notional,
        client_order_id="cid-full",
    )
    session._transport = open_testnet_transport(
        lambda method, url, headers: _identified("CANCELED", "0", format(reservation.qty, "f"), "cid-full")
    )
    session.cancel(symbol="BTCUSDT", client_order_id="cid-full", now=NOW)
    assert session.reserved_notional == D("0")
    stored = next(item for item in engine.occupancy().reservations if item.reservation_id == reservation.reservation_id)
    assert stored.status == ReservationStatus.RELEASED
    assert stored.pending_notional == D("0")


def test_another_risk_store_cannot_pause_during_the_post(tmp_path) -> None:
    import time

    from cts.risk.engine import RiskEngine
    from cts.risk.store import RiskStore

    engine = make_engine(tmp_path, equity="100000")
    reservation = engine.reserve([req("BTCUSDT", "s-writer")], now=NOW)[0]
    other = RiskEngine(RiskStore(engine.store.path), engine.account_id)
    started = threading.Event()
    attempting = threading.Event()
    done = threading.Event()

    def opener(method: str, url: str, headers: dict[str, str]):
        if method == "POST":
            started.set()
            assert attempting.wait(2)
            time.sleep(0.15)
            assert done.is_set() is False
            return complete_order_ack(url, {"orderId": 1, "status": "NEW", "executedQty": "0"})
        if "positionSide" in url:
            return {"dualSidePosition": False}
        if "multiAssetsMargin" in url:
            return {"multiAssetsMargin": False}
        return [{"symbol": "BTCUSDT", "marginType": "isolated"}]

    def pause() -> None:
        assert started.wait(2)
        attempting.set()
        other.add_blocker("MANUAL_PAUSE", SystemState.PAUSED, "另一连接暂停", now=NOW)
        done.set()

    session = LiveSession(
        grant(network="TESTNET", account_id=engine.account_id, max_notional=D("100")),
        VenueCredentials("key-id", SECRET),
        open_testnet_transport(opener),
        now=NOW,
        path=ensure_order_db(tmp_path / "writer.db"),
    )
    thread = threading.Thread(target=pause)
    thread.start()
    submit(session, engine, reservation.reservation_id)
    thread.join(2)
    assert done.is_set()
    assert thread.is_alive() is False
    other.close()


def test_blank_credentials_are_rejected() -> None:
    with pytest.raises(CtsError) as err:
        VenueCredentials("", SECRET)
    assert err.value.reason_code == "CREDENTIALS_MISSING"

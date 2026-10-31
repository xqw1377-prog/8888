import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cts.console.app import ConsoleApp, ConsoleState, apply_research_report
from cts.ledger.models import Side
from cts.ledger.journal import Ledger
from cts.money import D
from tests.test_ledger import TS


def _json(port: int, path: str, *, token: str | None = None, body: dict | None = None, key: str | None = None, cookie: str | None = None):
    headers = {}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    request = Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
    try:
        with urlopen(request) as response:
            raw = response.read().decode()
            return response.status, response.headers, raw
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode()


def test_console_separates_pause_flatten_and_hides_missing_evidence() -> None:
    state = ConsoleState(
        positions=[{"symbol": "BTCUSDT", "qty": "0", "freshness": "UNKNOWN"}],
        fills=[{"origin": "SIMULATION", "symbol": "BTCUSDT"}, {"origin": "LIVE", "symbol": "ETHUSDT"}],
    )
    app = ConsoleApp("secret", state)
    port = app.serve()
    try:
        status, _, body = _json(port, "/v1/overview")
        assert status == 401
        assert "需要重新认证" in body
        status, _, page = _json(port, "/")
        assert status == 200
        assert "会话口令" in page
        status, headers, html = _json(port, "/v1/session", body={"token": "secret"})
        assert status == 200
        assert "HOLD" in html
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, overview = _json(port, "/v1/overview", cookie=cookie)
        payload = json.loads(overview)
        assert payload["equity"] == "暂无证据"
        assert payload["positions"][0]["qty"] == "待核对"
        assert payload["live_authorization"] == "HOLD"
        assert payload["live_fills"] == [{"origin": "LIVE", "symbol": "ETHUSDT"}]
        status, _, paused = _json(
            port,
            "/v1/control/pause",
            cookie=cookie,
            body={"expected_version": payload["version"]},
            key="pause-1",
        )
        pause_body = json.loads(paused)
        assert pause_body["paused"] is True
        assert pause_body["flatten_requested"] is False
        status, _, again = _json(
            port,
            "/v1/control/pause",
            cookie=cookie,
            body={"expected_version": payload["version"]},
            key="pause-1",
        )
        assert json.loads(again)["version"] == pause_body["version"]
        status, _, conflict = _json(
            port,
            "/v1/control/flatten-request",
            cookie=cookie,
            body={"expected_version": pause_body["version"]},
            key="pause-1",
        )
        assert status == 409
        status, _, flat = _json(
            port,
            "/v1/control/flatten-request",
            cookie=cookie,
            body={"expected_version": pause_body["version"]},
            key="flat-1",
        )
        flat_body = json.loads(flat)
        assert flat_body["flatten_requested"] is True
        assert "尚未确认成交" in flat_body["message_zh"]
        status, _, resume = _json(
            port,
            "/v1/control/resume-request",
            cookie=cookie,
            body={"expected_version": flat_body["version"]},
            key="resume-1",
        )
        resume_body = json.loads(resume)
        assert resume_body["ok"] is False
        assert resume_body["system_state"] == "RECONCILING"
        status, _, missing = _json(port, "/v1/trades/missing/trace", cookie=cookie)
        assert status == 404
        assert "暂无证据" in missing
    finally:
        app.shutdown()


def test_console_reads_ledger_equity() -> None:
    ledger = Ledger("acct")
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep")
    state = ConsoleState.from_ledger(ledger)
    assert state.equity == "10000"
    assert state.mode == "SIMULATION"
    assert state.positions == []
    assert state.books["wallet"] == "10000"
    assert state.metrics["gross_leverage"] == "0"
    assert state.metrics["drawdown"] == "0"
    assert state.metrics["net_pnl"] == "0"
    assert state.report["conclusion"] == "证据不足"
    assert state.settings["approval"] == "实盘 HOLD"


def test_console_pages_cover_limits_books_and_recovery() -> None:
    ledger = Ledger("acct")
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep")
    app = ConsoleApp("secret", ConsoleState.from_ledger(ledger))
    port = app.serve()
    try:
        status, _, raw = _json(port, "/v1/overview", token="secret")
        body = json.loads(raw)
        assert status == 200
        assert body["metrics"]["data_freshness"] == "行情 NOT_CHECKED"
        assert body["books"]["wallet"] == "10000"
        assert body["books"]["gap"] == "未接交易所，差异暂无证据"
        assert body["last_reconcile_at"] == "暂无证据"
        assert body["live_authorization"] == "HOLD"
        status, _, tighten = _json(
            port,
            "/v1/control/tighten",
            token="secret",
            body={"expected_version": body["version"]},
            key="tighten-1",
        )
        tightened = json.loads(tighten)
        assert status == 200
        assert tightened["risk_coefficient"] == "0.5"
        assert "PROPOSED" in tightened["message_zh"]
        status, _, resumed = _json(
            port,
            "/v1/control/resume-request",
            token="secret",
            body={"expected_version": tightened["version"]},
            key="resume-dash",
        )
        resume_body = json.loads(resumed)
        assert status == 200
        assert resume_body["ok"] is False
        assert resume_body["system_state"] == "RECONCILING"
        status, _, overview = _json(port, "/v1/overview", token="secret")
        refreshed = json.loads(overview)
        assert refreshed["system_state"] == "RECONCILING"
        assert refreshed["approvals"] == ["恢复申请已记录，未批准"]
        assert refreshed["risk_coefficient"] == "0.5"
        status, _, recon = _json(
            port,
            "/v1/reconciliation-runs",
            token="secret",
            body={"expected_version": refreshed["version"]},
            key="recon-dash",
        )
        assert status == 200
        assert "未修改交易所余额" in recon
        status, _, exported = _json(port, "/v1/report/export", token="secret")
        report = json.loads(exported)
        assert status == 200
        assert report["conclusion"] == "证据不足"
        assert report["live_authorization"] == "HOLD"
        status, _, page = _json(port, "/", token="secret")
        assert "报告" in page and "设置" in page and "收紧额度" in page and "发起只读对账" in page
        assert report["confidence"] == "暂无证据"
        assert refreshed["settings"]["micro_live"] == "UNVERIFIED"
    finally:
        app.shutdown()


def test_trade_chain_names_each_link_or_says_evidence_is_missing() -> None:
    ledger = Ledger("acct")
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep")
    ledger.apply_fill(
        symbol="BTCUSDT",
        side=Side.LONG,
        qty=D("1"),
        price=D("100"),
        multiplier=D("1"),
        event_time=TS,
        received_at=TS,
        external_id="entry:BTCUSDT:1",
        venue="SIM",
        venue_trade_id="t1",
        annotations={
            "signal_time": TS.isoformat(),
            "risk_decision": "已按权益和止损距离定仓",
            "direction": "LONG",
            "protection": "研究止损 90，无交易所保护单证据",
            "liq_distance": "本地公式 0.2，不是交易所读回",
        },
    )
    ledger.apply_fill(
        symbol="BTCUSDT",
        side=Side.SHORT,
        qty=D("1"),
        price=D("110"),
        multiplier=D("1"),
        event_time=TS,
        received_at=TS,
        external_id="exit:BTCUSDT:1:STOP:m",
        venue="SIM",
        venue_trade_id="t2",
        reduce_only=True,
        annotations={"exit_reason": "STOP", "direction": "LONG"},
    )
    ledger.post_funding(
        payment=D("-0.5"),
        event_time=TS,
        received_at=TS,
        external_id="funding:BTCUSDT:1",
        venue="SIM",
        symbol="BTCUSDT",
    )
    state = ConsoleState.from_ledger(ledger)
    trace = state.traces["entry:BTCUSDT:1"]
    assert trace["signal"] == TS.isoformat()
    assert trace["risk"] == "已按权益和止损距离定仓"
    assert trace["protection"] == "研究止损 90，无交易所保护单证据"
    assert trace["exit_reason"] == "STOP"
    assert trace["direction"] == "LONG"
    assert trace["funding"] == "-0.5"
    assert "不是交易所读回" in trace["liq_distance"]
    assert state.report["concentration"].startswith("BTCUSDT")
    assert state.report["data_grade"] == "SIGNAL_RESEARCH_ONLY"

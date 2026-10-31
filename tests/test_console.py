import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cts.console.app import ConsoleApp, ConsoleState


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

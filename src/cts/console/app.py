"""本地控制台。数据接口必须带身份；暂停新开仓和退出持仓是两个操作。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

from cts import LIVE_TRADING_AUTHORIZATION


REASONS = {
    "STALE_DATA": "版本已过期，请刷新后再提交",
    "RISK_LIMIT": "风险额度不足",
    "UNKNOWN_ORDER": "订单结果未知，不能当成没有成交",
    "RECONCILIATION_FAILED": "对账未通过",
    "LIVE_TRADING_HOLD": "实盘未授权",
    "UNAUTHORIZED": "需要重新认证",
    "IDEMPOTENCY_CONFLICT": "同一个幂等键对应了不同请求",
}


@dataclass
class ConsoleState:
    mode: str = "SIMULATION"
    equity: Optional[str] = None
    positions: list[dict[str, str]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    version: int = 1
    experiments: dict[str, dict] = field(default_factory=dict)
    traces: dict[str, dict] = field(default_factory=dict)
    fills: list[dict[str, str]] = field(default_factory=list)
    system_state: str = "RECONCILING"
    flatten_requested: bool = False
    paused: bool = False
    reconciliations: list[str] = field(default_factory=list)


class ConsoleApp:
    def __init__(self, token: str, state: Optional[ConsoleState] = None) -> None:
        self.token = token
        self.state = state or ConsoleState()
        self._idempotency: dict[str, tuple[str, dict]] = {}
        self.httpd: Optional[ThreadingHTTPServer] = None

    def serve(self) -> int:
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:
                return

            def _cookie_token(self) -> str:
                jar = SimpleCookie(self.headers.get("Cookie"))
                morsel = jar.get("cts_session")
                return morsel.value if morsel else ""

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                bearer = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
                return bearer == app.token or self._cookie_token() == app.token

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length") or "0")
                if length == 0:
                    return {}
                return json.loads(self.rfile.read(length).decode())

            def _send(self, code: int, payload: dict) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _html(self, code: int, body: str, *, cookie: Optional[str] = None) -> None:
                raw = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if cookie is not None:
                    self.send_header("Set-Cookie", f"cts_session={cookie}; HttpOnly; Path=/")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _guard(self) -> bool:
                if self._authorized():
                    return True
                self._send(401, {"reason_code": "UNAUTHORIZED", "message_zh": REASONS["UNAUTHORIZED"]})
                return False

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/":
                    if not self._authorized():
                        self._html(200, LOGIN_HTML)
                        return
                    self._html(200, console_html(app.state))
                    return
                if not self._guard():
                    return
                if path == "/v1/overview":
                    self._send(200, overview_payload(app.state))
                    return
                if path.startswith("/v1/experiments/") and path.endswith("/report"):
                    experiment_id = path.split("/")[3]
                    report = app.state.experiments.get(experiment_id)
                    if report is None:
                        self._send(404, {"reason_code": "STALE_DATA", "message_zh": "暂无证据"})
                        return
                    self._send(200, report)
                    return
                if path.startswith("/v1/trades/") and path.endswith("/trace"):
                    trade_id = path.split("/")[3]
                    trace = app.state.traces.get(trade_id)
                    if trace is None:
                        self._send(404, {"reason_code": "STALE_DATA", "message_zh": "暂无证据"})
                        return
                    self._send(200, trace)
                    return
                self._send(404, {"reason_code": "STALE_DATA", "message_zh": "暂无证据"})

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                body = self._read_json()
                if path == "/v1/session":
                    if body.get("token") != app.token:
                        self._send(401, {"reason_code": "UNAUTHORIZED", "message_zh": REASONS["UNAUTHORIZED"]})
                        return
                    self._html(200, console_html(app.state), cookie=app.token)
                    return
                if not self._guard():
                    return
                key = self.headers.get("Idempotency-Key", "")
                if not key:
                    self._send(400, {"reason_code": "STALE_DATA", "message_zh": "写操作需要幂等键"})
                    return
                signature = json.dumps({"path": path, "body": body}, sort_keys=True, ensure_ascii=False)
                prior = app._idempotency.get(key)
                if prior is not None:
                    if prior[0] != signature:
                        self._send(409, {"reason_code": "IDEMPOTENCY_CONFLICT", "message_zh": REASONS["IDEMPOTENCY_CONFLICT"]})
                        return
                    self._send(200, prior[1])
                    return
                expected = body.get("expected_version")
                if expected is not None and int(expected) != app.state.version:
                    self._send(409, {"reason_code": "STALE_DATA", "message_zh": REASONS["STALE_DATA"]})
                    return
                if path == "/v1/control/pause":
                    app.state.paused = True
                    app.state.version += 1
                    if "MANUAL_PAUSE" not in app.state.blockers:
                        app.state.blockers.append("MANUAL_PAUSE")
                    payload = {"ok": True, "paused": True, "flatten_requested": app.state.flatten_requested, "version": app.state.version}
                elif path == "/v1/control/flatten-request":
                    app.state.flatten_requested = True
                    app.state.version += 1
                    payload = {
                        "ok": True,
                        "paused": app.state.paused,
                        "flatten_requested": True,
                        "version": app.state.version,
                        "message_zh": "已创建受控退出请求，尚未确认成交",
                    }
                elif path == "/v1/control/resume-request":
                    payload = {
                        "ok": False,
                        "system_state": app.state.system_state,
                        "reason_code": "RECOVERY_APPROVAL_REQUIRED",
                        "message_zh": "恢复申请已记录，不能跳过审批直接进入运行",
                    }
                elif path == "/v1/experiments":
                    experiment_id = str(body.get("experiment_id") or "E0")
                    app.state.experiments[experiment_id] = {
                        "experiment_id": experiment_id,
                        "conclusion": "证据不足",
                        "message_zh": "研究任务已冻结，不会触发交易",
                    }
                    payload = app.state.experiments[experiment_id]
                elif path == "/v1/reconciliation-runs":
                    run_id = f"rec-{len(app.state.reconciliations) + 1}"
                    app.state.reconciliations.append(run_id)
                    payload = {"run_id": run_id, "message_zh": "已发起只读对账，未修改交易所余额"}
                else:
                    self._send(404, {"reason_code": "STALE_DATA", "message_zh": "暂无证据"})
                    return
                app._idempotency[key] = (signature, payload)
                self._send(200, payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self.httpd.server_address[1]
        import threading

        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return port

    def shutdown(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()


def overview_payload(state: ConsoleState) -> dict:
    if state.equity is None and not state.positions:
        equity_text = "暂无证据"
    else:
        equity_text = state.equity if state.equity is not None else "暂无证据"
    positions = []
    for row in state.positions:
        freshness = row.get("freshness", "UNKNOWN")
        qty = "待核对" if freshness == "UNKNOWN" else row.get("qty", "待核对")
        positions.append({"symbol": row.get("symbol", ""), "qty": qty, "freshness": freshness})
    live_fills = [row for row in state.fills if row.get("origin") == "LIVE"]
    return {
        "mode": state.mode,
        "live_authorization": LIVE_TRADING_AUTHORIZATION,
        "message_zh": "本次会话不能替代全天候运行监控",
        "equity": equity_text,
        "system_state": state.system_state,
        "blockers": state.blockers,
        "positions": positions,
        "live_fills": live_fills,
        "simulation_fills": [row for row in state.fills if row.get("origin") == "SIMULATION"],
        "backtest_fills": [row for row in state.fills if row.get("origin") == "BACKTEST"],
        "version": state.version,
        "paused": state.paused,
        "flatten_requested": state.flatten_requested,
    }


def console_html(state: ConsoleState) -> str:
    data = json.dumps(overview_payload(state), ensure_ascii=False)
    return CONSOLE_HTML.replace("/*OVERVIEW*/null", data)


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>进入账室</title>
<style>
  :root { --ink:#1c1915; --paper:#f3ecdf; --seal:#9d2c2c; }
  body { margin:0; min-height:100vh; background:var(--paper); color:var(--ink);
    font-family: "Iowan Old Style", "Palatino Linotype", Palatino, serif; display:grid; place-items:center; }
  form { width:min(420px, 92vw); border-top:1px solid var(--ink); padding-top:18px; }
  h1 { font-weight:500; font-size:40px; margin:0 0 8px; }
  p { margin:0 0 18px; }
  input { width:100%; box-sizing:border-box; border:0; border-bottom:1px solid var(--ink); background:transparent;
    font:inherit; font-size:18px; padding:8px 0; }
  button { margin-top:18px; background:var(--ink); color:var(--paper); border:0; padding:10px 16px; font:inherit; }
</style>
</head>
<body>
<form method="post" action="/v1/session" onsubmit="return send(event)">
  <h1>账室</h1>
  <p>研究与模拟记录。实盘未授权。</p>
  <label>会话口令<input id="token" name="token" autocomplete="current-password"/></label>
  <button type="submit">进入</button>
</form>
<script>
async function send(event) {
  event.preventDefault();
  const token = document.getElementById("token").value;
  const response = await fetch("/v1/session", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({token})});
  if (!response.ok) { alert("口令不正确"); return false; }
  document.open(); document.write(await response.text()); document.close();
  return false;
}
</script>
</body>
</html>
"""


CONSOLE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>账室</title>
<style>
  :root { --ink:#1b1914; --paper:#f4efe4; --rule:#c9bfae; --seal:#8f2d2a; --moss:#315c45; }
  * { box-sizing:border-box; }
  body { margin:0; background:
    radial-gradient(circle at 12% -10%, #fffaf1, transparent 42%),
    var(--paper);
    color:var(--ink); font-family:"Iowan Old Style","Palatino Linotype",Palatino,serif; }
  header { display:grid; grid-template-columns:1.4fr .6fr; gap:24px; padding:28px 32px 12px; align-items:end; }
  h1 { font-size:64px; line-height:.85; margin:0; font-weight:520; letter-spacing:-1px; }
  .seal { justify-self:end; border:2px solid var(--seal); color:var(--seal); width:92px; height:92px;
    border-radius:50%; display:grid; place-items:center; transform:rotate(8deg); font-size:18px; letter-spacing:2px; }
  nav { display:flex; gap:18px; padding:0 32px 12px; border-bottom:1px solid var(--ink); }
  button.tab, button.act { font:inherit; background:transparent; border:0; padding:0; cursor:pointer; }
  button.tab[aria-current="true"] { border-bottom:2px solid var(--ink); }
  main { display:grid; grid-template-columns:1.3fr .7fr; gap:28px; padding:22px 32px 40px; }
  .stat { font-variant-numeric:tabular-nums; font-size:28px; margin:0; }
  .label { font-size:13px; letter-spacing:.08em; text-transform:uppercase; color:#5e584e; }
  .actions { display:flex; gap:12px; margin-top:16px; }
  button.act { border:1px solid var(--ink); padding:8px 12px; }
  button.act.stop { background:var(--ink); color:var(--paper); }
  section.panel { display:none; }
  section.panel.active { display:block; }
  ul { padding-left:18px; }
</style>
</head>
<body>
<header>
  <div>
    <p class="label">单账户研究账室</p>
    <h1>先看证据<br/>再谈下单</h1>
  </div>
  <div class="seal" id="seal">HOLD</div>
</header>
<nav>
  <button class="tab" data-panel="overview" aria-current="true">总览</button>
  <button class="tab" data-panel="research">研究</button>
  <button class="tab" data-panel="trades">交易</button>
  <button class="tab" data-panel="risk">风控</button>
  <button class="tab" data-panel="ledger">账本</button>
</nav>
<main>
  <div>
    <section id="overview" class="panel active">
      <p class="label">权益</p>
      <p class="stat" id="equity">暂无证据</p>
      <p id="session-note"></p>
      <div class="actions">
        <button class="act stop" id="pause">暂停新开仓</button>
        <button class="act" id="flatten">退出所有持仓</button>
      </div>
      <p id="action-note"></p>
    </section>
    <section id="research" class="panel"><h2>研究报告</h2><p id="research-body">暂无证据</p></section>
    <section id="trades" class="panel"><h2>成交来源</h2><ul id="trades-body"></ul></section>
    <section id="risk" class="panel"><h2>阻断</h2><ul id="risk-body"></ul></section>
    <section id="ledger" class="panel"><h2>仓位</h2><ul id="ledger-body"></ul></section>
  </div>
  <aside>
    <p class="label">模式</p>
    <p class="stat" id="mode"></p>
    <p class="label">状态</p>
    <p class="stat" id="system-state"></p>
  </aside>
</main>
<script>
const initial = /*OVERVIEW*/null;
function paint(data) {
  document.getElementById("equity").textContent = data.equity;
  document.getElementById("mode").textContent = data.mode;
  document.getElementById("system-state").textContent = data.system_state;
  document.getElementById("seal").textContent = data.live_authorization;
  document.getElementById("session-note").textContent = data.message_zh;
  document.getElementById("risk-body").innerHTML = (data.blockers.length ? data.blockers : ["暂无证据"]).map(item => `<li>${item}</li>`).join("");
  document.getElementById("ledger-body").innerHTML = (data.positions.length ? data.positions : [{symbol:"—", qty:"暂无证据"}]).map(item => `<li>${item.symbol} ${item.qty}</li>`).join("");
  const fills = [...data.backtest_fills.map(x => `回测 ${x.symbol||""}`), ...data.simulation_fills.map(x => `模拟 ${x.symbol||""}`), ...data.live_fills.map(x => `实盘 ${x.symbol||""}`)];
  document.getElementById("trades-body").innerHTML = (fills.length ? fills : ["暂无证据"]).map(item => `<li>${item}</li>`).join("");
}
paint(initial);
document.querySelectorAll("button.tab").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll("button.tab").forEach(item => item.removeAttribute("aria-current"));
  document.querySelectorAll("section.panel").forEach(item => item.classList.remove("active"));
  button.setAttribute("aria-current", "true");
  document.getElementById(button.dataset.panel).classList.add("active");
}));
async function post(path) {
  const response = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json","Idempotency-Key":crypto.randomUUID()}, body:JSON.stringify({expected_version:initial.version})});
  const payload = await response.json();
  document.getElementById("action-note").textContent = payload.message_zh || (payload.paused ? "已暂停新开仓，保护单仍保留" : "已记录退出请求");
  if (payload.version) initial.version = payload.version;
}
document.getElementById("pause").addEventListener("click", () => post("/v1/control/pause"));
document.getElementById("flatten").addEventListener("click", () => post("/v1/control/flatten-request"));
</script>
</body>
</html>
"""

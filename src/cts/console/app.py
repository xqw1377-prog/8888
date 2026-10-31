"""本地控制台。数据接口必须带身份；暂停新开仓和退出持仓是两个操作。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

from cts import LIVE_TRADING_AUTHORIZATION, __version__
from cts.clock import utc_now
from cts.ledger.models import AccountBucket
from cts.money import D


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
    metrics: dict[str, str] = field(default_factory=dict)
    books: dict[str, str] = field(default_factory=dict)
    report: dict[str, str] = field(default_factory=dict)
    settings: dict[str, str] = field(default_factory=dict)
    curve: list[str] = field(default_factory=list)
    risk_coefficient: str = "1"
    last_reconcile_at: str = ""
    approvals: list[str] = field(default_factory=list)

    @classmethod
    def from_ledger(cls, ledger, *, occupancy=None, mode: str = "SIMULATION") -> "ConsoleState":
        events = ledger.events()
        positions = []
        gross = D("0")
        for pos in ledger.positions():
            if pos.qty <= 0:
                continue
            positions.append({"symbol": pos.symbol, "qty": format(pos.qty, "f"), "freshness": "KNOWN"})
            gross += pos.qty * pos.avg_price * pos.multiplier
        fills = [
            {"origin": "SIMULATION", "symbol": event.symbol or "", "id": event.external_id}
            for event in events
            if event.event_type.value == "FILL"
        ]
        traces = {
            event.external_id: {
                "id": event.external_id,
                "kind": event.event_type.value,
                "symbol": event.symbol or "",
                "time": event.event_time.isoformat(),
                "note": event.metadata.get("note", ""),
                "price": event.metadata.get("price", ""),
                "payment": event.metadata.get("payment", ""),
                "origin": "SIMULATION",
            }
            for event in events
        }
        present = bool(events)
        equity = ledger.cash_equity() if present else None
        transfers = -ledger.balance(AccountBucket.EXTERNAL) if present else D("0")
        net = (equity - transfers) if equity is not None else None
        unrealized = "暂无证据" if positions else ("0" if present else "暂无证据")
        leverage = "暂无证据"
        drawdown = "暂无证据"
        if present and equity is not None and equity > 0 and not positions:
            leverage = format(gross / equity, "f")
            snap = ledger.equity_snapshot(as_of=events[-1].event_time, mark_prices={})
            if snap.high_water_mark > 0:
                drawdown = format((snap.high_water_mark - snap.unit_nav) / snap.high_water_mark, "f")
        elif present and equity is not None and equity > 0:
            leverage = format(gross / equity, "f")
        blockers: list[str] = []
        system_state = "RECONCILING"
        version = 1
        if occupancy is not None:
            blockers = [blocker.reason_code for blocker in occupancy.blockers]
            system_state = occupancy.system_state.value
            version = occupancy.account_state_version
        curve: list[str] = []
        running = D("0")
        for event in events:
            if event.event_type.value in ("TRANSFER_IN", "TRANSFER_OUT"):
                continue
            delta = sum(
                (line.debit - line.credit)
                for line in event.lines
                if line.bucket in (AccountBucket.WALLET, AccountBucket.ISOLATED_MARGIN)
            )
            running += delta
            curve.append(format(running, "f"))
        return cls(
            mode=mode,
            equity=None if equity is None else format(equity, "f"),
            positions=positions,
            blockers=blockers,
            version=version,
            traces=traces,
            fills=fills,
            system_state=system_state,
            metrics={
                "gross_leverage": leverage,
                "margin_used": format(ledger.balance(AccountBucket.ISOLATED_MARGIN), "f") if present else "暂无证据",
                "net_pnl": format(net, "f") if net is not None else "暂无证据",
                "drawdown": drawdown,
                "per_trade_risk": "0.20% PROPOSED",
                "portfolio_risk": "0.40% PROPOSED",
                "data_freshness": "行情 NOT_CHECKED" if present else "暂无证据",
            },
            books={
                "wallet": format(ledger.balance(AccountBucket.WALLET), "f") if present else "暂无证据",
                "unrealized": unrealized,
                "isolated_margin": format(ledger.balance(AccountBucket.ISOLATED_MARGIN), "f") if present else "暂无证据",
                "fees": format(ledger.balance(AccountBucket.FEE), "f") if present else "暂无证据",
                "funding": format(-ledger.balance(AccountBucket.FUNDING), "f") if present else "暂无证据",
                "transfers": format(transfers, "f") if present else "暂无证据",
                "realized": format(-ledger.balance(AccountBucket.REALIZED_PNL), "f") if present else "暂无证据",
                "gap": "未接交易所，差异暂无证据" if present else "暂无证据",
            },
            report={
                "conclusion": "证据不足" if present else "暂无证据",
                "cash_benchmark": "0" if present else "暂无证据",
                "btc_hold_benchmark": "暂无证据",
                "fees": format(ledger.balance(AccountBucket.FEE), "f") if present else "暂无证据",
                "funding": format(-ledger.balance(AccountBucket.FUNDING), "f") if present else "暂无证据",
            },
            settings=_settings(mode),
            curve=curve,
        )


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
                if path == "/v1/report/export":
                    self._send(200, _report_export(app.state))
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
                    note = "恢复申请已记录，未批准"
                    app.state.approvals.append(note)
                    payload = {
                        "ok": False,
                        "system_state": app.state.system_state,
                        "reason_code": "RECOVERY_APPROVAL_REQUIRED",
                        "message_zh": "恢复申请已记录，不能跳过审批直接进入运行",
                    }
                elif path == "/v1/control/tighten":
                    tightened = D(app.state.risk_coefficient) * D("0.5")
                    app.state.risk_coefficient = format(tightened, "f")
                    app.state.version += 1
                    payload = {
                        "ok": True,
                        "risk_coefficient": app.state.risk_coefficient,
                        "version": app.state.version,
                        "message_zh": f"本地风险系数收紧为 {app.state.risk_coefficient}，生产参数仍是 PROPOSED",
                    }
                elif path == "/v1/experiments":
                    experiment_id = str(body.get("experiment_id") or "E0")
                    app.state.experiments[experiment_id] = {
                        "experiment_id": experiment_id,
                        "conclusion": "证据不足",
                        "message_zh": "研究任务已冻结，不会触发交易",
                    }
                    app.state.report = {**app.state.report, "conclusion": "证据不足"}
                    payload = app.state.experiments[experiment_id]
                elif path == "/v1/reconciliation-runs":
                    run_id = f"rec-{len(app.state.reconciliations) + 1}"
                    app.state.reconciliations.append(run_id)
                    app.state.last_reconcile_at = utc_now().isoformat()
                    payload = {
                        "run_id": run_id,
                        "last_reconcile_at": app.state.last_reconcile_at,
                        "message_zh": "已发起只读对账，未修改交易所余额",
                    }
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


def _settings(mode: str) -> dict[str, str]:
    return {
        "connection": "模拟器" if mode == "SIMULATION" else "未连接",
        "credentials": "未配置",
        "alerts": "未接通",
        "version": __version__,
        "approval": "实盘 HOLD",
    }


def _filled(values: dict[str, str], defaults: dict[str, str]) -> dict[str, str]:
    merged = dict(defaults)
    merged.update({key: value for key, value in values.items() if value})
    return merged


def _report_export(state: ConsoleState) -> dict:
    report = _filled(
        state.report,
        {
            "conclusion": "暂无证据",
            "cash_benchmark": "暂无证据",
            "btc_hold_benchmark": "暂无证据",
            "fees": "暂无证据",
            "funding": "暂无证据",
        },
    )
    return {
        "live_authorization": LIVE_TRADING_AUTHORIZATION,
        "conclusion": report["conclusion"],
        "cash_benchmark": report["cash_benchmark"],
        "btc_hold_benchmark": report["btc_hold_benchmark"],
        "fees": report["fees"],
        "funding": report["funding"],
        "curve": state.curve,
        "message_zh": "导出的是本地研究记录，不是未来盈利保证",
    }


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
    metrics = _filled(
        state.metrics,
        {
            "gross_leverage": "暂无证据",
            "margin_used": "暂无证据",
            "net_pnl": "暂无证据",
            "drawdown": "暂无证据",
            "per_trade_risk": "0.20% PROPOSED",
            "portfolio_risk": "0.40% PROPOSED",
            "data_freshness": "暂无证据",
        },
    )
    books = _filled(
        state.books,
        {
            "wallet": "暂无证据",
            "unrealized": "暂无证据",
            "isolated_margin": "暂无证据",
            "fees": "暂无证据",
            "funding": "暂无证据",
            "transfers": "暂无证据",
            "realized": "暂无证据",
            "gap": "暂无证据",
        },
    )
    report = _filled(
        state.report,
        {
            "conclusion": "暂无证据",
            "cash_benchmark": "暂无证据",
            "btc_hold_benchmark": "暂无证据",
            "fees": "暂无证据",
            "funding": "暂无证据",
        },
    )
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
        "metrics": metrics,
        "books": books,
        "report": report,
        "settings": state.settings or _settings(state.mode),
        "curve": state.curve,
        "traces": list(state.traces.values()),
        "approvals": state.approvals,
        "risk_coefficient": state.risk_coefficient,
        "last_reconcile_at": state.last_reconcile_at or "暂无证据",
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
  h2 { font-weight:520; font-size:22px; margin:0 0 12px; }
  .seal { justify-self:end; border:2px solid var(--seal); color:var(--seal); width:92px; height:92px;
    border-radius:50%; display:grid; place-items:center; transform:rotate(8deg); font-size:18px; letter-spacing:2px; }
  nav { display:flex; gap:18px; padding:0 32px 12px; border-bottom:1px solid var(--ink); flex-wrap:wrap; }
  button.tab, button.act, button.link { font:inherit; background:transparent; border:0; padding:0; cursor:pointer; color:inherit; }
  button.tab[aria-current="true"] { border-bottom:2px solid var(--ink); }
  main { display:grid; grid-template-columns:1.3fr .7fr; gap:28px; padding:22px 32px 40px; }
  .stat { font-variant-numeric:tabular-nums; font-size:28px; margin:0; }
  .label { font-size:13px; letter-spacing:.08em; text-transform:uppercase; color:#5e584e; }
  .metrics { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:14px 18px; margin:18px 0; }
  .metrics p { margin:4px 0 0; font-variant-numeric:tabular-nums; }
  .actions { display:flex; gap:12px; margin-top:16px; flex-wrap:wrap; }
  button.act { border:1px solid var(--ink); padding:8px 12px; }
  button.act.stop { background:var(--ink); color:var(--paper); }
  section.panel { display:none; }
  section.panel.active { display:block; }
  ul { padding-left:18px; }
  .curve { margin:8px 0 16px; min-height:72px; }
  .note { min-height:1.4em; }
  aside .rule { border-top:1px solid var(--rule); margin:16px 0; }
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
  <button class="tab" data-panel="report">报告</button>
  <button class="tab" data-panel="trades">交易</button>
  <button class="tab" data-panel="risk">风控</button>
  <button class="tab" data-panel="ledger">账本</button>
  <button class="tab" data-panel="settings">设置</button>
</nav>
<main>
  <div>
    <section id="overview" class="panel active">
      <p class="label">权益</p>
      <p class="stat" id="equity">暂无证据</p>
      <div class="metrics" id="overview-metrics"></div>
      <p id="session-note"></p>
      <div class="actions">
        <button class="act stop" id="pause">暂停新开仓</button>
        <button class="act" id="flatten">退出所有持仓</button>
      </div>
    </section>
    <section id="research" class="panel">
      <h2>策略研究</h2>
      <p id="research-body">暂无证据</p>
      <div class="actions"><button class="act" id="create-research">创建研究版本</button></div>
    </section>
    <section id="report" class="panel">
      <h2>验证报告</h2>
      <p class="label">结论</p>
      <p id="report-conclusion">暂无证据</p>
      <div class="curve" id="report-curve"></div>
      <div class="metrics" id="report-metrics"></div>
      <div class="actions"><button class="act" id="export-report">导出结果</button></div>
      <p id="export-note"></p>
    </section>
    <section id="trades" class="panel">
      <h2>交易记录</h2>
      <ul id="trades-body"></ul>
      <p class="label">逐笔链路</p>
      <p id="trace-detail">暂无证据</p>
    </section>
    <section id="risk" class="panel">
      <h2>风控中心</h2>
      <div class="metrics" id="risk-limits"></div>
      <p class="label">阻断与恢复条件</p>
      <ul id="risk-body"></ul>
      <div class="actions">
        <button class="act" id="tighten">收紧额度</button>
        <button class="act" id="resume">提交恢复申请</button>
      </div>
    </section>
    <section id="ledger" class="panel">
      <h2>资金账本</h2>
      <div class="metrics" id="ledger-books"></div>
      <p class="label">仓位</p>
      <ul id="ledger-body"></ul>
      <div class="actions"><button class="act" id="reconcile">发起只读对账</button></div>
    </section>
    <section id="settings" class="panel">
      <h2>系统设置</h2>
      <div class="metrics" id="settings-body"></div>
      <p class="label">审批记录</p>
      <ul id="approvals"></ul>
    </section>
  </div>
  <aside>
    <p class="label">模式</p>
    <p class="stat" id="mode"></p>
    <p class="label">状态</p>
    <p class="stat" id="system-state"></p>
    <div class="rule"></div>
    <p class="label">最近对账</p>
    <p id="reconcile-at">暂无证据</p>
    <p class="note" id="action-note"></p>
  </aside>
</main>
<script>
const initial = /*OVERVIEW*/null;
function esc(value) {
  return String(value ?? "").replace(/[&<>"]/g, function(ch) {
    if (ch === "&") return "&amp;";
    if (ch === "<") return "&lt;";
    if (ch === ">") return "&gt;";
    return "&quot;";
  });
}
function cells(target, rows) {
  document.getElementById(target).innerHTML = rows.map(([label, value]) => `<div><p class="label">${esc(label)}</p><p>${esc(value)}</p></div>`).join("");
}
function spark(values) {
  if (!values || values.length < 2) return "暂无证据";
  const nums = values.map(Number);
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const width = 320;
  const height = 72;
  const points = nums.map((n, i) => {
    const x = (i / (nums.length - 1)) * width;
    const y = max === min ? height / 2 : height - ((n - min) / (max - min)) * (height - 4) - 2;
    return `${x},${y}`;
  }).join(" ");
  return `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="收益曲线"><polyline points="${points}" fill="none" stroke="#1b1914" stroke-width="1.6"/></svg>`;
}
function paint(data) {
  document.getElementById("equity").textContent = data.equity;
  document.getElementById("mode").textContent = data.mode;
  document.getElementById("system-state").textContent = data.system_state;
  document.getElementById("seal").textContent = data.live_authorization;
  document.getElementById("session-note").textContent = data.message_zh;
  document.getElementById("reconcile-at").textContent = data.last_reconcile_at;
  cells("overview-metrics", [
    ["总名义杠杆", data.metrics.gross_leverage],
    ["保证金占用", data.metrics.margin_used],
    ["净收益", data.metrics.net_pnl],
    ["回撤", data.metrics.drawdown],
    ["单笔额度", data.metrics.per_trade_risk],
    ["组合额度", data.metrics.portfolio_risk],
    ["数据新鲜度", data.metrics.data_freshness],
    ["当前阻断", data.blockers.length ? data.blockers.join("，") : "暂无证据"]
  ]);
  document.getElementById("research-body").textContent = data.report.conclusion;
  document.getElementById("report-conclusion").textContent = data.report.conclusion;
  document.getElementById("report-curve").innerHTML = spark(data.curve);
  cells("report-metrics", [
    ["费用", data.report.fees],
    ["资金费", data.report.funding],
    ["现金基准", data.report.cash_benchmark],
    ["持有基准", data.report.btc_hold_benchmark]
  ]);
  const origin = { BACKTEST: "回测", SIMULATION: "模拟", LIVE: "实盘" };
  const fills = [...data.backtest_fills, ...data.simulation_fills, ...data.live_fills];
  document.getElementById("trades-body").innerHTML = (fills.length ? fills : [{origin:"", symbol:"", id:""}]).map(item => {
    if (!item.origin) return "<li>暂无证据</li>";
    const label = `${origin[item.origin] || item.origin} ${item.symbol || ""}`;
    return item.id ? `<li><button class="link" data-trace="${esc(item.id)}">${esc(label)}</button></li>` : `<li>${esc(label)}</li>`;
  }).join("");
  document.querySelectorAll("[data-trace]").forEach(button => button.addEventListener("click", () => {
    const trace = (data.traces || []).find(item => item.id === button.dataset.trace);
    document.getElementById("trace-detail").textContent = trace
      ? `${trace.kind} ${trace.symbol} ${trace.time} 价格 ${trace.price || "暂无证据"} 资金费 ${trace.payment || "暂无证据"} ${trace.note || ""}`.trim()
      : "暂无证据";
  }));
  cells("risk-limits", [
    ["单笔额度", data.metrics.per_trade_risk],
    ["组合额度", data.metrics.portfolio_risk],
    ["本地系数", data.risk_coefficient],
    ["恢复条件", "需要证据、审批和版本一致"]
  ]);
  document.getElementById("risk-body").innerHTML = (data.blockers.length ? data.blockers : ["暂无证据"]).map(item => `<li>${esc(item)}</li>`).join("");
  cells("ledger-books", [
    ["钱包", data.books.wallet],
    ["未实现盈亏", data.books.unrealized],
    ["保证金", data.books.isolated_margin],
    ["手续费", data.books.fees],
    ["资金费", data.books.funding],
    ["出入金", data.books.transfers],
    ["已实现损益", data.books.realized],
    ["差异", data.books.gap]
  ]);
  document.getElementById("ledger-body").innerHTML = (data.positions.length ? data.positions : [{symbol:"—", qty:"暂无证据"}]).map(item => `<li>${esc(item.symbol)} ${esc(item.qty)}</li>`).join("");
  cells("settings-body", [
    ["连接健康", data.settings.connection],
    ["凭据", data.settings.credentials],
    ["告警", data.settings.alerts],
    ["版本", data.settings.version],
    ["实盘授权", data.live_authorization]
  ]);
  document.getElementById("approvals").innerHTML = (data.approvals.length ? data.approvals : ["暂无证据"]).map(item => `<li>${esc(item)}</li>`).join("");
  Object.assign(initial, data);
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
  if (response.ok || payload.reason_code === "RECOVERY_APPROVAL_REQUIRED") {
    const overview = await fetch("/v1/overview");
    if (overview.ok) paint(await overview.json());
  }
}
document.getElementById("pause").addEventListener("click", () => post("/v1/control/pause"));
document.getElementById("flatten").addEventListener("click", () => post("/v1/control/flatten-request"));
document.getElementById("tighten").addEventListener("click", () => post("/v1/control/tighten"));
document.getElementById("resume").addEventListener("click", () => post("/v1/control/resume-request"));
document.getElementById("reconcile").addEventListener("click", () => post("/v1/reconciliation-runs"));
document.getElementById("create-research").addEventListener("click", () => post("/v1/experiments"));
document.getElementById("export-report").addEventListener("click", async () => {
  const response = await fetch("/v1/report/export");
  const payload = await response.json();
  document.getElementById("export-note").textContent = payload.message_zh || payload.conclusion || "暂无证据";
});
</script>
</body>
</html>
"""

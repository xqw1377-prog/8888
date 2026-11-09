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
    marks: dict[str, str] = field(default_factory=dict)
    klines: dict[str, list[str]] = field(default_factory=dict)
    recent_orders: list[dict[str, str]] = field(default_factory=list)

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
        fills, traces = _trade_chains(events)
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
                "source": "本地账本" if present else "暂无证据",
                "available": "暂无证据",
            },
            report=_ledger_report(events, fees=ledger.balance(AccountBucket.FEE) if present else None, funding=-ledger.balance(AccountBucket.FUNDING) if present else None, drawdown=drawdown),
            settings=_settings(mode),
            curve=curve,
        )


class ConsoleApp:
    def __init__(self, token: str, state: Optional[ConsoleState] = None, *, watch_demo: bool = False) -> None:
        self.token = token
        self.state = state or ConsoleState()
        self.watch_demo = watch_demo
        self._idempotency: dict[str, tuple[str, dict]] = {}
        self.httpd: Optional[ThreadingHTTPServer] = None

    def refresh_demo_wallet(self) -> None:
        from cts.execution.live import read_demo_wallet

        try:
            apply_venue_wallet(self.state, read_demo_wallet(), as_of=utc_now().isoformat())
        except Exception:
            self.state.books["source"] = "币安模拟盘读取失败"
            self.state.books["wallet"] = "暂无证据"
            self.state.books["available"] = "暂无证据"
            self.state.books["unrealized"] = "暂无证据"
            self.state.metrics["data_freshness"] = "币安模拟盘读取失败"
            self.state.equity = None
        self.refresh_demo_marks()

    def refresh_demo_marks(self) -> None:
        from cts.execution.live import read_demo_marks

        try:
            self.state.marks = read_demo_marks()
        except Exception:
            self.state.marks = {}
        try:
            from cts.data.binance_feed import BinanceDataFeed, DataPullAuthorization
            from cts.data.store import DataStore
            from cts.clock import utc_now
            from datetime import timedelta

            feed = BinanceDataFeed(
                DataStore(),
                DataPullAuthorization(granted_by="console", expires_at=utc_now() + timedelta(minutes=30), max_requests=8),
            )
            klines: dict[str, list[str]] = {}
            for symbol in ("BTCUSDT", "ETHUSDT"):
                bars = feed.fetch_candles(symbol, "1m", 90)
                klines[symbol] = [format(bar.close, "f") for bar in bars[-90:]]
            self.state.klines = klines
        except Exception:
            self.state.klines = {}

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
                if not app.token:
                    return True  # 空口令模式：仅本机回环监听，直接放行
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
                    if app.watch_demo:
                        app.refresh_demo_wallet()
                    self._html(200, console_html(app.state))
                    return
                if not self._guard():
                    return
                if path == "/v1/overview":
                    if app.watch_demo:
                        app.refresh_demo_wallet()
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
                    if app.watch_demo:
                        app.refresh_demo_wallet()
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
        "venue": "币安 USDT 本位合约",
        "credentials": "未配置",
        "alerts": "未接通",
        "version": __version__,
        "approval": "实盘 HOLD",
        "venue_test": "UNVERIFIED",
        "alert_delivery": "UNVERIFIED",
        "production_protection": "UNVERIFIED",
        "micro_live": "UNVERIFIED",
    }


def _exit_reason(external_id: str) -> str:
    if ":STOP:" in external_id or external_id.endswith(":STOP"):
        return "STOP"
    if ":TREND" in external_id:
        return "TREND"
    return ""


def _trade_chains(events) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    chains: dict[str, dict[str, str]] = {}
    active: dict[str, str] = {}
    funding_sum: dict[str, Decimal] = {}

    def seed(event) -> dict[str, str]:
        meta = event.metadata
        reason = meta.get("exit_reason") or _exit_reason(event.external_id) or "暂无证据"
        return {
            "id": event.external_id,
            "symbol": event.symbol or "",
            "origin": "SIMULATION",
            "time": event.event_time.isoformat(),
            "signal": meta.get("signal_time") or "暂无证据",
            "risk": meta.get("risk_decision") or "暂无证据",
            "order": event.external_id,
            "fill": f"{meta.get('side', '')} {meta.get('qty', '')} @ {meta.get('price', '')}".strip(),
            "protection": meta.get("protection") or "暂无证据",
            "exit_reason": reason if meta.get("note") == "reduce" else "暂无证据",
            "direction": meta.get("direction") or meta.get("side") or "暂无证据",
            "funding": "暂无证据",
            "liq_distance": meta.get("liq_distance") or "暂无证据",
        }

    for event in events:
        kind = event.event_type.value
        symbol = event.symbol or ""
        if kind == "FILL" and event.metadata.get("note") == "open":
            chains[event.external_id] = seed(event)
            active[symbol] = event.external_id
            funding_sum[event.external_id] = D("0")
        elif kind == "FILL" and event.metadata.get("note") == "reduce":
            chain_id = active.get(symbol)
            reason = event.metadata.get("exit_reason") or _exit_reason(event.external_id) or "暂无证据"
            if chain_id in chains:
                chains[chain_id]["exit_reason"] = reason
                chains[chain_id]["fill"] += f"；退出 {event.metadata.get('price', '')} @ {event.event_time.isoformat()}"
            else:
                chains[event.external_id] = seed(event)
        elif kind == "FUNDING":
            chain_id = active.get(symbol)
            payment = event.metadata.get("payment")
            if chain_id in chains and payment:
                funding_sum[chain_id] = funding_sum.get(chain_id, D("0")) + D(payment)
                chains[chain_id]["funding"] = format(funding_sum[chain_id], "f")
    fills = [
        {"origin": chain["origin"], "symbol": chain["symbol"], "id": chain["id"]}
        for chain in chains.values()
    ]
    return fills, chains


def _ledger_report(events, *, fees, funding, drawdown: str) -> dict[str, str]:
    present = bool(events)
    notionals: dict[str, Decimal] = {}
    for event in events:
        if event.event_type.value != "FILL":
            continue
        qty = D(event.metadata.get("qty") or "0")
        price = D(event.metadata.get("price") or "0")
        symbol = event.symbol or "UNKNOWN"
        notionals[symbol] = notionals.get(symbol, D("0")) + abs(qty * price)
    total = sum(notionals.values(), D("0"))
    if total > 0:
        concentration = "；".join(
            f"{symbol} {format(value / total, 'f')}"
            for symbol, value in sorted(notionals.items(), key=lambda item: item[1], reverse=True)
        )
    else:
        concentration = "暂无证据"
    dates = {event.event_time.date() for event in events if event.event_type.value not in ("TRANSFER_IN", "TRANSFER_OUT")}
    return {
        "conclusion": "证据不足" if present else "暂无证据",
        "cash_benchmark": "0" if present else "暂无证据",
        "btc_hold_benchmark": "暂无证据",
        "fees": format(fees, "f") if fees is not None else "暂无证据",
        "funding": format(funding, "f") if funding is not None else "暂无证据",
        "confidence": "暂无证据",
        "rolling": "样本不足" if len(dates) < 2 else f"账本覆盖 {len(dates)} 个日期，完整滚动见研究报告",
        "concentration": concentration,
        "max_drawdown": drawdown,
        "data_grade": "SIGNAL_RESEARCH_ONLY" if present else "暂无证据",
    }


def apply_venue_wallet(state: ConsoleState, venue: dict[str, str], *, as_of: str) -> None:
    if state.books.get("source") == "本地账本":
        state.books["local_equity"] = state.equity or ""
    local = state.books.get("local_equity") or ""
    state.equity = venue["equity"]
    state.books["wallet"] = venue["wallet"]
    state.books["available"] = venue["available"]
    state.books["unrealized"] = venue["unrealized"]
    state.books["source"] = "币安模拟盘"
    state.metrics["margin_used"] = venue["initial_margin"]
    state.metrics["data_freshness"] = f"币安模拟盘 {as_of}"
    if local and local != "暂无证据":
        state.books["gap"] = format(D(venue["equity"]) - D(local), "f")
    else:
        state.books["gap"] = "本地账本无对照"


def apply_research_report(state: ConsoleState, report) -> None:
    if report.confidence_low is None or report.confidence_high is None:
        confidence = "暂无证据"
    else:
        confidence = f"{report.confidence_low} ~ {report.confidence_high}"
    state.report = {
        "conclusion": report.conclusion,
        "cash_benchmark": report.cash_benchmark,
        "btc_hold_benchmark": report.btc_hold_benchmark,
        "fees": report.fees,
        "funding": report.funding,
        "confidence": confidence,
        "rolling": report.rolling_performance,
        "concentration": report.concentration,
        "max_drawdown": report.max_drawdown,
        "data_grade": report.data_grade,
    }
    state.curve = list(report.curve_points)


def _report_defaults() -> dict[str, str]:
    return {
        "conclusion": "暂无证据",
        "cash_benchmark": "暂无证据",
        "btc_hold_benchmark": "暂无证据",
        "fees": "暂无证据",
        "funding": "暂无证据",
        "confidence": "暂无证据",
        "rolling": "暂无证据",
        "concentration": "暂无证据",
        "max_drawdown": "暂无证据",
        "data_grade": "暂无证据",
    }


def _filled(values: dict[str, str], defaults: dict[str, str]) -> dict[str, str]:
    merged = dict(defaults)
    merged.update({key: value for key, value in values.items() if value})
    return merged


def _report_export(state: ConsoleState) -> dict:
    report = _filled(state.report, _report_defaults())
    return {
        "live_authorization": LIVE_TRADING_AUTHORIZATION,
        "conclusion": report["conclusion"],
        "cash_benchmark": report["cash_benchmark"],
        "btc_hold_benchmark": report["btc_hold_benchmark"],
        "fees": report["fees"],
        "funding": report["funding"],
        "confidence": report["confidence"],
        "rolling": report["rolling"],
        "concentration": report["concentration"],
        "max_drawdown": report["max_drawdown"],
        "data_grade": report["data_grade"],
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
            "source": "暂无证据",
            "available": "暂无证据",
        },
    )
    report = _filled(state.report, _report_defaults())
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
        "marks": dict(state.marks), "klines": dict(state.klines), "recent_orders": list(state.recent_orders),
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
  :root {
    --bg:#f4f7fb; --card:#fff; --ink:#1c2434; --muted:#8b93a7; --line:#e6ebf4;
    --blue:#2f6bff; --blue-soft:#e8f0ff; --green:#12a36a; --red:#e23b4a;
    --orange:#f5a623; --violet:#7b61ff; --shadow:0 10px 30px rgba(28,39,68,.06);
  }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; background:var(--bg); color:var(--ink);
    font-family:"PingFang SC","Hiragino Sans GB","Noto Sans SC","Source Han Sans SC",sans-serif; }
  button, input { font:inherit; }
  .top { display:flex; align-items:center; gap:22px; padding:14px 28px; background:#fff; border-bottom:1px solid var(--line); }
  .brand { display:flex; align-items:center; gap:10px; color:inherit; text-decoration:none; font-weight:650; white-space:nowrap; }
  .brand b { color:var(--blue); font-weight:750; letter-spacing:.04em; }
  .logo { width:28px; height:28px; border-radius:8px; background:var(--blue); display:grid; place-items:center; }
  nav { display:flex; gap:18px; align-items:center; }
  button.tab { background:transparent; border:0; color:#5c6578; padding:8px 0; cursor:pointer; }
  button.tab[aria-current="true"] { color:var(--blue); box-shadow:inset 0 -2px 0 var(--blue); font-weight:650; }
  button.tab.ghost { color:var(--muted); }
  .search { margin-left:auto; background:#f6f8fc; border:1px solid var(--line); border-radius:999px; padding:7px 14px; min-width:220px; }
  .search input { border:0; background:transparent; width:100%; outline:none; color:inherit; }
  .pill { display:flex; align-items:center; gap:6px; background:var(--blue-soft); color:var(--blue); border-radius:999px; padding:6px 12px; white-space:nowrap; font-size:13px; }
  .dot { width:7px; height:7px; border-radius:50%; background:var(--blue); }
  #clock, .state-chip { color:var(--muted); font-size:13px; font-variant-numeric:tabular-nums; white-space:nowrap; }
  .avatar { width:32px; height:32px; border-radius:50%; background:var(--blue); color:#fff; display:grid; place-items:center; font-weight:700; }
  .session-note, .toast { margin:0; padding:0 28px; color:var(--muted); font-size:12px; }
  .toast:empty { display:none; }
  .toast { color:var(--ink); padding-top:8px; }
  main { padding:18px 28px 36px; }
  section.panel { display:none; }
  section.panel.active { display:block; }
  .hero { display:grid; grid-template-columns:1.4fr repeat(3, 1fr); gap:16px; }
  .card { background:var(--card); border-radius:16px; box-shadow:var(--shadow); padding:18px 20px; }
  .label { margin:0; color:var(--muted); font-size:13px; }
  .equity-line { display:flex; align-items:baseline; gap:8px; margin:8px 0 4px; }
  #equity { font-size:42px; line-height:1; font-weight:720; letter-spacing:-.04em; font-variant-numeric:tabular-nums; }
  .unit { color:var(--muted); font-size:14px; }
  #day-pnl, #equity-source { margin:0; font-size:13px; }
  .up { color:var(--green); } .down { color:var(--red); }
  .stat-card { display:flex; flex-direction:column; justify-content:center; gap:8px; }
  .stat-card strong { font-size:28px; font-weight:720; font-variant-numeric:tabular-nums; }
  .bar { height:6px; background:#edf1f7; border-radius:99px; overflow:hidden; }
  .bar span { display:block; height:100%; background:var(--blue); width:0; }
  .charts { display:grid; grid-template-columns:1.45fr .9fr; gap:16px; margin-top:16px; }
  .card-head { display:flex; justify-content:space-between; align-items:center; gap:12px; }
  h2 { margin:0; font-size:16px; font-weight:680; }
  .ranges { display:flex; gap:12px; color:var(--muted); font-size:13px; }
  .ranges button { border:0; background:transparent; color:inherit; cursor:pointer; padding:0; }
  .ranges button[aria-current="true"] { color:var(--blue); font-weight:680; }
  .legend { display:flex; gap:16px; color:var(--muted); font-size:12px; margin:8px 0; }
  .swatch { width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:4px; }
  .plot-empty, .empty-plot { min-height:220px; display:grid; place-items:center; color:var(--muted); background:
    linear-gradient(to top, transparent 31px, var(--line) 32px) 0 0 / 100% 32px;
    border-radius:12px; }
  .quotes { display:flex; justify-content:space-between; gap:12px; margin-bottom:8px; }
  .quotes b { display:block; font-size:13px; }
  .quotes strong { display:block; font-size:22px; font-variant-numeric:tabular-nums; }
  .quotes em { display:block; font-style:normal; color:var(--muted); font-size:12px; }
  .radar-head, .pos-head { display:flex; justify-content:space-between; align-items:baseline; gap:12px; margin:16px 0 10px; }
  .radar-head p, .pos-head p { margin:0; color:var(--muted); font-size:12px; }
  .radar { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  .signal { border:1px solid var(--line); border-radius:16px; padding:16px 18px; display:grid; grid-template-columns:1.1fr 1fr auto; gap:8px 18px; align-items:center; }
  .signal-top { display:flex; align-items:center; gap:8px; font-weight:680; }
  .tag { border-radius:99px; padding:2px 8px; font-size:12px; background:#f3f5f9; color:var(--muted); font-weight:550; }
  .px { font-size:28px; font-weight:720; font-variant-numeric:tabular-nums; }
  .signal ul { list-style:none; margin:0; padding:0; grid-column:2; grid-row:1 / span 2; }
  .signal .ghost-btn { grid-column:3; grid-row:1 / span 2; }
  .signal li { display:flex; justify-content:space-between; gap:12px; padding:4px 0; font-size:13px; }
  .ghost-btn, button.act, button.link { border:1px solid #d5ddeb; background:#fff; border-radius:999px; padding:8px 14px; cursor:pointer; color:var(--blue); }
  button.act.stop { background:var(--blue); color:#fff; border-color:var(--blue); }
  button.link { border:0; border-radius:0; padding:0; background:transparent; }
  .coin { width:28px; height:28px; border-radius:50%; display:inline-grid; place-items:center; color:#fff; font-size:13px; font-weight:750; }
  .coin.btc { background:var(--orange); } .coin.eth { background:var(--violet); }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:var(--muted); font-weight:550; padding:10px 8px; border-bottom:1px solid var(--line); }
  td { padding:12px 8px; border-bottom:1px solid #f2f5fa; font-variant-numeric:tabular-nums; }
  td.empty { text-align:center; color:var(--muted); padding:28px; }
  .side { border-radius:99px; padding:2px 8px; font-size:12px; }
  .side.long { background:#e9f8ef; color:var(--green); }
  .side.short { background:#fdecee; color:var(--red); }
  .metrics { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:12px; margin-top:12px; }
  .metrics div { background:#fff; border-radius:12px; padding:12px; box-shadow:var(--shadow); }
  .metrics p { margin:6px 0 0; font-variant-numeric:tabular-nums; }
  .page-card { margin-top:8px; }
  .actions { display:flex; gap:8px; flex-wrap:wrap; }
  @media (max-width: 1100px) {
    .top { flex-wrap:wrap; }
    .hero, .charts, .radar, .metrics { grid-template-columns:1fr; }
    #equity { font-size:34px; }
  }
</style>
</head>
<body>
<header class="top">
  <a class="brand" href="/">
    <span class="logo" aria-hidden="true"><svg viewBox="0 0 24 24" width="16" height="16"><path d="M4 16l5-5 3 3 8-9" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/></svg></span>
    <span>账室 <b>QUANT</b></span>
  </a>
  <nav>
    <button class="tab" data-panel="overview" aria-current="true">总览</button>
    <button class="tab" data-panel="market">行情</button>
    <button class="tab" data-panel="research">策略</button>
    <button class="tab" data-panel="trades">持仓</button>
    <button class="tab" data-panel="report">研究</button>
    <button class="tab ghost" data-panel="risk">风控</button>
    <button class="tab ghost" data-panel="ledger">账本</button>
    <button class="tab ghost" data-panel="settings">设置</button>
  </nav>
  <label class="search">搜索 <input id="symbol-search" placeholder="币种 BTC / ETH ..." /></label>
  <div class="pill"><span class="dot"></span><span id="mode">演示模式</span> · 实盘 <b id="seal">HOLD</b></div>
  <time id="clock"></time>
  <span id="system-state" class="state-chip"></span>
  <span class="avatar" aria-hidden="true">Z</span>
</header>
<p id="session-note" class="session-note"></p>
<p id="action-note" class="toast"></p>
<main>
  <section id="overview" class="panel active">
    <div class="hero">
      <article class="card">
        <p class="label">账户总权益</p>
        <p class="equity-line"><span id="equity">暂无证据</span><span class="unit">USDT</span></p>
        <p id="day-pnl">今日盈亏 暂无证据</p>
        <p id="equity-source">暂无证据</p>
      </article>
      <article class="card stat-card">
        <p class="label">当前风险敞口</p>
        <strong id="risk-exposure">暂无证据</strong>
        <div class="bar"><span id="risk-bar"></span></div>
      </article>
      <article class="card stat-card">
        <p class="label">运行中策略</p>
        <strong id="strategy-count">暂无证据</strong>
      </article>
      <article class="card stat-card">
        <p class="label">可用保证金</p>
        <strong id="available-margin">暂无证据</strong>
        <p class="label" id="margin-share">占总权益 暂无证据</p>
      </article>
    </div>
    <div class="charts">
      <article class="card">
        <div class="card-head">
          <h2>策略净值</h2>
          <div class="ranges" id="nav-ranges">
            <button type="button" data-nav="7">7D</button>
            <button type="button" data-nav="30" aria-current="true">30D</button>
            <button type="button" data-nav="90">90D</button>
            <button type="button" data-nav="0">全部</button>
          </div>
        </div>
        <div class="legend"><span><i class="swatch" style="background:var(--blue)"></i>策略净值</span><span><i class="swatch" style="background:#c5cedd"></i>BTC基准 暂无曲线</span></div>
        <div id="report-curve"></div>
      </article>
      <article class="card">
        <div class="card-head">
          <h2 id="market-heading">市场走势 · 模拟行情</h2>
          <div class="ranges">
            <button type="button" disabled>1H</button>
            <button type="button" disabled>4H</button>
            <button type="button" disabled>1W</button>
          </div>
        </div>
        <div id="market-board"></div>
      </article>
    </div>
    <div class="radar-head">
      <h2>机会雷达</h2>
      <p>只显示已有证据 · 模拟行情未接入时不生成信号</p>
    </div>
    <div class="radar" id="radar"></div>
    <div class="pos-head">
      <h2>持仓 <span id="position-count">(0)</span></h2>
      <p id="position-source">数据来源：本地记录</p>
      <div class="actions">
        <button class="act stop" id="pause">暂停新开仓</button>
        <button class="act" id="flatten">退出所有持仓</button>
      </div>
    </div>
    <article class="card">
      <table>
        <thead>
          <tr>
            <th>币种</th><th>方向</th><th>数量</th><th>开仓均价 (USDT)</th><th>标记价格 (USDT)</th>
            <th>浮动盈亏 (USDT)</th><th>收益率</th><th>风险预算</th><th>操作</th>
          </tr>
        </thead>
        <tbody id="positions-table"></tbody>
      </table>
      <div class="pos-head" style="margin-top:14px">
        <h2>近期委托 <span id="orders-count">(0)</span></h2>
        <p>含每笔绑定的合同杠杆</p>
      </div>
      <ul class="ledger" id="orders-body"></ul>
    </article>
  </section>
  <section id="market" class="panel">
    <article class="card page-card">
      <h2>行情</h2>
      <p id="market-page">未接行情</p>
    </article>
  </section>
  <section id="research" class="panel">
    <article class="card page-card">
      <h2>策略</h2>
      <p id="research-body">暂无证据</p>
      <div class="actions"><button class="act" id="create-research">创建研究版本</button></div>
    </article>
  </section>
  <section id="report" class="panel">
    <article class="card page-card">
      <h2>验证报告</h2>
      <p class="label">结论</p>
      <p id="report-conclusion">暂无证据</p>
      <div class="metrics" id="report-metrics"></div>
      <div class="actions"><button class="act" id="export-report">导出结果</button></div>
      <p id="export-note"></p>
    </article>
  </section>
  <section id="trades" class="panel">
    <article class="card page-card">
      <h2>持仓与交易记录</h2>
      <ul id="trades-body"></ul>
      <p class="label">逐笔链路</p>
      <p id="trace-detail">暂无证据</p>
    </article>
  </section>
  <section id="risk" class="panel">
    <article class="card page-card">
      <h2>风控中心</h2>
      <div class="metrics" id="overview-metrics"></div>
      <div class="metrics" id="risk-limits"></div>
      <p class="label">阻断与恢复条件</p>
      <ul id="risk-body"></ul>
      <div class="actions">
        <button class="act" id="tighten">收紧额度</button>
        <button class="act" id="resume">提交恢复申请</button>
      </div>
    </article>
  </section>
  <section id="ledger" class="panel">
    <article class="card page-card">
      <h2>资金账本</h2>
      <p class="label">最近对账</p>
      <p id="reconcile-at">暂无证据</p>
      <div class="metrics" id="ledger-books"></div>
      <p class="label">仓位</p>
      <ul id="ledger-body"></ul>
      <div class="actions"><button class="act" id="reconcile">发起只读对账</button></div>
    </article>
  </section>
  <section id="settings" class="panel">
    <article class="card page-card">
      <h2>系统设置</h2>
      <div class="metrics" id="settings-body"></div>
      <p class="label">审批记录</p>
      <ul id="approvals"></ul>
    </article>
  </section>
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
function grouped(value) {
  const text = String(value ?? "");
  if (!text || text === "暂无证据") return "暂无证据";
  if (!/^-?\d+(\.\d+)?$/.test(text)) return text;
  const negative = text.startsWith("-");
  const [whole, frac] = text.replace("-", "").split(".");
  const withComma = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return (negative ? "-" : "") + withComma + (frac ? "." + frac : "");
}
function numeric(value) {
  return /^-?\d+(\.\d+)?$/.test(String(value ?? ""));
}
function coin(symbol) {
  const kind = symbol === "ETHUSDT" ? "eth" : "btc";
  const mark = symbol === "ETHUSDT" ? "Ξ" : "B";
  return `<span class="coin ${kind}">${mark}</span>`;
}
function queryText() {
  const box = document.getElementById("symbol-search");
  return box ? box.value.trim().toUpperCase() : "";
}
function areaChart(values) {
  const picked = navWindow(values);
  if (picked.length < 2) return `<div class="plot-empty">暂无证据</div>`;
  const nums = picked.map(Number);
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const width = 640;
  const height = 240;
  const left = 54;
  const points = nums.map((n, i) => {
    const x = left + (i / (nums.length - 1)) * (width - left - 12);
    const y = max === min ? height / 2 : 16 + (1 - (n - min) / (max - min)) * (height - 36);
    return [x, y];
  });
  const line = points.map(pair => pair.join(",")).join(" ");
  const area = `${left},${height - 16} ${line} ${points[points.length - 1][0]},${height - 16}`;
  const last = points[points.length - 1];
  return `<svg viewBox="0 0 ${width} ${height}" width="100%" role="img" aria-label="策略净值">
    <text x="0" y="20" fill="#8b93a7" font-size="11">${esc(grouped(String(max)))}</text>
    <text x="0" y="${height - 12}" fill="#8b93a7" font-size="11">${esc(grouped(String(min)))}</text>
    <polygon points="${area}" fill="rgba(47,107,255,.16)"></polygon>
    <polyline points="${line}" fill="none" stroke="#2f6bff" stroke-width="2"></polyline>
    <circle cx="${last[0]}" cy="${last[1]}" r="4" fill="#2f6bff"></circle>
  </svg>`;
}
let navSpan = 30;
function navWindow(values) {
  const rows = values || [];
  if (!navSpan) return rows;
  return rows.slice(-navSpan);
}
function markOf(data, symbol) {
  const price = data.marks && data.marks[symbol];
  if (!price) return "暂无证据";
  const trimmed = String(price).replace(/(\.\d*?[1-9])0+$/, "$1").replace(/\.0+$/, "");
  return grouped(trimmed);
}
function board(data) {
  const names = ["BTCUSDT", "ETHUSDT"].filter(symbol => !queryText() || symbol.includes(queryText()));
  const connected = names.some(symbol => data.marks && data.marks[symbol]);
  const heading = document.getElementById("market-heading");
  if (heading) heading.textContent = connected ? "市场走势 · 标记价格" : "市场走势 · 模拟行情";
  document.getElementById("market-board").innerHTML = `
    <div class="quotes">${names.map(symbol => `<div>${coin(symbol)}<b>${esc(symbol)}</b><strong>${esc(markOf(data, symbol))}</strong><em>${connected ? "标记价格" : "未接行情"}</em></div>`).join("") || `<div><strong>暂无证据</strong></div>`}</div>
    ${names.map(symbol => {
      const series = (data.klines || {})[symbol];
      if (!series || series.length < 2) return `<div class="empty-plot">${esc(symbol)} 暂无 K 线</div>`;
      return `<div style="margin-top:10px"><div style="display:flex;justify-content:space-between;font-size:12px"><span>${esc(symbol)} · 最近 ${series.length} 根 1m 收盘</span><b>${esc(series[series.length-1])}</b></div>${areaChart(series)}</div>`;
    }).join("") || `<div class="empty-plot">未接行情，不绘制价格</div>`}`;
  document.getElementById("market-page").textContent = connected
    ? "BTCUSDT 与 ETHUSDT 的标记价格来自币安模拟盘公开行情。"
    : "未接行情。BTCUSDT 与 ETHUSDT 没有可显示的标记价格。";
  document.getElementById("radar").innerHTML = names.map(symbol => {
    const found = (data.positions || []).find(item => item.symbol === symbol);
    const qty = found ? found.qty : "暂无证据";
    return `<article class="signal">
      <div class="signal-top">${coin(symbol)}<b>${esc(symbol)}</b><span class="tag">暂无信号</span></div>
      <button class="ghost-btn" data-open="research">查看信号</button>
      <div class="px">${esc(markOf(data, symbol))}</div>
      <ul>
        <li><span>持仓数量</span><em>${esc(qty)}</em></li>
        <li><span>通道突破</span><em>暂无证据</em></li>
        <li><span>波动率</span><em>暂无证据</em></li>
        <li><span>成本预算</span><em>${esc(data.metrics.per_trade_risk)} / ${esc(data.metrics.portfolio_risk)}</em></li>
      </ul>
    </article>`;
  }).join("") || `<article class="signal"><div class="px">暂无证据</div></article>`;
  const rows = (data.positions || []).filter(item => !queryText() || String(item.symbol).toUpperCase().includes(queryText()));
  document.getElementById("position-count").textContent = `(${rows.length})`;
  document.getElementById("positions-table").innerHTML = rows.length ? rows.map(item => {
    const qty = String(item.qty ?? "");
    const side = !numeric(qty) ? "暂无证据" : (Number(qty) < 0 ? "空" : "多");
    const tone = side === "空" ? "short" : (side === "多" ? "long" : "");
    const lev = item.leverage ? `<b class="tag" style="margin-left:6px">${esc(item.leverage)}×</b>` : "";
    const entry = item.entry && Number(item.entry) > 0 ? esc(item.entry) : "暂无证据";
    const upnl = item.upnl !== undefined && item.upnl !== null && item.upnl !== "" ? esc(item.upnl) : "暂无证据";
    const roe = item.roe ? esc(item.roe) : "暂无证据";
    const budget = [
      item.margin ? `保证金 ${esc(item.margin)}` : "",
      item.notional ? `名义 ${esc(item.notional)}` : "",
      item.stop ? `止损 ${esc(item.stop)}` : "",
      item.liq ? `强平 ${esc(item.liq)}` : "",
    ].filter(Boolean).join("<br/>") || "暂无证据";
    return `<tr><td>${coin(item.symbol)} ${esc(item.symbol)}${lev}</td><td><span class="side ${tone}">${esc(side)}</span></td><td>${esc(qty)}</td><td>${entry}</td><td>${esc(markOf(data, item.symbol))}</td><td>${upnl}</td><td>${roe}</td><td>${budget}</td><td>—</td></tr>`;
  }).join("") : `<tr><td class="empty" colspan="9">暂无持仓</td></tr>`;
  const orders = data.recent_orders || [];
  document.getElementById("orders-count").textContent = `(${orders.length})`;
  document.getElementById("orders-body").innerHTML = orders.length ? orders.map(o =>
    `<li>${esc(o.time || "")} ${esc(o.symbol)} <span class="side ${o.side === "BUY" ? "long" : "short"}">${esc(o.side)}</span> ${esc(o.qty)} @ ${esc(o.price)} · ${esc(o.status)}${o.leverage ? ` · <b>${esc(o.leverage)}×</b>` : ""}</li>`
  ).join("") : `<li>暂无委托</li>`;
}
function paint(data) {
  board(data);
  document.getElementById("equity").textContent = grouped(data.equity);
  const source = data.books.source || "暂无证据";
  document.getElementById("equity-source").textContent = source;
  const unrealized = data.books.unrealized;
  const day = document.getElementById("day-pnl");
  day.textContent = numeric(unrealized) ? `未实现盈亏 ${grouped(unrealized)}` : "今日盈亏 暂无证据";
  day.className = numeric(unrealized) && Number(unrealized) < 0 ? "down" : (numeric(unrealized) && Number(unrealized) > 0 ? "up" : "");
  const exposure = data.metrics.gross_leverage;
  document.getElementById("risk-exposure").textContent = numeric(exposure) ? `${exposure} x` : "暂无证据";
  document.getElementById("risk-bar").style.width = numeric(exposure) ? `${Math.max(0, Math.min(100, Number(exposure) / 2 * 100))}%` : "0%";
  document.getElementById("strategy-count").textContent = "暂无证据";
  document.getElementById("available-margin").textContent = grouped(data.books.available);
  const share = document.getElementById("margin-share");
  share.textContent = numeric(data.books.available) && numeric(data.equity) && Number(data.equity) !== 0
    ? `占总权益 ${(Number(data.books.available) / Number(data.equity) * 100).toFixed(1)}%`
    : "占总权益 暂无证据";
  document.getElementById("position-source").textContent = `数据来源：${source}`;
  document.getElementById("mode").textContent = data.mode === "SIMULATION" ? "演示模式" : data.mode;
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
  document.getElementById("report-curve").innerHTML = areaChart(data.curve);
  cells("report-metrics", [
    ["费用", data.report.fees],
    ["资金费", data.report.funding],
    ["现金基准", data.report.cash_benchmark],
    ["持有基准", data.report.btc_hold_benchmark],
    ["置信区间", data.report.confidence],
    ["滚动表现", data.report.rolling],
    ["集中度", data.report.concentration],
    ["最大回撤", data.report.max_drawdown]
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
    const chain = trace ? [
      ["信号", trace.signal],
      ["风控决定", trace.risk],
      ["订单", trace.order],
      ["成交", trace.fill],
      ["保护单", trace.protection],
      ["退出原因", trace.exit_reason],
      ["方向", trace.direction],
      ["资金费", trace.funding],
      ["强平距离", trace.liq_distance]
    ].map(([label, value]) => `${label} ${value || "暂无证据"}`).join("；") : "暂无证据";
    document.getElementById("trace-detail").textContent = chain;
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
    ["可用", data.books.available],
    ["来源", data.books.source],
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
    ["交易场所", data.settings.venue],
    ["凭据", data.settings.credentials],
    ["告警", data.settings.alerts],
    ["版本", data.settings.version],
    ["实盘授权", data.live_authorization],
    ["交易所测试", data.settings.venue_test],
    ["告警投递", data.settings.alert_delivery],
    ["生产保护单", data.settings.production_protection],
    ["微实盘", data.settings.micro_live]
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
document.getElementById("symbol-search").addEventListener("input", () => paint(initial));
document.getElementById("nav-ranges").addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button) return;
  navSpan = Number(button.dataset.nav);
  document.querySelectorAll("#nav-ranges button").forEach(item => item.removeAttribute("aria-current"));
  button.setAttribute("aria-current", "true");
  paint(initial);
});
document.body.addEventListener("click", event => {
  const opener = event.target.closest("[data-open]");
  if (!opener) return;
  const tab = document.querySelector(`button.tab[data-panel="${opener.dataset.open}"]`);
  if (tab) tab.click();
});
function tick() {
  const now = new Date();
  const pad = value => String(value).padStart(2, "0");
  document.getElementById("clock").textContent = `${now.getUTCFullYear()}-${pad(now.getUTCMonth() + 1)}-${pad(now.getUTCDate())} ${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())} UTC`;
}
tick();
setInterval(tick, 30000);
</script>
</body>
</html>
"""

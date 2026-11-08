"""模拟盘常驻交易器：T1 信号 → 风控预留 → LiveSession 挂单 → 成交投影。

只服务 TESTNET 模拟盘。入场与平仓都走既定通道：反向等量成交按比例减仓（引擎平仓通道）。
停止方式：创建停止文件（默认 var/run/DEMO_TRADER_STOP），交易器会在撤掉未完成单后退出。
"""

from __future__ import annotations

import hmac
import hashlib
import json
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from decimal import ROUND_DOWN, ROUND_UP
from typing import Callable, Optional
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from cts.clock import utc_now
from cts.data.binance_feed import BinanceDataFeed, DataPullAuthorization
from cts.data.store import DataStore
from cts.errors import CtsError
from cts.execution.live import (
    LIVE_GRANT_STATEMENT,
    LiveGrant,
    LiveSession,
    VenueCredentials,
    _ssl_context,
    open_testnet_transport,
)
from cts.ledger.models import Side
from cts.risk.engine import RiskEngine
from cts.risk.models import ReserveRequest
from cts.strategy.t1 import (
    T1Params,
    evaluate_exit,
    evaluate_signal,
    initial_stop,
)

STOP_FILE_DEFAULT = "/Users/xinquanwang/8888/var/run/DEMO_TRADER_STOP"
OWNER_DIRECTIVE_FILE = "/Users/xinquanwang/8888/var/run/OWNER_TRADING_DIRECTIVE"


@dataclass(frozen=True)
class DemoTraderConfig:
    symbol: str
    interval: str
    params: T1Params
    target_notional: D
    grant_max_notional: D
    poll_seconds: float
    max_order_age_seconds: float
    aggression: D
    bars: int = 200
    stop_file: str = STOP_FILE_DEFAULT
    state_path: str = "/Users/xinquanwang/8888/var/run/demo_trader_state.json"
    log_path: str = "/Users/xinquanwang/8888/var/run/demo_trader.log"

    @classmethod
    def fast_1m(cls, symbol: str = "BTCUSDT") -> "DemoTraderConfig":
        return cls(
            symbol=symbol,
            interval="1m",
            params=T1Params(entry_lookback=20, exit_lookback=10, atr_period=14, warmup_bars=60, version="T1-DEMO-1M"),
            target_notional=D("150"),
            grant_max_notional=D("300"),
            poll_seconds=20.0,
            max_order_age_seconds=150.0,
            aggression=D("0.002"),
        )


def round_to_step(value: D, step: D, *, up: bool) -> D:
    units = value / step
    integral = units.to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)
    return integral * step


class DemoTrader:
    def __init__(
        self,
        config: DemoTraderConfig,
        *,
        risk: RiskEngine,
        credentials: VenueCredentials,
        feed: BinanceDataFeed,
        session: LiveSession,
        signer: Callable[[str, dict], tuple[int, str]],
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        renew: Optional[Callable[[], None]] = None,
    ) -> None:
        self.config = config
        self.risk = risk
        self.feed = feed
        self.session = session
        self.signer = signer
        self.clock = clock
        self.sleeper = sleeper
        self.credentials = credentials
        self.renew = renew
        self._heartbeat = 0.0
        self._failures = 0
        self.state: dict = {"position": None, "pending": None}
        self._load_state()

    def log(self, text: str) -> None:
        line = f"[{utc_now().isoformat()}] {text}"
        os.makedirs(os.path.dirname(self.config.log_path), exist_ok=True)
        with open(self.config.log_path, "a") as handle:
            handle.write(line + "\n")

    def _load_state(self) -> None:
        try:
            with open(self.config.state_path) as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict) or "position" not in loaded or "pending" not in loaded:
                raise ValueError("状态文件结构不完整")
            self.state = loaded
        except FileNotFoundError:
            pass
        except Exception:
            # 损坏的状态文件不得静默当成空仓，熔断等待人工核对
            self.state = {"position": None, "pending": None, "halted": "corrupt-state"}

    def _save_state(self) -> None:
        os.makedirs(os.path.dirname(self.config.state_path), exist_ok=True)
        with open(self.config.state_path, "w") as handle:
            json.dump(self.state, handle, ensure_ascii=False, indent=1)

    # ---- 行情与规则 ----

    def _bars(self):
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                return self.feed.fetch_candles(self.config.symbol, self.config.interval, self.config.bars)
            except Exception as exc:
                last_error = exc
                self.sleeper(1.5 if attempt < 2 else 0)
        raise last_error

    def _mark(self) -> Optional[D]:
        try:
            return self.feed.fetch_mark(self.config.symbol).mark_price
        except Exception:
            return None

    def _filters(self) -> tuple[D, D, D]:
        info = self.feed._get("/fapi/v1/exchangeInfo", {})
        sym = next(s for s in info["symbols"] if s["symbol"] == self.config.symbol)
        filters = {f["filterType"]: f for f in sym["filters"]}
        return (
            D(filters["PRICE_FILTER"]["tickSize"]),
            D(filters["LOT_SIZE"]["stepSize"]),
            D(filters["LOT_SIZE"]["minQty"]),
        )

    def _venue_position(self) -> D:
        last: Optional[Exception] = None
        for attempt in range(3):
            try:
                status, body = self.signer("/fapi/v2/positionRisk", {"symbol": self.config.symbol})
                if status != 200:
                    raise CtsError(f"positionRisk 非 200: {status}", reason_code="RECONCILIATION_FAILED")
                rows = json.loads(body)
                return D(rows[0]["positionAmt"]) if rows else D("0")
            except Exception as exc:
                last = exc
                self.sleeper(1.5 if attempt < 2 else 0)
        raise last

    # ---- 订单 ----

    def _place(self, *, side: Side, qty: D, ref_price: D, tag: str, stop: Optional[D], exit_order: bool = False) -> Optional[dict]:
        tick, step, min_qty = self._filters()
        qty = max(round_to_step(qty, step, up=False), min_qty)
        if side == Side.LONG:
            price = round_to_step(ref_price * (D("1") + self.config.aggression), tick, up=True)
        else:
            price = round_to_step(ref_price * (D("1") - self.config.aggression), tick, up=False)
        notional = qty * price
        if notional > self.config.grant_max_notional:
            self.log(f"{tag} 名义 {notional} 超限，跳过")
            return None
        if stop is not None:
            stop_price = D(stop)
        else:
            atr = self._last_atr()
            stop_price = initial_stop(side, price, atr if atr else price * D("0.01"), self.config.params)
        request = ReserveRequest(
            symbol=self.config.symbol,
            side=side,
            signal_key=f"demo-{tag}-{int(self.clock())}",
            entry_price=price,
            stop_price=stop_price,
            multiplier=D("1"),
            lot_size=step,
        )
        reservation = self.risk.reserve([request], now=utc_now(), exit_purpose=exit_order)[0]
        if reservation.qty < qty:
            qty = reservation.qty
            notional = qty * price
        coid = f"cts_demo_{tag}_{int(self.clock())}"
        try:
            placed = self.session.submit(
                symbol=self.config.symbol,
                side=side,
                qty=qty,
                price=price,
                client_order_id=coid,
                notional=notional,
                now=utc_now(),
                risk=self.risk,
                reservation_id=reservation.reservation_id,
            )
        except Exception as exc:
            if getattr(exc, "reason_code", "") != "UNKNOWN_ORDER":
                # 未发出或被本地拒绝：预留立即释放，避免占满组合预算
                self.risk.release_unbound_reservation(reservation.reservation_id)
                self.log(f"{tag} 提交失败已释放预留 {reservation.reservation_id[:8]}: {getattr(exc, 'reason_code', '')}")
            raise
        self.log(f"{tag} 挂单 {coid} {side.value} {qty} @ {price} (预留 {reservation.reservation_id[:8]})")
        return {
            "coid": coid,
            "side": side.value,
            "qty": format(qty, "f"),
            "reservation": reservation.reservation_id,
            "placed_at": self.clock(),
            "status": placed.get("status", "NEW"),
            "kind": tag,
        }

    def _last_atr(self) -> Optional[D]:
        try:
            return self._last_atr_from(self._bars())
        except Exception:
            return None

    def _last_atr_from(self, bars) -> Optional[D]:
        try:
            from cts.strategy.indicators import atr_sma as atr

            return atr(bars, self.config.params.atr_period)
        except Exception:
            return None

    def _poll_pending(self) -> None:
        pending = self.state.get("pending")
        if not pending:
            return
        try:
            result = self.session.query(
                symbol=self.config.symbol, client_order_id=pending["coid"], now=utc_now(), risk=self.risk
            )
        except CtsError as exc:
            self.log(f"查询 {pending['coid']} 失败 {getattr(exc, 'reason_code', '')} {exc}")
            return
        status = result.get("status")
        cumulative = D(result.get("executedQty") or "0")
        applied = D(pending.get("applied") or "0")
        delta = cumulative - applied
        if delta > 0:
            self.risk.project_live_cumulative(
                pending["reservation"], pending["coid"], cumulative, now=utc_now()
            )
            pending["applied"] = format(cumulative, "f")
            # 部分成交立即反映到本地持仓，不再等 FILLED
            prev = self.state.get("position")
            if pending["kind"] == "ENTRY":
                if prev and prev.get("side") == pending["side"]:
                    prev["qty"] = format(D(prev["qty"]) + delta, "f")
                else:
                    self.state["position"] = {
                        "side": pending["side"],
                        "qty": format(cumulative, "f"),
                        "entry": result.get("avgPrice") or result.get("price"),
                        "placed": pending["placed_at"],
                    }
            elif prev:
                remain = D(prev["qty"]) - delta
                self.state["position"] = None if remain <= 0 else {**prev, "qty": format(remain, "f")}
            self.log(f"{pending['coid']} 已成交增量 {delta}（累计 {cumulative}）")
        if status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            self.log(f"{pending['coid']} 终态 {status} executedQty={cumulative}")
            self.state["pending"] = None
        elif self.clock() - float(pending["placed_at"]) > self.config.max_order_age_seconds:
            try:
                self.session.cancel(
                    symbol=self.config.symbol, client_order_id=pending["coid"], now=utc_now(), risk=self.risk
                )
                self.log(f"{pending['coid']} 超时撤单")
            except CtsError as exc:
                self.log(f"撤单 {pending['coid']} 失败 {getattr(exc, 'reason_code', '')}")

    # ---- 主循环 ----

    def _resolve_unknown_orders(self) -> None:
        """UNKNOWN 订单按交易所证据核销：无此单置 REJECTED，终态落账，释放净敞口。"""
        from dataclasses import replace as _replace

        try:
            account_orders = self.session.orders.for_account(self.risk.account_id)
        except Exception:
            return
        for order in account_orders:
            stale_intent = (
                order.status == "INTENT"
                and getattr(order, "dispatch_started", 0)
                and (utc_now() - (order.updated_at if getattr(order, "updated_at", None) else utc_now())).total_seconds() > 300
            )
            if order.status != "UNKNOWN" and not stale_intent:
                continue
            try:
                status, body = self.signer(
                    "/fapi/v1/order",
                    {"symbol": order.symbol, "origClientOrderId": order.client_order_id},
                )
            except Exception:
                continue
            if status == 200:
                data = json.loads(body)
                self.session.orders.save(
                    _replace(order, status=data.get("status", "UNKNOWN"), executed_qty=D(str(data.get("executedQty") or "0"))),
                    utc_now(),
                )
                self.log(f"未决订单 {order.client_order_id} 按交易所置为 {data.get('status')}")
            else:
                try:
                    code = json.loads(body).get("code")
                except Exception:
                    code = None
                if code == -2013:
                    self.session.orders.save(_replace(order, status="REJECTED", executed_qty=D("0")), utc_now())
                    self.log(f"未决订单 {order.client_order_id} 交易所无此单，置 REJECTED")

    def _release_stale_reservations(self) -> None:
        """超过 15 分钟仍 PENDING 的 demo 预留视为残留，释放以免吃满组合预算。"""
        cutoff = utc_now() - timedelta(minutes=15)
        with self.risk.store.txn() as conn:
            rows = conn.execute(
                "SELECT reservation_id, signal_key, created_at FROM reservations "
                "WHERE account_id=? AND status='PENDING' AND signal_key LIKE 'demo-%'",
                (self.risk.account_id,),
            ).fetchall()
        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"])
                if created < cutoff:
                    self.risk.release_unbound_reservation(row["reservation_id"])
                    self.log(f"释放超时预留 {row['reservation_id'][:8]} ({row['signal_key']})")
            except Exception:
                pass

    def _signed_local(self) -> D:
        position = self.state.get("position")
        if not position:
            return D("0")
        qty = D(position["qty"])
        return qty if position["side"] == "LONG" else -qty

    def step(self) -> bool:
        self._poll_pending()
        self._resolve_unknown_orders()
        self._release_stale_reservations()
        halted = self.state.get("halted")
        # 对账前置：任何新单之前先核对交易所仓位；不一致即熔断（只撤不挂）
        venue = None
        reconcile_error = ""
        try:
            venue = self._venue_position()
        except Exception as exc:
            reconcile_error = f"{type(exc).__name__}: {exc}"
        if venue is None and not self.state.get("pending"):
            # 对账未知时不得下任何新单：入场会叠加未知敞口，反向平仓无仅减仓保证可能翻仓
            self.log(f"仓位核对失败，本轮不触发交易 {reconcile_error}")
            self._save_state()
            return True
        if venue is not None and not self.state.get("pending"):
            if halted == "position-mismatch":
                if venue == self._signed_local():
                    self.state["halted"] = None
                    self.log("仓位核对一致，解除熔断")
                    halted = None
                else:
                    self._save_state()
                    return True
            elif venue != self._signed_local():
                self.state["halted"] = "position-mismatch"
                self.log(f"仓位核对不一致 本地={self._signed_local()} 交易所={venue}，熔断：不再挂新单，等待人工核对")
                self._save_state()
                return True
        if halted == "corrupt-state":
            self.log("状态文件损坏，熔断中：人工核对仓位后删除状态文件再重启")
            self._save_state()
            return True
        if self.state.get("halted"):
            self._save_state()
            return True
        # 行情；失败且有持仓时退化为仅标记价止损
        try:
            bars = self._bars()
        except Exception as exc:
            self.log(f"K线获取失败 {type(exc).__name__}: {exc}")
            position = self.state.get("position")
            if position and not self.state.get("pending"):
                mark = self._mark()
                stop = D(position.get("stop") or "0")
                if mark is not None and stop > 0:
                    hit = mark <= stop if position["side"] == "LONG" else mark >= stop
                    if hit:
                        opposite = Side.SHORT if position["side"] == "LONG" else Side.LONG
                        self.state["pending"] = self._place(
                            side=opposite, qty=D(position["qty"]), ref_price=mark, tag="EXIT", stop=stop, exit_order=True
                        )
                        self.log(f"数据降级：标记价 {mark} 触发止损 {stop}")
            self._save_state()
            return True
        position = self.state.get("position")
        if position is not None and D(position.get("stop") or "0") <= 0:
            # 恢复状态缺止损：按当前 ATR 重算，避免 stop=0 立即/永不触发
            side = Side.LONG if position["side"] == "LONG" else Side.SHORT
            atr = self._last_atr_from(bars)
            entry = D(position.get("entry") or format(bars[-1].close, "f"))
            stop = initial_stop(side, entry, atr if atr and atr > 0 else entry * D("0.005"), self.config.params)
            position["stop"] = format(stop, "f")
            self.log(f"恢复状态缺止损，按 ATR={atr} 重算 stop={stop}")
        mark = self._mark()
        if position is None and not self.state.get("pending"):
            signal = evaluate_signal(bars, params=self.config.params, has_position=False, has_pending_entry=False)
            if signal is not None:
                last = bars[-1].close
                qty = self.config.target_notional / last
                self.state["pending"] = self._place(
                    side=signal.side, qty=qty, ref_price=last, tag="ENTRY", stop=None
                )
                self.log(f"入场信号 {signal.side.value} @band={signal.entry_band} atr={signal.atr}")
        elif position is not None and not self.state.get("pending"):
            open_position = _as_open_position(position, self.config.symbol)
            updated, exit_decision = evaluate_exit(open_position, bars, mark_price=mark, params=self.config.params)
            position["stop"] = format(updated.stop, "f")
            if exit_decision is not None:
                if venue is not None and abs(venue) < D(position["qty"]):
                    self.state["halted"] = "position-mismatch"
                    self.log(f"拟平 {position['qty']} 超过交易所实际 {abs(venue)}，熔断")
                    self._save_state()
                    return True
                last = bars[-1].close
                self.state["pending"] = self._place(
                    side=Side.SHORT if position["side"] == "LONG" else Side.LONG,
                    qty=D(position["qty"]),
                    ref_price=last,
                    tag="EXIT",
                    stop=D(position["stop"]),
                    exit_order=True,
                )
                self.log(f"退出信号 {exit_decision.reason} stop={exit_decision.stop}")
        self._save_state()
        return True

    def run(self) -> None:
        self.log(f"交易器启动 {self.config.symbol} {self.config.interval} {self.config.params.version}")
        while True:
            if os.path.exists(self.config.stop_file):
                pending = self.state.get("pending")
                if pending:
                    try:
                        self.session.cancel(
                            symbol=self.config.symbol, client_order_id=pending["coid"], now=utc_now(), risk=self.risk
                        )
                    except CtsError:
                        pass
                self.log("停止文件存在，交易器退出")
                return
            if self.renew is not None:
                try:
                    self.renew()
                except Exception as exc:
                    self.log(f"续期失败 {type(exc).__name__}: {exc}")
            try:
                self.step()
                self._failures = 0
            except Exception as exc:
                self._failures += 1
                self.log(f"迭代异常({self._failures} 连续) {type(exc).__name__}: {getattr(exc, 'reason_code', '')} {exc}")
                if self._failures == 10 and self.state.get("position"):
                    self.log("连续 10 次失败且持仓在场：无法管理风险，请人工检查（停止文件可退出）")
            if self.clock() - self._heartbeat >= 900:
                self._heartbeat = self.clock()
                pos = self.state.get("position")
                self.log(f"心跳 持仓={pos['side'] + ' ' + pos['qty'] if pos else '无'} 待定单={'有' if self.state.get('pending') else '无'}")
            self.sleeper(self.config.poll_seconds)


def _as_open_position(position: dict, symbol: str):
    from cts.strategy.t1 import OpenPosition

    return OpenPosition(
        symbol=symbol,
        side=Side.LONG if position["side"] == "LONG" else Side.SHORT,
        qty=D(position["qty"]),
        avg_price=D(position.get("entry") or "0"),
        signal_atr=D("1"),
        stop=D(position.get("stop") or "0"),
        extreme=D(position.get("entry") or "0"),
        entry_event_time=utc_now() - timedelta(minutes=1),
    )


def make_signer(credentials: VenueCredentials, proxy: str) -> Callable[[str, dict], tuple[int, str]]:
    opener = build_opener(
        ProxyHandler({"https": proxy, "http": proxy}),
        HTTPSHandler(context=_ssl_context()),
    )

    def signed_get(path: str, params: dict) -> tuple[int, str]:
        merged = dict(params)
        merged.setdefault("recvWindow", "5000")
        merged.setdefault("timestamp", str(int(utc_now().timestamp() * 1000)))
        query = urlencode(sorted(merged.items()))
        signature = hmac.new(
            credentials.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        request = Request(
            f"https://demo-fapi.binance.com{path}?{query}&signature={signature}",
            headers={"X-MBX-APIKEY": credentials.api_key},
        )
        try:
            with opener.open(request, timeout=15) as response:
                return response.status, response.read().decode()
        except Exception as exc:  # noqa: BLE001
            body = getattr(exc, "read", lambda: b"")()
            return getattr(exc, "code", 0) or 0, body.decode() if body else str(exc)

    return signed_get


def load_env(path: str = "/Users/xinquanwang/8888/.env") -> dict[str, str]:
    env: dict[str, str] = {}
    with open(path) as handle:
        for raw in handle:
            if "=" in raw and not raw.strip().startswith("#"):
                key, value = raw.strip().split("=", 1)
                env[key] = value
    return env


def build_default(config: DemoTraderConfig, *, proxy: str = "http://127.0.0.1:17890") -> DemoTrader:
    env = load_env()
    if env.get("CTS_BINANCE_NETWORK") != "TESTNET":
        raise CtsError("仅允许 TESTNET", reason_code="LIVE_VENUE_UNVERIFIED")
    os.environ["https_proxy"] = os.environ["HTTPS_PROXY"] = proxy
    os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = proxy
    credentials = VenueCredentials(env["CTS_BINANCE_API_KEY"], env["CTS_BINANCE_API_SECRET"])
    feed = BinanceDataFeed(
        DataStore(),
        DataPullAuthorization(granted_by="xinquanwang", expires_at=utc_now() + timedelta(hours=12), max_requests=10_000_000),
    )
    from cts.risk.store import RiskStore

    risk = RiskEngine(RiskStore("/Users/xinquanwang/8888/var/ledger/risk.db"), "local-ledger")
    grant = LiveGrant(
        granted_by="xinquanwang(会话现场指示)",
        statement=LIVE_GRANT_STATEMENT,
        network="TESTNET",
        account_id="local-ledger",
        max_notional=config.grant_max_notional,
        expires_at=utc_now() + timedelta(hours=12),
    )
    session = LiveSession(
        grant,
        credentials,
        open_testnet_transport(),
        now=utc_now(),
        path="/Users/xinquanwang/8888/var/ledger/orders.db",
        risk=risk,
    )
    holder: dict = {"session": session, "feed": feed}

    def renew() -> None:
        now = utc_now()
        try:
            with open(OWNER_DIRECTIVE_FILE) as handle:
                directive = json.load(handle)
            directive_valid = now < datetime.fromisoformat(directive["expires_at"])
        except Exception:
            directive_valid = False
        if not directive_valid:
            if holder["trader"].state.get("halted") != "directive-expired":
                holder["trader"].state["halted"] = "directive-expired"
                holder["trader"].log("所有者交易指示缺失或过期：停止续期并熔断新单")
            holder["trader"].session = holder["session"]
            return
        if now >= holder["session"].grant.expires_at - timedelta(minutes=30):
            holder["session"] = LiveSession(
                LiveGrant(
                    granted_by="xinquanwang(会话现场指示)",
                    statement=LIVE_GRANT_STATEMENT,
                    network="TESTNET",
                    account_id="local-ledger",
                    max_notional=config.grant_max_notional,
                    expires_at=now + timedelta(hours=12),
                ),
                credentials,
                open_testnet_transport(),
                now=now,
                path="/Users/xinquanwang/8888/var/ledger/orders.db",
                risk=risk,
            )
            feed.authorization = DataPullAuthorization(
                granted_by="xinquanwang", expires_at=now + timedelta(hours=12), max_requests=10_000_000
            )
        holder["trader"].session = holder["session"]

    trader = DemoTrader(
        config,
        risk=risk,
        credentials=credentials,
        feed=feed,
        session=session,
        signer=make_signer(credentials, proxy),
        renew=renew,
    )
    holder["trader"] = trader
    return trader


if __name__ == "__main__":
    trader = build_default(DemoTraderConfig.fast_1m())
    trader.run()

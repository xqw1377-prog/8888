"""实盘只走币安 USDT 本位合约。进程默认 HOLD，不向交易所发送请求。

S1-A 收口发单入口。S1-B 只补模拟盘订单生命周期：意图先落盘，超时变为 UNKNOWN 并占住额度，
过期后停止新开仓，但本会话已有订单仍可查询和撤单。授权材料不能代替这一步。
凭据不写入源码，也不出现在日志文本里。
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import ssl
import threading
from uuid import uuid4
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from cts import LIVE_TRADING_AUTHORIZATION
from cts.errors import ConflictError, CtsError, NewRiskBlockedError, UnitValidationError
from cts.execution.orders import LiveOrder, OrderStore, budget_notional, net_session_exposure
from cts.ledger.models import Side
from cts.money import D
from cts.risk.engine import RiskEngine


LIVE_VENUE = "BINANCE_USDT_M"
LIVE_GRANT_STATEMENT = "授权本进程在限额内向币安发送 BTC/ETH USDT 逐仓单向实盘单"
ALLOWED_SYMBOLS = ("BTCUSDT", "ETHUSDT")
TESTNET_BASE = "https://demo-fapi.binance.com"
MAINNET_BASE = "https://fapi.binance.com"

Transport = Callable[[str, str, str, dict[str, str]], dict | list]


class LiveExchangeAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def _refuse(self) -> None:
        self.calls += 1
        raise CtsError(
            "实盘下单未授权，未向交易所发送请求",
            reason_code="LIVE_TRADING_HOLD" if LIVE_TRADING_AUTHORIZATION == "HOLD" else "LIVE_VENUE_UNVERIFIED",
        )

    def submit(self, *args, **kwargs) -> None:
        self._refuse()

    def query(self, *args, **kwargs) -> None:
        self._refuse()

    def cancel(self, *args, **kwargs) -> None:
        self._refuse()

    def account_info(self, *args, **kwargs) -> None:
        self._refuse()


@dataclass(frozen=True)
class VenueCredentials:
    api_key: str
    api_secret: str

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_secret:
            raise CtsError("缺少交易所凭据，未发送请求", reason_code="CREDENTIALS_MISSING")

    def __repr__(self) -> str:
        return "VenueCredentials(redacted)"


@dataclass(frozen=True)
class LiveGrant:
    granted_by: str
    statement: str
    network: str
    account_id: str
    max_notional: Decimal
    expires_at: datetime
    symbols: tuple[str, ...] = ALLOWED_SYMBOLS
    venue: str = LIVE_VENUE

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_notional", D(self.max_notional))
        object.__setattr__(self, "symbols", tuple(self.symbols))


@dataclass(frozen=True)
class TransportResult:
    status: int
    body: dict | list | None = None
    location: str = ""


class PinnedTransport:
    """只访问工厂写死的模拟盘主机。调用方不能改主机，3xx 不跟随。"""

    def __init__(self, opener: Callable[[str, str, dict[str, str]], object]) -> None:
        self.base_url = TESTNET_BASE
        self._opener = opener

    def __call__(self, method: str, path: str, query: str, headers: dict[str, str]) -> dict:
        url = _bound_demo_url(path, query)
        result = self._opener(method, url, headers)
        if isinstance(result, TransportResult):
            if 300 <= result.status < 400:
                raise CtsError("拒绝离开绑定主机的重定向，未跟随新地址", reason_code="LIVE_VENUE_UNVERIFIED")
            if result.status != 200 or result.body is None:
                raise CtsError("模拟盘传输没有返回可读正文", reason_code="RECONCILIATION_FAILED")
            payload = result.body
        else:
            payload = result
        if isinstance(payload, list):
            return {"positions": payload}
        if not isinstance(payload, dict):
            raise CtsError("模拟盘传输返回了无法核对的正文", reason_code="RECONCILIATION_FAILED")
        return payload


def open_testnet_transport(opener: Callable[[str, str, dict[str, str]], object] | None = None) -> PinnedTransport:
    return PinnedTransport(opener or _urllib_opener())


class LiveSession:
    def __init__(
        self,
        grant: LiveGrant,
        credentials: VenueCredentials,
        transport: Transport,
        *,
        now: datetime,
        path: str | None = None,
        risk: RiskEngine | None = None,
    ) -> None:
        self._risk: RiskEngine | None = None
        identity = _admit_identity(grant)
        has_book = path is not None and str(path).strip() != "" and Path(path).is_file()
        if not has_book:
            if grant.expires_at <= now:
                raise CtsError("本进程实盘授权已过期，未发送请求", reason_code="LIVE_GRANT_EXPIRED")
            if type(transport) is not PinnedTransport or transport.base_url != TESTNET_BASE or grant.network != "TESTNET":
                raise CtsError("MAINNET 写单未放行，未发送请求", reason_code="LIVE_VENUE_UNVERIFIED")
            raise CtsError("找不到原订单库或路径不明确，拒绝开仓", reason_code="RECONCILIATION_FAILED")
        self.orders = OrderStore(str(path))
        self.orders.promote_inflight(grant.account_id)
        if self.orders.for_account(grant.account_id):
            self.grant = identity
        else:
            self.grant = _admit_grant(grant, now)
        if type(transport) is not PinnedTransport or transport.base_url != TESTNET_BASE:
            raise CtsError("传输必须由模拟盘工厂绑定到指定测试主机", reason_code="LIVE_VENUE_UNVERIFIED")
        if grant.network != "TESTNET":
            raise CtsError("MAINNET 写单未放行，未发送请求", reason_code="LIVE_VENUE_UNVERIFIED")
        self._credentials = credentials
        self._transport = transport
        self.base_url = TESTNET_BASE
        self.orders_sent = 0
        self._budget_lock = threading.Lock()
        self._hold_lock = threading.Lock()
        self._finished_holds: dict[str, str] = {}
        self._owner_id: str | None = None
        self._owner_lock_fd: int | None = None
        self._reserved_notional = sum(
            (budget_notional(order) for order in self.orders.for_account(self.grant.account_id)),
            start=D("0"),
        )
        if isinstance(risk, RiskEngine):
            self._bind_risk(risk, now)
        self._recover_unknown_orders(now)

    @property
    def reserved_notional(self) -> Decimal:
        self._recompute_reserved()
        return self._reserved_notional

    def get_order(self, client_order_id: str) -> LiveOrder:
        order = self.orders.get(client_order_id)
        if order is None or order.account_id != self.grant.account_id:
            raise CtsError("不是本会话订单", reason_code="RECONCILIATION_FAILED")
        return order

    def __repr__(self) -> str:
        return f"LiveSession(network={self.grant.network}, account_id={self.grant.account_id}, orders_sent={self.orders_sent})"

    def submit(
        self,
        *,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        client_order_id: str,
        notional: Decimal,
        now: datetime,
        risk: RiskEngine,
        reservation_id: str,
        contract_unit: Decimal = D("1"),
        reduce_only: bool = False,
    ) -> dict:
        _admit_grant(self.grant, now)
        self._assert_demo_write_closed()
        computed = _order_notional(
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            contract_unit=contract_unit,
            claimed=notional,
            client_order_id=client_order_id,
            allowed=self.grant.symbols,
        )
        if isinstance(risk, RiskEngine):
            self._bind_risk(risk, now)
        if reduce_only:
            raise CtsError("减仓写单不在本批放行，未发送请求", reason_code="LIVE_TRADING_HOLD")
        if computed > self.grant.max_notional:
            raise CtsError("名义价值超出本进程限额，未发送请求", reason_code="RISK_LIMIT")
        existing = self._require_known(client_order_id, allow_missing=True)
        if existing is not None:
            self._require_same_order(
                existing,
                symbol=symbol,
                side=side,
                qty=D(qty),
                price=D(price),
                notional=computed,
                reservation_id=reservation_id,
            )
            if existing.status == "UNKNOWN" or (existing.status == "INTENT" and existing.dispatch_started):
                raise CtsError("订单状态未知，先查询对账，未再次发送", reason_code="UNKNOWN_ORDER")
            if existing.status != "INTENT":
                return existing.body
        elif self._has_unknown():
            raise CtsError("存在未知订单，禁止新增风险", reason_code="UNKNOWN_ORDER")
        if existing is None:
            self._admit_open(risk, reservation_id, symbol, side, D(qty), computed)
            risk.bind_live_order(
                reservation_id,
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                qty=D(qty),
                notional=computed,
                price=D(price),
            )
            try:
                self._persist_intent(
                    client_order_id=client_order_id,
                    reservation_id=reservation_id,
                    symbol=symbol,
                    side=side,
                    qty=D(qty),
                    price=D(price),
                    notional=computed,
                    now=now,
                )
            except Exception:
                risk.release_live_order(client_order_id)
                raise
        sent = False
        duplicate = False
        try:
            self._require_isolated_one_way(symbol, now)
            params = [
                ("symbol", symbol),
                ("side", "BUY" if side == Side.LONG else "SELL"),
                ("type", "LIMIT"),
                ("timeInForce", "GTC"),
                ("quantity", format(D(qty), "f")),
                ("price", format(D(price), "f")),
                ("newClientOrderId", client_order_id),
                ("reduceOnly", "false"),
                ("recvWindow", "5000"),
                ("timestamp", _millis(now)),
            ]
            with risk.live_send_guard():
                risk.recheck_live_open(
                    reservation_id,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    qty=D(qty),
                    notional=computed,
                    price=D(price),
                )
                if not self.orders.claim_dispatch(client_order_id, self.grant.account_id, now):
                    duplicate = True
                    raise CtsError("订单正在发送或已发送，未再次发送", reason_code="UNKNOWN_ORDER")
                sent = True
                body = self._signed("POST", "/fapi/v1/order", params)
        except Exception:
            if duplicate:
                raise
            if sent:
                saved = self._save_order(self.get_order(client_order_id), status="UNKNOWN", dispatch_started=True, now=now)
                self._hold_risk_unknown(saved.reservation_id, now, saved.client_order_id)
                raise CtsError("下单响应丢失，订单状态未知，额度继续占用", reason_code="UNKNOWN_ORDER") from None
            self._save_order(self.get_order(client_order_id), status="REJECTED", dispatch_started=False, now=now)
            risk.release_live_order(client_order_id)
            self._recompute_reserved()
            raise
        self._apply_venue(client_order_id, body, now)
        self.orders_sent += 1
        return body

    def query(self, *, symbol: str, client_order_id: str, now: datetime, risk: RiskEngine | None = None) -> dict:
        if isinstance(risk, RiskEngine):
            self._bind_risk(risk, now)
        order = self._require_own(client_order_id, symbol)
        hold_id = None
        if order.status not in ("CANCELED", "EXPIRED", "REJECTED", "FILLED"):
            self._bound_risk(order)
            self._save_order(order, status="QUERYING", dispatch_started=True, now=now)
            hold_id = self._begin_request_hold(order, "QUERYING", now)
        try:
            body = self._signed(
                "GET",
                "/fapi/v1/order",
                [("symbol", symbol), ("origClientOrderId", client_order_id), ("recvWindow", "5000"), ("timestamp", _millis(now))],
            )
        except Exception:
            self._note_request_finished(hold_id, client_order_id)
            saved = self._save_order(self.get_order(client_order_id), status="UNKNOWN", dispatch_started=True, now=now)
            self._hold_risk_unknown(saved.reservation_id, now, saved.client_order_id)
            raise CtsError("查询响应丢失，订单状态未知，额度继续占用", reason_code="UNKNOWN_ORDER") from None
        try:
            saved = self._apply_venue(order.client_order_id, body, now)
        except Exception:
            self._note_request_finished(hold_id, client_order_id)
            raise
        if hold_id is not None:
            self._settle_request_hold(saved, hold_id, now)
        return body

    def cancel(self, *, symbol: str, client_order_id: str, now: datetime, risk: RiskEngine | None = None) -> dict:
        if isinstance(risk, RiskEngine):
            self._bind_risk(risk, now)
        order = self._require_own(client_order_id, symbol)
        if order.status == "FILLED":
            raise CtsError("订单已全部成交，未发送撤单", reason_code="RECONCILIATION_FAILED")
        if order.status in ("CANCELED", "EXPIRED", "REJECTED"):
            return order.body
        self._bound_risk(order)
        self._save_order(order, status="CANCELING", dispatch_started=True, now=now)
        hold_id = self._begin_request_hold(order, "CANCELING", now)
        try:
            body = self._signed(
                "DELETE",
                "/fapi/v1/order",
                [("symbol", symbol), ("origClientOrderId", client_order_id), ("recvWindow", "5000"), ("timestamp", _millis(now))],
            )
        except Exception:
            self._note_request_finished(hold_id, client_order_id)
            saved = self._save_order(self.get_order(client_order_id), status="UNKNOWN", dispatch_started=True, now=now)
            self._hold_risk_unknown(saved.reservation_id, now, saved.client_order_id)
            raise CtsError("撤单响应丢失，订单状态未知，额度继续占用", reason_code="UNKNOWN_ORDER") from None
        try:
            saved = self._apply_venue(order.client_order_id, body, now)
        except Exception:
            self._note_request_finished(hold_id, client_order_id)
            raise
        self._settle_request_hold(saved, hold_id, now)
        return body

    def _require_known(self, client_order_id: str, *, allow_missing: bool) -> LiveOrder | None:
        order = self.orders.get(client_order_id)
        if order is None:
            if allow_missing:
                return None
            raise CtsError("不是本会话订单，未发送请求", reason_code="RECONCILIATION_FAILED")
        if order.account_id != self.grant.account_id:
            raise CtsError("不是本会话订单，未发送请求", reason_code="RECONCILIATION_FAILED")
        return order

    def _require_own(self, client_order_id: str, symbol: str) -> LiveOrder:
        order = self._require_known(client_order_id, allow_missing=False)
        if order is None or order.symbol != symbol:
            raise CtsError("不是本会话订单，未发送请求", reason_code="RECONCILIATION_FAILED")
        return order

    def _require_same_order(
        self,
        order: LiveOrder,
        *,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        notional: Decimal,
        reservation_id: str,
    ) -> None:
        if (
            order.symbol != symbol
            or order.side != side.value
            or order.qty != qty
            or order.price != price
            or order.notional != notional
            or order.reservation_id != reservation_id
        ):
            raise ConflictError("同一订单号的数量、价格或方向不一致")

    def _has_unknown(self) -> bool:
        return any(row.status == "UNKNOWN" for row in self.orders.for_account(self.grant.account_id))

    def _persist_intent(
        self,
        *,
        client_order_id: str,
        reservation_id: str,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        notional: Decimal,
        now: datetime,
    ) -> None:
        draft = LiveOrder(
            client_order_id=client_order_id,
            account_id=self.grant.account_id,
            reservation_id=reservation_id,
            symbol=symbol,
            side=side.value,
            qty=qty,
            price=price,
            notional=notional,
            status="INTENT",
            executed_qty=D("0"),
            dispatch_started=False,
            body={},
        )
        if self._has_unknown():
            raise CtsError("存在未知订单，禁止新增风险", reason_code="UNKNOWN_ORDER")
        self.orders.insert_intent_within_cap(draft, self.grant.max_notional, now)
        self._recompute_reserved()

    def _save_order(
        self,
        order: LiveOrder,
        *,
        status: str,
        dispatch_started: bool,
        now: datetime,
        executed_qty: Decimal | None = None,
        body: dict | None = None,
    ) -> LiveOrder:
        updated = LiveOrder(
            client_order_id=order.client_order_id,
            account_id=order.account_id,
            reservation_id=order.reservation_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=order.price,
            notional=order.notional,
            status=status,
            executed_qty=order.executed_qty if executed_qty is None else executed_qty,
            dispatch_started=dispatch_started,
            body=order.body if body is None else body,
        )
        self.orders.save(updated, now)
        self._recompute_reserved()
        return updated

    def _recompute_reserved(self) -> None:
        with self._budget_lock:
            self._reserved_notional = sum(
                (budget_notional(order) for order in self.orders.for_account(self.grant.account_id)),
                start=D("0"),
            )

    def _apply_venue(self, client_order_id: str, body: dict, now: datetime) -> LiveOrder:
        order = self.get_order(client_order_id)
        status = body.get("status")
        definitive = {"NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "REJECTED"}
        try:
            executed = D(str(body["executedQty"])) if "executedQty" in body else None
        except (ArithmeticError, TypeError, ValueError):
            executed = None
        if status not in definitive or executed is None or not self._identity_ok(order, body):
            self._save_order(order, status="UNKNOWN", dispatch_started=True, now=now)
            self._hold_risk_unknown(order.reservation_id, now, order.client_order_id)
            raise CtsError("交易所订单状态不能确认", reason_code="UNKNOWN_ORDER")
        try:
            if not executed.is_finite() or executed < 0 or executed > order.qty:
                raise ValueError("executedQty")
            if status == "NEW" and executed != 0:
                raise ValueError("new")
            if status == "FILLED" and executed != order.qty:
                raise ValueError("filled")
            if status == "PARTIALLY_FILLED" and (executed == 0 or executed == order.qty):
                raise ValueError("partial")
        except (ValueError, ArithmeticError, TypeError):
            self._save_order(order, status="UNKNOWN", dispatch_started=True, now=now)
            self._hold_risk_unknown(order.reservation_id, now, order.client_order_id)
            raise CtsError("成交数量与原订单不一致，未释放额度", reason_code="RECONCILIATION_FAILED") from None
        if executed < order.executed_qty:
            raise CtsError("成交数量不能回退", reason_code="RECONCILIATION_FAILED")
        if executed > order.executed_qty or status in ("CANCELED", "EXPIRED", "REJECTED", "FILLED", "PARTIALLY_FILLED"):
            self._bound_risk(order)
        if status == "REJECTED" and executed != 0:
            self._project_execution(order, executed, "FILLED" if executed == order.qty else "PARTIALLY_FILLED", now)
            self._save_order(
                order,
                status="UNKNOWN",
                dispatch_started=True,
                now=now,
                executed_qty=executed,
                body=body,
            )
            self._hold_risk_unknown(order.reservation_id, now, order.client_order_id)
            raise CtsError("拒绝状态却带有成交数量，未释放额度", reason_code="RECONCILIATION_FAILED")
        self._project_execution(order, executed, str(status), now)
        return self._save_order(
            order,
            status=str(status),
            dispatch_started=True,
            now=now,
            executed_qty=executed,
            body=body,
        )

    def _identity_ok(self, order: LiveOrder, body: dict) -> bool:
        expected_side = "BUY" if order.side == Side.LONG.value else "SELL"
        if body.get("clientOrderId") != order.client_order_id or body.get("symbol") != order.symbol:
            return False
        if body.get("side") != expected_side or "origQty" not in body:
            return False
        try:
            return D(str(body["origQty"])) == order.qty
        except (ArithmeticError, TypeError, ValueError):
            return False

    def _project_execution(self, order: LiveOrder, executed: Decimal, status: str, now: datetime) -> None:
        risk = self._risk
        if not isinstance(risk, RiskEngine) or risk.account_id != order.account_id:
            if executed > order.executed_qty or status in ("CANCELED", "EXPIRED", "REJECTED", "FILLED", "PARTIALLY_FILLED"):
                raise CtsError("风险引擎未绑定，未写入成交或释放", reason_code="RECONCILIATION_FAILED")
            return
        try:
            risk.project_live_cumulative(order.reservation_id, order.client_order_id, executed, now=now)
        except UnitValidationError as exc:
            raise CtsError("成交数量不能回退", reason_code="RECONCILIATION_FAILED") from exc
        if status in ("CANCELED", "EXPIRED", "REJECTED", "FILLED"):
            risk.release_live_order(order.client_order_id)
            risk.release_unbound_reservation(order.reservation_id)

    def _bind_risk(self, risk: RiskEngine, now: datetime) -> None:
        if risk.account_id != self.grant.account_id:
            raise CtsError("风险引擎不属于该账户", reason_code="RECONCILIATION_FAILED")
        self._risk = risk
        self.orders.set_risk_path(self.grant.account_id, risk.store.path)
        self._ensure_hold_owner(now)

    def _bound_risk(self, order: LiveOrder) -> RiskEngine:
        risk = self._risk
        if not isinstance(risk, RiskEngine) or risk.account_id != order.account_id:
            raise CtsError("风险引擎未绑定，未写入成交或释放", reason_code="RECONCILIATION_FAILED")
        return risk

    def _recover_unknown_orders(self, now: datetime) -> None:
        unknown = [order for order in self.orders.for_account(self.grant.account_id) if order.status == "UNKNOWN"]
        if not unknown:
            return
        if self._risk is None:
            path = self.orders.get_risk_path(self.grant.account_id)
            if not path or not Path(path).is_file():
                return
            from cts.risk.store import RiskStore

            self._risk = RiskEngine(RiskStore(path), self.grant.account_id)
        self._ensure_hold_owner(now)
        for order in unknown:
            self._risk.hold_live_unknown(
                order.reservation_id,
                client_order_id=order.client_order_id,
                owner_id=self._owner_id or "",
                now=now,
            )

    def _ensure_hold_owner(self, now: datetime) -> str:
        if self._owner_id is not None:
            return self._owner_id
        risk = self._risk
        if not isinstance(risk, RiskEngine):
            raise CtsError("风险引擎未绑定，未写入成交或释放", reason_code="RECONCILIATION_FAILED")
        owner_id = str(uuid4())
        fd = os.open(risk.owner_lock_path(owner_id), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise CtsError("风险阻断所有者未能占锁", reason_code="RECONCILIATION_FAILED") from None
        self._owner_lock_fd = fd
        self._owner_id = owner_id
        identity = os.fstat(fd)
        risk.register_hold_owner(owner_id, lock_dev=identity.st_dev, lock_ino=identity.st_ino, now=now)
        return owner_id

    def _begin_request_hold(self, order: LiveOrder, reason: str, now: datetime) -> str:
        risk = self._bound_risk(order)
        owner_id = self._ensure_hold_owner(now)
        hold_id = f"{order.client_order_id}:{reason}:{uuid4()}"
        try:
            risk.hold_live_request(
                order.reservation_id,
                order.client_order_id,
                hold_id=hold_id,
                reason=reason,
                owner_id=owner_id,
                now=now,
            )
        except Exception:
            self._note_request_finished(hold_id, order.client_order_id)
            raise
        return hold_id

    def _note_request_finished(self, hold_id: str | None, client_order_id: str | None) -> None:
        if hold_id is None or client_order_id is None:
            return
        with self._hold_lock:
            self._finished_holds[hold_id] = client_order_id

    def _settle_request_hold(self, order: LiveOrder, hold_id: str, now: datetime) -> None:
        risk = self._risk
        owner_id = self._owner_id
        if not isinstance(risk, RiskEngine) or risk.account_id != order.account_id or not owner_id:
            return
        others_blocked = self._other_request_blocked(order)
        with self._hold_lock:
            release_ids = {
                finished_id
                for finished_id, finished_order in self._finished_holds.items()
                if finished_order == order.client_order_id
            }
            release_ids.add(hold_id)
        remaining = risk.settle_live_request(
            hold_id=hold_id,
            owner_id=owner_id,
            client_order_id=order.client_order_id,
            reservation_id=order.reservation_id,
            now=now,
            release_hold_ids=release_ids,
        )
        with self._hold_lock:
            for finished_id in release_ids:
                self._finished_holds.pop(finished_id, None)
        if remaining or others_blocked:
            return
        risk.clear_live_unknown(order.reservation_id)

    def _other_request_blocked(self, order: LiveOrder) -> bool:
        return any(
            row.client_order_id != order.client_order_id
            and row.reservation_id == order.reservation_id
            and row.status in ("UNKNOWN", "QUERYING", "CANCELING")
            for row in self.orders.for_account(order.account_id)
        )

    def _hold_risk_unknown(self, reservation_id: str, now: datetime, client_order_id: str) -> None:
        risk = self._risk
        if isinstance(risk, RiskEngine):
            risk.hold_live_unknown(
                reservation_id,
                client_order_id=client_order_id,
                owner_id=self._ensure_hold_owner(now),
                now=now,
            )

    def _assert_demo_write_closed(self) -> None:
        if self.grant.network != "TESTNET" or self.base_url != TESTNET_BASE or self._transport.base_url != TESTNET_BASE:
            raise CtsError("MAINNET 写单未放行，未发送请求", reason_code="LIVE_VENUE_UNVERIFIED")

    def _admit_open(
        self,
        risk: RiskEngine,
        reservation_id: str,
        symbol: str,
        side: Side,
        qty: Decimal,
        notional: Decimal,
    ) -> None:
        if not isinstance(risk, RiskEngine):
            raise NewRiskBlockedError("开仓必须经过风险引擎", reason_code="NEW_RISK_BLOCKED")
        if risk.account_id != self.grant.account_id or not reservation_id:
            raise NewRiskBlockedError("没有有效风险批准", reason_code="NEW_RISK_BLOCKED")
        risk.admit_live_open(reservation_id, symbol=symbol, side=side, qty=D(qty), notional=notional)

    def _hold(self, notional: Decimal) -> None:
        with self._budget_lock:
            # 限额按净敞口判定：已平仓的成交净额归零，不永久占用常驻进程预算
            exposure = net_session_exposure(self.orders.for_account(self.grant.account_id))
            if exposure + notional > self.grant.max_notional:
                raise CtsError("累计名义超出本进程限额，未发送请求", reason_code="RISK_LIMIT")
            self._reserved_notional += notional

    def _release(self, notional: Decimal) -> None:
        with self._budget_lock:
            self._reserved_notional -= notional
            if self._reserved_notional < 0:
                self._reserved_notional = D("0")

    def _require_isolated_one_way(self, symbol: str, now: datetime) -> None:
        dual = self._signed("GET", "/fapi/v1/positionSide/dual", [("recvWindow", "5000"), ("timestamp", _millis(now))])
        _require_explicit_false(dual, "dualSidePosition", "账户持仓模式字段缺失或不是单向，未发送下单请求")
        assets = self._signed("GET", "/fapi/v1/multiAssetsMargin", [("recvWindow", "5000"), ("timestamp", _millis(now))])
        _require_explicit_false(assets, "multiAssetsMargin", "联合保证金模式字段缺失或已开启，未发送下单请求")
        risk = self._signed(
            "GET",
            "/fapi/v2/positionRisk",
            [("symbol", symbol), ("recvWindow", "5000"), ("timestamp", _millis(now))],
        )
        rows = risk.get("positions", [])
        match = next((row for row in rows if row.get("symbol") == symbol), None)
        margin = None if match is None else match.get("marginType")
        if not isinstance(margin, str) or margin.casefold() != "isolated":
            raise CtsError("账户逐仓字段缺失或不是逐仓，未发送下单请求", reason_code="ACCOUNT_MODE_DRIFT")

    def _signed(self, method: str, path: str, params: list[tuple[str, str]]) -> dict:
        query = _sign(params, self._credentials.api_secret)
        if self._credentials.api_secret in query:
            raise CtsError("签名材料异常，未发送请求", reason_code="CREDENTIALS_MISSING")
        headers = {"X-MBX-APIKEY": self._credentials.api_key}
        payload = self._transport(method, path, query, headers)
        if isinstance(payload, list):
            return {"positions": payload}
        return payload


def load_local_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            os.environ.setdefault(key, value)


def parse_usdt_wallet(body: dict) -> dict[str, str]:
    usdt = next((row for row in body.get("assets") or [] if row.get("asset") == "USDT"), None)
    if usdt is None:
        raise CtsError("模拟盘账户没有 USDT 钱包", reason_code="RECONCILIATION_FAILED")
    return {
        "wallet": str(usdt["walletBalance"]),
        "available": str(usdt["availableBalance"]),
        "unrealized": str(usdt["unrealizedProfit"]),
        "equity": str(usdt.get("marginBalance") or body.get("totalMarginBalance")),
        "initial_margin": str(usdt.get("initialMargin") or "0"),
    }


def read_demo_marks() -> dict[str, str]:
    load_local_env()
    if os.environ.get("CTS_BINANCE_NETWORK") != "TESTNET":
        raise CtsError("只读取币安模拟盘行情，未访问实盘", reason_code="LIVE_TRADING_HOLD")
    transport = open_testnet_transport()
    marks: dict[str, str] = {}
    for symbol in ("BTCUSDT", "ETHUSDT"):
        body = transport("GET", "/fapi/v1/premiumIndex", f"symbol={symbol}", {})
        price = body.get("markPrice")
        if not price:
            raise CtsError(f"{symbol} 没有标记价格", reason_code="RECONCILIATION_FAILED")
        marks[symbol] = str(price)
    return marks


def parse_demo_positions(body: dict) -> list[dict[str, str]]:
    """把持仓风险读数收成看板用的行。数量为零的合约不占一行。"""
    rows = []
    for row in body.get("positions") or []:
        amount = D(str(row.get("positionAmt") or "0"))
        if amount == 0:
            continue
        qty = abs(amount)
        mark = D(str(row.get("markPrice") or "0"))
        entry = D(str(row.get("entryPrice") or "0"))
        upnl = D(str(row.get("unRealizedProfit") or row.get("unrealizedProfit") or "0"))
        margin = D(str(row.get("isolatedWallet") or row.get("initialMargin") or "0"))
        rows.append(
            {
                "symbol": str(row.get("symbol") or ""),
                "qty": format(qty, "f"),
                "side": "多" if amount > 0 else "空",
                "entry": format(entry, "f"),
                "leverage": str(row.get("leverage") or ""),
                "upnl": format(upnl, "f"),
                "roe": format(upnl / margin, "f") if margin > 0 else "",
                "margin": format(margin, "f"),
                "notional": format(qty * mark, "f"),
                "liq": str(row.get("liquidationPrice") or ""),
                "freshness": "KNOWN",
            }
        )
    return rows


def read_demo_positions(now: datetime | None = None) -> list[dict[str, str]]:
    load_local_env()
    if os.environ.get("CTS_BINANCE_NETWORK") != "TESTNET":
        raise CtsError("只读取币安模拟盘持仓，未访问实盘", reason_code="LIVE_TRADING_HOLD")
    creds = credentials_from_env()
    moment = now or datetime.now(timezone.utc)
    params = [("recvWindow", "5000"), ("timestamp", _millis(moment))]
    body = open_testnet_transport()(
        "GET",
        "/fapi/v2/positionRisk",
        _sign(params, creds.api_secret),
        {"X-MBX-APIKEY": creds.api_key},
    )
    return [row for row in parse_demo_positions(body) if row["symbol"] in {"BTCUSDT", "ETHUSDT"}]


def read_demo_wallet(now: datetime | None = None) -> dict[str, str]:
    load_local_env()
    if os.environ.get("CTS_BINANCE_NETWORK") != "TESTNET":
        raise CtsError("只读取币安模拟盘钱包，未访问实盘", reason_code="LIVE_TRADING_HOLD")
    creds = credentials_from_env()
    moment = now or datetime.now(timezone.utc)
    params = [("recvWindow", "5000"), ("timestamp", _millis(moment))]
    body = open_testnet_transport()(
        "GET",
        "/fapi/v2/account",
        _sign(params, creds.api_secret),
        {"X-MBX-APIKEY": creds.api_key},
    )
    return parse_usdt_wallet(body)


def credentials_from_env() -> VenueCredentials:
    return VenueCredentials(os.environ.get("CTS_BINANCE_API_KEY", ""), os.environ.get("CTS_BINANCE_API_SECRET", ""))


def urllib_transport(base_url: str) -> PinnedTransport:
    if base_url != TESTNET_BASE:
        raise CtsError("只允许绑定币安模拟盘主机", reason_code="LIVE_VENUE_UNVERIFIED")
    return open_testnet_transport()


def _ssl_context() -> ssl.SSLContext:
    paths = ssl.get_default_verify_paths()
    if paths.cafile and os.path.exists(paths.cafile):
        return ssl.create_default_context()
    system_bundle = "/etc/ssl/cert.pem"
    if os.path.exists(system_bundle):
        return ssl.create_default_context(cafile=system_bundle)
    return ssl.create_default_context()


def _admit_identity(grant: LiveGrant) -> LiveGrant:
    if LIVE_TRADING_AUTHORIZATION != "HOLD":
        raise CtsError("实盘授权常量不是 HOLD，拒绝使用未复核的进程开关", reason_code="LIVE_VENUE_UNVERIFIED")
    if grant.venue != LIVE_VENUE:
        raise CtsError("交易只走币安 USDT 本位合约，未发送请求", reason_code="LIVE_VENUE_UNVERIFIED")
    if not grant.granted_by or grant.statement != LIVE_GRANT_STATEMENT:
        raise CtsError("缺少本进程实盘授权，未发送请求", reason_code="LIVE_TRADING_HOLD")
    if grant.network not in ("TESTNET", "MAINNET"):
        raise CtsError("实盘网络未指明，未发送请求", reason_code="LIVE_TRADING_HOLD")
    if grant.max_notional <= 0:
        raise CtsError("实盘限额无效，未发送请求", reason_code="RISK_LIMIT")
    if not grant.account_id:
        raise CtsError("缺少账户编号，未发送请求", reason_code="LIVE_TRADING_HOLD")
    return grant


def _admit_grant(grant: LiveGrant, now: datetime) -> LiveGrant:
    admitted = _admit_identity(grant)
    if grant.expires_at <= now:
        raise CtsError("本进程实盘授权已过期，未发送请求", reason_code="LIVE_GRANT_EXPIRED")
    return admitted


def _sign(params: list[tuple[str, str]], secret: str) -> str:
    query = urlencode(params)
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}"


def _millis(now: datetime) -> str:
    return str(int(now.timestamp() * 1000))


def _bound_demo_url(path: str, query: str) -> str:
    if not path.startswith("/") or path.startswith("//"):
        raise CtsError("请求路径离开绑定主机，未发送请求", reason_code="LIVE_VENUE_UNVERIFIED")
    url = f"{TESTNET_BASE}{path}?{query}" if query else f"{TESTNET_BASE}{path}"
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != "demo-fapi.binance.com"
        or parts.port not in (None, 443)
        or parts.username
        or parts.password
    ):
        raise CtsError("主机不在模拟盘绑定内，未发送请求", reason_code="LIVE_VENUE_UNVERIFIED")
    return url


def _finite_positive(value: Decimal, name: str) -> Decimal:
    try:
        number = D(value)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise UnitValidationError(f"{name} 必须是有限正数") from exc
    if not isinstance(number, Decimal) or not number.is_finite() or number <= 0:
        raise UnitValidationError(f"{name} 必须是有限正数")
    return number


def _order_notional(
    *,
    symbol: str,
    side: object,
    qty: Decimal,
    price: Decimal,
    contract_unit: Decimal,
    claimed: Decimal,
    client_order_id: object,
    allowed: tuple[str, ...],
) -> Decimal:
    if symbol not in allowed or symbol not in ALLOWED_SYMBOLS:
        raise CtsError("品种不在本进程授权内", reason_code="RISK_LIMIT")
    if not isinstance(side, Side):
        raise UnitValidationError("方向必须是多或空")
    if not isinstance(client_order_id, str) or not client_order_id.strip():
        raise UnitValidationError("订单号不能为空")
    quantity = _finite_positive(qty, "数量")
    limit = _finite_positive(price, "价格")
    unit = _finite_positive(contract_unit, "合约单位")
    if unit != D("1"):
        raise UnitValidationError("合约单位必须是 1，不能用它缩小占用")
    stated = _finite_positive(claimed, "外传名义额")
    computed = quantity * limit * unit
    if stated != computed:
        raise UnitValidationError("外传名义额与数量、价格、合约单位不一致")
    return computed


def _require_explicit_false(payload: dict, key: str, message: str) -> None:
    if key not in payload:
        raise CtsError(message, reason_code="ACCOUNT_MODE_DRIFT")
    value = payload[key]
    if isinstance(value, str):
        closed = value.strip().casefold() == "false"
    else:
        closed = value is False
    if not closed:
        raise CtsError(message, reason_code="ACCOUNT_MODE_DRIFT")


class _RefuseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CtsError("拒绝离开绑定主机的重定向，未跟随新地址", reason_code="LIVE_VENUE_UNVERIFIED")


def _urllib_opener() -> Callable[[str, str, dict[str, str]], object]:
    http = build_opener(_RefuseRedirect, HTTPSHandler(context=_ssl_context()))

    def opener(method: str, url: str, headers: dict[str, str]) -> object:
        if urlsplit(url).hostname != "demo-fapi.binance.com":
            raise CtsError("主机不在模拟盘绑定内，未发送请求", reason_code="LIVE_VENUE_UNVERIFIED")
        request = Request(url, method=method, headers=headers)
        with http.open(request, timeout=10) as response:
            return json.loads(response.read().decode())

    return opener

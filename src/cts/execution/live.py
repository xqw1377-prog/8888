"""候选交易所私有接口。进程默认 HOLD，不向交易所发送请求。

实盘发送只走 LiveSession，并且要同时具备本进程授权、限额、凭据和账户模式核对。
凭据不写入源码，也不出现在日志文本里。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cts import LIVE_TRADING_AUTHORIZATION
from cts.errors import CtsError
from cts.ledger.models import Side
from cts.money import D


LIVE_GRANT_STATEMENT = "授权本进程在限额内发送 BTC/ETH USDT 逐仓单向实盘单"
ALLOWED_SYMBOLS = ("BTCUSDT", "ETHUSDT")
TESTNET_BASE = "https://testnet.binancefuture.com"
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_notional", D(self.max_notional))
        object.__setattr__(self, "symbols", tuple(self.symbols))


class LiveSession:
    def __init__(self, grant: LiveGrant, credentials: VenueCredentials, transport: Transport, *, now: datetime) -> None:
        self.grant = _admit_grant(grant, now)
        self._credentials = credentials
        self._transport = transport
        self.base_url = MAINNET_BASE if grant.network == "MAINNET" else TESTNET_BASE
        self.orders_sent = 0

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
        reduce_only: bool = False,
    ) -> dict:
        self._admit_order(symbol, notional, now)
        self._require_isolated_one_way(symbol, now)
        params = [
            ("symbol", symbol),
            ("side", "BUY" if side == Side.LONG else "SELL"),
            ("type", "LIMIT"),
            ("timeInForce", "GTC"),
            ("quantity", format(D(qty), "f")),
            ("price", format(D(price), "f")),
            ("newClientOrderId", client_order_id),
            ("reduceOnly", "true" if reduce_only else "false"),
            ("recvWindow", "5000"),
            ("timestamp", _millis(now)),
        ]
        body = self._signed("POST", "/fapi/v1/order", params)
        self.orders_sent += 1
        return body

    def query(self, *, symbol: str, client_order_id: str, now: datetime) -> dict:
        _admit_grant(self.grant, now)
        if symbol not in self.grant.symbols:
            raise CtsError("品种不在本进程授权内", reason_code="RISK_LIMIT")
        return self._signed(
            "GET",
            "/fapi/v1/order",
            [("symbol", symbol), ("origClientOrderId", client_order_id), ("recvWindow", "5000"), ("timestamp", _millis(now))],
        )

    def cancel(self, *, symbol: str, client_order_id: str, now: datetime) -> dict:
        _admit_grant(self.grant, now)
        if symbol not in self.grant.symbols:
            raise CtsError("品种不在本进程授权内", reason_code="RISK_LIMIT")
        return self._signed(
            "DELETE",
            "/fapi/v1/order",
            [("symbol", symbol), ("origClientOrderId", client_order_id), ("recvWindow", "5000"), ("timestamp", _millis(now))],
        )

    def _admit_order(self, symbol: str, notional: Decimal, now: datetime) -> None:
        _admit_grant(self.grant, now)
        if symbol not in self.grant.symbols or symbol not in ALLOWED_SYMBOLS:
            raise CtsError("品种不在本进程授权内", reason_code="RISK_LIMIT")
        if D(notional) <= 0 or D(notional) > self.grant.max_notional:
            raise CtsError("名义价值超出本进程限额，未发送请求", reason_code="RISK_LIMIT")

    def _require_isolated_one_way(self, symbol: str, now: datetime) -> None:
        dual = self._signed("GET", "/fapi/v1/positionSide/dual", [("recvWindow", "5000"), ("timestamp", _millis(now))])
        if _truthy(dual.get("dualSidePosition")):
            raise CtsError("账户不是单向持仓，未发送下单请求", reason_code="ACCOUNT_MODE_DRIFT")
        assets = self._signed("GET", "/fapi/v1/multiAssetsMargin", [("recvWindow", "5000"), ("timestamp", _millis(now))])
        if _truthy(assets.get("multiAssetsMargin")):
            raise CtsError("账户不是单资产保证金，未发送下单请求", reason_code="ACCOUNT_MODE_DRIFT")
        risk = self._signed(
            "GET",
            "/fapi/v2/positionRisk",
            [("symbol", symbol), ("recvWindow", "5000"), ("timestamp", _millis(now))],
        )
        rows = risk if isinstance(risk, list) else risk.get("positions", [])
        match = next((row for row in rows if row.get("symbol") == symbol), None)
        margin = "" if match is None else str(match.get("marginType", ""))
        if margin.casefold() != "isolated":
            raise CtsError("账户不是逐仓，未发送下单请求", reason_code="ACCOUNT_MODE_DRIFT")

    def _signed(self, method: str, path: str, params: list[tuple[str, str]]) -> dict:
        query = _sign(params, self._credentials.api_secret)
        if self._credentials.api_secret in query:
            raise CtsError("签名材料异常，未发送请求", reason_code="CREDENTIALS_MISSING")
        headers = {"X-MBX-APIKEY": self._credentials.api_key}
        payload = self._transport(method, path, query, headers)
        if isinstance(payload, list):
            return {"positions": payload}
        return payload


def credentials_from_env() -> VenueCredentials:
    return VenueCredentials(os.environ.get("CTS_BINANCE_API_KEY", ""), os.environ.get("CTS_BINANCE_API_SECRET", ""))


def urllib_transport(base_url: str) -> Transport:
    def request(method: str, path: str, query: str, headers: dict[str, str]) -> dict:
        req = Request(f"{base_url}{path}?{query}", method=method, headers=headers)
        with urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode())

    return request


def _admit_grant(grant: LiveGrant, now: datetime) -> LiveGrant:
    if LIVE_TRADING_AUTHORIZATION != "HOLD":
        raise CtsError("实盘授权常量不是 HOLD，拒绝使用未复核的进程开关", reason_code="LIVE_VENUE_UNVERIFIED")
    if not grant.granted_by or grant.statement != LIVE_GRANT_STATEMENT:
        raise CtsError("缺少本进程实盘授权，未发送请求", reason_code="LIVE_TRADING_HOLD")
    if grant.network not in ("TESTNET", "MAINNET"):
        raise CtsError("实盘网络未指明，未发送请求", reason_code="LIVE_TRADING_HOLD")
    if grant.expires_at <= now:
        raise CtsError("本进程实盘授权已过期，未发送请求", reason_code="LIVE_GRANT_EXPIRED")
    if grant.max_notional <= 0:
        raise CtsError("实盘限额无效，未发送请求", reason_code="RISK_LIMIT")
    if not grant.account_id:
        raise CtsError("缺少账户编号，未发送请求", reason_code="LIVE_TRADING_HOLD")
    return grant


def _sign(params: list[tuple[str, str]], secret: str) -> str:
    query = urlencode(params)
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}"


def _millis(now: datetime) -> str:
    return str(int(now.timestamp() * 1000))


def _truthy(value: Optional[object]) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() == "true"
    return bool(value)

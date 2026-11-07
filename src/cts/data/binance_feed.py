"""币安模拟盘公共行情接入：白名单 GET、无凭据、授权配额、幂等入库。

只连接 `demo-fapi.binance.com` 的三个公共路径：K 线、标记价、资金费率。
不带 API Key，不发送签名，不跟随重定向。每次请求都消耗当次数据拉取授权的配额。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional
from urllib.request import HTTPSHandler, build_opener

from cts.clock import require_utc, utc_now
from cts.data.models import Candle, FundingSettlement, MarkSample
from cts.data.store import DataStore
from cts.errors import CtsError
from cts.execution.live import TESTNET_BASE, _RefuseRedirect, _bound_demo_url, _ssl_context
from cts.money import D

ALLOWED_SYMBOLS = ("BTCUSDT", "ETHUSDT")
ALLOWED_INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d")
MAX_KLINES = 1000
DEMO_HOST = "demo-fapi.binance.com"

_DATA_STATEMENT = "授权本进程从币安模拟盘拉取公共行情数据"


@dataclass(frozen=True)
class DataPullAuthorization:
    granted_by: str
    expires_at: datetime
    max_requests: int
    network: str = "TESTNET"

    def __post_init__(self) -> None:
        object.__setattr__(self, "expires_at", require_utc(self.expires_at, "expires_at"))
        if self.network != "TESTNET":
            raise CtsError("数据拉取只允许 TESTNET", reason_code="LIVE_VENUE_UNVERIFIED")
        if self.max_requests <= 0:
            raise CtsError("数据拉取配额必须为正", reason_code="LIVE_GRANT_EXPIRED")


def _default_opener() -> Callable[[str, str, dict[str, str]], object]:
    opener = build_opener(_RefuseRedirect(), HTTPSHandler(context=_ssl_context()))

    def send(method: str, url: str, headers: dict[str, str]) -> object:
        req = _request(method, url, headers)
        return opener.open(req, timeout=15)

    return send


def _request(method: str, url: str, headers: dict[str, str]) -> object:
    from urllib.request import Request

    return Request(url, method=method, headers=headers)


class BinanceDataFeed:
    """把模拟盘公共行情转换成数据合同实体并幂等入库。"""

    def __init__(
        self,
        store: DataStore,
        authorization: DataPullAuthorization,
        *,
        opener: Optional[Callable[[str, str, dict[str, str]], object]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.store = store
        self.authorization = authorization
        self._opener = opener or _default_opener()
        self._now = now or utc_now
        self.requests_used = 0
        self.request_log: list[str] = []

    def _get(self, path: str, params: dict[str, str]) -> object:
        now = self._now()
        if now >= self.authorization.expires_at:
            raise CtsError("数据拉取授权已过期，未发送请求", reason_code="LIVE_GRANT_EXPIRED")
        if self.requests_used >= self.authorization.max_requests:
            raise CtsError("数据拉取配额已用尽，未发送请求", reason_code="LIVE_GRANT_EXPIRED")
        query = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
        url = _bound_demo_url(path, query)
        if not url.startswith(f"https://{DEMO_HOST}/"):
            raise CtsError("数据拉取主机不被允许", reason_code="LIVE_VENUE_UNVERIFIED")
        self.requests_used += 1
        try:
            response = self._opener("GET", url, {})
        except Exception as exc:
            self.request_log.append(f"GET {path} {DEMO_HOST} 未完成")
            raise CtsError(f"数据拉取传输失败: {type(exc).__name__}", reason_code="RECONCILIATION_FAILED") from None
        status = getattr(response, "status", None) or getattr(response, "code", 0)
        self.request_log.append(f"GET {path} {DEMO_HOST} {status}")
        if 300 <= status < 400:
            raise CtsError("拒绝离开绑定主机的重定向", reason_code="LIVE_VENUE_UNVERIFIED")
        if status != 200:
            raise CtsError(f"数据拉取返回非 200: {status}", reason_code="RECONCILIATION_FAILED")
        return json.loads(response.read().decode("utf-8"))

    def fetch_candles(self, symbol: str, interval: str = "4h", limit: int = 500) -> list[Candle]:
        if symbol not in ALLOWED_SYMBOLS:
            raise CtsError(f"数据拉取品种不在白名单: {symbol}", reason_code="LIVE_VENUE_UNVERIFIED")
        if interval not in ALLOWED_INTERVALS:
            raise CtsError(f"数据拉取周期不在白名单: {interval}", reason_code="LIVE_VENUE_UNVERIFIED")
        if not 1 <= limit <= MAX_KLINES:
            raise CtsError(f"K 线数量必须在 1 与 {MAX_KLINES} 之间", reason_code="LIVE_VENUE_UNVERIFIED")
        rows = self._get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": str(limit)})
        received = self._now()
        candles: list[Candle] = []
        for row in rows:
            open_time = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc)
            close_time = datetime.fromtimestamp(int(row[6]) / 1000, tz=timezone.utc)
            if close_time > received:
                continue  # 未收盘的最后一根不入库
            candles.append(
                Candle(
                    venue="BINANCE-DEMO",
                    symbol=symbol,
                    interval=interval,
                    event_time=open_time,
                    received_at=received,
                    available_at=close_time,
                    open=D(row[1]),
                    high=D(row[2]),
                    low=D(row[3]),
                    close=D(row[4]),
                    volume=D(row[5]),
                    source_id=f"demo-klines:{interval}",
                    is_closed=True,
                )
            )
        for candle in candles:
            self.store.put(candle)
        return candles

    def fetch_mark(self, symbol: str) -> MarkSample:
        if symbol not in ALLOWED_SYMBOLS:
            raise CtsError(f"数据拉取品种不在白名单: {symbol}", reason_code="LIVE_VENUE_UNVERIFIED")
        body = self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        received = self._now()
        event_time = datetime.fromtimestamp(int(body["time"]) / 1000, tz=timezone.utc)
        sample = MarkSample(
            venue="BINANCE-DEMO",
            symbol=symbol,
            event_time=event_time,
            received_at=received,
            available_at=received,
            mark_price=D(body["markPrice"]),
            index_price=D(body["indexPrice"]) if body.get("indexPrice") else None,
            source_id="demo-premiumIndex",
        )
        self.store.put(sample)
        return sample

    def fetch_funding(self, symbol: str, limit: int = 100) -> list[FundingSettlement]:
        if symbol not in ALLOWED_SYMBOLS:
            raise CtsError(f"数据拉取品种不在白名单: {symbol}", reason_code="LIVE_VENUE_UNVERIFIED")
        if not 1 <= limit <= MAX_KLINES:
            raise CtsError(f"资金费条数必须在 1 与 {MAX_KLINES} 之间", reason_code="LIVE_VENUE_UNVERIFIED")
        rows = self._get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": str(limit)})
        received = self._now()
        settlements: list[FundingSettlement] = []
        for row in rows:
            event_time = datetime.fromtimestamp(int(row["fundingTime"]) / 1000, tz=timezone.utc)
            settlements.append(
                FundingSettlement(
                    venue="BINANCE-DEMO",
                    symbol=symbol,
                    event_time=event_time,
                    received_at=received,
                    available_at=event_time,
                    settlement_id=str(row["fundingTime"]),
                    funding_rate=D(row["fundingRate"]),
                    payment=D(row["fundingRate"]),
                    interval_minutes=None,
                    source_id="demo-fundingRate",
                )
            )
        for settlement in settlements:
            self.store.put(settlement)
        return settlements


_ = TESTNET_BASE  # 主机常量与 live 共源，防止两处漂移

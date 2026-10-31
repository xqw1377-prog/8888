"""本地模拟交易所。原始请求与响应只追加，不连接网络。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from cts.money import D


@dataclass
class VenueScript:
    submit_outcome: str = "ACK"
    fill_ratio: Decimal = Decimal("1")
    protect_delay_ms: int = 0
    liq_mode: str = "ECHO"
    liq_fixed: str = ""
    rate_limited: bool = False


@dataclass
class SimVenue:
    script: VenueScript = field(default_factory=VenueScript)
    margin_mode: str = "ISOLATED"
    amr: Optional[bool] = False
    funding_interval_minutes: Decimal = Decimal("480")
    timeline: list[dict[str, str]] = field(default_factory=list)
    orders: dict[str, dict[str, str]] = field(default_factory=dict)
    external: list[dict[str, str]] = field(default_factory=list)
    submit_count: int = 0
    canceled: list[str] = field(default_factory=list)

    def _rec(self, endpoint: str, body: dict[str, str], elapsed_ms: int) -> dict[str, str]:
        self.timeline.append({"endpoint": endpoint, "elapsed_ms": str(elapsed_ms), **body})
        return body

    def account_info(self, elapsed_ms: int) -> dict[str, str]:
        if self.amr is None:
            amr = "unknown"
        else:
            amr = "true" if self.amr else "false"
        return self._rec(
            "GET /sim/account/info",
            {
                "marginMode": self.margin_mode,
                "autoAddMargin": amr,
                "source": "SIMULATOR",
                "scope": "ACCOUNT",
                "evidence_layer": "LOCAL_INJECTION",
            },
            elapsed_ms,
        )

    def liq_quote(self, symbol: str, model_price: Optional[Decimal]) -> str:
        if self.script.liq_mode == "EMPTY":
            return ""
        if self.script.liq_mode == "FIXED":
            return self.script.liq_fixed
        if model_price is None:
            return ""
        return format(model_price, "f")

    def submit(self, order: dict[str, str], elapsed_ms: int) -> dict[str, str]:
        self.submit_count += 1
        cid = order["client_order_id"]
        if self.script.rate_limited:
            return self._rec("POST /sim/order", {"status": "RATE_LIMIT", "client_order_id": cid}, elapsed_ms)
        if cid in self.orders:
            current = self.orders[cid]
            return self._rec(
                "POST /sim/order",
                {"status": "DUPLICATE", "client_order_id": cid, "filled": current["filled"]},
                elapsed_ms,
            )
        qty = D(order["qty"])
        filled = qty * D(self.script.fill_ratio) if order.get("kind") == "ENTRY" else qty
        if order.get("kind") != "ENTRY":
            filled = qty if self.script.protect_delay_ms == 0 or order.get("kind") == "EXIT" else Decimal("0")
        status = "FILLED" if filled == qty and filled > 0 else ("PARTIAL" if filled > 0 else "OPEN")
        if order.get("kind") == "PROTECTION":
            ready = elapsed_ms + self.script.protect_delay_ms
            status = "CONFIRMED" if self.script.protect_delay_ms == 0 else "PENDING"
            filled = qty if status == "CONFIRMED" else Decimal("0")
        if self.script.submit_outcome == "REJECT" and order.get("kind") == "ENTRY":
            status = "REJECTED"
            filled = Decimal("0")
        record = {
            "client_order_id": cid,
            "kind": order["kind"],
            "side": order["side"],
            "qty": format(qty, "f"),
            "filled": format(filled, "f"),
            "status": status,
            "reduce_only": order.get("reduce_only", "false"),
            "price": order.get("price", "0"),
            "trigger_price": order.get("trigger_price", ""),
            "ready_at": str(elapsed_ms + self.script.protect_delay_ms),
            "symbol": order["symbol"],
        }
        self.orders[cid] = record
        if self.script.submit_outcome == "TIMEOUT" and order.get("kind") == "ENTRY":
            return self._rec("POST /sim/order", {"status": "TIMEOUT", "client_order_id": cid}, elapsed_ms)
        return self._rec(
            "POST /sim/order",
            {"status": status, "client_order_id": cid, "filled": record["filled"], "kind": order["kind"]},
            elapsed_ms,
        )

    def query(self, client_order_id: str, elapsed_ms: int) -> dict[str, str]:
        record = self.orders.get(client_order_id)
        if record is None:
            return self._rec("GET /sim/order", {"status": "NOT_FOUND", "client_order_id": client_order_id}, elapsed_ms)
        self._mature_protection(record, elapsed_ms)
        return self._rec("GET /sim/order", dict(record), elapsed_ms)

    def cancel(self, client_order_id: str, elapsed_ms: int, *, kind: str) -> dict[str, str]:
        record = self.orders.get(client_order_id)
        if record is None:
            return self._rec("POST /sim/cancel", {"status": "NOT_FOUND", "client_order_id": client_order_id}, elapsed_ms)
        if record["kind"] != kind:
            return self._rec(
                "POST /sim/cancel",
                {"status": "KIND_MISMATCH", "client_order_id": client_order_id, "kind": record["kind"]},
                elapsed_ms,
            )
        if record["status"] not in ("FILLED", "CANCELED", "REJECTED", "CONFIRMED"):
            record["status"] = "CANCELED"
        self.canceled.append(client_order_id)
        return self._rec(
            "POST /sim/cancel",
            {"status": record["status"], "client_order_id": client_order_id, "filled": record["filled"], "kind": record["kind"]},
            elapsed_ms,
        )

    def set_cumulative(self, client_order_id: str, filled: Decimal) -> None:
        record = self.orders[client_order_id]
        record["filled"] = format(D(filled), "f")
        qty = D(record["qty"])
        record["status"] = "FILLED" if D(record["filled"]) == qty else "PARTIAL"

    def inject_external(self, *, external_id: str, symbol: str, kind: str, qty: Optional[str], price: str) -> None:
        self.external.append(
            {
                "external_id": external_id,
                "symbol": symbol,
                "kind": kind,
                "qty": "" if qty is None else qty,
                "price": price,
            }
        )

    def _mature_protection(self, record: dict[str, str], elapsed_ms: int) -> None:
        if record["kind"] == "PROTECTION" and record["status"] == "PENDING" and elapsed_ms >= int(record["ready_at"]):
            record["status"] = "CONFIRMED"
            record["filled"] = record["qty"]

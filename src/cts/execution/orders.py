"""模拟盘订单意图。先落盘，再允许传输；崩溃后未完成的发送视为 UNKNOWN。"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from cts.errors import ConflictError, CtsError, RiskLimitError
from cts.ledger.models import Side
from cts.money import D


OPEN_STATUSES = ("INTENT", "NEW", "PARTIALLY_FILLED", "UNKNOWN")
TERMINAL_RELEASE = ("CANCELED", "EXPIRED", "REJECTED")


@dataclass(frozen=True)
class LiveOrder:
    client_order_id: str
    account_id: str
    reservation_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    notional: Decimal
    status: str
    executed_qty: Decimal
    dispatch_started: bool
    body: dict


def budget_notional(order: LiveOrder) -> Decimal:
    """账户限额占用。未发送的意图也占用，避免两个会话同时穿过上限。"""
    if order.status == "INTENT" and not order.dispatch_started:
        return order.notional
    return charged_notional(order)


def net_session_exposure(orders) -> Decimal:
    """会话限额占用：已成交部分按币种取绝对净额后求和（跨币种不得抵销），
    在场/未决/在途意图（含已派发未终报）按全额计。"""
    per_symbol: dict = {}
    resting = Decimal("0")
    for order in orders:
        if order.status == "INTENT":
            # 已开始派发的意图同样占用额度：响应未确认不代表名义不存在
            resting += order.notional
        elif order.status in ("NEW", "PARTIALLY_FILLED", "UNKNOWN", "QUERYING", "CANCELING"):
            resting += order.notional
        elif order.status in ("CANCELED", "EXPIRED", "FILLED"):
            fraction = order.executed_qty / order.qty if order.qty > 0 else Decimal("0")
            if fraction > 0:
                signed = Decimal("1") if order.side == Side.LONG else Decimal("-1")
                per_symbol[order.symbol] = per_symbol.get(order.symbol, Decimal("0")) + order.notional * fraction * signed
    filled_gross = sum((abs(net) for net in per_symbol.values()), start=Decimal("0"))
    return filled_gross + resting


def charged_notional(order: LiveOrder) -> Decimal:
    if order.status == "REJECTED" or (order.status == "INTENT" and not order.dispatch_started):
        return D("0")
    if order.status in ("CANCELED", "EXPIRED"):
        if order.qty <= 0:
            return order.notional
        return order.notional * order.executed_qty / order.qty
    return order.notional


class OrderStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_orders (
                client_order_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                reservation_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty TEXT NOT NULL,
                price TEXT NOT NULL,
                notional TEXT NOT NULL,
                status TEXT NOT NULL,
                executed_qty TEXT NOT NULL,
                dispatch_started INTEGER NOT NULL,
                body TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_session_meta (
                account_id TEXT PRIMARY KEY,
                risk_db_path TEXT NOT NULL
            )
            """
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def promote_inflight(self, account_id: str) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    UPDATE live_orders
                    SET status='UNKNOWN'
                    WHERE account_id=? AND (
                        (dispatch_started=1 AND status='INTENT')
                        OR status IN ('CANCELING', 'QUERYING')
                    )
                    """,
                    (account_id,),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def set_risk_path(self, account_id: str, path: str) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO live_session_meta(account_id, risk_db_path) VALUES (?,?)
                    ON CONFLICT(account_id) DO UPDATE SET risk_db_path=excluded.risk_db_path
                    """,
                    (account_id, path),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_risk_path(self, account_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT risk_db_path FROM live_session_meta WHERE account_id=?",
                (account_id,),
            ).fetchone()
        return None if row is None else str(row["risk_db_path"])

    def for_account(self, account_id: str) -> list[LiveOrder]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM live_orders WHERE account_id=? ORDER BY updated_at",
                (account_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, client_order_id: str) -> LiveOrder | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM live_orders WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
        return None if row is None else self._row(row)

    def insert_intent_within_cap(self, order: LiveOrder, max_notional: Decimal, updated_at: datetime) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT * FROM live_orders WHERE client_order_id=?",
                    (order.client_order_id,),
                ).fetchone()
                if existing is not None:
                    current = self._row(existing)
                    if not _same_economics(current, order):
                        raise ConflictError("同一订单号的数量、价格或方向不一致")
                    self._conn.execute("COMMIT")
                    return
                existing_orders = [
                    self._row(row)
                    for row in self._conn.execute(
                        "SELECT * FROM live_orders WHERE account_id=?",
                        (order.account_id,),
                    ).fetchall()
                ]
                charged = net_session_exposure(existing_orders)
                if charged + order.notional > D(max_notional):
                    raise RiskLimitError("累计名义超出本进程限额，未发送请求")
                self._conn.execute(
                    """
                    INSERT INTO live_orders(
                        client_order_id, account_id, reservation_id, symbol, side, qty, price,
                        notional, status, executed_qty, dispatch_started, body, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    _values(order, updated_at),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def claim_dispatch(self, client_order_id: str, account_id: str, updated_at: datetime) -> bool:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    """
                    UPDATE live_orders
                    SET dispatch_started=1, updated_at=?
                    WHERE client_order_id=? AND account_id=? AND status='INTENT' AND dispatch_started=0
                    """,
                    (updated_at.isoformat(), client_order_id, account_id),
                )
                won = cursor.rowcount == 1
                self._conn.execute("COMMIT")
                return won
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def insert_intent(self, order: LiveOrder, updated_at: datetime) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT * FROM live_orders WHERE client_order_id=?",
                    (order.client_order_id,),
                ).fetchone()
                if existing is not None:
                    current = self._row(existing)
                    if not _same_economics(current, order):
                        raise ConflictError("同一订单号的数量、价格或方向不一致")
                    raise CtsError("订单意图已存在", reason_code="IDEMPOTENCY_CONFLICT")
                self._conn.execute(
                    """
                    INSERT INTO live_orders(
                        client_order_id, account_id, reservation_id, symbol, side, qty, price,
                        notional, status, executed_qty, dispatch_started, body, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    _values(order, updated_at),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def save(self, order: LiveOrder, updated_at: datetime) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    UPDATE live_orders SET
                        status=?, executed_qty=?, dispatch_started=?, body=?, updated_at=?
                    WHERE client_order_id=? AND account_id=?
                    """,
                    (
                        order.status,
                        format(order.executed_qty, "f"),
                        1 if order.dispatch_started else 0,
                        json.dumps(order.body, ensure_ascii=False, sort_keys=True),
                        updated_at.isoformat(),
                        order.client_order_id,
                        order.account_id,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _row(self, row: sqlite3.Row) -> LiveOrder:
        return LiveOrder(
            client_order_id=row["client_order_id"],
            account_id=row["account_id"],
            reservation_id=row["reservation_id"],
            symbol=row["symbol"],
            side=row["side"],
            qty=D(row["qty"]),
            price=D(row["price"]),
            notional=D(row["notional"]),
            status=row["status"],
            executed_qty=D(row["executed_qty"]),
            dispatch_started=bool(row["dispatch_started"]),
            body=json.loads(row["body"]),
        )


def _same_economics(left: LiveOrder, right: LiveOrder) -> bool:
    return (
        left.account_id == right.account_id
        and left.reservation_id == right.reservation_id
        and left.symbol == right.symbol
        and left.side == right.side
        and left.qty == right.qty
        and left.price == right.price
        and left.notional == right.notional
    )


def _values(order: LiveOrder, updated_at: datetime) -> tuple:
    return (
        order.client_order_id,
        order.account_id,
        order.reservation_id,
        order.symbol,
        order.side,
        format(order.qty, "f"),
        format(order.price, "f"),
        format(order.notional, "f"),
        order.status,
        format(order.executed_qty, "f"),
        1 if order.dispatch_started else 0,
        json.dumps(order.body, ensure_ascii=False, sort_keys=True),
        updated_at.isoformat(),
    )

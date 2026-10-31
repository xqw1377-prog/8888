"""账户级可串行化 SQLite 存储。提交后崩溃不得丢失预留。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Iterator, Optional

from cts.clock import UTC, require_utc
from cts.ledger.models import Side
from cts.money import D
from cts.risk.models import (
    Blocker,
    ParameterBundle,
    ParameterStatus,
    PositionOccupancy,
    Reservation,
    ReservationStatus,
    RuntimeMode,
    SystemState,
    TraceEvent,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS account_state (
    account_id TEXT PRIMARY KEY,
    risk_version INTEGER NOT NULL,
    account_state_version INTEGER NOT NULL,
    equity_version INTEGER NOT NULL,
    blocker_set_version INTEGER NOT NULL,
    snapshot_version INTEGER NOT NULL,
    system_state TEXT NOT NULL,
    recovery_in_progress INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    param_version TEXT NOT NULL,
    param_status TEXT NOT NULL,
    param_payload TEXT NOT NULL,
    param_approved_by TEXT,
    equity TEXT NOT NULL,
    high_water_mark TEXT NOT NULL,
    unit_nav TEXT NOT NULL,
    daily_nav_open TEXT NOT NULL,
    mode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT,
    qty TEXT NOT NULL,
    notional TEXT NOT NULL,
    risk_amount TEXT NOT NULL,
    isolated_margin TEXT NOT NULL,
    position_version INTEGER NOT NULL,
    PRIMARY KEY (account_id, symbol)
);
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    signal_key TEXT NOT NULL,
    qty TEXT NOT NULL,
    filled_qty TEXT NOT NULL,
    notional TEXT NOT NULL,
    risk_amount TEXT NOT NULL,
    stress_loss_ratio TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_version INTEGER NOT NULL,
    equity_version INTEGER NOT NULL,
    account_state_version INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    unknown_since TEXT,
    created_at TEXT NOT NULL,
    venue_cumulative_filled TEXT NOT NULL DEFAULT '0',
    UNIQUE (account_id, signal_key)
);
CREATE TABLE IF NOT EXISTS reservation_fills (
    fill_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    qty TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blockers (
    blocker_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    severity TEXT NOT NULL,
    created_at TEXT NOT NULL,
    cleared_at TEXT,
    details TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_blocker
    ON blockers(account_id, reason_code) WHERE cleared_at IS NULL;
CREATE TABLE IF NOT EXISTS recovery_attempts (
    attempt_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied_at TEXT,
    result TEXT,
    reason_code TEXT
);
CREATE TABLE IF NOT EXISTS traces (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


def _iso(ts: datetime) -> str:
    return require_utc(ts, "ts").isoformat()


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


class RiskStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def bootstrap(
        self,
        *,
        account_id: str,
        equity: Decimal,
        bundle: ParameterBundle,
        mode: RuntimeMode,
        now: datetime,
    ) -> None:
        with self.txn() as conn:
            exists = conn.execute("SELECT 1 FROM account_state WHERE account_id=?", (account_id,)).fetchone()
            if exists:
                return
            conn.execute(
                """
                INSERT INTO account_state(
                    account_id, risk_version, account_state_version, equity_version,
                    blocker_set_version, snapshot_version, system_state, recovery_in_progress,
                    config_hash, param_version, param_status, param_payload, param_approved_by,
                    equity, high_water_mark, unit_nav, daily_nav_open, mode
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    account_id,
                    1,
                    1,
                    1,
                    1,
                    0,
                    SystemState.RUNNING.value,
                    0,
                    bundle.config_hash,
                    bundle.version,
                    bundle.status.value,
                    json.dumps(bundle.values, sort_keys=True),
                    bundle.approved_by,
                    format(equity, "f"),
                    "1",
                    "1",
                    "1",
                    mode.value,
                ),
            )
            self._trace(conn, account_id, now, "BOOTSTRAP", {"equity": format(equity, "f"), "mode": mode.value})

    def mark_restart(self, account_id: str, now: datetime) -> None:
        with self.txn() as conn:
            self._insert_blocker(
                conn,
                account_id=account_id,
                reason_code="RESTART_RECONCILE",
                severity=SystemState.RECONCILING,
                details="进程重启，默认 RECONCILING，预留不得丢失",
                now=now,
            )
            self._recompute_state(conn, account_id)
            self._trace(conn, account_id, now, "RESTART", {"state": self._state(conn, account_id)})

    def load_account(self, conn: sqlite3.Connection, account_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM account_state WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            raise KeyError(account_id)
        return row

    def load_bundle(self, row: sqlite3.Row) -> ParameterBundle:
        return ParameterBundle(
            version=row["param_version"],
            status=ParameterStatus(row["param_status"]),
            values=json.loads(row["param_payload"]),
            config_hash=row["config_hash"],
            approved_by=row["param_approved_by"],
        )

    def list_reservations(self, conn: sqlite3.Connection, account_id: str) -> list[Reservation]:
        rows = conn.execute("SELECT * FROM reservations WHERE account_id=?", (account_id,)).fetchall()
        return [self._reservation(r) for r in rows]

    def get_reservation(self, conn: sqlite3.Connection, reservation_id: str) -> Reservation:
        row = conn.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
        if row is None:
            raise KeyError(reservation_id)
        return self._reservation(row)

    def list_positions(self, conn: sqlite3.Connection, account_id: str) -> list[PositionOccupancy]:
        rows = conn.execute("SELECT * FROM positions WHERE account_id=?", (account_id,)).fetchall()
        return [
            PositionOccupancy(
                symbol=r["symbol"],
                side=Side(r["side"]) if r["side"] else None,
                qty=D(r["qty"]),
                notional=D(r["notional"]),
                risk_amount=D(r["risk_amount"]),
                isolated_margin=D(r["isolated_margin"]),
                position_version=r["position_version"],
            )
            for r in rows
        ]

    def active_blockers(self, conn: sqlite3.Connection, account_id: str) -> list[Blocker]:
        rows = conn.execute(
            "SELECT * FROM blockers WHERE account_id=? AND cleared_at IS NULL ORDER BY created_at",
            (account_id,),
        ).fetchall()
        return [self._blocker(r) for r in rows]

    def traces(self, account_id: str) -> list[TraceEvent]:
        rows = self._conn.execute(
            "SELECT * FROM traces WHERE account_id=? ORDER BY seq",
            (account_id,),
        ).fetchall()
        return [
            TraceEvent(
                seq=r["seq"],
                at=datetime.fromisoformat(r["at"]).astimezone(UTC),
                action=r["action"],
                payload=json.loads(r["payload"]),
            )
            for r in rows
        ]

    def _reservation(self, r: sqlite3.Row) -> Reservation:
        return Reservation(
            reservation_id=r["reservation_id"],
            account_id=r["account_id"],
            symbol=r["symbol"],
            side=Side(r["side"]),
            signal_key=r["signal_key"],
            qty=D(r["qty"]),
            filled_qty=D(r["filled_qty"]),
            notional=D(r["notional"]),
            risk_amount=D(r["risk_amount"]),
            stress_loss_ratio=D(r["stress_loss_ratio"]),
            status=ReservationStatus(r["status"]),
            risk_version=r["risk_version"],
            equity_version=r["equity_version"],
            account_state_version=r["account_state_version"],
            config_hash=r["config_hash"],
            unknown_since=_parse_ts(r["unknown_since"]),
            created_at=datetime.fromisoformat(r["created_at"]).astimezone(UTC),
        )

    def _blocker(self, r: sqlite3.Row) -> Blocker:
        return Blocker(
            blocker_id=r["blocker_id"],
            reason_code=r["reason_code"],
            severity=SystemState(r["severity"]),
            created_at=datetime.fromisoformat(r["created_at"]).astimezone(UTC),
            cleared_at=_parse_ts(r["cleared_at"]),
            details=r["details"],
        )

    def _insert_blocker(
        self,
        conn: sqlite3.Connection,
        *,
        account_id: str,
        reason_code: str,
        severity: SystemState,
        details: str,
        now: datetime,
        blocker_id: Optional[str] = None,
    ) -> str:
        existing = conn.execute(
            "SELECT blocker_id FROM blockers WHERE account_id=? AND reason_code=? AND cleared_at IS NULL",
            (account_id, reason_code),
        ).fetchone()
        if existing:
            return existing["blocker_id"]
        from uuid import uuid4

        bid = blocker_id or uuid4().hex
        conn.execute(
            """
            INSERT INTO blockers(blocker_id, account_id, reason_code, severity, created_at, cleared_at, details)
            VALUES (?,?,?,?,?,?,?)
            """,
            (bid, account_id, reason_code, severity.value, _iso(now), None, details),
        )
        conn.execute(
            "UPDATE account_state SET blocker_set_version=blocker_set_version+1, account_state_version=account_state_version+1 WHERE account_id=?",
            (account_id,),
        )
        return bid

    def _clear_blocker(self, conn: sqlite3.Connection, account_id: str, reason_code: str, now: datetime) -> bool:
        cur = conn.execute(
            """
            UPDATE blockers SET cleared_at=?
            WHERE account_id=? AND reason_code=? AND cleared_at IS NULL
            """,
            (_iso(now), account_id, reason_code),
        )
        if cur.rowcount:
            conn.execute(
                "UPDATE account_state SET blocker_set_version=blocker_set_version+1, account_state_version=account_state_version+1 WHERE account_id=?",
                (account_id,),
            )
            return True
        return False

    def _recompute_state(self, conn: sqlite3.Connection, account_id: str) -> SystemState:
        from cts.risk.models import STATE_SEVERITY

        blockers = self.active_blockers(conn, account_id)
        row = self.load_account(conn, account_id)
        state = SystemState.RECONCILING if row["recovery_in_progress"] else SystemState.RUNNING
        for blocker in blockers:
            if STATE_SEVERITY[blocker.severity] > STATE_SEVERITY[state]:
                state = blocker.severity
        conn.execute("UPDATE account_state SET system_state=? WHERE account_id=?", (state.value, account_id))
        return state

    def _state(self, conn: sqlite3.Connection, account_id: str) -> str:
        return self.load_account(conn, account_id)["system_state"]

    def _trace(self, conn: sqlite3.Connection, account_id: str, now: datetime, action: str, payload: dict[str, str]) -> None:
        conn.execute(
            "INSERT INTO traces(account_id, at, action, payload) VALUES (?,?,?,?)",
            (account_id, _iso(now), action, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )

    def _bump_risk(self, conn: sqlite3.Connection, account_id: str) -> int:
        conn.execute(
            "UPDATE account_state SET risk_version=risk_version+1 WHERE account_id=?",
            (account_id,),
        )
        return self.load_account(conn, account_id)["risk_version"]

"""执行出件箱与租约。失去租约的进程不能发单。"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional


class ExecutionStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lease (
                account_id TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                boot_id TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_state (
                account_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )
            """
        )

    def close(self) -> None:
        self._conn.close()

    def claim(self, account_id: str, token: str, boot_id: str) -> str:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute("SELECT token FROM lease WHERE account_id=?", (account_id,)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO lease(account_id, token, boot_id) VALUES (?,?,?)",
                    (account_id, token, boot_id),
                )
                held = token
            else:
                held = row["token"]
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return held

    def load(self, account_id: str) -> dict:
        row = self._conn.execute("SELECT payload FROM execution_state WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            return {}
        return json.loads(row["payload"])

    def save(self, account_id: str, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """
                INSERT INTO execution_state(account_id, payload) VALUES (?,?)
                ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload
                """,
                (account_id, encoded),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

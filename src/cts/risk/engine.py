"""账户风险预留、多原因暂停锁与恢复 CAS。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Sequence
from uuid import uuid4

from cts.checksum import sha256_hex
from cts.clock import utc_now
from cts.errors import (
    ConflictError,
    ExitRejectedError,
    NewRiskBlockedError,
    ParameterAdmissionError,
    RecoveryStaleError,
    RiskLimitError,
    StaleDataError,
    UnitValidationError,
)
from cts.ledger.models import Side
from cts.money import D, floor_to_lot
from cts.risk.models import (
    NEW_RISK_BLOCKING_STATES,
    Blocker,
    ExitAuthorization,
    OccupancyView,
    ParameterBundle,
    PositionOccupancy,
    RecoveryAttempt,
    RECOVERY_FRESHNESS_TTL,
    RecoverySnapshot,
    Reservation,
    ReservationStatus,
    ReserveRequest,
    RuntimeMode,
    SystemState,
)
from cts.risk.parameters import admit_parameters, proposed_bundle
from cts.risk.store import RiskStore
from cts.strategy.sizing import SizedPosition, size_position


class _RecoveryAbort(Exception):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _within_freshness(origin: datetime, now: datetime) -> bool:
    age = now - origin
    return timedelta(0) <= age <= RECOVERY_FRESHNESS_TTL


def _blocker_ids(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part for part in raw.split(",") if part)


class RiskEngine:
    def __init__(self, store: RiskStore, account_id: str) -> None:
        self.store = store
        self.account_id = account_id

    @classmethod
    def create(
        cls,
        path: str,
        account_id: str,
        *,
        equity: Decimal,
        bundle: Optional[ParameterBundle] = None,
        mode: RuntimeMode = RuntimeMode.SIMULATION,
        now: Optional[datetime] = None,
    ) -> "RiskEngine":
        store = RiskStore(path)
        store.bootstrap(
            account_id=account_id,
            equity=D(equity),
            bundle=bundle or proposed_bundle(),
            mode=mode,
            now=now or utc_now(),
        )
        return cls(store, account_id)

    @classmethod
    def reopen(cls, path: str, account_id: str, *, now: Optional[datetime] = None) -> "RiskEngine":
        store = RiskStore(path)
        engine = cls(store, account_id)
        engine.store.mark_restart(account_id, now or utc_now())
        return engine

    def close(self) -> None:
        self.store.close()

    def occupancy(self) -> OccupancyView:
        with self.store.txn() as conn:
            return self._view(conn)

    def traces(self):
        return self.store.traces(self.account_id)

    def install_parameters(self, bundle: ParameterBundle, *, now: Optional[datetime] = None) -> None:
        ts = now or utc_now()
        with self.store.txn() as conn:
            conn.execute(
                """
                UPDATE account_state SET
                    config_hash=?, param_version=?, param_status=?, param_payload=?,
                    param_approved_by=?, account_state_version=account_state_version+1
                WHERE account_id=?
                """,
                (
                    bundle.config_hash,
                    bundle.version,
                    bundle.status.value,
                    json.dumps(bundle.values, sort_keys=True),
                    bundle.approved_by,
                    self.account_id,
                ),
            )
            self.store._trace(
                conn,
                self.account_id,
                ts,
                "INSTALL_PARAMETERS",
                {"status": bundle.status.value, "hash": bundle.config_hash},
            )

    def set_equity(self, equity: Decimal, *, reason: str, now: Optional[datetime] = None) -> int:
        ts = now or utc_now()
        with self.store.txn() as conn:
            conn.execute(
                """
                UPDATE account_state
                SET equity=?, equity_version=equity_version+1, account_state_version=account_state_version+1
                WHERE account_id=?
                """,
                (format(D(equity), "f"), self.account_id),
            )
            version = self.store.load_account(conn, self.account_id)["equity_version"]
            self.store._trace(
                conn,
                self.account_id,
                ts,
                "EQUITY_UPDATE",
                {"equity": format(D(equity), "f"), "reason": reason, "equity_version": str(version)},
            )
            return version

    def sync_position(
        self,
        *,
        symbol: str,
        side: Optional[Side],
        qty: Decimal,
        notional: Decimal,
        risk_amount: Decimal,
        isolated_margin: Decimal,
        now: Optional[datetime] = None,
    ) -> None:
        ts = now or utc_now()
        with self.store.txn() as conn:
            row = conn.execute(
                "SELECT position_version FROM positions WHERE account_id=? AND symbol=?",
                (self.account_id, symbol),
            ).fetchone()
            version = (row["position_version"] + 1) if row else 1
            conn.execute(
                """
                INSERT INTO positions(account_id, symbol, side, qty, notional, risk_amount, isolated_margin, position_version)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(account_id, symbol) DO UPDATE SET
                    side=excluded.side, qty=excluded.qty, notional=excluded.notional,
                    risk_amount=excluded.risk_amount, isolated_margin=excluded.isolated_margin,
                    position_version=excluded.position_version
                """,
                (
                    self.account_id,
                    symbol,
                    side.value if side else None,
                    format(D(qty), "f"),
                    format(D(notional), "f"),
                    format(D(risk_amount), "f"),
                    format(D(isolated_margin), "f"),
                    version,
                ),
            )
            self.store._bump_risk(conn, self.account_id)
            self.store._trace(conn, self.account_id, ts, "SYNC_POSITION", {"symbol": symbol, "qty": format(D(qty), "f")})

    def add_blocker(self, reason_code: str, severity: SystemState, details: str = "", *, now: Optional[datetime] = None) -> str:
        ts = now or utc_now()
        with self.store.txn() as conn:
            bid = self.store._insert_blocker(
                conn,
                account_id=self.account_id,
                reason_code=reason_code,
                severity=severity,
                details=details,
                now=ts,
            )
            state = self.store._recompute_state(conn, self.account_id)
            self.store._trace(
                conn,
                self.account_id,
                ts,
                "ADD_BLOCKER",
                {"reason_code": reason_code, "severity": severity.value, "state": state.value},
            )
            return bid

    def clear_blocker(
        self,
        reason_code: str,
        *,
        resolution_evidence_id: str = "",
        now: Optional[datetime] = None,
    ) -> SystemState:
        ts = now or utc_now()
        evidence_id = resolution_evidence_id.strip()
        if not evidence_id:
            raise NewRiskBlockedError("清除阻断必须提供解决证据", reason_code="RESOLUTION_EVIDENCE_REQUIRED")
        with self.store.txn() as conn:
            active = [b for b in self.store.active_blockers(conn, self.account_id) if b.reason_code == reason_code]
            if active and active[0].severity == SystemState.FAULT:
                raise NewRiskBlockedError("FAULT 不能通过清除阻断直接解除", reason_code="FAULT_REQUIRES_INVESTIGATION")
            self._require_resolution_evidence(conn, reason_code, evidence_id, ts)
            cleared = self.store._clear_blocker(conn, self.account_id, reason_code, ts, evidence_id)
            if cleared:
                self.store._consume_evidence(conn, evidence_id, ts)
            state = self.store._recompute_state(conn, self.account_id)
            self.store._trace(
                conn,
                self.account_id,
                ts,
                "CLEAR_BLOCKER",
                {"reason_code": reason_code, "state": state.value, "evidence_id": evidence_id},
            )
            return state

    def register_recovery_approver(self, approver_id: str, *, now: Optional[datetime] = None) -> None:
        ts = now or utc_now()
        approver = approver_id.strip()
        if not approver:
            raise NewRiskBlockedError("批准人不能为空", reason_code="RECOVERY_APPROVER_UNKNOWN")
        with self.store.txn() as conn:
            self.store._register_approver(conn, self.account_id, approver, ts)

    def record_resolution_evidence(
        self,
        reason_code: str,
        *,
        statement: str,
        recorded_by: str,
        now: Optional[datetime] = None,
    ) -> str:
        ts = now or utc_now()
        reason = reason_code.strip()
        body = statement.strip()
        recorder = recorded_by.strip()
        if not reason or not body or not recorder:
            raise NewRiskBlockedError("解决证据缺少原因、内容或记录人", reason_code="RESOLUTION_EVIDENCE_REQUIRED")
        with self.store.txn() as conn:
            if not self.store._approver_exists(conn, self.account_id, recorder):
                raise NewRiskBlockedError("证据记录人未登记", reason_code="RECOVERY_APPROVER_UNKNOWN")
            active = [b for b in self.store.active_blockers(conn, self.account_id) if b.reason_code == reason and ts >= b.created_at]
            blocker_id = active[0].blocker_id if active else ""
            evidence_id = sha256_hex(
                {
                    "account_id": self.account_id,
                    "blocker_id": blocker_id,
                    "reason_code": reason,
                    "statement": body,
                    "recorded_by": recorder,
                    "recorded_at": ts.isoformat(),
                }
            )
            self.store._insert_evidence(
                conn,
                evidence_id=evidence_id,
                account_id=self.account_id,
                blocker_id=blocker_id,
                reason_code=reason,
                statement=body,
                artifact_hash=evidence_id,
                recorded_by=recorder,
                now=ts,
            )
            self.store._trace(conn, self.account_id, ts, "RECORD_EVIDENCE", {"reason_code": reason, "evidence_id": evidence_id})
        return evidence_id

    def _authoritative_snapshot(self, conn, snapshot_version: int, now: datetime):
        stored = self.store._load_recovery_snapshot(conn, self.account_id, snapshot_version)
        if stored is None or stored["health_result"] != "PASS" or stored["health_checked_at"] != stored["created_at"]:
            raise RecoveryStaleError("恢复快照或健康检查不存在", reason_code="RECOVERY_SNAPSHOT_STALE")
        created_at = datetime.fromisoformat(stored["created_at"])
        if not _within_freshness(created_at, now):
            raise RecoveryStaleError("恢复快照已超过有效期", reason_code="RECOVERY_APPROVAL_EXPIRED")
        return stored

    def _require_resolution_evidence(self, conn, reason_code: str, evidence_id: str, now: datetime) -> None:
        row = self.store._evidence_row(conn, evidence_id)
        if row is None or row["account_id"] != self.account_id:
            raise NewRiskBlockedError("解决证据不存在", reason_code="RESOLUTION_EVIDENCE_INVALID")
        if row["reason_code"] != reason_code or row["consumed_at"] is not None:
            raise NewRiskBlockedError("解决证据与当前阻断不匹配或已使用", reason_code="RESOLUTION_EVIDENCE_INVALID")
        active = [b for b in self.store.active_blockers(conn, self.account_id) if b.reason_code == reason_code]
        recorded_at = datetime.fromisoformat(row["recorded_at"])
        if (
            not active
            or not row["blocker_id"]
            or row["blocker_id"] != active[0].blocker_id
            or recorded_at < active[0].created_at
            or not _within_freshness(recorded_at, now)
        ):
            raise NewRiskBlockedError("解决证据未绑定当前故障实例", reason_code="RESOLUTION_EVIDENCE_INVALID")

    def reserve(self, requests: Sequence[ReserveRequest], *, now: Optional[datetime] = None) -> list[Reservation]:
        ts = now or utc_now()
        if not requests:
            return []
        with self.store.txn() as conn:
            view = self._view(conn)
            row = self.store.load_account(conn, self.account_id)
            bundle = self.store.load_bundle(row)
            mode = RuntimeMode(row["mode"])
            self._admit_new_risk(view, bundle, mode)
            for req in requests:
                if req.expected_equity_version is not None and req.expected_equity_version != view.equity_version:
                    raise StaleDataError("权益版本已变，旧预算不能批准新仓")
                if req.expected_risk_version is not None and req.expected_risk_version != view.risk_version:
                    raise StaleDataError("风险版本已变，必须重算预留")
            sized = self._size_batch(requests, view, bundle)
            created: list[Reservation] = []
            skipped: list[str] = []
            for req, item in zip(requests, sized):
                if item is None:
                    skipped.append(req.signal_key)
                    continue
                existing = conn.execute(
                    "SELECT reservation_id, status FROM reservations WHERE account_id=? AND signal_key=?",
                    (self.account_id, req.signal_key),
                ).fetchone()
                if existing:
                    raise ConflictError(f"重复信号 {req.signal_key}")
                reservation_id = uuid4().hex
                conn.execute(
                    """
                    INSERT INTO reservations(
                        reservation_id, account_id, symbol, side, signal_key, qty, filled_qty,
                        notional, risk_amount, stress_loss_ratio, status, risk_version,
                        equity_version, account_state_version, config_hash, unknown_since, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        reservation_id,
                        self.account_id,
                        req.symbol,
                        req.side.value,
                        req.signal_key,
                        format(item.qty, "f"),
                        "0",
                        format(item.notional, "f"),
                        format(item.risk_amount, "f"),
                        format(item.stress_loss_ratio, "f"),
                        ReservationStatus.PENDING.value,
                        view.risk_version,
                        view.equity_version,
                        view.account_state_version,
                        view.config_hash,
                        None,
                        ts.isoformat(),
                    ),
                )
                created.append(self.store.get_reservation(conn, reservation_id))
            if not created:
                raise RiskLimitError("共同缩放后没有可分配数量")
            risk_version = self.store._bump_risk(conn, self.account_id)
            self.store._trace(
                conn,
                self.account_id,
                ts,
                "RESERVE",
                {
                    "count": str(len(created)),
                    "ids": ",".join(r.reservation_id for r in created),
                    "symbols": ",".join(r.symbol for r in created),
                    "pending_risk": format(sum((r.risk_amount for r in created), start=D("0")), "f"),
                    "risk_version": str(risk_version),
                    "unallocated": ",".join(skipped),
                },
            )
            return created

    def admit_live_open(
        self,
        reservation_id: str,
        *,
        symbol: str,
        side: Side,
        qty: Decimal,
        notional: Decimal,
    ) -> Reservation:
        """开仓的唯一风险准入。暂停、故障或没有仍有效的预留时拒绝。"""
        qty = D(qty)
        notional = D(notional)
        with self.store.txn() as conn:
            view = self._view(conn)
            row = self.store.load_account(conn, self.account_id)
            bundle = self.store.load_bundle(row)
            mode = RuntimeMode(row["mode"])
            self._admit_new_risk(view, bundle, mode)
            try:
                reservation = self.store.get_reservation(conn, reservation_id)
            except KeyError as exc:
                raise NewRiskBlockedError("没有有效风险批准", reason_code="NEW_RISK_BLOCKED") from exc
            if reservation.account_id != self.account_id:
                raise NewRiskBlockedError("风险批准不属于该账户", reason_code="NEW_RISK_BLOCKED")
            if reservation.status not in (ReservationStatus.PENDING, ReservationStatus.PARTIAL):
                raise NewRiskBlockedError("风险批准已失效", reason_code="NEW_RISK_BLOCKED")
            if reservation.symbol != symbol or reservation.side != side:
                raise NewRiskBlockedError("风险批准与订单不一致", reason_code="NEW_RISK_BLOCKED")
            if qty > reservation.unfilled_qty or notional > reservation.pending_notional:
                raise RiskLimitError("订单超出已批准的风险预留")
            return reservation

    def apply_fill(
        self,
        reservation_id: str,
        fill_qty: Decimal,
        *,
        fill_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Reservation:
        ts = now or utc_now()
        fill_qty = D(fill_qty)
        if fill_qty <= 0:
            raise UnitValidationError("成交数量必须为正")
        fill_id = fill_id or uuid4().hex
        with self.store.txn() as conn:
            res = self.store.get_reservation(conn, reservation_id)
            existed = conn.execute(
                "SELECT reservation_id, qty FROM reservation_fills WHERE fill_id=?",
                (fill_id,),
            ).fetchone()
            if existed:
                if existed["reservation_id"] != reservation_id:
                    raise ConflictError(f"fill_id 归属冲突: {fill_id}")
                if D(existed["qty"]) != fill_qty:
                    raise ConflictError(f"fill_id 异容: {fill_id}")
                return res
            previous = self._filled_qty(conn, reservation_id)
            conn.execute(
                "INSERT INTO reservation_fills(fill_id, reservation_id, qty) VALUES (?,?,?)",
                (fill_id, reservation_id, format(fill_qty, "f")),
            )
            new_filled = self._filled_qty(conn, reservation_id)
            if new_filled > res.qty:
                raise UnitValidationError("禁止用超量成交翻仓或超预留占用")
            added = new_filled - previous
            if res.status == ReservationStatus.RELEASED:
                status = ReservationStatus.FILLED if new_filled == res.qty else ReservationStatus.RELEASED
            else:
                status = self._status_after_fill(res, new_filled)
            conn.execute(
                "UPDATE reservations SET filled_qty=?, status=? WHERE reservation_id=?",
                (format(new_filled, "f"), status.value, reservation_id),
            )
            if added > 0:
                self._add_position_occupancy(
                    conn,
                    res,
                    added,
                    (added / res.qty) * res.notional,
                    (added / res.qty) * res.risk_amount,
                )
            self.store._bump_risk(conn, self.account_id)
            self.store._trace(
                conn,
                self.account_id,
                ts,
                "FILL",
                {
                    "reservation_id": reservation_id,
                    "fill_id": fill_id,
                    "fill_qty": format(fill_qty, "f"),
                    "filled_qty": format(new_filled, "f"),
                    "status": status.value,
                },
            )
            return self.store.get_reservation(conn, reservation_id)

    def mark_unknown(self, reservation_id: str, *, now: Optional[datetime] = None) -> Reservation:
        ts = now or utc_now()
        with self.store.txn() as conn:
            res = self.store.get_reservation(conn, reservation_id)
            if res.status == ReservationStatus.UNKNOWN:
                return res
            if res.status in (ReservationStatus.RELEASED, ReservationStatus.FILLED):
                return res
            conn.execute(
                """
                UPDATE reservations
                SET status=?, unknown_since=COALESCE(unknown_since, ?)
                WHERE reservation_id=?
                """,
                (ReservationStatus.UNKNOWN.value, ts.isoformat(), reservation_id),
            )
            self.store._insert_blocker(
                conn,
                account_id=self.account_id,
                reason_code="UNKNOWN_ORDER",
                severity=SystemState.PAUSED,
                details=f"reservation {reservation_id} unknown",
                now=ts,
            )
            self.store._recompute_state(conn, self.account_id)
            self.store._bump_risk(conn, self.account_id)
            self.store._trace(conn, self.account_id, ts, "UNKNOWN", {"reservation_id": reservation_id})
            return self.store.get_reservation(conn, reservation_id)

    def expire_ttl(self, reservation_id: str, *, now: Optional[datetime] = None) -> Reservation:
        ts = now or utc_now()
        with self.store.txn() as conn:
            res = self.store.get_reservation(conn, reservation_id)
            if res.status == ReservationStatus.UNKNOWN:
                self.store._trace(conn, self.account_id, ts, "TTL_NO_RELEASE", {"reservation_id": reservation_id})
                return res
            self.store._trace(conn, self.account_id, ts, "TTL_NO_RELEASE", {"reservation_id": reservation_id, "status": res.status.value})
            return res

    def request_cancel(self, reservation_id: str, *, now: Optional[datetime] = None) -> Reservation:
        ts = now or utc_now()
        with self.store.txn() as conn:
            res = self.store.get_reservation(conn, reservation_id)
            if res.status == ReservationStatus.UNKNOWN or res.unknown_since is not None:
                self.store._trace(conn, self.account_id, ts, "CANCEL_BLOCKED_UNKNOWN", {"reservation_id": reservation_id})
                return res
            if res.status in (ReservationStatus.RELEASED, ReservationStatus.FILLED):
                return res
            conn.execute(
                "UPDATE reservations SET status=? WHERE reservation_id=?",
                (ReservationStatus.CANCEL_REQUESTED.value, reservation_id),
            )
            self.store._trace(conn, self.account_id, ts, "CANCEL_REQUEST", {"reservation_id": reservation_id})
            return self.store.get_reservation(conn, reservation_id)

    def confirm_cancel(
        self,
        reservation_id: str,
        venue_cumulative_filled: Decimal,
        *,
        now: Optional[datetime] = None,
    ) -> Reservation:
        ts = now or utc_now()
        venue_filled = D(venue_cumulative_filled)
        with self.store.txn() as conn:
            res = self.store.get_reservation(conn, reservation_id)
            if res.status == ReservationStatus.UNKNOWN or res.unknown_since is not None:
                raise NewRiskBlockedError("UNKNOWN 预留不能因撤单确认直接释放")
            previous = self._filled_qty(conn, reservation_id)
            conn.execute(
                "UPDATE reservations SET venue_cumulative_filled=? WHERE reservation_id=?",
                (format(max(D(self._venue_cumulative(conn, reservation_id)), venue_filled), "f"), reservation_id),
            )
            filled = self._filled_qty(conn, reservation_id)
            if filled > res.qty:
                raise UnitValidationError("累计成交不能超过预留")
            added = filled - previous
            if added > 0:
                self._add_position_occupancy(
                    conn,
                    res,
                    added,
                    (added / res.qty) * res.notional,
                    (added / res.qty) * res.risk_amount,
                )
            status = ReservationStatus.FILLED if filled == res.qty else ReservationStatus.RELEASED
            conn.execute(
                "UPDATE reservations SET filled_qty=?, status=? WHERE reservation_id=?",
                (format(filled, "f"), status.value, reservation_id),
            )
            self.store._bump_risk(conn, self.account_id)
            self.store._trace(
                conn,
                self.account_id,
                ts,
                "CANCEL_CONFIRM",
                {
                    "reservation_id": reservation_id,
                    "filled_qty": format(filled, "f"),
                    "released_qty": format(res.qty - filled, "f"),
                    "status": status.value,
                },
            )
            return self.store.get_reservation(conn, reservation_id)

    def authorize_exit(
        self,
        *,
        symbol: str,
        qty: Decimal,
        expected_position_version: Optional[int] = None,
        liq_validation_failed: bool = False,
        param_admission_failed: bool = False,
        now: Optional[datetime] = None,
    ) -> ExitAuthorization:
        ts = now or utc_now()
        qty = D(qty)
        with self.store.txn() as conn:
            positions = {p.symbol: p for p in self.store.list_positions(conn, self.account_id)}
            pos = positions.get(symbol)
            if pos is None or pos.qty <= 0 or pos.side is None:
                raise ExitRejectedError("没有可确认仓位，不能声称已经退出")
            if expected_position_version is not None and expected_position_version != pos.position_version:
                raise ExitRejectedError("仓位版本陈旧，禁止按旧数量重建退出", reason_code="STALE_POSITION")
            if qty > pos.qty:
                raise ExitRejectedError("退出数量超过剩余真实持仓，禁止翻仓", reason_code="EXIT_WOULD_FLIP")
            _ = liq_validation_failed, param_admission_failed
            auth = ExitAuthorization(
                allowed=True,
                reason_code=None,
                symbol=symbol,
                qty=qty,
                position_version=pos.position_version,
            )
            self.store._trace(
                conn,
                self.account_id,
                ts,
                "AUTHORIZE_EXIT",
                {"symbol": symbol, "qty": format(qty, "f"), "position_version": str(pos.position_version)},
            )
            return auth

    def create_recovery_snapshot(self, *, now: Optional[datetime] = None) -> RecoverySnapshot:
        ts = now or utc_now()
        with self.store.txn() as conn:
            conn.execute(
                "UPDATE account_state SET recovery_in_progress=1, snapshot_version=snapshot_version+1 WHERE account_id=?",
                (self.account_id,),
            )
            self.store._recompute_state(conn, self.account_id)
            row = self.store.load_account(conn, self.account_id)
            view = self._view(conn)
            snap = RecoverySnapshot(
                snapshot_version=row["snapshot_version"],
                blocker_set_version=view.blocker_set_version,
                risk_version=view.risk_version,
                account_state_version=row["account_state_version"],
                equity_version=view.equity_version,
                config_hash=view.config_hash,
                blocker_ids=tuple(b.blocker_id for b in view.blockers),
                system_state=view.system_state,
                created_at=ts,
            )
            check_id = uuid4().hex
            detail = json.dumps(
                {
                    "config_hash": snap.config_hash,
                    "risk_version": snap.risk_version,
                    "equity_version": snap.equity_version,
                    "blocker_ids": list(snap.blocker_ids),
                    "market_data": "NOT_CHECKED",
                },
                sort_keys=True,
            )
            conn.execute(
                """
                INSERT INTO health_checks(check_id, account_id, snapshot_version, checked_at, scope, result, detail)
                VALUES (?,?,?,?,?,?,?)
                """,
                (check_id, self.account_id, snap.snapshot_version, ts.isoformat(), "LOCAL_RECOVERY", "PASS", detail),
            )
            conn.execute(
                """
                INSERT INTO recovery_snapshots(
                    account_id, snapshot_version, blocker_set_version, risk_version, account_state_version,
                    equity_version, config_hash, blocker_ids, system_state, created_at, health_check_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    self.account_id,
                    snap.snapshot_version,
                    snap.blocker_set_version,
                    snap.risk_version,
                    snap.account_state_version,
                    snap.equity_version,
                    snap.config_hash,
                    ",".join(snap.blocker_ids),
                    snap.system_state.value,
                    ts.isoformat(),
                    check_id,
                ),
            )
            self.store._trace(
                conn,
                self.account_id,
                ts,
                "RECOVERY_SNAPSHOT",
                {
                    "snapshot_version": str(snap.snapshot_version),
                    "blocker_set_version": str(snap.blocker_set_version),
                    "risk_version": str(snap.risk_version),
                },
            )
            return snap

    def approve_recovery(self, snapshot: RecoverySnapshot, *, approved_by: str, now: Optional[datetime] = None) -> str:
        ts = now or utc_now()
        approver = approved_by.strip()
        if not approver:
            raise NewRiskBlockedError("恢复审批缺少批准人", reason_code="RECOVERY_APPROVAL_REQUIRED")
        approval_id = uuid4().hex
        expires_at = ts + RECOVERY_FRESHNESS_TTL
        with self.store.txn() as conn:
            if not self.store._approver_exists(conn, self.account_id, approver):
                raise NewRiskBlockedError("批准人未登记", reason_code="RECOVERY_APPROVER_UNKNOWN")
            stored = self._authoritative_snapshot(conn, snapshot.snapshot_version, ts)
            row = self.store.load_account(conn, self.account_id)
            blockers = self.store.active_blockers(conn, self.account_id)
            if any(b.severity == SystemState.FAULT for b in blockers):
                raise NewRiskBlockedError("FAULT 不能通过恢复审批解除", reason_code="FAULT_REQUIRES_INVESTIGATION")
            if blockers:
                raise NewRiskBlockedError("仍有未解决阻断，不能审批恢复", reason_code="BLOCKER_UNRESOLVED")
            current_ids = tuple(b.blocker_id for b in blockers)
            if (
                row["snapshot_version"] != stored["snapshot_version"]
                or row["blocker_set_version"] != stored["blocker_set_version"]
                or row["risk_version"] != stored["risk_version"]
                or row["config_hash"] != stored["config_hash"]
                or row["equity_version"] != stored["equity_version"]
                or current_ids != _blocker_ids(stored["blocker_ids"])
            ):
                raise RecoveryStaleError("审批时快照已失效")
            conn.execute(
                """
                INSERT INTO recovery_approvals(
                    approval_id, account_id, snapshot_version, blocker_set_version, risk_version,
                    equity_version, config_hash, blocker_ids, approved_by, created_at, expires_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    approval_id,
                    self.account_id,
                    stored["snapshot_version"],
                    stored["blocker_set_version"],
                    stored["risk_version"],
                    stored["equity_version"],
                    stored["config_hash"],
                    stored["blocker_ids"],
                    approver,
                    ts.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            self.store._trace(conn, self.account_id, ts, "APPROVE_RECOVERY", {"approval_id": approval_id, "approved_by": approver})
        return approval_id

    def apply_recovery(
        self,
        snapshot: RecoverySnapshot,
        *,
        approval_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> RecoveryAttempt:
        ts = now or utc_now()
        attempt_id = uuid4().hex
        try:
            return self._apply_recovery_txn(snapshot, attempt_id, ts, approval_id)
        except _RecoveryAbort as exc:
            self._persist_failed_recovery(attempt_id, snapshot, ts, exc.reason_code)
            if exc.reason_code in {"RECOVERY_SNAPSHOT_STALE", "RECOVERY_APPROVAL_EXPIRED"}:
                raise RecoveryStaleError(str(exc), reason_code=exc.reason_code) from None
            raise NewRiskBlockedError(str(exc), reason_code=exc.reason_code) from None

    def _apply_recovery_txn(
        self,
        snapshot: RecoverySnapshot,
        attempt_id: str,
        ts: datetime,
        approval_id: Optional[str],
    ) -> RecoveryAttempt:
        with self.store.txn() as conn:
            row = self.store.load_account(conn, self.account_id)
            blockers = self.store.active_blockers(conn, self.account_id)
            current_ids = tuple(b.blocker_id for b in blockers)
            stored = self.store._load_recovery_snapshot(conn, self.account_id, snapshot.snapshot_version)
            if stored is None or stored["health_result"] != "PASS" or stored["health_checked_at"] != stored["created_at"]:
                raise _RecoveryAbort("RECOVERY_SNAPSHOT_STALE", "恢复快照或健康检查不存在")
            created_at = datetime.fromisoformat(stored["created_at"])
            stored_ids = _blocker_ids(stored["blocker_ids"])
            stale = (
                row["snapshot_version"] != stored["snapshot_version"]
                or row["blocker_set_version"] != stored["blocker_set_version"]
                or row["risk_version"] != stored["risk_version"]
                or row["config_hash"] != stored["config_hash"]
                or row["equity_version"] != stored["equity_version"]
                or current_ids != stored_ids
            )
            payload = json.dumps(
                {
                    "snapshot_version": snapshot.snapshot_version,
                    "blocker_set_version": snapshot.blocker_set_version,
                    "risk_version": snapshot.risk_version,
                    "config_hash": snapshot.config_hash,
                },
                sort_keys=True,
            )
            if any(blocker.severity == SystemState.FAULT for blocker in blockers):
                raise _RecoveryAbort("FAULT_REQUIRES_INVESTIGATION", "FAULT 不能通过恢复快照直接解除")
            if not approval_id:
                raise _RecoveryAbort("RECOVERY_APPROVAL_REQUIRED", "恢复缺少人工审批")
            approval = conn.execute(
                "SELECT * FROM recovery_approvals WHERE approval_id=? AND account_id=?",
                (approval_id, self.account_id),
            ).fetchone()
            if approval is None:
                raise _RecoveryAbort("RECOVERY_APPROVAL_REQUIRED", "恢复审批不存在")
            if (
                approval["snapshot_version"] != stored["snapshot_version"]
                or approval["blocker_set_version"] != stored["blocker_set_version"]
                or approval["risk_version"] != stored["risk_version"]
                or approval["config_hash"] != stored["config_hash"]
                or approval["equity_version"] != stored["equity_version"]
                or approval["blocker_ids"] != stored["blocker_ids"]
            ):
                raise _RecoveryAbort("RECOVERY_APPROVAL_MISMATCH", "审批与快照版本不一致")
            if stale or blockers:
                if blockers and not stale:
                    raise _RecoveryAbort("BLOCKER_UNRESOLVED", "活动阻断尚未用解决证据清除，不能恢复")
                raise _RecoveryAbort("RECOVERY_SNAPSHOT_STALE", "审批快照已失效，恢复失败")
            expires_at = datetime.fromisoformat(approval["expires_at"])
            if ts > expires_at or not _within_freshness(created_at, ts):
                raise _RecoveryAbort("RECOVERY_APPROVAL_EXPIRED", "恢复审批已超过有效期")
            conn.execute(
                "UPDATE account_state SET recovery_in_progress=0, pause_latched=0 WHERE account_id=?",
                (self.account_id,),
            )
            state = self.store._recompute_state(conn, self.account_id)
            conn.execute(
                """
                INSERT INTO recovery_attempts(attempt_id, account_id, snapshot_json, created_at, applied_at, result, reason_code)
                VALUES (?,?,?,?,?,?,?)
                """,
                (attempt_id, self.account_id, payload, ts.isoformat(), ts.isoformat(), "APPLIED", state.value),
            )
            self.store._trace(conn, self.account_id, ts, "RECOVERY_APPLIED", {"state": state.value, "attempt_id": attempt_id})
            return RecoveryAttempt(attempt_id=attempt_id, snapshot=snapshot, result="APPLIED", reason_code=None)

    def _persist_failed_recovery(
        self,
        attempt_id: str,
        snapshot: RecoverySnapshot,
        ts: datetime,
        reason_code: str,
    ) -> None:
        payload = json.dumps(
            {
                "snapshot_version": snapshot.snapshot_version,
                "blocker_set_version": snapshot.blocker_set_version,
                "risk_version": snapshot.risk_version,
                "config_hash": snapshot.config_hash,
            },
            sort_keys=True,
        )
        with self.store.txn() as conn:
            conn.execute(
                """
                INSERT INTO recovery_attempts(attempt_id, account_id, snapshot_json, created_at, applied_at, result, reason_code)
                VALUES (?,?,?,?,?,?,?)
                """,
                (attempt_id, self.account_id, payload, ts.isoformat(), ts.isoformat(), "FAILED", reason_code),
            )
            self.store._trace(conn, self.account_id, ts, "RECOVERY_FAILED", {"attempt_id": attempt_id, "reason_code": reason_code})

    def _admit_new_risk(self, view: OccupancyView, bundle: ParameterBundle, mode: RuntimeMode) -> None:
        admit_parameters(mode, bundle, view.config_hash)
        if view.system_state in NEW_RISK_BLOCKING_STATES:
            raise NewRiskBlockedError(
                f"系统状态 {view.system_state.value} 禁止新增风险",
                reason_code=view.blockers[0].reason_code if view.blockers else view.system_state.value,
            )
        if any(r.status == ReservationStatus.UNKNOWN for r in view.reservations):
            raise NewRiskBlockedError("存在 UNKNOWN 预留，禁止新增风险", reason_code="UNKNOWN_ORDER")

    def _size_batch(
        self,
        requests: Sequence[ReserveRequest],
        view: OccupancyView,
        bundle: ParameterBundle,
    ) -> list[Optional[SizedPosition]]:
        coefficient = D("1")
        if view.system_state == SystemState.REDUCED:
            coefficient = bundle.decimal("risk_coefficient_reduced")
        used_risk = view.total_risk
        used_abs = view.total_abs_notional
        used_margin = view.isolated_margin + view.pending_margin
        symbol_used = {p.symbol: p.notional for p in view.positions}
        for res in view.reservations:
            symbol_used[res.symbol] = symbol_used.get(res.symbol, D("0")) + res.pending_notional
        remaining_margin = view.equity * bundle.decimal("margin_budget_pct") - used_margin
        leverage = bundle.decimal("leverage")
        draft: list[Optional[SizedPosition]] = []
        for req in requests:
            draft.append(
                size_position(
                    side=req.side,
                    equity=view.equity,
                    entry_price=req.entry_price,
                    stop_price=req.stop_price,
                    multiplier=req.multiplier,
                    lot_size=req.lot_size,
                    per_symbol_risk_pct=bundle.decimal("per_symbol_risk_pct") * coefficient,
                    portfolio_risk_pct=bundle.decimal("portfolio_risk_pct") * coefficient,
                    used_portfolio_risk=used_risk,
                    per_symbol_notional_pct=bundle.decimal("per_symbol_notional_pct"),
                    used_symbol_notional=symbol_used.get(req.symbol, D("0")),
                    total_abs_notional_pct=bundle.decimal("total_abs_notional_pct"),
                    used_abs_notional=used_abs,
                    leverage=leverage,
                    remaining_margin_budget=max(remaining_margin, D("0")),
                    fee_rate_one_way=req.fee_rate_one_way,
                    stress_slippage=req.stress_slippage,
                    funding_reserve_ratio=req.funding_reserve_ratio,
                )
            )
        live = [(req, item) for req, item in zip(requests, draft) if item is not None]
        if len(live) <= 1:
            return draft
        portfolio_remaining = view.equity * bundle.decimal("portfolio_risk_pct") * coefficient - view.total_risk
        notional_remaining = view.equity * bundle.decimal("total_abs_notional_pct") - view.total_abs_notional
        new_risk = sum((item.risk_amount for _, item in live), start=D("0"))
        new_notional = sum((item.notional for _, item in live), start=D("0"))
        new_margin = sum((item.notional / leverage for _, item in live), start=D("0"))
        scales = [D("1")]
        if new_risk > portfolio_remaining:
            if portfolio_remaining <= 0:
                return [None for _ in requests]
            scales.append(portfolio_remaining / new_risk)
        if new_notional > notional_remaining:
            if notional_remaining <= 0:
                return [None for _ in requests]
            scales.append(notional_remaining / new_notional)
        if new_margin > remaining_margin:
            if remaining_margin <= 0:
                return [None for _ in requests]
            scales.append(remaining_margin / new_margin)
        symbol_scale: dict[str, Decimal] = {}
        grouped: dict[str, list[SizedPosition]] = {}
        for req, item in live:
            grouped.setdefault(req.symbol, []).append(item)
        for symbol, items in grouped.items():
            cap = view.equity * bundle.decimal("per_symbol_notional_pct") - symbol_used.get(symbol, D("0"))
            group_notional = sum((item.notional for item in items), start=D("0"))
            if group_notional <= 0:
                symbol_scale[symbol] = D("1")
            elif cap <= 0:
                symbol_scale[symbol] = D("0")
            else:
                symbol_scale[symbol] = min(D("1"), cap / group_notional)
        global_scale = min(scales)
        scaled: list[Optional[SizedPosition]] = []
        for req, item in zip(requests, draft):
            if item is None:
                scaled.append(None)
                continue
            scale = min(global_scale, symbol_scale.get(req.symbol, D("1")))
            if scale >= 1:
                scaled.append(item)
                continue
            if scale <= 0:
                scaled.append(None)
                continue
            qty = floor_to_lot(item.qty * scale, req.lot_size)
            if qty <= 0:
                scaled.append(None)
                continue
            notional = qty * req.entry_price * req.multiplier
            scaled.append(
                SizedPosition(
                    side=item.side,
                    qty=qty,
                    notional=notional,
                    risk_amount=notional * item.stress_loss_ratio,
                    stress_loss_ratio=item.stress_loss_ratio,
                    binding_constraint="JOINT_SCALE",
                    effective_notional_capacity=item.effective_notional_capacity,
                )
            )
        return scaled

    def _venue_cumulative(self, conn, reservation_id: str) -> Decimal:
        row = conn.execute(
            "SELECT venue_cumulative_filled FROM reservations WHERE reservation_id=?",
            (reservation_id,),
        ).fetchone()
        return D(row["venue_cumulative_filled"] if row and row["venue_cumulative_filled"] is not None else "0")

    def _filled_qty(self, conn, reservation_id: str) -> Decimal:
        row = conn.execute(
            "SELECT COALESCE(SUM(CAST(qty AS TEXT)), '0') AS ignored FROM reservation_fills WHERE reservation_id=?",
            (reservation_id,),
        ).fetchone()
        total = D("0")
        for item in conn.execute(
            "SELECT qty FROM reservation_fills WHERE reservation_id=?",
            (reservation_id,),
        ).fetchall():
            total += D(item["qty"])
        return max(total, self._venue_cumulative(conn, reservation_id))

    def _status_after_fill(self, res: Reservation, new_filled: Decimal) -> ReservationStatus:
        if new_filled == res.qty:
            return ReservationStatus.FILLED
        if res.status == ReservationStatus.RELEASED:
            return ReservationStatus.PARTIAL
        if res.status == ReservationStatus.UNKNOWN:
            return ReservationStatus.PARTIAL
        return ReservationStatus.PARTIAL

    def _add_position_occupancy(
        self,
        conn,
        res: Reservation,
        fill_qty: Decimal,
        fill_notional: Decimal,
        fill_risk: Decimal,
    ) -> None:
        positions = {p.symbol: p for p in self.store.list_positions(conn, self.account_id)}
        pos = positions.get(res.symbol)
        if pos and pos.side and pos.side != res.side and pos.qty > 0:
            raise UnitValidationError("预留成交方向与已有仓位相反，禁止翻仓")
        if pos is None:
            qty = fill_qty
            notional = fill_notional
            risk = fill_risk
            margin = fill_notional / D("2")
            version = 1
            side = res.side
        else:
            qty = pos.qty + fill_qty
            notional = pos.notional + fill_notional
            risk = pos.risk_amount + fill_risk
            margin = pos.isolated_margin + fill_notional / D("2")
            version = pos.position_version + 1
            side = pos.side or res.side
        conn.execute(
            """
            INSERT INTO positions(account_id, symbol, side, qty, notional, risk_amount, isolated_margin, position_version)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id, symbol) DO UPDATE SET
                side=excluded.side, qty=excluded.qty, notional=excluded.notional,
                risk_amount=excluded.risk_amount, isolated_margin=excluded.isolated_margin,
                position_version=excluded.position_version
            """,
            (
                self.account_id,
                res.symbol,
                side.value,
                format(qty, "f"),
                format(notional, "f"),
                format(risk, "f"),
                format(margin, "f"),
                version,
            ),
        )

    def _view(self, conn) -> OccupancyView:
        row = self.store.load_account(conn, self.account_id)
        reservations = self.store.list_reservations(conn, self.account_id)
        positions = self.store.list_positions(conn, self.account_id)
        blockers = self.store.active_blockers(conn, self.account_id)
        pending_risk = sum((r.pending_risk for r in reservations), start=D("0"))
        pending_notional = sum((r.pending_notional for r in reservations), start=D("0"))
        position_risk = sum((p.risk_amount for p in positions), start=D("0"))
        position_notional = sum((p.notional for p in positions), start=D("0"))
        isolated = sum((p.isolated_margin for p in positions), start=D("0"))
        leverage = D(json.loads(row["param_payload"])["leverage"])
        pending_margin = pending_notional / leverage if leverage > 0 else D("0")
        return OccupancyView(
            equity=D(row["equity"]),
            equity_version=row["equity_version"],
            risk_version=row["risk_version"],
            account_state_version=row["account_state_version"],
            blocker_set_version=row["blocker_set_version"],
            config_hash=row["config_hash"],
            system_state=SystemState(row["system_state"]),
            position_risk=position_risk,
            pending_risk=pending_risk,
            total_risk=position_risk + pending_risk,
            position_abs_notional=position_notional,
            pending_abs_notional=pending_notional,
            total_abs_notional=position_notional + pending_notional,
            isolated_margin=isolated,
            pending_margin=pending_margin,
            reservations=tuple(reservations),
            positions=tuple(positions),
            blockers=tuple(blockers),
        )

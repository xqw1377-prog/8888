"""可对账资金账本：事件与分录原子落库，原始流水只追加。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import uuid4

from cts.errors import ConflictError, LedgerImbalanceError, SnapshotInconsistentError, UnitValidationError
from cts.ledger.models import (
    AccountBucket,
    DeltaAttribution,
    EquitySnapshot,
    JournalLine,
    LedgerEvent,
    LedgerEventType,
    PositionState,
    ReconcileResult,
    Side,
)
from cts.money import D, require_positive


def _lines(asset: str, debit: AccountBucket, credit: AccountBucket, amount: Decimal, note: str) -> tuple[JournalLine, ...]:
    amount = D(amount)
    if amount <= 0:
        raise UnitValidationError("分录金额必须为正")
    return (
        JournalLine(asset=asset, bucket=debit, debit=amount, credit=Decimal("0"), note=note),
        JournalLine(asset=asset, bucket=credit, debit=Decimal("0"), credit=amount, note=note),
    )


@dataclass
class AccountBooks:
    balances: dict[tuple[str, AccountBucket], Decimal] = field(default_factory=dict)
    shares: Decimal = Decimal("0")
    high_water_mark: Decimal = Decimal("1")
    last_unit_nav: Decimal = Decimal("1")
    reserved_budget: Decimal = Decimal("0")


class Ledger:
    def __init__(self, account_id: str, asset: str = "USDT", path: Optional[str] = None) -> None:
        self.account_id = account_id
        self.asset = asset
        self._events: list[LedgerEvent] = []
        self._by_external: dict[tuple[str, str], LedgerEvent] = {}
        self._books = AccountBooks()
        self._positions: dict[str, PositionState] = {}
        self._loading = False
        self._store: Optional[LedgerStore] = LedgerStore(path) if path else None
        if self._store is not None:
            self._loading = True
            try:
                for event in self._store.load(account_id):
                    self.post_journal(event)
                    self._project_position(event)
                    if not self._has_open_position():
                        self._refresh_nav(unrealized=Decimal("0"))
            finally:
                self._loading = False

    def append_event(self, event: LedgerEvent) -> LedgerEvent:
        if event.account_id != self.account_id:
            raise UnitValidationError("事件账户与账本不一致")
        if event.asset != self.asset:
            raise UnitValidationError("事件资产与账本不一致")
        key = (event.venue, event.external_id)
        existing = self._by_external.get(key)
        if existing is not None:
            if existing.event_type != event.event_type or existing.lines != event.lines:
                raise ConflictError(f"重复流水内容冲突: {key}")
            return existing
        self.post_journal(event)
        return event

    def post_journal(self, event: LedgerEvent) -> None:
        key = (event.venue, event.external_id)
        if key in self._by_external:
            raise ConflictError("post_journal 收到已存在事件，应走 append_event 幂等路径")
        self._assert_share_ok(event)
        if self._store is not None and not self._loading:
            self._store.append(event)
        if event.event_type in (LedgerEventType.TRANSFER_IN, LedgerEventType.TRANSFER_OUT):
            self._apply_share_mutation(event)
        for line in event.lines:
            bal_key = (line.asset, line.bucket)
            self._books.balances[bal_key] = self._books.balances.get(bal_key, Decimal("0")) + line.signed
        self._events.append(event)
        self._by_external[key] = event
        if not self._loading and not self._has_open_position():
            self._refresh_nav(unrealized=Decimal("0"))

    def _assert_share_ok(self, event: LedgerEvent) -> None:
        if event.event_type not in (LedgerEventType.TRANSFER_IN, LedgerEventType.TRANSFER_OUT):
            return
        amount = sum(line.debit - line.credit for line in event.lines if line.bucket == AccountBucket.WALLET)
        if self._books.shares == 0:
            if amount <= 0:
                raise UnitValidationError("空账本不能先出金")
            return
        if amount < 0:
            nav = self._books.last_unit_nav if self._books.last_unit_nav > 0 else Decimal("1")
            if self._books.shares - (-amount) / nav < 0:
                raise LedgerImbalanceError("份额不能为负")

    def transfer_in(
        self,
        *,
        amount: Decimal,
        event_time: datetime,
        received_at: datetime,
        external_id: str,
        venue: str = "internal",
        source: str = "owner",
        mark_prices: Optional[dict[str, Decimal]] = None,
        as_of: Optional[datetime] = None,
    ) -> LedgerEvent:
        amount = require_positive(D(amount), "amount")
        self._prepare_nav_for_transfer(mark_prices=mark_prices, as_of=as_of or event_time)
        return self.append_event(
            LedgerEvent(
                event_id=uuid4().hex,
                account_id=self.account_id,
                venue=venue,
                event_type=LedgerEventType.TRANSFER_IN,
                event_time=event_time,
                received_at=received_at,
                asset=self.asset,
                external_id=external_id,
                source=source,
                lines=_lines(self.asset, AccountBucket.WALLET, AccountBucket.EXTERNAL, amount, "deposit"),
            )
        )

    def transfer_out(
        self,
        *,
        amount: Decimal,
        event_time: datetime,
        received_at: datetime,
        external_id: str,
        venue: str = "internal",
        source: str = "owner",
        mark_prices: Optional[dict[str, Decimal]] = None,
        as_of: Optional[datetime] = None,
    ) -> LedgerEvent:
        amount = require_positive(D(amount), "amount")
        if self.balance(AccountBucket.WALLET) < amount:
            raise LedgerImbalanceError("可用钱包余额不足以出金")
        self._prepare_nav_for_transfer(mark_prices=mark_prices, as_of=as_of or event_time)
        return self.append_event(
            LedgerEvent(
                event_id=uuid4().hex,
                account_id=self.account_id,
                venue=venue,
                event_type=LedgerEventType.TRANSFER_OUT,
                event_time=event_time,
                received_at=received_at,
                asset=self.asset,
                external_id=external_id,
                source=source,
                lines=_lines(self.asset, AccountBucket.EXTERNAL, AccountBucket.WALLET, amount, "withdraw"),
            )
        )

    def lock_margin(
        self,
        *,
        amount: Decimal,
        symbol: str,
        event_time: datetime,
        received_at: datetime,
        external_id: str,
        venue: str,
    ) -> LedgerEvent:
        amount = require_positive(D(amount), "amount")
        existing = self._by_external.get((venue, external_id))
        if existing is not None:
            return existing
        if self.balance(AccountBucket.WALLET) < amount:
            raise LedgerImbalanceError("可用钱包余额不足以划入逐仓保证金")
        event = self.append_event(
            LedgerEvent(
                event_id=uuid4().hex,
                account_id=self.account_id,
                venue=venue,
                event_type=LedgerEventType.MARGIN_LOCK,
                event_time=event_time,
                received_at=received_at,
                asset=self.asset,
                external_id=external_id,
                source="system",
                symbol=symbol,
                lines=_lines(
                    self.asset,
                    AccountBucket.ISOLATED_MARGIN,
                    AccountBucket.WALLET,
                    amount,
                    "margin_lock",
                ),
            )
        )
        pos = self.position(symbol)
        pos.isolated_margin += amount
        pos.version += 1
        return event

    def release_margin(
        self,
        *,
        amount: Decimal,
        symbol: str,
        event_time: datetime,
        received_at: datetime,
        external_id: str,
        venue: str,
    ) -> LedgerEvent:
        amount = require_positive(D(amount), "amount")
        existing = self._by_external.get((venue, external_id))
        if existing is not None:
            return existing
        pos = self.position(symbol)
        if pos.isolated_margin < amount or self.balance(AccountBucket.ISOLATED_MARGIN) < amount:
            raise LedgerImbalanceError("逐仓保证金不足以划回钱包")
        event = self.append_event(
            LedgerEvent(
                event_id=uuid4().hex,
                account_id=self.account_id,
                venue=venue,
                event_type=LedgerEventType.MARGIN_RELEASE,
                event_time=event_time,
                received_at=received_at,
                asset=self.asset,
                external_id=external_id,
                source="system",
                symbol=symbol,
                lines=_lines(
                    self.asset,
                    AccountBucket.WALLET,
                    AccountBucket.ISOLATED_MARGIN,
                    amount,
                    "margin_release",
                ),
            )
        )
        pos.isolated_margin -= amount
        pos.version += 1
        return event

    def apply_fill(
        self,
        *,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        multiplier: Decimal,
        event_time: datetime,
        received_at: datetime,
        external_id: str,
        venue: str,
        venue_trade_id: str,
        reduce_only: bool = False,
        allow_add: bool = False,
        credit_isolated: bool = True,
        price_source: str = "",
        quote_grade: str = "",
        price_event_time: Optional[datetime] = None,
        event_type: LedgerEventType = LedgerEventType.FILL,
    ) -> tuple[LedgerEvent, Decimal]:
        key = (venue, external_id)
        qty = require_positive(D(qty), "qty")
        price = require_positive(D(price), "price")
        multiplier = require_positive(D(multiplier), "multiplier")
        pos = self.position(symbol, multiplier=multiplier)
        existing = self._by_external.get(key)
        if existing is not None:
            if existing.metadata.get("qty") != format(qty, "f") or existing.metadata.get("price") != format(price, "f") or existing.metadata.get("side") != side.value:
                raise ConflictError(f"重复流水内容冲突: {key}")
            for meta_key, meta_value in (("price_source", price_source), ("quote_grade", quote_grade)):
                prior = existing.metadata.get(meta_key, "")
                if meta_value and prior and prior != meta_value:
                    raise ConflictError(f"重复流水价格来源冲突: {key}")
            return existing, D(existing.metadata.get("realized", "0"))

        realized = Decimal("0")
        note = "open"
        new_side = side
        new_qty = qty
        new_avg = price
        new_realized_cum = pos.realized_pnl_cum

        if pos.qty == 0:
            if reduce_only:
                raise UnitValidationError("reduceOnly 成交但当前无仓")
        elif pos.side == side:
            if not allow_add:
                raise UnitValidationError("T1 不加仓；如为外部成交需单独入账")
            new_qty = pos.qty + qty
            new_avg = (pos.qty * pos.avg_price + qty * price) / new_qty
            new_side = pos.side
            note = "add"
        else:
            if qty > pos.qty:
                raise UnitValidationError("禁止用超量平仓单直接翻仓")
            delta = price - pos.avg_price
            signed = delta if pos.side == Side.LONG else -delta
            realized = signed * qty * pos.multiplier
            new_qty = pos.qty - qty
            new_avg = pos.avg_price if new_qty else Decimal("0")
            new_side = pos.side if new_qty else None
            new_realized_cum = pos.realized_pnl_cum + realized
            note = "reduce"

        lines: tuple[JournalLine, ...] = tuple()
        next_isolated = pos.isolated_margin
        if realized != 0:
            target = AccountBucket.ISOLATED_MARGIN if credit_isolated else AccountBucket.WALLET
            if realized > 0:
                lines = _lines(self.asset, target, AccountBucket.REALIZED_PNL, realized, "realized_pnl")
            else:
                loss = -realized
                available = pos.isolated_margin if credit_isolated else self.balance(target)
                if credit_isolated and available < loss:
                    raise LedgerImbalanceError("已实现亏损超过逐仓保证金，禁止自动追加")
                if not credit_isolated and self.balance(target) < loss:
                    raise LedgerImbalanceError("已实现亏损超过钱包余额")
                lines = _lines(self.asset, AccountBucket.REALIZED_PNL, target, loss, "realized_pnl")
            if credit_isolated:
                next_isolated = pos.isolated_margin + realized
                if next_isolated < 0:
                    raise LedgerImbalanceError("逐仓保证金不能为负")

        snapshot = _capture_position(pos)
        pos.side = new_side
        pos.qty = new_qty
        pos.avg_price = new_avg
        pos.multiplier = multiplier
        pos.realized_pnl_cum = new_realized_cum
        pos.isolated_margin = next_isolated
        pos.version += 1
        event = LedgerEvent(
            event_id=uuid4().hex,
            account_id=self.account_id,
            venue=venue,
            event_type=event_type,
            event_time=event_time,
            received_at=received_at,
            asset=self.asset,
            external_id=external_id,
            source="venue",
            symbol=symbol,
            lines=lines,
            metadata={
                "side": side.value,
                "qty": format(qty, "f"),
                "price": format(price, "f"),
                "multiplier": format(multiplier, "f"),
                "venue_trade_id": venue_trade_id,
                "realized": format(realized, "f"),
                "note": note,
                **({"price_source": price_source} if price_source else {}),
                **({"quote_grade": quote_grade} if quote_grade else {}),
                **({"price_event_time": price_event_time.isoformat()} if price_event_time is not None else {}),
            },
        )
        try:
            stored = self.append_event(event)
        except Exception:
            _restore_position(pos, snapshot)
            raise
        return stored, realized

    def post_fee(
        self,
        *,
        amount: Decimal,
        event_time: datetime,
        received_at: datetime,
        external_id: str,
        venue: str,
        symbol: Optional[str] = None,
        from_isolated: bool = False,
    ) -> LedgerEvent:
        amount = require_positive(D(amount), "amount")
        src = AccountBucket.ISOLATED_MARGIN if from_isolated else AccountBucket.WALLET
        lines = _lines(self.asset, AccountBucket.FEE, src, amount, "fee")
        existing = self._by_external.get((venue, external_id))
        if existing is not None:
            _require_same_lines(existing, LedgerEventType.FEE, lines)
            return existing
        if self.balance(src) < amount:
            raise LedgerImbalanceError("手续费扣款账户余额不足")
        if from_isolated and symbol and self.position(symbol).isolated_margin < amount:
            raise LedgerImbalanceError("手续费扣除后逐仓保证金为负")
        event = self.append_event(
            LedgerEvent(
                event_id=uuid4().hex,
                account_id=self.account_id,
                venue=venue,
                event_type=LedgerEventType.FEE,
                event_time=event_time,
                received_at=received_at,
                asset=self.asset,
                external_id=external_id,
                source="venue",
                symbol=symbol,
                lines=lines,
            )
        )
        if from_isolated and symbol:
            pos = self.position(symbol)
            pos.isolated_margin -= amount
            pos.version += 1
        return event

    def post_funding(
        self,
        *,
        payment: Decimal,
        event_time: datetime,
        received_at: datetime,
        external_id: str,
        venue: str,
        symbol: str,
        from_isolated: bool = False,
    ) -> LedgerEvent:
        payment = D(payment)
        if payment == 0:
            raise UnitValidationError("资金费金额不能为 0")
        src = AccountBucket.ISOLATED_MARGIN if from_isolated else AccountBucket.WALLET
        if payment > 0:
            lines = _lines(self.asset, src, AccountBucket.FUNDING, payment, "funding_receive")
        else:
            lines = _lines(self.asset, AccountBucket.FUNDING, src, -payment, "funding_pay")
        existing = self._by_external.get((venue, external_id))
        if existing is not None:
            _require_same_lines(existing, LedgerEventType.FUNDING, lines)
            if existing.metadata.get("payment") != format(payment, "f") or existing.symbol != symbol:
                raise ConflictError(f"重复资金费内容冲突: {(venue, external_id)}")
            return existing
        pos = self.position(symbol)
        if payment < 0 and self.balance(src) < -payment:
            raise LedgerImbalanceError("资金费扣款账户余额不足")
        if from_isolated and pos.isolated_margin + payment < 0:
            raise LedgerImbalanceError("资金费扣除后逐仓保证金为负，保持阻断")
        event = self.append_event(
            LedgerEvent(
                event_id=uuid4().hex,
                account_id=self.account_id,
                venue=venue,
                event_type=LedgerEventType.FUNDING,
                event_time=event_time,
                received_at=received_at,
                asset=self.asset,
                external_id=external_id,
                source="venue",
                symbol=symbol,
                lines=lines,
                metadata={"payment": format(payment, "f"), "from_isolated": str(from_isolated)},
            )
        )
        pos.funding_cum += payment
        if from_isolated:
            pos.isolated_margin += payment
            pos.version += 1
        return event

    def reverse(self, event_id: str, *, event_time: datetime, received_at: datetime, external_id: str) -> LedgerEvent:
        original = next((e for e in self._events if e.event_id == event_id), None)
        if original is None:
            raise UnitValidationError("冲正目标不存在")
        if not original.lines:
            raise UnitValidationError("无资金分录的事件用新记录标记，不覆盖原 FILL")
        reversed_lines = tuple(
            JournalLine(
                asset=line.asset,
                bucket=line.bucket,
                debit=line.credit,
                credit=line.debit,
                note=f"reversal_of:{original.event_id}",
            )
            for line in original.lines
        )
        return self.append_event(
            LedgerEvent(
                event_id=uuid4().hex,
                account_id=self.account_id,
                venue=original.venue,
                event_type=LedgerEventType.REVERSAL,
                event_time=event_time,
                received_at=received_at,
                asset=self.asset,
                external_id=external_id,
                source="system",
                symbol=original.symbol,
                lines=reversed_lines,
                reversed_event_id=original.event_id,
            )
        )

    def balance(self, bucket: AccountBucket, asset: Optional[str] = None) -> Decimal:
        return self._books.balances.get((asset or self.asset, bucket), Decimal("0"))

    def cash_equity(self) -> Decimal:
        return self.balance(AccountBucket.WALLET) + self.balance(AccountBucket.ISOLATED_MARGIN)

    def position(self, symbol: str, multiplier: Optional[Decimal] = None) -> PositionState:
        if symbol not in self._positions:
            self._positions[symbol] = PositionState(
                account_id=self.account_id,
                symbol=symbol,
                multiplier=D(multiplier or "1"),
            )
        return self._positions[symbol]

    def positions(self) -> list[PositionState]:
        return list(self._positions.values())

    def events(self) -> list[LedgerEvent]:
        return list(self._events)

    def set_reserved_budget(self, amount: Decimal) -> None:
        self._books.reserved_budget = D(amount)
        if self._books.reserved_budget < 0:
            raise UnitValidationError("预留不能为负")

    def equity_snapshot(
        self,
        *,
        as_of: datetime,
        mark_prices: dict[str, Decimal],
        venue_equity: Optional[Decimal] = None,
        freshness: str = "FRESH",
        valuation_source: str = "MODEL",
    ) -> EquitySnapshot:
        upnl = Decimal("0")
        for pos in self._positions.values():
            if pos.qty == 0:
                continue
            if pos.symbol not in mark_prices:
                raise SnapshotInconsistentError(f"持仓 {pos.symbol} 缺少标记价，暂停收益确认")
            upnl += pos.unrealized_pnl(mark_prices[pos.symbol])
        equity = self.cash_equity() + upnl
        shares = self._books.shares
        unit_nav = (equity / shares) if shares > 0 else Decimal("1")
        if unit_nav > self._books.high_water_mark:
            self._books.high_water_mark = unit_nav
        self._books.last_unit_nav = unit_nav
        return EquitySnapshot(
            account_id=self.account_id,
            as_of=as_of,
            asset=self.asset,
            wallet=self.balance(AccountBucket.WALLET),
            isolated_margin=self.balance(AccountBucket.ISOLATED_MARGIN),
            reserved_budget=self._books.reserved_budget,
            unrealized_pnl=upnl,
            equity=equity,
            shares=shares,
            unit_nav=unit_nav,
            high_water_mark=self._books.high_water_mark,
            freshness=freshness,
            venue_equity=D(venue_equity) if venue_equity is not None else None,
            valuation_source=valuation_source,
        )

    def reconcile(
        self,
        *,
        as_of: datetime,
        mark_prices: dict[str, Decimal],
        venue_equity: Optional[Decimal],
        tolerance: Decimal,
        input_version: str = "v1",
    ) -> ReconcileResult:
        snap = self.equity_snapshot(as_of=as_of, mark_prices=mark_prices, venue_equity=venue_equity)
        realized = -self.balance(AccountBucket.REALIZED_PNL)
        fees = self.balance(AccountBucket.FEE)
        funding_bucket = self.balance(AccountBucket.FUNDING)
        funding_pnl = -funding_bucket
        transfers = -self.balance(AccountBucket.EXTERNAL)
        margin_transfers = Decimal("0")
        for event in self._events:
            if event.event_type in (LedgerEventType.MARGIN_LOCK, LedgerEventType.MARGIN_RELEASE):
                margin_transfers += sum(
                    line.debit - line.credit for line in event.lines if line.bucket == AccountBucket.ISOLATED_MARGIN
                )
        explained = transfers + realized - fees + funding_pnl + snap.unrealized_pnl
        residual = snap.equity - explained
        consistent = abs(residual) <= D(tolerance)
        venue_ok = True
        if venue_equity is not None:
            venue_ok = abs(snap.equity - D(venue_equity)) <= D(tolerance)
        ok = consistent and venue_ok
        attribution = DeltaAttribution(
            realized_pnl=realized,
            fees=fees,
            funding=funding_pnl,
            margin_transfers=margin_transfers,
            price_revaluation=snap.unrealized_pnl,
            transfers=transfers,
            unexplained_residual=residual,
            event_ids=tuple(e.event_id for e in self._events),
            input_version=input_version,
            valuation_basis="mark_price",
        )
        return ReconcileResult(
            account_id=self.account_id,
            as_of=as_of,
            consistent=ok,
            tolerance=D(tolerance),
            model_equity=snap.equity,
            venue_equity=D(venue_equity) if venue_equity is not None else None,
            attribution=attribution,
            conclusion="MATCH" if ok else "SNAPSHOT_INCONSISTENT",
            reason_code=None if ok else "RECONCILIATION_FAILED",
        )

    def replay(self) -> "Ledger":
        clone = Ledger(self.account_id, self.asset)
        for event in self._events:
            clone.append_event(
                LedgerEvent(
                    event_id=event.event_id,
                    account_id=event.account_id,
                    venue=event.venue,
                    event_type=event.event_type,
                    event_time=event.event_time,
                    received_at=event.received_at,
                    asset=event.asset,
                    external_id=event.external_id,
                    source=event.source,
                    lines=event.lines,
                    symbol=event.symbol,
                    position_id=event.position_id,
                    metadata=dict(event.metadata),
                    reversed_event_id=event.reversed_event_id,
                )
            )
            clone._project_position(event)
        clone._books.reserved_budget = self._books.reserved_budget
        clone._books.high_water_mark = max(clone._books.high_water_mark, self._books.high_water_mark)
        return clone

    def _project_position(self, event: LedgerEvent) -> None:
        if event.event_type in (LedgerEventType.FILL, LedgerEventType.LIQUIDATION, LedgerEventType.ADL) and event.symbol:
            _restore_position_from_fill(self, event)
        if event.event_type == LedgerEventType.FUNDING and event.symbol:
            self.position(event.symbol).funding_cum += D(event.metadata.get("payment", "0"))
        if event.event_type == LedgerEventType.MARGIN_LOCK and event.symbol:
            amt = sum(line.debit for line in event.lines if line.bucket == AccountBucket.ISOLATED_MARGIN)
            self.position(event.symbol).isolated_margin += amt
        if event.event_type == LedgerEventType.MARGIN_RELEASE and event.symbol:
            amt = sum(line.credit for line in event.lines if line.bucket == AccountBucket.ISOLATED_MARGIN)
            self.position(event.symbol).isolated_margin -= amt
        if event.symbol and event.event_type == LedgerEventType.FUNDING and event.metadata.get("from_isolated") == "True":
            self.position(event.symbol).isolated_margin += D(event.metadata["payment"])

    def _has_open_position(self) -> bool:
        return any(pos.qty > 0 for pos in self._positions.values())

    def _prepare_nav_for_transfer(
        self,
        *,
        mark_prices: Optional[dict[str, Decimal]],
        as_of: datetime,
    ) -> None:
        if self._has_open_position():
            if not mark_prices:
                raise SnapshotInconsistentError("有持仓时出入金必须提供标记价，暂停收益确认")
            self.equity_snapshot(as_of=as_of, mark_prices=mark_prices)

    def _apply_share_mutation(self, event: LedgerEvent) -> None:
        amount = sum(line.debit - line.credit for line in event.lines if line.bucket == AccountBucket.WALLET)
        nav = self._books.last_unit_nav if self._books.shares > 0 else Decimal("1")
        if self._books.shares == 0:
            if amount <= 0:
                raise UnitValidationError("空账本不能先出金")
            self._books.shares = amount
            self._books.last_unit_nav = Decimal("1")
            self._books.high_water_mark = Decimal("1")
            return
        if amount > 0:
            self._books.shares += amount / nav
        else:
            self._books.shares -= (-amount) / nav
        if self._books.shares < 0:
            raise LedgerImbalanceError("份额不能为负")

    def _refresh_nav(self, unrealized: Decimal) -> None:
        if self._books.shares <= 0:
            return
        self._books.last_unit_nav = (self.cash_equity() + unrealized) / self._books.shares
        if self._books.last_unit_nav > self._books.high_water_mark:
            self._books.high_water_mark = self._books.last_unit_nav


class LedgerStore:
    """追加写入的账本流水。内存投影可以重启，原始分录以这里为准。"""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self.fail_appends = 0
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                account_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                external_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_time TEXT NOT NULL,
                received_at TEXT NOT NULL,
                asset TEXT NOT NULL,
                source TEXT NOT NULL,
                symbol TEXT,
                position_id TEXT,
                reversed_event_id TEXT,
                lines_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                UNIQUE (account_id, venue, external_id)
            )
            """
        )

    def append(self, event: LedgerEvent) -> None:
        if self.fail_appends:
            self.fail_appends -= 1
            raise LedgerImbalanceError("账本磁盘提交失败，内存保持原样")
        lines = [
            {
                "asset": line.asset,
                "bucket": line.bucket.value,
                "debit": format(line.debit, "f"),
                "credit": format(line.credit, "f"),
                "note": line.note,
            }
            for line in event.lines
        ]
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """
                INSERT INTO ledger_events(
                    event_id, account_id, venue, external_id, event_type, event_time, received_at,
                    asset, source, symbol, position_id, reversed_event_id, lines_json, metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event.event_id,
                    event.account_id,
                    event.venue,
                    event.external_id,
                    event.event_type.value,
                    event.event_time.isoformat(),
                    event.received_at.isoformat(),
                    event.asset,
                    event.source,
                    event.symbol,
                    event.position_id,
                    event.reversed_event_id,
                    json.dumps(lines, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(event.metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            )
            self._conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            self._conn.execute("ROLLBACK")
            raise ConflictError(f"账本已存在不同流水: {(event.venue, event.external_id)}") from exc
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def load(self, account_id: str) -> list[LedgerEvent]:
        rows = self._conn.execute(
            """
            SELECT event_id, account_id, venue, external_id, event_type, event_time, received_at,
                   asset, source, symbol, position_id, reversed_event_id, lines_json, metadata_json
            FROM ledger_events WHERE account_id=? ORDER BY seq
            """,
            (account_id,),
        ).fetchall()
        return [_event_from_row(row) for row in rows]


def _event_from_row(row: tuple) -> LedgerEvent:
    lines = tuple(
        JournalLine(
            asset=item["asset"],
            bucket=AccountBucket(item["bucket"]),
            debit=D(item["debit"]),
            credit=D(item["credit"]),
            note=item.get("note", ""),
        )
        for item in json.loads(row[12])
    )
    return LedgerEvent(
        event_id=row[0],
        account_id=row[1],
        venue=row[2],
        external_id=row[3],
        event_type=LedgerEventType(row[4]),
        event_time=datetime.fromisoformat(row[5]),
        received_at=datetime.fromisoformat(row[6]),
        asset=row[7],
        source=row[8],
        lines=lines,
        symbol=row[9],
        position_id=row[10],
        metadata=json.loads(row[13]),
        reversed_event_id=row[11],
    )


def _capture_position(pos: PositionState) -> tuple:
    return (
        pos.side,
        pos.qty,
        pos.avg_price,
        pos.multiplier,
        pos.isolated_margin,
        pos.realized_pnl_cum,
        pos.funding_cum,
        pos.version,
    )


def _restore_position(pos: PositionState, snapshot: tuple) -> None:
    (
        pos.side,
        pos.qty,
        pos.avg_price,
        pos.multiplier,
        pos.isolated_margin,
        pos.realized_pnl_cum,
        pos.funding_cum,
        pos.version,
    ) = snapshot


def _require_same_lines(existing: LedgerEvent, event_type: LedgerEventType, lines: tuple[JournalLine, ...]) -> None:
    if existing.event_type != event_type or existing.lines != lines:
        raise ConflictError(f"重复流水内容冲突: {(existing.venue, existing.external_id)}")


def _restore_position_from_fill(ledger: Ledger, event: LedgerEvent) -> None:
    meta = event.metadata
    symbol = event.symbol
    if symbol is None or "qty" not in meta:
        return
    qty = D(meta["qty"])
    price = D(meta["price"])
    multiplier = D(meta["multiplier"])
    side = Side(meta["side"])
    realized = D(meta.get("realized", "0"))
    pos = ledger.position(symbol, multiplier=multiplier)
    note = meta.get("note", "open")
    if note in ("open", "add"):
        if pos.qty == 0:
            pos.side = side
            pos.qty = qty
            pos.avg_price = price
            pos.multiplier = multiplier
        else:
            new_qty = pos.qty + qty
            pos.avg_price = (pos.qty * pos.avg_price + qty * price) / new_qty
            pos.qty = new_qty
    else:
        pos.qty -= qty
        pos.realized_pnl_cum += realized
        if realized != 0:
            pos.isolated_margin += realized
        if pos.qty == 0:
            pos.side = None
            pos.avg_price = Decimal("0")
    pos.version += 1

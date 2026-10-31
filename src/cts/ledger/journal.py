"""可对账资金账本：事件与分录原子落库，原始流水只追加。"""

from __future__ import annotations

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
    def __init__(self, account_id: str, asset: str = "USDT") -> None:
        self.account_id = account_id
        self.asset = asset
        self._events: list[LedgerEvent] = []
        self._by_external: dict[tuple[str, str], LedgerEvent] = {}
        self._books = AccountBooks()
        self._positions: dict[str, PositionState] = {}

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
        if event.event_type in (LedgerEventType.TRANSFER_IN, LedgerEventType.TRANSFER_OUT):
            self._apply_share_mutation(event)
        for line in event.lines:
            bal_key = (line.asset, line.bucket)
            self._books.balances[bal_key] = self._books.balances.get(bal_key, Decimal("0")) + line.signed
        self._events.append(event)
        self._by_external[key] = event
        if not self._has_open_position():
            self._refresh_nav(unrealized=Decimal("0"))

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
    ) -> tuple[LedgerEvent, Decimal]:
        key = (venue, external_id)
        existing = self._by_external.get(key)
        if existing is not None:
            return existing, D(existing.metadata.get("realized", "0"))

        qty = require_positive(D(qty), "qty")
        price = require_positive(D(price), "price")
        multiplier = require_positive(D(multiplier), "multiplier")
        pos = self.position(symbol, multiplier=multiplier)
        realized = Decimal("0")
        note = "open"

        if pos.qty == 0:
            if reduce_only:
                raise UnitValidationError("reduceOnly 成交但当前无仓")
            pos.side = side
            pos.qty = qty
            pos.avg_price = price
            pos.multiplier = multiplier
        elif pos.side == side:
            if not allow_add:
                raise UnitValidationError("T1 不加仓；如为外部成交需单独入账")
            new_qty = pos.qty + qty
            pos.avg_price = (pos.qty * pos.avg_price + qty * price) / new_qty
            pos.qty = new_qty
            note = "add"
        else:
            if qty > pos.qty:
                raise UnitValidationError("禁止用超量平仓单直接翻仓")
            delta = price - pos.avg_price
            signed = delta if pos.side == Side.LONG else -delta
            realized = signed * qty * pos.multiplier
            pos.qty -= qty
            pos.realized_pnl_cum += realized
            note = "reduce"
            if pos.qty == 0:
                pos.side = None
                pos.avg_price = Decimal("0")
        pos.version += 1

        lines: tuple[JournalLine, ...] = tuple()
        if realized != 0:
            target = AccountBucket.ISOLATED_MARGIN if credit_isolated else AccountBucket.WALLET
            if realized > 0:
                lines = _lines(self.asset, target, AccountBucket.REALIZED_PNL, realized, "realized_pnl")
            else:
                loss = -realized
                available = self.balance(target)
                if available < loss and target == AccountBucket.ISOLATED_MARGIN:
                    raise LedgerImbalanceError("已实现亏损超过逐仓保证金，禁止自动追加")
                if available < loss:
                    raise LedgerImbalanceError("已实现亏损超过钱包余额")
                lines = _lines(self.asset, AccountBucket.REALIZED_PNL, target, loss, "realized_pnl")
            if credit_isolated:
                pos.isolated_margin += realized
                if pos.isolated_margin < 0:
                    raise LedgerImbalanceError("逐仓保证金不能为负")

        event = LedgerEvent(
            event_id=uuid4().hex,
            account_id=self.account_id,
            venue=venue,
            event_type=LedgerEventType.FILL,
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
            },
        )
        return self.append_event(event), realized

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
        existing = self._by_external.get((venue, external_id))
        if existing is not None:
            return existing
        src = AccountBucket.ISOLATED_MARGIN if from_isolated else AccountBucket.WALLET
        if self.balance(src) < amount:
            raise LedgerImbalanceError("手续费扣款账户余额不足")
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
                lines=_lines(self.asset, AccountBucket.FEE, src, amount, "fee"),
            )
        )
        if from_isolated and symbol:
            pos = self.position(symbol)
            pos.isolated_margin -= amount
            if pos.isolated_margin < 0:
                raise LedgerImbalanceError("手续费扣除后逐仓保证金为负")
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
        existing = self._by_external.get((venue, external_id))
        if existing is not None:
            return existing
        src = AccountBucket.ISOLATED_MARGIN if from_isolated else AccountBucket.WALLET
        if payment > 0:
            lines = _lines(self.asset, src, AccountBucket.FUNDING, payment, "funding_receive")
        else:
            if self.balance(src) < -payment:
                raise LedgerImbalanceError("资金费扣款账户余额不足")
            lines = _lines(self.asset, AccountBucket.FUNDING, src, -payment, "funding_pay")
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
        pos = self.position(symbol)
        pos.funding_cum += payment
        if from_isolated:
            pos.isolated_margin += payment
            if pos.isolated_margin < 0:
                raise LedgerImbalanceError("资金费扣除后逐仓保证金为负，保持阻断")
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
            if event.event_type == LedgerEventType.FILL and event.symbol:
                _restore_position_from_fill(clone, event)
            if event.event_type == LedgerEventType.FUNDING and event.symbol:
                clone.position(event.symbol).funding_cum += D(event.metadata.get("payment", "0"))
            if event.event_type == LedgerEventType.MARGIN_LOCK and event.symbol:
                amt = sum(l.debit for l in event.lines if l.bucket == AccountBucket.ISOLATED_MARGIN)
                clone.position(event.symbol).isolated_margin += amt
            if event.event_type == LedgerEventType.MARGIN_RELEASE and event.symbol:
                amt = sum(l.credit for l in event.lines if l.bucket == AccountBucket.ISOLATED_MARGIN)
                clone.position(event.symbol).isolated_margin -= amt
            if event.symbol and event.event_type == LedgerEventType.FUNDING and event.metadata.get("from_isolated") == "True":
                clone.position(event.symbol).isolated_margin += D(event.metadata["payment"])
        clone._books.reserved_budget = self._books.reserved_budget
        clone._books.high_water_mark = max(clone._books.high_water_mark, self._books.high_water_mark)
        return clone

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

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


REVERSIBLE_FLOW_EVENTS = (
    LedgerEventType.MARGIN_LOCK,
    LedgerEventType.MARGIN_RELEASE,
    LedgerEventType.FEE,
    LedgerEventType.FUNDING,
)


def _isolated_debit(event: LedgerEvent) -> Decimal:
    return sum(
        (line.debit for line in event.lines if line.bucket == AccountBucket.ISOLATED_MARGIN),
        Decimal("0"),
    )


def _isolated_credit(event: LedgerEvent) -> Decimal:
    return sum(
        (line.credit for line in event.lines if line.bucket == AccountBucket.ISOLATED_MARGIN),
        Decimal("0"),
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
        self._event_seqs: dict[str, int] = {}
        self._last_nav_refresh_seq = 0
        self._nav_observation_seq: Optional[int] = None
        self._store: Optional[LedgerStore] = LedgerStore(path) if path else None
        if self._store is not None:
            for seq, event in self._store.load(account_id):
                self._load_event(event, seq=seq)
            self._overlay_books_state(self._store)

    def append_event(
        self,
        event: LedgerEvent,
        *,
        observation: Optional[tuple[Decimal, Decimal]] = None,
    ) -> LedgerEvent:
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
        self.post_journal(event, observation=observation)
        return event

    def post_journal(
        self,
        event: LedgerEvent,
        *,
        observation: Optional[tuple[Decimal, Decimal]] = None,
    ) -> None:
        key = (event.venue, event.external_id)
        if key in self._by_external:
            raise ConflictError("post_journal 收到已存在事件，应走 append_event 幂等路径")
        self._assert_share_ok(event)
        seq: Optional[int] = None
        if self._store is not None and not self._loading:
            seq = self._store.append(event, observation=observation)
        if event.event_type in (LedgerEventType.TRANSFER_IN, LedgerEventType.TRANSFER_OUT):
            self._apply_share_mutation(event)
        for line in event.lines:
            bal_key = (line.asset, line.bucket)
            self._books.balances[bal_key] = self._books.balances.get(bal_key, Decimal("0")) + line.signed
        self._events.append(event)
        self._by_external[key] = event
        if seq is None:
            seq = self._event_seqs.get(event.event_id)
        if seq is None:
            seq = len(self._events)
        self._event_seqs[event.event_id] = seq
        if observation is not None and not self._loading:
            unit_nav, high_water_mark = observation
            self._books.last_unit_nav = unit_nav
            self._books.high_water_mark = high_water_mark
            self._nav_observation_seq = seq
        elif not self._loading and not self._has_open_position():
            self._refresh_nav(unrealized=Decimal("0"), seq=seq)

    def _load_event(self, event: LedgerEvent, *, seq: Optional[int] = None) -> None:
        if seq is not None:
            self._event_seqs[event.event_id] = seq
        self._loading = True
        try:
            self.post_journal(event)
            self._project_position(event)
            if not self._has_open_position():
                self._refresh_nav(unrealized=Decimal("0"), seq=self._event_seqs.get(event.event_id, len(self._events)))
        finally:
            self._loading = False

    def _overlay_books_state(self, store: LedgerStore) -> None:
        state = store.load_books_state(self.account_id)
        if state is None:
            return
        anchor_seq, last_unit_nav, high_water_mark = state
        max_seq = store.max_event_seq(self.account_id)
        if anchor_seq > max_seq:
            # 锚点不属于本账户当前事件范围，整行不可信，按事件推导
            return
        if high_water_mark > self._books.high_water_mark:
            self._books.high_water_mark = high_water_mark
        # 刷新时点与锚点都是 ledger_events.seq，不用本账户事件数。
        # 锚点之后若没有新的净值刷新，最后一次观察仍然有效，但时点保持原锚点。
        # 平仓等后续刷新产生的净值更新，不用旧观察覆盖，也不把旧锚点改到后续事件。
        if anchor_seq == max_seq or self._last_nav_refresh_seq <= anchor_seq:
            self._books.last_unit_nav = last_unit_nav
            self._nav_observation_seq = anchor_seq

    def _nav_for_share_mutation(self, event: LedgerEvent) -> Decimal:
        nav_text = event.metadata.get("nav_at_issue")
        if nav_text is not None:
            return D(nav_text)
        if self._books.shares > 0:
            return self._books.last_unit_nav
        return Decimal("1")

    def _assert_share_ok(self, event: LedgerEvent) -> None:
        if event.event_type not in (LedgerEventType.TRANSFER_IN, LedgerEventType.TRANSFER_OUT):
            return
        amount = sum(line.debit - line.credit for line in event.lines if line.bucket == AccountBucket.WALLET)
        if self._books.shares == 0:
            if amount <= 0:
                raise UnitValidationError("空账本不能先出金")
            return
        nav = self._nav_for_share_mutation(event)
        if nav <= 0:
            raise LedgerImbalanceError("单位净值为零或为负，暂停出入金，先冲正或人工处理")
        if amount < 0:
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
        observation = None
        nav_text = format(self._books.last_unit_nav, "f")
        if (venue, external_id) not in self._by_external:
            observation = self._quote_transfer_observation(mark_prices=mark_prices)
            if observation is not None:
                nav_text = format(observation[0], "f")
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
                metadata={"nav_at_issue": nav_text},
            ),
            observation=observation,
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
        observation = None
        nav_text = format(self._books.last_unit_nav, "f")
        if (venue, external_id) not in self._by_external:
            if self.balance(AccountBucket.WALLET) < amount:
                raise LedgerImbalanceError("可用钱包余额不足以出金")
            observation = self._quote_transfer_observation(mark_prices=mark_prices)
            if observation is not None:
                nav_text = format(observation[0], "f")
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
                metadata={"nav_at_issue": nav_text},
            ),
            observation=observation,
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
        annotations: Optional[dict[str, str]] = None,
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
            for meta_key, meta_value in (annotations or {}).items():
                prior = existing.metadata.get(meta_key, "")
                if meta_value and prior and prior != meta_value:
                    raise ConflictError(f"重复流水链路冲突: {key}")
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
                "credit_isolated": str(credit_isolated),
                **({"price_source": price_source} if price_source else {}),
                **({"quote_grade": quote_grade} if quote_grade else {}),
                **({"price_event_time": price_event_time.isoformat()} if price_event_time is not None else {}),
                **{key: value for key, value in (annotations or {}).items() if value},
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
            prior_isolated = existing.metadata.get("from_isolated")
            if (prior_isolated is not None and prior_isolated != str(from_isolated)) or existing.symbol != symbol:
                raise ConflictError(f"重复流水内容冲突: {(venue, external_id)}")
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
                metadata={"from_isolated": str(from_isolated)},
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
            raise UnitValidationError("冲正目标不存在", reason_code="REVERSAL_TARGET_MISSING")
        if original.event_type == LedgerEventType.REVERSAL:
            raise UnitValidationError("冲正不能再次冲正，另用新记录标记", reason_code="REVERSAL_OF_REVERSAL")
        if original.event_type not in REVERSIBLE_FLOW_EVENTS:
            raise UnitValidationError(
                "冲正只覆盖划转、手续费与资金费；成交与出入金另用新记录标记",
                reason_code="REVERSAL_TARGET_NOT_FLOW",
            )
        if not original.lines:
            raise UnitValidationError("无资金分录的事件用新记录标记，不覆盖原 FILL", reason_code="REVERSAL_TARGET_NO_LINES")
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
        existing = self._by_external.get((original.venue, external_id))
        if existing is not None:
            if (
                existing.event_type == LedgerEventType.REVERSAL
                and existing.reversed_event_id == original.event_id
                and existing.lines == reversed_lines
            ):
                return existing
            raise ConflictError(f"重复流水内容冲突: {(original.venue, external_id)}")
        if any(
            event.event_type == LedgerEventType.REVERSAL and event.reversed_event_id == original.event_id
            for event in self._events
        ):
            raise UnitValidationError("原事件已被冲正，不能再次经济冲正", reason_code="REVERSAL_ALREADY_APPLIED")
        self._validate_reversal_position_effect(original)
        self._validate_reversal_balances(reversed_lines)
        event_id = uuid4().hex
        event = self.append_event(
            LedgerEvent(
                event_id=event_id,
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
        if event.event_id == event_id:
            self._apply_reversal_position_effect(original)
        return event

    def _validate_reversal_position_effect(self, original: LedgerEvent) -> None:
        if original.symbol is None:
            return
        if original.event_type == LedgerEventType.MARGIN_LOCK:
            if self.position(original.symbol).isolated_margin - _isolated_debit(original) < 0:
                raise LedgerImbalanceError(
                    "冲正后逐仓保证金为负，先处理后续占用",
                    reason_code="REVERSAL_MARGIN_UNDERFLOW",
                )
        if original.event_type == LedgerEventType.FUNDING and original.metadata.get("from_isolated") == "True":
            if self.position(original.symbol).isolated_margin - D(original.metadata.get("payment", "0")) < 0:
                raise LedgerImbalanceError(
                    "冲正后逐仓保证金为负，先处理后续占用",
                    reason_code="REVERSAL_MARGIN_UNDERFLOW",
                )

    def _validate_reversal_balances(self, reversed_lines: tuple[JournalLine, ...]) -> None:
        cash_buckets = {AccountBucket.WALLET, AccountBucket.ISOLATED_MARGIN}
        deltas: dict[tuple[str, AccountBucket], Decimal] = {}
        for line in reversed_lines:
            if line.bucket not in cash_buckets:
                continue
            key = (line.asset, line.bucket)
            deltas[key] = deltas.get(key, Decimal("0")) + line.signed
        for key, delta in deltas.items():
            if self._books.balances.get(key, Decimal("0")) + delta < 0:
                raise LedgerImbalanceError(
                    "冲正后钱包或逐仓资金为负",
                    reason_code="REVERSAL_BALANCE_UNDERFLOW",
                )

    def _apply_reversal_position_effect(self, original: LedgerEvent) -> None:
        if original.symbol is None:
            return
        pos = self.position(original.symbol)
        if original.event_type == LedgerEventType.MARGIN_LOCK:
            pos.isolated_margin -= _isolated_debit(original)
            pos.version += 1
        elif original.event_type == LedgerEventType.MARGIN_RELEASE:
            pos.isolated_margin += _isolated_credit(original)
            pos.version += 1
        elif original.event_type == LedgerEventType.FEE:
            amount = _isolated_credit(original)
            if amount > 0:
                pos.isolated_margin += amount
                pos.version += 1
        elif original.event_type == LedgerEventType.FUNDING:
            payment = D(original.metadata.get("payment", "0"))
            pos.funding_cum -= payment
            if original.metadata.get("from_isolated") == "True":
                pos.isolated_margin -= payment
            pos.version += 1

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
        value = D(amount)
        if value < 0:
            raise UnitValidationError("预留不能为负")
        self._books.reserved_budget = value

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
        next_hwm = self._books.high_water_mark if self._books.high_water_mark >= unit_nav else unit_nav
        if unit_nav != self._books.last_unit_nav or next_hwm != self._books.high_water_mark:
            if self._store is not None:
                self._store.save_books_state(
                    self.account_id,
                    last_unit_nav=unit_nav,
                    high_water_mark=next_hwm,
                )
                self._nav_observation_seq = self._store.max_event_seq(self.account_id)
            else:
                self._nav_observation_seq = len(self._events)
            self._books.last_unit_nav = unit_nav
            self._books.high_water_mark = next_hwm
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
            clone._load_event(
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
                ),
                seq=self._event_seqs.get(event.event_id),
            )
        clone._books.reserved_budget = self._books.reserved_budget
        if self._store is not None:
            clone._overlay_books_state(self._store)
        else:
            clone._books.last_unit_nav = self._books.last_unit_nav
            clone._nav_observation_seq = self._nav_observation_seq
            if self._books.high_water_mark > clone._books.high_water_mark:
                clone._books.high_water_mark = self._books.high_water_mark
        return clone

    def _project_position(self, event: LedgerEvent) -> None:
        if event.event_type in (LedgerEventType.FILL, LedgerEventType.LIQUIDATION, LedgerEventType.ADL) and event.symbol:
            _restore_position_from_fill(self, event)
        if event.event_type == LedgerEventType.FUNDING and event.symbol:
            self.position(event.symbol).funding_cum += D(event.metadata.get("payment", "0"))
        if event.event_type == LedgerEventType.MARGIN_LOCK and event.symbol:
            amt = sum(line.debit for line in event.lines if line.bucket == AccountBucket.ISOLATED_MARGIN)
            pos = self.position(event.symbol)
            pos.isolated_margin += amt
            pos.version += 1
        if event.event_type == LedgerEventType.MARGIN_RELEASE and event.symbol:
            amt = sum(line.credit for line in event.lines if line.bucket == AccountBucket.ISOLATED_MARGIN)
            pos = self.position(event.symbol)
            pos.isolated_margin -= amt
            pos.version += 1
        if event.event_type == LedgerEventType.FEE and event.symbol:
            amt = _isolated_credit(event)
            if amt > 0:
                pos = self.position(event.symbol)
                pos.isolated_margin -= amt
                pos.version += 1
        if event.event_type == LedgerEventType.REVERSAL and event.reversed_event_id:
            original = next((e for e in self._events if e.event_id == event.reversed_event_id), None)
            if original is not None:
                self._apply_reversal_position_effect(original)
        if event.symbol and event.event_type == LedgerEventType.FUNDING and event.metadata.get("from_isolated") == "True":
            pos = self.position(event.symbol)
            pos.isolated_margin += D(event.metadata["payment"])
            pos.version += 1

    def _has_open_position(self) -> bool:
        return any(pos.qty > 0 for pos in self._positions.values())

    def _quote_transfer_observation(
        self,
        *,
        mark_prices: Optional[dict[str, Decimal]],
    ) -> Optional[tuple[Decimal, Decimal]]:
        """持仓转账的估值只报价，不落盘、不改内存。提交与回滚由 append 同一事务完成。"""
        if not self._has_open_position():
            return None
        if not mark_prices:
            raise SnapshotInconsistentError("有持仓时出入金必须提供标记价，暂停收益确认")
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
        next_hwm = self._books.high_water_mark if self._books.high_water_mark >= unit_nav else unit_nav
        return unit_nav, next_hwm

    def _apply_share_mutation(self, event: LedgerEvent) -> None:
        amount = sum(line.debit - line.credit for line in event.lines if line.bucket == AccountBucket.WALLET)
        nav = self._nav_for_share_mutation(event)
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

    def _refresh_nav(self, unrealized: Decimal, *, seq: Optional[int] = None) -> None:
        if self._books.shares <= 0:
            return
        self._books.last_unit_nav = (self.cash_equity() + unrealized) / self._books.shares
        if self._books.last_unit_nav > self._books.high_water_mark:
            self._books.high_water_mark = self._books.last_unit_nav
        self._last_nav_refresh_seq = seq if seq is not None else len(self._events)
        self._nav_observation_seq = None


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
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS books_state (
                account_id TEXT PRIMARY KEY,
                anchor_seq INTEGER NOT NULL,
                last_unit_nav TEXT NOT NULL,
                high_water_mark TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ledger_reversal_once
            ON ledger_events(account_id, reversed_event_id)
            WHERE event_type = 'REVERSAL' AND reversed_event_id IS NOT NULL
            """
        )

    def save_books_state(self, account_id: str, *, last_unit_nav: Decimal, high_water_mark: Decimal) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            anchor = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM ledger_events WHERE account_id=?",
                (account_id,),
            ).fetchone()[0]
            self._conn.execute(
                """
                INSERT INTO books_state(account_id, anchor_seq, last_unit_nav, high_water_mark)
                VALUES (?,?,?,?)
                ON CONFLICT(account_id) DO UPDATE SET
                    anchor_seq=excluded.anchor_seq,
                    last_unit_nav=excluded.last_unit_nav,
                    high_water_mark=excluded.high_water_mark
                """,
                (account_id, int(anchor), format(last_unit_nav, "f"), format(high_water_mark, "f")),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def load_books_state(self, account_id: str) -> Optional[tuple[int, Decimal, Decimal]]:
        row = self._conn.execute(
            "SELECT anchor_seq, last_unit_nav, high_water_mark FROM books_state WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            anchor, nav, hwm = int(row[0]), D(row[1]), D(row[2])
            if not nav.is_finite() or not hwm.is_finite() or nav < 0 or hwm < 0:
                raise SnapshotInconsistentError(
                    f"账本状态行数值非法，拒绝覆盖净值与高水位: {account_id}",
                    reason_code="BOOKS_STATE_CORRUPT",
                )
        except SnapshotInconsistentError:
            raise
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise SnapshotInconsistentError(
                f"账本状态行损坏，拒绝覆盖净值与高水位: {account_id}",
                reason_code="BOOKS_STATE_CORRUPT",
            ) from exc
        return anchor, nav, hwm

    def max_event_seq(self, account_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM ledger_events WHERE account_id=?",
            (account_id,),
        ).fetchone()
        return int(row[0])

    def append(self, event: LedgerEvent, *, observation: Optional[tuple[Decimal, Decimal]] = None) -> int:
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
            if event.event_type == LedgerEventType.REVERSAL and event.reversed_event_id:
                prior = self._conn.execute(
                    """
                    SELECT 1 FROM ledger_events
                    WHERE account_id=? AND event_type=? AND reversed_event_id=?
                    """,
                    (event.account_id, LedgerEventType.REVERSAL.value, event.reversed_event_id),
                ).fetchone()
                if prior is not None:
                    raise UnitValidationError(
                        "原事件已被冲正，不能再次经济冲正",
                        reason_code="REVERSAL_ALREADY_APPLIED",
                    )
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
            inserted_seq = int(self._conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            if observation is not None:
                unit_nav, high_water_mark = observation
                anchor = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM ledger_events WHERE account_id=?",
                    (event.account_id,),
                ).fetchone()[0]
                self._conn.execute(
                    """
                    INSERT INTO books_state(account_id, anchor_seq, last_unit_nav, high_water_mark)
                    VALUES (?,?,?,?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        anchor_seq=excluded.anchor_seq,
                        last_unit_nav=excluded.last_unit_nav,
                        high_water_mark=excluded.high_water_mark
                    """,
                    (event.account_id, int(anchor), format(unit_nav, "f"), format(high_water_mark, "f")),
                )
            self._conn.execute("COMMIT")
            return inserted_seq
        except UnitValidationError:
            self._conn.execute("ROLLBACK")
            raise
        except sqlite3.IntegrityError as exc:
            self._conn.execute("ROLLBACK")
            if "reversed_event_id" in str(exc):
                raise UnitValidationError(
                    "原事件已被冲正，不能再次经济冲正",
                    reason_code="REVERSAL_ALREADY_APPLIED",
                ) from exc
            raise ConflictError(f"账本已存在不同流水: {(event.venue, event.external_id)}") from exc
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def load(self, account_id: str) -> list[tuple[int, LedgerEvent]]:
        rows = self._conn.execute(
            """
            SELECT seq, event_id, account_id, venue, external_id, event_type, event_time, received_at,
                   asset, source, symbol, position_id, reversed_event_id, lines_json, metadata_json
            FROM ledger_events WHERE account_id=? ORDER BY seq
            """,
            (account_id,),
        ).fetchall()
        return [(int(row[0]), _event_from_row(row[1:])) for row in rows]


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
            # 入账账户以分录为事实判定；新旧事件同规则，缺证据时不默默取默认值
            targets = {line.bucket for line in event.lines} - {AccountBucket.REALIZED_PNL}
            if targets == {AccountBucket.ISOLATED_MARGIN}:
                pos.isolated_margin += realized
            elif targets != {AccountBucket.WALLET}:
                raise LedgerImbalanceError(
                    f"成交盈亏分录无法判定入账账户: {event.external_id}",
                    reason_code="EVIDENCE_INSUFFICIENT",
                )
        if pos.qty == 0:
            pos.side = None
            pos.avg_price = Decimal("0")
    pos.version += 1

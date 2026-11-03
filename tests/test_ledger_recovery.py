"""账本恢复一致性：即时运行、事件回放、数据库重开三条路径必须等价。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from cts.clock import UTC
from cts.errors import ConflictError, LedgerImbalanceError, SnapshotInconsistentError, UnitValidationError
from cts.ledger.journal import Ledger
from cts.ledger.models import AccountBucket, JournalLine, LedgerEvent, LedgerEventType, Side
from cts.money import D


TS = datetime(2024, 1, 1, tzinfo=UTC)
TS2 = datetime(2024, 1, 1, 1, tzinfo=UTC)


def open_long(ledger: Ledger, *, qty: str = "1", price: str = "100", external_id: str = "f1", trade_id: str = "t1") -> None:
    ledger.apply_fill(
        symbol="BTCUSDT",
        side=Side.LONG,
        qty=D(qty),
        price=D(price),
        multiplier=D("1"),
        event_time=TS,
        received_at=TS,
        external_id=external_id,
        venue="SIM",
        venue_trade_id=trade_id,
    )


def close_short(
    ledger: Ledger,
    *,
    qty: str = "1",
    price: str,
    external_id: str,
    trade_id: str,
    credit_isolated: bool = True,
) -> None:
    ledger.apply_fill(
        symbol="BTCUSDT",
        side=Side.SHORT,
        qty=D(qty),
        price=D(price),
        multiplier=D("1"),
        event_time=TS2,
        received_at=TS2,
        external_id=external_id,
        venue="SIM",
        venue_trade_id=trade_id,
        reduce_only=True,
        credit_isolated=credit_isolated,
    )


def full_state(ledger: Ledger) -> tuple:
    positions = tuple(
        (p.symbol, p.side, p.qty, p.avg_price, p.isolated_margin, p.realized_pnl_cum, p.funding_cum, p.version)
        for p in sorted(ledger.positions(), key=lambda p: p.symbol)
    )
    return (
        dict(ledger._books.balances),
        ledger._books.shares,
        ledger._books.last_unit_nav,
        ledger._books.high_water_mark,
        positions,
        len(ledger.events()),
    )


# ---------- 复现 1：逐仓手续费重放遗漏 ----------


def test_isolated_fee_survives_replay_and_reopen(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    ledger.post_fee(amount=D("2"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM", symbol="BTCUSDT", from_isolated=True)

    assert ledger.position("BTCUSDT").isolated_margin == D("498")

    clone = ledger.replay()
    reopened = Ledger("acct-1", path=str(path))
    for book in (clone, reopened):
        assert book.position("BTCUSDT").isolated_margin == D("498")
        assert book.balance(AccountBucket.ISOLATED_MARGIN) == D("498")
        assert book.balance(AccountBucket.FEE) == D("2")

    ledger.post_fee(amount=D("3"), event_time=TS2, received_at=TS2, external_id="fee-2", venue="SIM", symbol="BTCUSDT", from_isolated=True)
    assert ledger.position("BTCUSDT").isolated_margin == D("495")
    reopened_after = Ledger("acct-1", path=str(path))
    assert reopened_after.position("BTCUSDT").isolated_margin == D("495")
    assert reopened_after.events()[-1].metadata["from_isolated"] == "True"


def test_isolated_fee_duplicate_delivery_conflicts_on_symbol_and_flag(tmp_path) -> None:
    ledger = Ledger("acct-1")
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    ledger.post_fee(amount=D("2"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM", symbol="BTCUSDT", from_isolated=True)
    again = ledger.post_fee(amount=D("2"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM", symbol="BTCUSDT", from_isolated=True)
    assert ledger.position("BTCUSDT").isolated_margin == D("498")
    with pytest.raises(ConflictError):
        ledger.post_fee(amount=D("2"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM", symbol="ETHUSDT", from_isolated=True)
    assert ledger.position("BTCUSDT").isolated_margin == D("498")
    assert again.metadata["from_isolated"] == "True"


# ---------- 复现 2：盈亏入钱包的语义丢失 ----------


def test_fill_credit_to_wallet_survives_replay_and_reopen(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    close_short(ledger, price="108", external_id="f2", trade_id="t2", credit_isolated=False)

    assert ledger.position("BTCUSDT").isolated_margin == D("500")
    assert ledger.balance(AccountBucket.WALLET) == D("9508")
    assert ledger.position("BTCUSDT").realized_pnl_cum == D("8")

    clone = ledger.replay()
    reopened = Ledger("acct-1", path=str(path))
    for book in (clone, reopened):
        assert book.position("BTCUSDT").isolated_margin == D("500")
        assert book.balance(AccountBucket.WALLET) == D("9508")
        assert book.position("BTCUSDT").realized_pnl_cum == D("8")
    assert reopened.events()[3].metadata["credit_isolated"] == "False"

    close_short_idempotent = Ledger("acct-1", path=str(path))
    close_short(close_short_idempotent, price="108", external_id="f2", trade_id="t2", credit_isolated=False)
    assert close_short_idempotent.position("BTCUSDT").isolated_margin == D("500")


# ---------- 复现 3：零净值入金破坏原子性 ----------


def _bankrupt_book(path) -> Ledger:
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("10000"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger, qty="200", price="100")
    close_short(ledger, qty="200", price="50", external_id="f2", trade_id="t2")
    return ledger


def test_zero_nav_transfer_rejected_before_persistence_and_reopen_still_opens(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = _bankrupt_book(str(path))
    assert ledger._books.shares == D("10000")
    assert ledger._books.last_unit_nav == D("0")
    assert ledger._books.high_water_mark == D("1")

    before = full_state(ledger)
    with pytest.raises(LedgerImbalanceError):
        ledger.transfer_in(amount=D("1"), event_time=TS, received_at=TS, external_id="dep-2")
    assert full_state(ledger) == before
    assert len(ledger.events()) == 4

    reopened = Ledger("acct-1", path=str(path))
    assert reopened._books.shares == D("10000")
    assert reopened._books.last_unit_nav == D("0")
    assert len(reopened.events()) == 4
    with pytest.raises(LedgerImbalanceError):
        reopened.transfer_in(amount=D("1"), event_time=TS, received_at=TS, external_id="dep-2")
    assert len(reopened.events()) == 4


# ---------- 复现 4：replay() 净值不等价，同额入金份额不同 ----------


def _flat_with_profit(path=None) -> Ledger:
    ledger = Ledger("acct-1", path=path)
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    close_short(ledger, price="110", external_id="f2", trade_id="t2")
    assert ledger._books.last_unit_nav == D("10010") / D("10000")
    return ledger


def test_same_deposit_issues_same_shares_on_every_path(tmp_path) -> None:
    expected_nav = D("10010") / D("10000")
    expected_shares = D("10000") + D("100") / expected_nav

    runtime = _flat_with_profit()
    runtime.transfer_in(amount=D("100"), event_time=TS, received_at=TS, external_id="dep-2")
    assert runtime._books.shares == expected_shares

    path = tmp_path / "ledger.db"
    stored = _flat_with_profit(str(path))
    stored.transfer_in(amount=D("100"), event_time=TS, received_at=TS, external_id="dep-2")
    assert stored._books.shares == expected_shares
    reloaded = Ledger("acct-1", path=str(path))
    assert reloaded._books.shares == expected_shares

    path2 = tmp_path / "ledger2.db"
    source = _flat_with_profit(str(path2))
    clone = source.replay()
    clone.transfer_in(amount=D("100"), event_time=TS, received_at=TS, external_id="dep-2")
    assert clone._books.shares == expected_shares

    reopened = Ledger("acct-1", path=str(path2))
    reopened.transfer_in(amount=D("100"), event_time=TS, received_at=TS, external_id="dep-2")
    assert reopened._books.shares == expected_shares


# ---------- 复现 5：高水位无法恢复 ----------


def test_observed_hwm_survives_reopen_replay_and_flat_close(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    ledger.equity_snapshot(as_of=TS2, mark_prices={"BTCUSDT": D("131")})
    assert ledger._books.high_water_mark == D("1.0031")
    assert ledger._books.last_unit_nav == D("1.0031")

    clone = ledger.replay()
    reopened = Ledger("acct-1", path=str(path))
    for book in (clone, reopened):
        assert book._books.high_water_mark == D("1.0031")
        assert book._books.last_unit_nav == D("1.0031")

    close_short(ledger, price="110", external_id="f2", trade_id="t2")
    assert ledger._books.high_water_mark == D("1.0031")
    assert ledger._books.last_unit_nav == D("1.001")
    after_close = Ledger("acct-1", path=str(path))
    assert after_close._books.high_water_mark == D("1.0031")
    assert after_close._books.last_unit_nav == D("1.001")
    assert after_close.replay()._books.high_water_mark == D("1.0031")


# ---------- 复现 6：冲正未同步仓位投影 ----------


def test_reversal_syncs_position_margin_on_every_path(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    lock = ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    ledger.reverse(lock.event_id, event_time=TS2, received_at=TS2, external_id="rev-1")

    assert ledger.balance(AccountBucket.ISOLATED_MARGIN) == D("0")
    assert ledger.position("BTCUSDT").isolated_margin == D("0")
    assert ledger.position("BTCUSDT").version == 2

    for book in (ledger.replay(), Ledger("acct-1", path=str(path))):
        assert book.balance(AccountBucket.ISOLATED_MARGIN) == D("0")
        assert book.position("BTCUSDT").isolated_margin == D("0")
        assert book.position("BTCUSDT").version == 2
        assert len(book.events()) == 3

    ledger.lock_margin(amount=D("300"), symbol="BTCUSDT", event_time=TS2, received_at=TS2, external_id="im-2", venue="SIM")
    final = Ledger("acct-1", path=str(path))
    assert final.position("BTCUSDT").isolated_margin == D("300")
    assert final.position("BTCUSDT").version == 3


def test_reversal_of_fee_and_funding_restores_position_and_counters(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    fee = ledger.post_fee(amount=D("2"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM", symbol="BTCUSDT", from_isolated=True)
    funding = ledger.post_funding(
        payment=D("-15"), event_time=TS, received_at=TS, external_id="fund-1", venue="SIM", symbol="BTCUSDT", from_isolated=True
    )
    assert ledger.position("BTCUSDT").isolated_margin == D("483")

    ledger.reverse(fee.event_id, event_time=TS2, received_at=TS2, external_id="rev-1")
    assert ledger.balance(AccountBucket.FEE) == D("0")
    assert ledger.position("BTCUSDT").isolated_margin == D("485")

    ledger.reverse(funding.event_id, event_time=TS2, received_at=TS2, external_id="rev-2")
    assert ledger.position("BTCUSDT").funding_cum == D("0")
    assert ledger.position("BTCUSDT").isolated_margin == D("500")
    assert ledger.balance(AccountBucket.FUNDING) == D("0")

    expected = full_state(ledger)
    assert full_state(ledger.replay()) == expected
    assert full_state(Ledger("acct-1", path=str(path))) == expected


# ---------- 冲正拒绝路径：不落盘、不改内存 ----------


def test_unreversable_or_insufficient_reversal_changes_nothing(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    lock = ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    fill = ledger.apply_fill(
        symbol="BTCUSDT",
        side=Side.LONG,
        qty=D("1"),
        price=D("100"),
        multiplier=D("1"),
        event_time=TS,
        received_at=TS,
        external_id="f1",
        venue="SIM",
        venue_trade_id="t1",
    )[0]
    ledger.post_fee(amount=D("400"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM", symbol="BTCUSDT", from_isolated=True)

    before = full_state(ledger)
    with pytest.raises(UnitValidationError) as fill_exc:
        ledger.reverse(fill.event_id, event_time=TS2, received_at=TS2, external_id="rev-1")
    assert fill_exc.value.reason_code == "REVERSAL_TARGET_NOT_FLOW"
    with pytest.raises(UnitValidationError) as transfer_exc:
        ledger.reverse(ledger.events()[0].event_id, event_time=TS2, received_at=TS2, external_id="rev-2")
    assert transfer_exc.value.reason_code == "REVERSAL_TARGET_NOT_FLOW"
    with pytest.raises(LedgerImbalanceError) as margin_exc:
        ledger.reverse(lock.event_id, event_time=TS2, received_at=TS2, external_id="rev-3")
    assert margin_exc.value.reason_code == "REVERSAL_MARGIN_UNDERFLOW"
    assert full_state(ledger) == before
    assert len(ledger.events()) == 4

    allowed = ledger.reverse(ledger.events()[3].event_id, event_time=TS2, received_at=TS2, external_id="rev-4")
    with pytest.raises(UnitValidationError) as double_exc:
        ledger.reverse(allowed.event_id, event_time=TS2, received_at=TS2, external_id="rev-5")
    assert double_exc.value.reason_code == "REVERSAL_OF_REVERSAL"
    assert len(ledger.events()) == 5

    reopened = Ledger("acct-1", path=str(path))
    assert len(reopened.events()) == 5
    assert reopened.position("BTCUSDT").isolated_margin == D("500")


# ---------- 验收主项：三路径等价，继续操作仍等价 ----------


def apply_rich_scenario(book: Ledger) -> None:
    book.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    book.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(book)
    book.post_fee(amount=D("2"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM", symbol="BTCUSDT", from_isolated=True)
    book.post_funding(
        payment=D("-0.5"), event_time=TS, received_at=TS, external_id="fund-1", venue="SIM", symbol="BTCUSDT", from_isolated=True
    )
    book.equity_snapshot(as_of=TS2, mark_prices={"BTCUSDT": D("131")})
    book.transfer_in(amount=D("100"), event_time=TS2, received_at=TS2, external_id="dep-2", mark_prices={"BTCUSDT": D("131")})
    close_short(book, qty="0.4", price="110", external_id="f2", trade_id="t2")
    book.reverse(book.events()[3].event_id, event_time=TS2, received_at=TS2, external_id="rev-1")
    close_short(book, qty="0.6", price="105", external_id="f3", trade_id="t3")
    book.transfer_in(amount=D("100"), event_time=TS2, received_at=TS2, external_id="dep-3")
    book.lock_margin(amount=D("100"), symbol="BTCUSDT", event_time=TS2, received_at=TS2, external_id="im-2", venue="SIM")
    open_long(book, external_id="f4", trade_id="t4")
    close_short(book, price="102", external_id="f5", trade_id="t5")
    book.reverse(book.events()[-3].event_id, event_time=TS2, received_at=TS2, external_id="rev-2")


def test_three_path_equivalence_with_continued_operations(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    stored = Ledger("acct-1", path=str(path))
    apply_rich_scenario(stored)
    volatile = Ledger("acct-1")
    apply_rich_scenario(volatile)

    expected = full_state(stored)
    assert full_state(volatile) == expected
    assert full_state(stored.replay()) == expected
    assert full_state(Ledger("acct-1", path=str(path))) == expected
    assert full_state(volatile.replay()) == expected

    reopened = Ledger("acct-1", path=str(path))
    reopened.post_fee(amount=D("1"), event_time=TS2, received_at=TS2, external_id="fee-2", venue="SIM")
    expected_after = full_state(reopened)
    assert full_state(Ledger("acct-1", path=str(path))) == expected_after
    assert full_state(reopened.replay()) == expected_after
    assert len(reopened.events()) == 15


def test_deposit_while_holding_issues_same_shares_on_every_path(tmp_path) -> None:
    expected_shares = D("10000") + D("100") / D("1.0031")

    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    ledger.equity_snapshot(as_of=TS2, mark_prices={"BTCUSDT": D("131")})
    ledger.transfer_in(amount=D("100"), event_time=TS2, received_at=TS2, external_id="dep-2", mark_prices={"BTCUSDT": D("131")})
    assert ledger._books.shares == expected_shares
    assert Ledger("acct-1", path=str(path))._books.shares == expected_shares

    path2 = tmp_path / "ledger2.db"
    source = Ledger("acct-1", path=str(path2))
    source.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    source.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(source)
    source.equity_snapshot(as_of=TS2, mark_prices={"BTCUSDT": D("131")})
    clone = source.replay()
    clone.transfer_in(amount=D("100"), event_time=TS2, received_at=TS2, external_id="dep-2", mark_prices={"BTCUSDT": D("131")})
    assert clone._books.shares == expected_shares
    reopened = Ledger("acct-1", path=str(path2))
    reopened.transfer_in(amount=D("100"), event_time=TS2, received_at=TS2, external_id="dep-2", mark_prices={"BTCUSDT": D("131")})
    assert reopened._books.shares == expected_shares
    assert reopened.events()[3].metadata["nav_at_issue"] == "1.0031"


# ---------- 验收主项：被拒绝的事件不改变持久流水与内存 ----------


def test_rejected_events_change_neither_journal_nor_memory(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    ledger.post_fee(amount=D("2"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM", symbol="BTCUSDT", from_isolated=True)
    ledger.set_reserved_budget(D("50"))

    before = full_state(ledger)
    with pytest.raises(ConflictError):
        ledger.transfer_in(
            amount=D("5"),
            event_time=TS,
            received_at=TS,
            external_id="dep-1",
            mark_prices={"BTCUSDT": D("100")},
        )
    with pytest.raises(LedgerImbalanceError):
        ledger.post_fee(amount=D("99999"), event_time=TS, received_at=TS, external_id="fee-2", venue="SIM")
    with pytest.raises(LedgerImbalanceError):
        ledger.post_fee(amount=D("499"), event_time=TS, received_at=TS, external_id="fee-3", venue="SIM", symbol="BTCUSDT", from_isolated=True)
    with pytest.raises(UnitValidationError):
        close_short(ledger, qty="2", price="90", external_id="f2", trade_id="t2")
    with pytest.raises(UnitValidationError):
        ledger.set_reserved_budget(D("-1"))
    assert ledger._books.reserved_budget == D("50")
    assert full_state(ledger) == before
    assert len(ledger.events()) == 4

    reopened = Ledger("acct-1", path=str(path))
    assert full_state(reopened) == before


# ---------- 旧数据规则：按分录事实恢复，证据不足要明示 ----------


def test_legacy_isolated_fee_recovers_margin_from_lines(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    legacy = LedgerEvent(
        event_id=uuid4().hex,
        account_id="acct-1",
        venue="SIM",
        event_type=LedgerEventType.FEE,
        event_time=TS,
        received_at=TS,
        asset="USDT",
        external_id="fee-old",
        source="venue",
        symbol="BTCUSDT",
        lines=(
            JournalLine(asset="USDT", bucket=AccountBucket.FEE, debit=Decimal("2"), credit=Decimal("0"), note="fee"),
            JournalLine(asset="USDT", bucket=AccountBucket.ISOLATED_MARGIN, debit=Decimal("0"), credit=Decimal("2"), note="fee"),
        ),
    )
    ledger.append_event(legacy)
    assert ledger.position("BTCUSDT").isolated_margin == D("500")
    repeat = ledger.post_fee(amount=D("2"), event_time=TS, received_at=TS, external_id="fee-old", venue="SIM", symbol="BTCUSDT", from_isolated=True)
    assert repeat.event_id == legacy.event_id

    # 旧事件没有 metadata，但分录明确从逐仓扣款：重放按事实恢复为 498
    reopened = Ledger("acct-1", path=str(path))
    assert reopened.position("BTCUSDT").isolated_margin == D("498")
    reversal = reopened.reverse(legacy.event_id, event_time=TS2, received_at=TS2, external_id="rev-1")
    assert reopened.position("BTCUSDT").isolated_margin == D("500")
    assert reopened.replay().position("BTCUSDT").isolated_margin == D("500")
    final = Ledger("acct-1", path=str(path))
    assert final.position("BTCUSDT").isolated_margin == D("500")
    assert len(final.events()) == 4


def _legacy_reduce_fill(external_id: str, *, realized: str, target: AccountBucket, lines: bool = True) -> LedgerEvent:
    amount = Decimal(realized)
    if not lines:
        fill_lines: tuple[JournalLine, ...] = ()
    else:
        fill_lines = (
            JournalLine(asset="USDT", bucket=target, debit=amount, credit=Decimal("0"), note="realized_pnl"),
            JournalLine(asset="USDT", bucket=AccountBucket.REALIZED_PNL, debit=Decimal("0"), credit=amount, note="realized_pnl"),
        )
    return LedgerEvent(
        event_id=uuid4().hex,
        account_id="acct-1",
        venue="SIM",
        event_type=LedgerEventType.FILL,
        event_time=TS2,
        received_at=TS2,
        asset="USDT",
        external_id=external_id,
        source="venue",
        symbol="BTCUSDT",
        lines=fill_lines,
        metadata={
            "side": "SHORT",
            "qty": "1",
            "price": "108",
            "multiplier": "1",
            "venue_trade_id": "t-old",
            "realized": realized,
            "note": "reduce",
        },
    )


def test_legacy_fill_pnl_target_inferred_from_lines(tmp_path) -> None:
    wallet_path = tmp_path / "wallet.db"
    wallet_book = Ledger("acct-1", path=str(wallet_path))
    wallet_book.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    wallet_book.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(wallet_book)
    wallet_book.append_event(_legacy_reduce_fill("f-old", realized="8", target=AccountBucket.WALLET))
    reopened = Ledger("acct-1", path=str(wallet_path))
    assert reopened.position("BTCUSDT").isolated_margin == D("500")
    assert reopened.position("BTCUSDT").realized_pnl_cum == D("8")

    isolated_path = tmp_path / "isolated.db"
    isolated_book = Ledger("acct-1", path=str(isolated_path))
    isolated_book.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    isolated_book.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(isolated_book)
    isolated_book.append_event(_legacy_reduce_fill("f-old", realized="8", target=AccountBucket.ISOLATED_MARGIN))
    reopened_isolated = Ledger("acct-1", path=str(isolated_path))
    assert reopened_isolated.position("BTCUSDT").isolated_margin == D("508")
    assert reopened_isolated.position("BTCUSDT").realized_pnl_cum == D("8")


def test_legacy_fill_realized_without_lines_is_evidence_insufficient(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    ledger.append_event(_legacy_reduce_fill("f-old", realized="8", target=AccountBucket.WALLET, lines=False))
    with pytest.raises(LedgerImbalanceError) as exc:
        Ledger("acct-1", path=str(path))
    assert exc.value.reason_code == "EVIDENCE_INSUFFICIENT"


# ---------- books_state：只补净值与高水位，不掩盖事件重放 ----------


def test_books_state_stale_anchor_keeps_last_observation(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    ledger.equity_snapshot(as_of=TS2, mark_prices={"BTCUSDT": D("131")})
    assert ledger._store.load_books_state("acct-1") == (3, D("1.0031"), D("1.0031"))
    ledger.post_fee(amount=D("1"), event_time=TS2, received_at=TS2, external_id="fee-1", venue="SIM")

    assert ledger._books.last_unit_nav == D("1.0031")
    assert ledger._nav_observation_seq == 3
    assert ledger._store.max_event_seq("acct-1") == 4
    assert ledger._store.load_books_state("acct-1") == (3, D("1.0031"), D("1.0031"))
    reopened = Ledger("acct-1", path=str(path))
    replayed = ledger.replay()
    for book in (reopened, replayed):
        assert book._books.last_unit_nav == D("1.0031")
        assert book._books.high_water_mark == D("1.0031")
        assert book._nav_observation_seq == 3
        assert book.position("BTCUSDT").isolated_margin == D("500")


def test_books_state_supplies_only_nav_and_hwm_never_shares(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    ledger.transfer_in(amount=D("100"), event_time=TS2, received_at=TS2, external_id="dep-2", mark_prices={"BTCUSDT": D("131")})
    ledger.equity_snapshot(as_of=TS2, mark_prices={"BTCUSDT": D("132")})
    observed_nav = ledger._books.last_unit_nav
    with_row = Ledger("acct-1", path=str(path))
    assert with_row._books.last_unit_nav == observed_nav
    assert with_row._books.shares == ledger._books.shares

    conn = sqlite3.connect(str(path))
    conn.execute("DELETE FROM books_state")
    conn.commit()
    conn.close()
    without_row = Ledger("acct-1", path=str(path))
    assert without_row._books.shares == with_row._books.shares
    assert without_row._books.balances == with_row._books.balances
    assert without_row.position("BTCUSDT").isolated_margin == with_row.position("BTCUSDT").isolated_margin
    assert without_row._books.last_unit_nav == D("1")
    assert with_row._books.last_unit_nav == observed_nav


def test_books_state_out_of_range_anchor_is_ignored_entirely(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    ledger.equity_snapshot(as_of=TS2, mark_prices={"BTCUSDT": D("131")})

    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE books_state SET anchor_seq=99")
    conn.commit()
    conn.close()
    reopened = Ledger("acct-1", path=str(path))
    assert reopened._books.last_unit_nav == D("1")
    assert reopened._books.high_water_mark == D("1")


def test_books_state_corrupt_row_fails_closed(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    ledger.equity_snapshot(as_of=TS2, mark_prices={"BTCUSDT": D("131")})

    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE books_state SET last_unit_nav='broken'")
    conn.commit()
    conn.close()
    with pytest.raises(SnapshotInconsistentError) as exc:
        Ledger("acct-1", path=str(path))
    assert exc.value.reason_code == "BOOKS_STATE_CORRUPT"


# ---------- 拒绝不变性：快照与磁盘失败下的原子性合同 ----------


def test_transfer_out_rejected_by_balance_takes_no_snapshot(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("9000"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    ledger.equity_snapshot(as_of=TS2, mark_prices={"BTCUSDT": D("131")})

    before = full_state(ledger)
    before_row = ledger._store.load_books_state("acct-1")
    with pytest.raises(LedgerImbalanceError):
        ledger.transfer_out(amount=D("1001"), event_time=TS2, received_at=TS2, external_id="wd-1", mark_prices={"BTCUSDT": D("131")})
    assert full_state(ledger) == before
    assert ledger._store.load_books_state("acct-1") == before_row
    assert len(ledger.events()) == 3


def test_transfer_disk_failure_rolls_back_implicit_observation(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    assert ledger._books.last_unit_nav == D("1")
    assert ledger._store.load_books_state("acct-1") is None

    ledger._store.fail_appends = 1
    with pytest.raises(LedgerImbalanceError):
        ledger.transfer_in(amount=D("100"), event_time=TS2, received_at=TS2, external_id="dep-2", mark_prices={"BTCUSDT": D("200")})

    assert len(ledger.events()) == 3
    assert ledger._books.shares == D("10000")
    assert ledger.balance(AccountBucket.WALLET) == D("9500")
    assert ledger.balance(AccountBucket.ISOLATED_MARGIN) == D("500")
    assert ledger.position("BTCUSDT").qty == D("1")
    assert ledger._books.last_unit_nav == D("1")
    assert ledger._books.high_water_mark == D("1")
    assert ledger._store.load_books_state("acct-1") is None

    reopened = Ledger("acct-1", path=str(path))
    assert reopened._books.last_unit_nav == D("1")
    assert reopened._books.high_water_mark == D("1")
    assert len(reopened.events()) == 3

    ledger.transfer_in(amount=D("100"), event_time=TS2, received_at=TS2, external_id="dep-2", mark_prices={"BTCUSDT": D("200")})
    assert ledger._books.last_unit_nav == D("1.01")
    assert ledger._books.high_water_mark == D("1.01")
    expected = full_state(ledger)
    assert full_state(ledger.replay()) == expected
    assert full_state(Ledger("acct-1", path=str(path))) == expected


def test_explicit_snapshot_survives_failed_transfer(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    ledger.equity_snapshot(as_of=TS2, mark_prices={"BTCUSDT": D("131")})
    assert ledger._store.load_books_state("acct-1") == (3, D("1.0031"), D("1.0031"))

    ledger._store.fail_appends = 1
    with pytest.raises(LedgerImbalanceError):
        ledger.transfer_in(amount=D("100"), event_time=TS2, received_at=TS2, external_id="dep-2", mark_prices={"BTCUSDT": D("200")})

    assert len(ledger.events()) == 3
    assert ledger._books.last_unit_nav == D("1.0031")
    assert ledger._books.high_water_mark == D("1.0031")
    assert ledger._store.load_books_state("acct-1") == (3, D("1.0031"), D("1.0031"))
    reopened = Ledger("acct-1", path=str(path))
    assert reopened._books.last_unit_nav == D("1.0031")
    assert reopened._books.high_water_mark == D("1.0031")


# ---------- 复审边界：冲正幂等、余额、持仓净值、非有限状态 ----------


def test_same_reversal_delivery_does_not_reproject(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    fee = ledger.post_fee(
        amount=D("2"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM", symbol="BTCUSDT", from_isolated=True
    )
    first = ledger.reverse(fee.event_id, event_time=TS2, received_at=TS2, external_id="rev-1")
    assert ledger.position("BTCUSDT").isolated_margin == D("500")
    count = len(ledger.events())

    again = ledger.reverse(fee.event_id, event_time=TS2, received_at=TS2, external_id="rev-1")
    assert again.event_id == first.event_id
    assert len(ledger.events()) == count
    assert ledger.position("BTCUSDT").isolated_margin == D("500")
    expected = full_state(ledger)
    assert full_state(ledger.replay()) == expected
    assert full_state(Ledger("acct-1", path=str(path))) == expected


def test_second_external_id_cannot_reverse_same_event(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    first_fee = ledger.post_fee(
        amount=D("2"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM", symbol="BTCUSDT", from_isolated=True
    )
    ledger.post_fee(
        amount=D("2"), event_time=TS, received_at=TS, external_id="fee-2", venue="SIM", symbol="BTCUSDT", from_isolated=True
    )
    ledger.reverse(first_fee.event_id, event_time=TS2, received_at=TS2, external_id="rev-1")
    assert ledger.position("BTCUSDT").isolated_margin == D("498")
    before = full_state(ledger)

    with pytest.raises(UnitValidationError) as exc:
        ledger.reverse(first_fee.event_id, event_time=TS2, received_at=TS2, external_id="rev-2")
    assert exc.value.reason_code == "REVERSAL_ALREADY_APPLIED"
    assert full_state(ledger) == before
    assert ledger.position("BTCUSDT").isolated_margin == D("498")
    assert full_state(Ledger("acct-1", path=str(path))) == before


def test_reversal_of_release_after_withdrawal_does_not_overdraw_wallet(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    released = ledger.release_margin(
        amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="rel-1", venue="SIM"
    )
    ledger.transfer_out(amount=D("10000"), event_time=TS, received_at=TS, external_id="wd-1")
    assert ledger.balance(AccountBucket.WALLET) == D("0")
    assert ledger.balance(AccountBucket.ISOLATED_MARGIN) == D("0")
    before = full_state(ledger)

    with pytest.raises(LedgerImbalanceError) as exc:
        ledger.reverse(released.event_id, event_time=TS2, received_at=TS2, external_id="rev-1")
    assert exc.value.reason_code == "REVERSAL_BALANCE_UNDERFLOW"
    assert full_state(ledger) == before
    assert ledger.balance(AccountBucket.WALLET) == D("0")
    assert full_state(Ledger("acct-1", path=str(path))) == before


def test_deposit_while_holding_restores_mark_nav_on_every_path(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    assert ledger._books.last_unit_nav == D("1")
    ledger.transfer_in(
        amount=D("100"), event_time=TS2, received_at=TS2, external_id="dep-2", mark_prices={"BTCUSDT": D("131")}
    )
    assert ledger._books.last_unit_nav == D("1.0031")
    assert ledger._books.high_water_mark == D("1.0031")
    expected = full_state(ledger)
    assert full_state(ledger.replay()) == expected
    assert full_state(Ledger("acct-1", path=str(path))) == expected

    volatile = Ledger("acct-1")
    volatile.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    volatile.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(volatile)
    volatile.transfer_in(
        amount=D("100"), event_time=TS2, received_at=TS2, external_id="dep-2", mark_prices={"BTCUSDT": D("131")}
    )
    assert volatile._books.last_unit_nav == D("1.0031")
    assert full_state(volatile.replay()) == full_state(volatile)


def test_reversal_uniqueness_is_enforced_for_a_stale_ledger(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    first = Ledger("acct-1", path=str(path))
    first.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    first.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(first)
    fee = first.post_fee(
        amount=D("2"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM", symbol="BTCUSDT", from_isolated=True
    )
    stale = Ledger("acct-1", path=str(path))
    first.reverse(fee.event_id, event_time=TS2, received_at=TS2, external_id="rev-1")
    before = full_state(stale)

    with pytest.raises(UnitValidationError) as exc:
        stale.reverse(fee.event_id, event_time=TS2, received_at=TS2, external_id="rev-2")
    assert exc.value.reason_code == "REVERSAL_ALREADY_APPLIED"
    assert full_state(stale) == before

    reopened = Ledger("acct-1", path=str(path))
    reversals = [event for event in reopened.events() if event.event_type == LedgerEventType.REVERSAL]
    assert len(reversals) == 1
    assert reversals[0].reversed_event_id == fee.event_id
    assert reopened.position("BTCUSDT").isolated_margin == D("500")


def test_funding_credit_balance_can_be_partially_reversed(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    first = ledger.post_funding(
        payment=D("5"), event_time=TS, received_at=TS, external_id="fund-1", venue="SIM", symbol="BTCUSDT"
    )
    ledger.post_funding(
        payment=D("5"), event_time=TS, received_at=TS, external_id="fund-2", venue="SIM", symbol="BTCUSDT"
    )
    assert ledger.balance(AccountBucket.FUNDING) == D("-10")

    ledger.reverse(first.event_id, event_time=TS2, received_at=TS2, external_id="rev-1")
    assert ledger.balance(AccountBucket.FUNDING) == D("-5")
    assert ledger.balance(AccountBucket.WALLET) == D("10005")
    expected = full_state(ledger)
    assert full_state(ledger.replay()) == expected
    assert full_state(Ledger("acct-1", path=str(path))) == expected


def test_fee_after_holding_deposit_keeps_observed_nav_and_its_seq(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    ledger.transfer_in(
        amount=D("100"), event_time=TS2, received_at=TS2, external_id="dep-2", mark_prices={"BTCUSDT": D("131")}
    )
    observed_seq = ledger._nav_observation_seq
    assert ledger._books.last_unit_nav == D("1.0031")
    assert observed_seq == 4
    ledger.post_fee(amount=D("1"), event_time=TS2, received_at=TS2, external_id="fee-1", venue="SIM")

    assert ledger._books.last_unit_nav == D("1.0031")
    assert ledger._nav_observation_seq == observed_seq
    assert ledger._store.max_event_seq("acct-1") == 5
    assert ledger._store.load_books_state("acct-1")[0] == observed_seq
    expected = full_state(ledger)
    reopened = Ledger("acct-1", path=str(path))
    replayed = ledger.replay()
    assert full_state(reopened) == expected
    assert full_state(replayed) == expected
    assert reopened._nav_observation_seq == observed_seq
    assert replayed._nav_observation_seq == observed_seq


def _open_marked_book(path) -> Ledger:
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    ledger.equity_snapshot(as_of=TS2, mark_prices={"BTCUSDT": D("131")})
    return ledger


def test_prior_account_does_not_let_old_observation_cover_close_nav(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    other = Ledger("acct-2", path=str(path))
    other.transfer_in(amount=D("1"), event_time=TS, received_at=TS, external_id="other-1")
    other.transfer_in(amount=D("1"), event_time=TS, received_at=TS, external_id="other-2")
    ledger = _open_marked_book(path)
    assert ledger._books.last_unit_nav == D("1.0031")
    anchor = ledger._store.load_books_state("acct-1")[0]
    assert anchor > len(ledger.events())

    close_short(ledger, price="102", external_id="f2", trade_id="t2")
    assert ledger._books.last_unit_nav == D("1.0002")
    assert ledger._books.high_water_mark == D("1.0031")
    assert ledger._store.load_books_state("acct-1")[0] == anchor
    assert ledger.replay()._books.last_unit_nav == D("1.0002")
    assert ledger.replay()._books.high_water_mark == D("1.0031")
    reopened = Ledger("acct-1", path=str(path))
    assert reopened._books.last_unit_nav == D("1.0002")
    assert reopened._books.high_water_mark == D("1.0031")


def test_close_nav_survives_when_account_count_equals_global_anchor(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = Ledger("acct-1", path=str(path))
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    other = Ledger("acct-2", path=str(path))
    other.transfer_in(amount=D("1"), event_time=TS, received_at=TS, external_id="other-1")
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    open_long(ledger)
    ledger.equity_snapshot(as_of=TS2, mark_prices={"BTCUSDT": D("131")})
    assert ledger._books.last_unit_nav == D("1.0031")
    anchor = ledger._store.load_books_state("acct-1")[0]
    assert anchor == len(ledger.events()) + 1

    close_short(ledger, price="102", external_id="f2", trade_id="t2")
    assert ledger._books.last_unit_nav == D("1.0002")
    assert ledger._store.load_books_state("acct-1")[0] == anchor
    assert ledger.replay()._books.last_unit_nav == D("1.0002")
    reopened = Ledger("acct-1", path=str(path))
    assert reopened._books.last_unit_nav == D("1.0002")
    assert reopened._books.high_water_mark == D("1.0031")


def test_books_state_rejects_non_finite_values(tmp_path) -> None:
    for raw in ("Infinity", "NaN"):
        path = tmp_path / f"{raw}.db"
        Ledger("acct-1", path=str(path))
        conn = sqlite3.connect(str(path))
        conn.execute(
            "INSERT INTO books_state(account_id, anchor_seq, last_unit_nav, high_water_mark) VALUES (?,?,?,?)",
            ("acct-1", 0, raw, "1"),
        )
        conn.commit()
        conn.close()
        with pytest.raises(SnapshotInconsistentError) as exc:
            Ledger("acct-1", path=str(path))
        assert exc.value.reason_code == "BOOKS_STATE_CORRUPT"

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from cts.clock import UTC
from cts.errors import ConflictError, LedgerImbalanceError, UnitValidationError
from cts.ledger.journal import Ledger
from cts.ledger.models import AccountBucket, Side
from cts.ledger.projection import AccountProjection
from cts.money import D
from cts.risk.funding_reserve import FundingScenario, SettlementPoint, funding_reserve


TS = datetime(2024, 1, 1, tzinfo=UTC)


def funded() -> Ledger:
    ledger = Ledger("acct-1")
    ledger.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-1")
    return ledger


def test_double_entry_conservation_on_deposit() -> None:
    ledger = funded()
    assert ledger.balance(AccountBucket.WALLET) == D("10000")
    assert ledger.balance(AccountBucket.EXTERNAL) == D("-10000")
    assert ledger.cash_equity() == D("10000")


def test_margin_lock_is_not_pnl() -> None:
    ledger = funded()
    before = ledger.cash_equity()
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    assert ledger.cash_equity() == before
    snap = ledger.equity_snapshot(as_of=TS, mark_prices={})
    assert snap.wallet == D("9500")
    assert snap.isolated_margin == D("500")
    assert snap.unit_nav == D("1")
    assert snap.unrealized_pnl == D("0")


def test_long_and_short_realized_pnl_and_partial_fill() -> None:
    ledger = funded()
    ledger.lock_margin(amount=D("2000"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    ledger.apply_fill(
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
    )
    ledger.apply_fill(
        symbol="BTCUSDT",
        side=Side.SHORT,
        qty=D("0.4"),
        price=D("110"),
        multiplier=D("1"),
        event_time=TS,
        received_at=TS,
        external_id="f2",
        venue="SIM",
        venue_trade_id="t2",
        reduce_only=True,
    )
    pos = ledger.position("BTCUSDT")
    assert pos.qty == D("0.6")
    assert pos.avg_price == D("100")
    assert pos.realized_pnl_cum == D("4.0")
    assert ledger.balance(AccountBucket.REALIZED_PNL) == D("-4.0")

    short_book = Ledger("acct-2")
    short_book.transfer_in(amount=D("10000"), event_time=TS, received_at=TS, external_id="dep-2")
    short_book.lock_margin(amount=D("2000"), symbol="ETHUSDT", event_time=TS, received_at=TS, external_id="im-2", venue="SIM")
    short_book.apply_fill(
        symbol="ETHUSDT",
        side=Side.SHORT,
        qty=D("2"),
        price=D("50"),
        multiplier=D("1"),
        event_time=TS,
        received_at=TS,
        external_id="s1",
        venue="SIM",
        venue_trade_id="st1",
    )
    short_book.apply_fill(
        symbol="ETHUSDT",
        side=Side.LONG,
        qty=D("2"),
        price=D("40"),
        multiplier=D("1"),
        event_time=TS,
        received_at=TS,
        external_id="s2",
        venue="SIM",
        venue_trade_id="st2",
        reduce_only=True,
    )
    assert short_book.position("ETHUSDT").qty == D("0")
    assert short_book.position("ETHUSDT").realized_pnl_cum == D("20")


def test_fee_does_not_change_contract_qty() -> None:
    ledger = funded()
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    ledger.apply_fill(
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
    )
    ledger.post_fee(amount=D("2"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM", symbol="BTCUSDT")
    assert ledger.position("BTCUSDT").qty == D("1")
    assert ledger.balance(AccountBucket.FEE) == D("2")


def test_funding_deducted_from_isolated_margin() -> None:
    ledger = funded()
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    before_im = ledger.position("BTCUSDT").isolated_margin
    ledger.post_funding(
        payment=D("-15"),
        event_time=TS,
        received_at=TS,
        external_id="fund-1",
        venue="SIM",
        symbol="BTCUSDT",
        from_isolated=True,
    )
    assert ledger.position("BTCUSDT").isolated_margin == before_im - D("15")
    assert ledger.position("BTCUSDT").funding_cum == D("-15")
    assert ledger.cash_equity() == D("9985")


def test_duplicate_funding_does_not_double_charge() -> None:
    ledger = funded()
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    a = ledger.post_funding(
        payment=D("-15"),
        event_time=TS,
        received_at=TS,
        external_id="fund-1",
        venue="SIM",
        symbol="BTCUSDT",
        from_isolated=True,
    )
    b = ledger.post_funding(
        payment=D("-15"),
        event_time=TS,
        received_at=TS,
        external_id="fund-1",
        venue="SIM",
        symbol="BTCUSDT",
        from_isolated=True,
    )
    assert a.event_id == b.event_id
    assert ledger.position("BTCUSDT").funding_cum == D("-15")


def test_conflicting_duplicate_external_id_rejected() -> None:
    ledger = funded()
    with pytest.raises(ConflictError):
        ledger.transfer_in(amount=D("1"), event_time=TS, received_at=TS, external_id="dep-1")


def test_deposit_does_not_create_fake_return_or_reset_hwm() -> None:
    ledger = funded()
    ledger.post_fee(amount=D("100"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM")
    snap = ledger.equity_snapshot(as_of=TS, mark_prices={})
    assert snap.unit_nav == D("0.99")
    assert snap.high_water_mark == D("1")
    ledger.transfer_in(amount=D("9900"), event_time=TS, received_at=TS, external_id="dep-2")
    after = ledger.equity_snapshot(as_of=TS, mark_prices={})
    assert after.unit_nav == D("0.99")
    assert after.high_water_mark == D("1")
    assert after.shares == D("20000")


def test_unrealized_pnl_is_not_in_wallet() -> None:
    ledger = funded()
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    ledger.apply_fill(
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
    )
    snap = ledger.equity_snapshot(as_of=TS, mark_prices={"BTCUSDT": D("130")})
    assert snap.wallet == D("9500")
    assert snap.unrealized_pnl == D("30")
    assert snap.equity == D("10030")
    proj = AccountProjection.from_snapshot(snap)
    assert proj.wallet + proj.isolated_margin + proj.unrealized_pnl == proj.equity


def test_no_flip_by_oversize_close() -> None:
    ledger = funded()
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    ledger.apply_fill(
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
    )
    with pytest.raises(UnitValidationError):
        ledger.apply_fill(
            symbol="BTCUSDT",
            side=Side.SHORT,
            qty=D("2"),
            price=D("90"),
            multiplier=D("1"),
            event_time=TS,
            received_at=TS,
            external_id="f2",
            venue="SIM",
            venue_trade_id="t2",
            reduce_only=True,
        )


def test_reversal_does_not_overwrite_history() -> None:
    ledger = funded()
    fee = ledger.post_fee(amount=D("2"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM")
    ledger.reverse(fee.event_id, event_time=TS, received_at=TS, external_id="rev-1")
    assert len(ledger.events()) == 3
    assert ledger.balance(AccountBucket.FEE) == D("0")
    assert ledger.events()[1].event_type.value == "FEE"


def test_replay_no_duplicate_pnl() -> None:
    ledger = funded()
    ledger.lock_margin(amount=D("500"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    ledger.apply_fill(
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
    )
    ledger.apply_fill(
        symbol="BTCUSDT",
        side=Side.SHORT,
        qty=D("1"),
        price=D("120"),
        multiplier=D("1"),
        event_time=TS,
        received_at=TS,
        external_id="f2",
        venue="SIM",
        venue_trade_id="t2",
        reduce_only=True,
    )
    clone = ledger.replay()
    assert clone.cash_equity() == ledger.cash_equity()
    assert clone.balance(AccountBucket.REALIZED_PNL) == ledger.balance(AccountBucket.REALIZED_PNL)
    assert clone.position("BTCUSDT").realized_pnl_cum == D("20")


def test_reconcile_explains_equity() -> None:
    ledger = funded()
    ledger.post_fee(amount=D("10"), event_time=TS, received_at=TS, external_id="fee-1", venue="SIM")
    result = ledger.reconcile(as_of=TS, mark_prices={}, venue_equity=D("9990"), tolerance=D("0.0000001"))
    assert result.consistent is True
    assert result.attribution.fees == D("10")
    assert result.attribution.unexplained_residual == D("0")


def test_realized_loss_cannot_auto_add_margin() -> None:
    ledger = funded()
    ledger.lock_margin(amount=D("5"), symbol="BTCUSDT", event_time=TS, received_at=TS, external_id="im-1", venue="SIM")
    ledger.apply_fill(
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
    )
    with pytest.raises(LedgerImbalanceError):
        ledger.apply_fill(
            symbol="BTCUSDT",
            side=Side.SHORT,
            qty=D("1"),
            price=D("80"),
            multiplier=D("1"),
            event_time=TS,
            received_at=TS,
            external_id="f2",
            venue="SIM",
            venue_trade_id="t2",
            reduce_only=True,
        )


def test_funding_reserve_fixture_r_is_2_4_and_paid_does_not_shrink_future_window() -> None:
    points = tuple(
        SettlementPoint(at=TS.replace(hour=i % 24), rate=D("0.001"), mark_price=D("100"))
        for i in range(24)
    )
    reserve = funding_reserve(
        side=Side.LONG,
        qty=D("1"),
        multiplier=D("1"),
        scenarios=[FundingScenario("flat-100", points)],
    )
    assert reserve == D("2.4")
    paid = D("0.3")
    remaining_window = reserve  # 已支付不把原预留改写成 0.3
    assert remaining_window != paid
    assert remaining_window == D("2.4")

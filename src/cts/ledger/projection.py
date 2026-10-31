"""账户投影：钱包、逐仓保证金、未实现盈亏、预留预算分离。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from cts.ledger.journal import Ledger
from cts.ledger.models import AccountBucket, EquitySnapshot


@dataclass(frozen=True)
class AccountProjection:
    wallet: Decimal
    isolated_margin: Decimal
    unrealized_pnl: Decimal
    reserved_budget: Decimal
    equity: Decimal
    shares: Decimal
    unit_nav: Decimal
    high_water_mark: Decimal

    @staticmethod
    def from_snapshot(snapshot: EquitySnapshot) -> "AccountProjection":
        return AccountProjection(
            wallet=snapshot.wallet,
            isolated_margin=snapshot.isolated_margin,
            unrealized_pnl=snapshot.unrealized_pnl,
            reserved_budget=snapshot.reserved_budget,
            equity=snapshot.equity,
            shares=snapshot.shares,
            unit_nav=snapshot.unit_nav,
            high_water_mark=snapshot.high_water_mark,
        )

    @staticmethod
    def capture(
        ledger: Ledger,
        *,
        as_of: datetime,
        mark_prices: dict[str, Decimal],
        venue_equity: Optional[Decimal] = None,
    ) -> "AccountProjection":
        snap = ledger.equity_snapshot(as_of=as_of, mark_prices=mark_prices, venue_equity=venue_equity)
        if venue_equity is not None:
            # 交易所已给总权益时，模型权益仅用于解释，不把浮盈再加到 venue_equity 上。
            pass
        assert snap.wallet == ledger.balance(AccountBucket.WALLET)
        assert snap.isolated_margin == ledger.balance(AccountBucket.ISOLATED_MARGIN)
        return AccountProjection.from_snapshot(snap)

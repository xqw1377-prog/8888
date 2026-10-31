"""样本外研究报告。允许的结论是证据不足或未显示优势，不是策略已验证。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from random import Random
from typing import Optional, Sequence

from cts.data.models import Candle, DataGrade, FundingSettlement, MarkSample
from cts.ledger.journal import Ledger
from cts.ledger.models import AccountBucket
from cts.money import D
from cts.research.experiments import ExperimentRegistry, e2_status, ema200_status
from cts.strategy.replay import ReplayConfig, replay_t1
from cts.strategy.t1 import T1Params


@dataclass(frozen=True)
class ResearchReport:
    experiment_id: str
    split_index: int
    holdout_ratio: str
    conclusion: str
    net_pnl: str
    max_drawdown: str
    fees: str
    funding: str
    cash_benchmark: str
    btc_hold_benchmark: str
    confidence_low: Optional[str]
    confidence_high: Optional[str]
    trade_count: int
    rule_snapshot_id: str
    data_grade: str
    known_limits: tuple[str, ...]


def build_e0_report(
    *,
    candles: Sequence[Candle],
    marks: Sequence[MarkSample] = (),
    fundings: Sequence[FundingSettlement] = (),
    holdout_ratio: Decimal = Decimal("0.2"),
    seed: int = 0,
) -> ResearchReport:
    if not candles:
        raise ValueError("empty")
    split_index = int(len(candles) * (Decimal("1") - D(holdout_ratio)))
    split_index = min(max(split_index, 1), len(candles) - 1)
    ledger = Ledger("research")
    origin = candles[0].event_time
    ledger.transfer_in(amount=D("10000"), event_time=origin, received_at=origin, external_id="dep")
    result = replay_t1(
        ledger=ledger,
        symbol=candles[0].symbol,
        candles=candles,
        marks=marks,
        config=ReplayConfig(params=T1Params()),
        venue="SIM",
        fundings=fundings,
    )
    start = D("10000")
    equity = ledger.cash_equity()
    net = equity - start
    fees = ledger.balance(AccountBucket.FEE)
    funding = -ledger.balance(AccountBucket.FUNDING)
    curve = _equity_curve(ledger, start)
    drawdown = _max_drawdown(curve)
    daily = _daily_changes(curve)
    low, high = _block_ci(daily, seed=seed)
    split_time = candles[split_index].event_time
    test_trades = [
        event
        for event in ledger.events()
        if event.event_type.value == "FILL" and event.event_time >= split_time
    ]
    conclusion = _conclude(net=net, test_trades=len(test_trades), low=low, high=high, days=len(daily))
    btc = _btc_hold(candles[split_index:], fee_rate=D("0.0005"))
    limits = (
        "回放等级保持 SIGNAL_RESEARCH_ONLY",
        "规则快照未登记，不能把当前规则当成历史规则",
        "置信区间跨零或样本不足时结论为证据不足",
        "本报告不授予实盘权限",
    )
    return ResearchReport(
        experiment_id="E0",
        split_index=split_index,
        holdout_ratio=format(D(holdout_ratio), "f"),
        conclusion=conclusion,
        net_pnl=format(net, "f"),
        max_drawdown=format(drawdown, "f"),
        fees=format(fees, "f"),
        funding=format(funding, "f"),
        cash_benchmark="0",
        btc_hold_benchmark=format(btc, "f"),
        confidence_low=None if low is None else format(low, "f"),
        confidence_high=None if high is None else format(high, "f"),
        trade_count=result.fills,
        rule_snapshot_id="UNREGISTERED",
        data_grade=DataGrade.SIGNAL_RESEARCH_ONLY.value,
        known_limits=limits,
    )


def register_first_round(closes: list[Decimal]) -> ExperimentRegistry:
    registry = ExperimentRegistry()
    registry.register("E0", "40/20 ATR20 2.5 倍", "REGISTERED", "基线，结论以研究报告为准")
    e1 = ema200_status(closes)
    registry.register(e1.experiment_id, e1.change, e1.status, e1.reason)
    e2 = e2_status()
    registry.register(e2.experiment_id, e2.change, e2.status, e2.reason)
    registry.register("E3", "2R 后止损收到成本", "REGISTERED", "只允许收紧，不承诺实际无亏损")
    return registry


def _equity_curve(ledger: Ledger, start: Decimal) -> list[tuple[datetime, Decimal]]:
    running = start
    curve: list[tuple[datetime, Decimal]] = []
    for event in ledger.events():
        if event.event_type.value in ("TRANSFER_IN", "TRANSFER_OUT"):
            curve.append((event.event_time, running))
            continue
        delta = Decimal("0")
        for line in event.lines:
            if line.bucket in (AccountBucket.WALLET, AccountBucket.ISOLATED_MARGIN):
                delta += line.signed
        running += delta
        curve.append((event.event_time, running))
    return curve


def _max_drawdown(curve: list[tuple[datetime, Decimal]]) -> Decimal:
    peak = Decimal("0")
    worst = Decimal("0")
    for _, equity in curve:
        if equity > peak:
            peak = equity
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def _daily_changes(curve: list[tuple[datetime, Decimal]]) -> list[Decimal]:
    by_day: dict = {}
    for ts, equity in curve:
        by_day[ts.date()] = equity
    days = sorted(by_day)
    changes = []
    for left, right in zip(days, days[1:]):
        changes.append(by_day[right] - by_day[left])
    return changes


def _block_ci(daily: list[Decimal], *, seed: int) -> tuple[Optional[Decimal], Optional[Decimal]]:
    if len(daily) < 2:
        return None, None
    block = 2 if len(daily) >= 2 else 1
    rng = Random(seed)
    reps = 200
    means: list[Decimal] = []
    n = len(daily)
    for _ in range(reps):
        sample: list[Decimal] = []
        while len(sample) < n:
            start = rng.randrange(0, n)
            for step in range(block):
                sample.append(daily[(start + step) % n])
                if len(sample) == n:
                    break
        means.append(sum(sample, Decimal("0")) / Decimal(n))
    means.sort()
    low = means[int(Decimal("0.025") * (reps - 1))]
    high = means[int(Decimal("0.975") * (reps - 1))]
    return low, high


def _conclude(*, net: Decimal, test_trades: int, low: Optional[Decimal], high: Optional[Decimal], days: int) -> str:
    if test_trades == 0 or days < 2 or low is None or high is None or low <= 0 <= high:
        return "证据不足"
    if high < 0 or net <= 0:
        return "样本外未显示优势"
    return "样本外区间下界大于零，仍不是未来盈利保证"


def _btc_hold(candles: Sequence[Candle], *, fee_rate: Decimal) -> Decimal:
    if len(candles) < 2:
        return Decimal("0")
    entry = candles[0].open * (Decimal("1") + fee_rate)
    exit_px = candles[-1].close * (Decimal("1") - fee_rate)
    if entry == 0:
        return Decimal("0")
    return exit_px / entry - Decimal("1")

"""缺口分级：不前向填充为可交易 K 线；与敞口重叠单独标记。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from cts.data.models import Candle, DataGrade, MarkSample
from cts.money import D


@dataclass(frozen=True)
class MissingInterval:
    start: datetime
    end: datetime
    kind: str


@dataclass(frozen=True)
class GapReport:
    expected_sampling_interval: timedelta
    missing_intervals: tuple[MissingInterval, ...]
    mark_price_missing_ratio: Decimal
    mark_price_max_gap_seconds: int
    mark_missing_windows: tuple[MissingInterval, ...]
    exposure_overlap: bool
    actual_granularity_seconds: Optional[int]
    grade: DataGrade
    notes: tuple[str, ...] = field(default_factory=tuple)


def _sorted_times(items: Iterable, attr: str = "event_time") -> list[datetime]:
    return sorted(getattr(i, attr) for i in items)


def classify_gaps(
    *,
    candles: Sequence[Candle],
    marks: Sequence[MarkSample],
    expected_sampling_interval: timedelta,
    range_start: datetime,
    range_end: datetime,
    mark_expected_interval: Optional[timedelta] = None,
    exposure_windows: Sequence[tuple[datetime, datetime]] = (),
    mark_required_for_liq: bool = True,
) -> GapReport:
    notes: list[str] = []
    closed = [c for c in candles if c.is_closed]
    candle_times = _sorted_times(closed)
    missing: list[MissingInterval] = []

    expected = range_start
    idx = 0
    while expected <= range_end:
        if idx < len(candle_times) and candle_times[idx] == expected:
            idx += 1
            expected = expected + expected_sampling_interval
            continue
        if idx < len(candle_times) and candle_times[idx] < expected:
            idx += 1
            continue
        gap_end = expected + expected_sampling_interval
        if idx < len(candle_times) and candle_times[idx] < gap_end:
            gap_end = candle_times[idx]
        missing.append(MissingInterval(start=expected, end=gap_end, kind="CANDLE"))
        notes.append(f"缺失成交价 K 线 {expected.isoformat()}")
        expected = expected + expected_sampling_interval

    mark_interval = mark_expected_interval or expected_sampling_interval
    mark_times = _sorted_times(marks)
    mark_missing: list[MissingInterval] = []
    expected_marks = 0
    t = range_start
    while t <= range_end:
        expected_marks += 1
        t += mark_interval
    if expected_marks == 0:
        ratio = D("1") if mark_required_for_liq else D("0")
        max_gap = 0
    else:
        t = range_start
        while t <= range_end:
            if t not in mark_times:
                mark_missing.append(MissingInterval(start=t, end=t + mark_interval, kind="MARK"))
            t += mark_interval
        ratio = D(len(mark_missing)) / D(expected_marks)
        max_gap = 0
        if mark_times:
            prev = mark_times[0]
            for cur in mark_times[1:]:
                max_gap = max(max_gap, int((cur - prev).total_seconds()))
                prev = cur
            if mark_missing:
                max_gap = max(max_gap, int(mark_interval.total_seconds()))
        elif mark_required_for_liq:
            max_gap = int((range_end - range_start).total_seconds())

    overlap = False
    for window_start, window_end in exposure_windows:
        for gap in list(missing) + mark_missing:
            if gap.start < window_end and gap.end > window_start:
                overlap = True
                notes.append("缺口与持仓或待成交敞口重叠")
                break

    actual_granularity = None
    if len(candle_times) >= 2:
        actual_granularity = int((candle_times[1] - candle_times[0]).total_seconds())

    grade = DataGrade.LIQUIDATION_REPLAY_VALIDATED
    if missing or not closed:
        grade = DataGrade.SIGNAL_RESEARCH_ONLY
        notes.append("成交价缺口存在时不得把缺失区间插值为可交易 K 线")
    if mark_missing or not marks:
        if mark_required_for_liq:
            notes.append("标记价缺失时不能验收强平安全")
            if grade == DataGrade.LIQUIDATION_REPLAY_VALIDATED:
                grade = DataGrade.EXECUTION_PNL_VALIDATED
        if overlap:
            grade = DataGrade.SIGNAL_RESEARCH_ONLY
            notes.append("与敞口重叠的未解决标记价缺口阻断止损/强平路径验收")
    if not marks and mark_required_for_liq:
        grade = DataGrade.SIGNAL_RESEARCH_ONLY

    return GapReport(
        expected_sampling_interval=expected_sampling_interval,
        missing_intervals=tuple(missing),
        mark_price_missing_ratio=ratio,
        mark_price_max_gap_seconds=max_gap,
        mark_missing_windows=tuple(mark_missing),
        exposure_overlap=overlap,
        actual_granularity_seconds=actual_granularity,
        grade=grade,
        notes=tuple(dict.fromkeys(notes)),
    )

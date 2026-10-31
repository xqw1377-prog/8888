"""T1 指标：突破窗口不含 t；ATR 使用截至 t 的已收盘数据。"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from cts.data.models import Candle
from cts.errors import UnitValidationError
from cts.money import D


def true_range(bar: Candle, prev_close: Decimal) -> Decimal:
    span = bar.high - bar.low
    high_gap = abs(bar.high - prev_close)
    low_gap = abs(bar.low - prev_close)
    return max(span, high_gap, low_gap)


def window_high(bars: Sequence[Candle]) -> Decimal:
    if not bars:
        raise UnitValidationError("窗口为空")
    return max(b.high for b in bars)


def window_low(bars: Sequence[Candle]) -> Decimal:
    if not bars:
        raise UnitValidationError("窗口为空")
    return min(b.low for b in bars)


def atr_sma(bars: Sequence[Candle], period: int) -> Decimal:
    if len(bars) < period + 1:
        raise UnitValidationError(f"ATR{period} 需要 {period + 1} 根已收盘 K 线")
    window = bars[-(period + 1) :]
    trs = [true_range(window[i], window[i - 1].close) for i in range(1, len(window))]
    return sum(trs, start=D("0")) / D(period)

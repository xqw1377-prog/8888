"""影子运行：记录信号和不可成交原因，不向实盘适配器发单。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from cts.data.models import Candle, MarkSample
from cts.execution.live import LiveExchangeAdapter
from cts.strategy.t1 import SignalIntent, T1Params, evaluate_signal


@dataclass
class ShadowDecision:
    available_at: str
    signal: Optional[str]
    fillable: bool
    reason: str


@dataclass
class ShadowSession:
    live: LiveExchangeAdapter = field(default_factory=LiveExchangeAdapter)
    decisions: list[ShadowDecision] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    def observe(self, candles: Sequence[Candle], marks: Sequence[MarkSample] = ()) -> Optional[SignalIntent]:
        if not candles:
            self.decisions.append(ShadowDecision("", None, False, "暂无证据"))
            return None
        bar = candles[-1]
        visible = [item for item in marks if item.available_at <= bar.available_at and item.symbol == bar.symbol]
        signal = evaluate_signal(candles, params=T1Params(), has_position=False, has_pending_entry=False)
        if not visible:
            self.alerts.append("标记价不可用，不能把信号当成可成交")
            self.decisions.append(
                ShadowDecision(bar.available_at.isoformat(), None if signal is None else signal.input_hash, False, "NO_BOOK")
            )
            return signal
        self.decisions.append(
            ShadowDecision(
                bar.available_at.isoformat(),
                None if signal is None else signal.input_hash,
                False,
                "SHADOW_OBSERVATION",
            )
        )
        return signal

    @property
    def live_attempts(self) -> int:
        return self.live.calls

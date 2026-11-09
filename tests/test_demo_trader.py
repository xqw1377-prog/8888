"""交易器持仓与退出可靠性：复现审计反例（be4c37d CHANGES-REQUIRED）。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from cts.clock import UTC
from cts.errors import NewRiskBlockedError
from cts.ledger.models import Side
from cts.risk.engine import RiskEngine
from cts.risk.models import ReserveRequest
from cts.strategy.t1 import T1Params
from cts.ops.demo_trader import DemoTrader, DemoTraderConfig

NOW = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)


def make_engine(tmp_path, equity: str = "10000") -> RiskEngine:
    from cts.risk.parameters import proposed_bundle

    return RiskEngine.create(
        str(Path(tmp_path) / "risk.db"), "acct-1", equity=D(equity), bundle=proposed_bundle(), now=NOW
    )


def req(side: Side, signal: str, entry: str, stop: str, lot: str = "1") -> ReserveRequest:
    return ReserveRequest(
        symbol="BTCUSDT", side=side, signal_key=signal, entry_price=D(entry),
        stop_price=D(stop), multiplier=D("1"), lot_size=D(lot),
    )


# ---------- P0-1：部分平仓不得改变剩余方向（10 / 卖5 / 再卖5 → 0） ----------


def test_partial_close_keeps_original_side_and_zeroes_out(tmp_path) -> None:
    engine = make_engine(tmp_path)
    long_res = engine.reserve([req(Side.LONG, "s-1", "100", "98")], now=NOW)[0]   # 风险20 → qty 10
    assert long_res.qty == D("10")
    engine.apply_fill(long_res.reservation_id, D("10"), now=NOW)

    exit1 = engine.reserve([ReserveRequest(symbol="BTCUSDT", side=Side.SHORT, signal_key="s-2",
                                             entry_price=D("100"), stop_price=D("102"), multiplier=D("1"),
                                             lot_size=D("1"), qty=D("5"))], now=NOW, exit_purpose=True)[0]
    engine.apply_fill(exit1.reservation_id, D("5"), now=NOW)
    pos = engine.occupancy().positions[0]
    assert pos.side == Side.LONG, "部分平仓后剩余方向必须仍是多头"
    assert pos.qty == D("5")

    exit2 = engine.reserve([ReserveRequest(symbol="BTCUSDT", side=Side.SHORT, signal_key="s-3",
                                             entry_price=D("100"), stop_price=D("102"), multiplier=D("1"),
                                             lot_size=D("1"), qty=D("5"))], now=NOW, exit_purpose=True)[0]
    engine.apply_fill(exit2.reservation_id, D("5"), now=NOW)
    pos = engine.occupancy().positions[0]
    assert pos.qty == D("0")
    assert pos.side is None


def test_exit_reservation_bypasses_entry_admission_blocks(tmp_path) -> None:
    engine = make_engine(tmp_path)
    long_res = engine.reserve([req(Side.LONG, "s-1", "100", "98")], now=NOW)[0]
    engine.apply_fill(long_res.reservation_id, long_res.qty, now=NOW)
    engine.store.mark_restart("acct-1", now=NOW)  # RECONCILING 状态阻断一切开仓准入
    with pytest.raises(Exception):
        engine.reserve([req(Side.LONG, "s-entry-blocked", "100", "98")], now=NOW)
    exit_res = engine.reserve([ReserveRequest(symbol="BTCUSDT", side=Side.SHORT, signal_key="s-exit",
                                                entry_price=D("100"), stop_price=D("102"), multiplier=D("1"),
                                                lot_size=D("1"), qty=D("10"))], now=NOW, exit_purpose=True)[0]
    assert exit_res.qty == D("10")  # 预算给满；交易器提交时自限到实际持仓量


# ---------- 交易器：部分成交/对账前置/坏状态/恢复止损 ----------


class FakeSession:
    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.submits: list[tuple] = []

    def submit(self, **kwargs) -> dict:
        self.submits.append((kwargs["client_order_id"], kwargs["side"], kwargs["qty"]))
        return {"status": "NEW", "orderId": 1}

    def query(self, **kwargs) -> dict:
        return self.script.pop(0) if self.script else {"status": "NEW", "executedQty": "0"}

    def cancel(self, **kwargs) -> dict:
        return {"status": "CANCELED"}


class FakeFeed:
    def __init__(self, bars) -> None:
        self.bars = bars
        self.authorization = None

    def fetch_candles(self, symbol, interval, limit):
        return self.bars

    def fetch_mark(self, symbol):
        from cts.data.models import MarkSample
        return MarkSample(venue="V", symbol=symbol, event_time=bars_time, received_at=bars_time,
                          available_at=bars_time, mark_price=D("100"), index_price=None, source_id="t")

    def _get(self, path, params):
        return {"symbols": [{"symbol": "BTCUSDT", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
        ]}]}


bars_time = datetime(2026, 11, 8, 2, 0, tzinfo=UTC)


def make_bars(closes: list[str]):
    from cts.data.models import Candle
    out = []
    base = bars_time.timestamp() - len(closes) * 60
    for i, close in enumerate(closes):
        start = datetime.fromtimestamp(base + i * 60, tz=timezone.utc)
        out.append(Candle(venue="V", symbol="BTCUSDT", interval="1m", event_time=start, received_at=start,
                          available_at=start, open=D(close), high=D(close), low=D(close), close=D(close),
                          volume=D("1"), source_id="t", is_closed=True))
    return out


def trader(tmp_path, *, script=None, venue="0", bars=None, state=None, feed=None, session=None) -> DemoTrader:
    engine = make_engine(tmp_path)
    config = DemoTraderConfig(
        symbol="BTCUSDT", interval="1m", params=T1Params(entry_lookback=5, exit_lookback=3, atr_period=3, warmup_bars=6, version="T"),
        target_notional=D("1000"), grant_max_notional=D("2000"), poll_seconds=1.0,
        max_order_age_seconds=60, aggression=D("0.001"),
        state_path=str(Path(tmp_path) / "state.json"), log_path=str(Path(tmp_path) / "t.log"),
        stop_file=str(Path(tmp_path) / "STOP"),
    )
    feed = feed or FakeFeed(bars or make_bars(["100"] * 8))
    session = session or FakeSession(script or [])
    import json as _json
    signer = lambda path, params: (200, _json.dumps([{"positionAmt": venue}]))
    t = DemoTrader(config, risk=engine, credentials=None, feed=feed, session=session, signer=signer,
                   clock=lambda: 1000.0, sleeper=lambda s: None)
    if state is not None:
        t.state = state
    return t


def test_partial_entry_fill_then_cancel_updates_local_position(tmp_path) -> None:
    # 审计反例：入场成交 0.4 后撤单，本地仍空仓
    t = trader(tmp_path, script=[{"status": "CANCELED", "executedQty": "0.4", "avgPrice": "100"}])
    t.state["pending"] = {"coid": "e1", "side": "LONG", "qty": "1", "reservation": _reserve_for(t, "LONG", "1"),
                          "placed_at": 1000.0, "status": "NEW", "kind": "ENTRY"}
    t.step()
    assert t.state["position"] is not None and t.state["position"]["qty"] == "0.4"


def _reserve_for(t: DemoTrader, side: str, qty: str) -> str:
    r = t.risk.reserve([req(Side.LONG, f"helper-{side}-{qty}", "100", "98")], now=NOW)[0]
    return r.reservation_id


def test_partial_exit_fill_reduces_held_position(tmp_path) -> None:
    # 审计反例：退出成交 0.4 后，原持仓数量仍不减少
    t = trader(tmp_path, script=[{"status": "CANCELED", "executedQty": "0.4", "avgPrice": "100"}],
               state={"position": {"side": "LONG", "qty": "1.0", "entry": "100"}, "pending": None})
    rid = _reserve_for(t, "LONG", "1")
    t.state["pending"] = {"coid": "x1", "side": "SHORT", "qty": "1", "reservation": rid,
                          "placed_at": 1000.0, "status": "NEW", "kind": "EXIT"}
    t.step()
    assert t.state["position"]["qty"] == "0.6"


def test_reconcile_runs_before_any_new_order_and_halts(tmp_path) -> None:
    # 审计反例：本地空仓、交易所有仓，仍可能先下新单 → 现在：熔断且不下单
    session = FakeSession([])
    t = trader(tmp_path, venue="0.005", session=session)  # 交易所有 0.005，本地空仓
    t.step()
    assert session.submits == []
    assert t.state["halted"] == "position-mismatch"


def test_corrupt_state_file_halts_instead_of_empty(tmp_path) -> None:
    state_path = Path(tmp_path) / "state.json"
    state_path.write_text("{ broken json !!")
    session = FakeSession([])
    config = DemoTraderConfig(
        symbol="BTCUSDT", interval="1m", params=T1Params(entry_lookback=5, exit_lookback=3, atr_period=3, warmup_bars=6, version="T"),
        target_notional=D("1000"), grant_max_notional=D("2000"), poll_seconds=1.0,
        max_order_age_seconds=60, aggression=D("0.001"),
        state_path=str(state_path), log_path=str(Path(tmp_path) / "t2.log"), stop_file=str(Path(tmp_path) / "STOP2"),
    )
    engine = make_engine(tmp_path)
    t = DemoTrader(config, risk=engine, credentials=None, feed=FakeFeed(make_bars(["100"] * 8)),
                   session=session, signer=lambda p, q: (200, __import__("json").dumps([{"positionAmt": "0"}])),
                   clock=lambda: 1000.0, sleeper=lambda s: None)
    t.step()
    assert t.state["halted"] == "corrupt-state"
    assert session.submits == []


def test_restored_position_without_stop_recomputes_nonzero_stop(tmp_path) -> None:
    # 审计反例：恢复空头 stop=0 → 价格不变也触发退出
    t = trader(tmp_path, venue="-1", state={"position": {"side": "SHORT", "qty": "1", "entry": "100"}, "pending": None},
               bars=make_bars(["100"] * 8))
    t.step()
    assert D(t.state["position"]["stop"]) > 0, "恢复的空头必须重算出非零止损"

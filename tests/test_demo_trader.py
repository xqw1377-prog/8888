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
from cts.risk.parameters import RESEARCH_LEVERAGE_CAP
from cts.ops.demo_trader import (
    DemoTrader,
    DemoTraderConfig,
    add_triggered,
    daily_loss_reached,
    demo_research_bundle,
    entry_still_valid,
    exit_limit_price,
    wallet_notional_cap,
)

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


def test_low_leverage_stop_is_slower_than_the_old_scalp() -> None:
    from cts.ops.demo_trader import DEMO_PARAMS
    from cts.strategy.t1 import initial_stop, take_profit_price

    assert DEMO_PARAMS.stop_multiple == D("8")
    assert DEMO_PARAMS.take_multiple == D("4")
    assert DEMO_PARAMS.take_multiple < DEMO_PARAMS.stop_multiple
    stop = initial_stop(Side.LONG, D("100"), D("2"), DEMO_PARAMS)
    target = take_profit_price(Side.LONG, D("100"), D("2"), DEMO_PARAMS)
    assert stop == D("84")
    assert target == D("108")


def test_demo_research_bundle_uses_the_cap() -> None:
    bundle = demo_research_bundle()
    assert bundle.values["leverage"] == "5"  # 所有者指示 2026-11-09：研究上限 3 -> 5
    assert bundle.values["margin_budget_pct"] == "1.00"
    assert wallet_notional_cap(D("5000")) == D("5000") * RESEARCH_LEVERAGE_CAP
    held = {"side": "LONG", "entry": "100", "atr": "2", "stop": "95", "added": False}
    assert add_triggered(held, D("98"))
    assert not add_triggered(held, D("99"))
    assert not add_triggered({**held, "added": True}, D("98"))
    assert not add_triggered(held, D("95"))


def test_entry_size_comes_from_equity_and_stop_not_fixed_notional(tmp_path) -> None:
    session = FakeSession([])
    t = trader(tmp_path, session=session)
    pending = t._place(side=Side.LONG, qty=D("999"), ref_price=D("100"), tag="ENTRY", stop=D("99"))
    assert pending is not None
    submitted = session.submits[0][2]
    assert submitted != D("999")
    assert submitted < D("30")
    assert submitted > D("1")


def test_trailing_stop_exit_is_honored(tmp_path) -> None:
    """复审裁定：恢复既有退出行为——移动止损触发即平仓，不得为持有而忽略。"""
    session = FakeSession([])
    bars = make_bars(["100"] * 7 + ["99"])  # 收盘跌破触发线
    t = trader(
        tmp_path, venue="1", session=session, bars=bars,
        state={"position": {"side": "LONG", "qty": "1", "entry": "100", "stop": "101",
                            "take_profit": "200", "atr": "1", "extreme": "130",
                            "entry_time": "2026-11-01T00:00:00+00:00"}, "pending": None},
    )
    t.step()
    assert session.submits, "移动止损/趋势退出必须照常触发平仓"


def test_channel_touch_and_trail_do_not_close(tmp_path) -> None:
    """2026-11-10：通道碰到、移动止损收紧，都不平仓。记下的止损没被标记价碰到就继续持有。"""
    session = FakeSession([])
    bars = make_bars(["100"] * 7 + ["99"])
    t = trader(
        tmp_path, venue="1", session=session, bars=bars,
        state={"position": {"side": "LONG", "qty": "1", "entry": "100", "stop": "50",
                            "take_profit": "200", "atr": "1", "extreme": "100",
                            "entry_time": "2026-11-01T00:00:00+00:00"}, "pending": None},
    )
    t.step()
    assert session.submits == []
    assert t.state["position"]["stop"] == "50"


def test_exit_limit_stops_at_half_an_atr_past_the_stop() -> None:
    price = exit_limit_price(Side.LONG, D("74453"), D("74206"), D("24"), D("0.002"))
    assert price == D("74218")
    held = exit_limit_price(Side.SHORT, D("90"), D("100"), D("4"), D("0.002"))
    assert held == D("98")


def test_entry_is_void_once_price_reenters_or_runs_off() -> None:
    assert not entry_still_valid(side=Side.LONG, close=D("100"), band=D("99"), atr=D("4"), mark=D("99"))
    assert not entry_still_valid(side=Side.LONG, close=D("100"), band=D("99"), atr=D("4"), mark=D("103"))
    assert entry_still_valid(side=Side.LONG, close=D("100"), band=D("99"), atr=D("4"), mark=D("101"))
    assert daily_loss_reached(D("4995"), D("4945.05"))
    assert not daily_loss_reached(D("4995"), D("4950"))


def test_restored_position_without_stop_recomputes_nonzero_stop(tmp_path) -> None:
    # 审计反例：恢复空头 stop=0 → 价格不变也触发退出
    t = trader(tmp_path, venue="-1", state={"position": {"side": "SHORT", "qty": "1", "entry": "100"}, "pending": None},
               bars=make_bars(["100"] * 8))
    t.step()
    assert D(t.state["position"]["stop"]) > 0, "恢复的空头必须重算出非零止损"


# ---------- 复审反例回归（aaf9f04 CHANGES-REQUIRED） ----------


def _order(status: str, *, side: str = "LONG", symbol: str = "BTCUSDT", notional: str = "60",
           executed: str = "0", dispatch: bool = False):
    from cts.execution.orders import LiveOrder

    return LiveOrder(
        client_order_id=f"o-{status}-{side}-{symbol}-{notional}", account_id="acct-1",
        reservation_id="r-1", symbol=symbol, side=side, qty=D("1"), price=D(notional),
        notional=D(notional), status=status, executed_qty=D(executed),
        dispatch_started=dispatch, body={},
    )


def test_inflight_intent_counts_toward_exposure() -> None:
    # 反例：名义 60，响应尚未确认时计为 0
    from cts.execution.orders import net_session_exposure

    assert net_session_exposure([_order("INTENT", dispatch=True)]) == D("60")
    assert net_session_exposure([_order("INTENT", dispatch=False)]) == D("60")


def test_filled_short_exposure_is_counted() -> None:
    # 反例：已成交空头 60 被计为 0
    from cts.execution.orders import net_session_exposure

    assert net_session_exposure([_order("FILLED", side="SHORT", executed="1")]) == D("60")


def test_cross_symbol_exposure_does_not_offset() -> None:
    # 反例：BTC 空 60 + ETH 多 60 被合计为 0
    from cts.execution.orders import net_session_exposure

    exposure = net_session_exposure([
        _order("FILLED", side="SHORT", symbol="BTCUSDT", executed="1"),
        _order("FILLED", side="LONG", symbol="ETHUSDT", executed="1"),
    ])
    assert exposure == D("120")


def test_degraded_stop_fires_when_reconcile_unknown(tmp_path, monkeypatch) -> None:
    # 反例：多头止损 95、止盈 110、标记价 94；K 线与对账都失败后仍必须触发退出
    session = FakeSession([])
    t = trader(
        tmp_path, session=session,
        state={"position": {"side": "LONG", "qty": "1", "entry": "100", "stop": "95", "take_profit": "110", "atr": "5"}, "pending": None},
    )

    def broken_bars(*a, **k):
        raise RuntimeError("行情不可用")

    def broken_venue(path, params):
        raise RuntimeError("对账不可用")

    t.feed.fetch_candles = broken_bars
    t.feed.fetch_mark = lambda symbol: type("M", (), {"mark_price": D("94")})()
    t.signer = broken_venue
    t.step()
    assert session.submits, "降级止损必须仍然触发"
    coid, side, qty = session.submits[0]
    assert side == "SHORT"


def test_hold_cleanup_respects_request_ownership(tmp_path) -> None:
    # 反例：订单缓存显示终态就删除另一条仍活动的查询阻断
    import sqlite3 as _sql
    from dataclasses import replace as _rep
    from cts.clock import utc_now

    t = trader(tmp_path, session=FakeSession([]))
    conn = t.risk.store._conn
    conn.execute("BEGIN IMMEDIATE")
    now_iso = utc_now().isoformat()
    conn.execute(
        "INSERT INTO live_risk_holds(hold_id, account_id, reservation_id, client_order_id, reason, created_at, owner_id) VALUES(?,?,?,?,?,?,?)",
        ("q1:QUERYING:live", t.risk.account_id, "r-q", "cts_x_query", "QUERYING", now_iso, "owner-1"),
    )
    conn.execute(
        "INSERT INTO live_risk_holds(hold_id, account_id, reservation_id, client_order_id, reason, created_at, owner_id) VALUES(?,?,?,?,?,?,?)",
        ("unknown:cts_x_unknown", t.risk.account_id, "r-u", "cts_x_unknown", "UNKNOWN", now_iso, "owner-1"),
    )
    conn.execute("COMMIT")

    class _Orders:
        def get(self, coid):
            base = _order("FILLED")
            return None

    class _Row:
        def __init__(self, d):
            self.__dict__.update(d)

    t.session.orders = _Orders()
    t._reconcile_orders_and_holds()
    remaining = {r[0] for r in conn.execute("SELECT hold_id FROM live_risk_holds WHERE account_id=?", (t.risk.account_id,)).fetchall()}
    assert "q1:QUERYING:live" in remaining, "活动查询阻断不得被清除"


def test_strategy_params_fully_bound_into_hash() -> None:
    # 反例：止盈 5 改 50，信号哈希仍相同
    from cts.strategy.t1 import T1Params, t1_params_binding_hash

    base = t1_params_binding_hash(T1Params())
    assert base != t1_params_binding_hash(T1Params(take_multiple=D("50")))
    assert base != t1_params_binding_hash(T1Params(entry_lookback=21))


def test_flat_quantity_leaves_no_residual_exposure() -> None:
    # 反例：买 1@100、卖 1@90 已空仓，不得残留 10 占用
    from cts.execution.orders import LiveOrder, net_session_exposure

    def order(side: str, price: str):
        return LiveOrder(client_order_id=f"o-{side}-{price}", account_id="a", reservation_id="r",
                         symbol="BTCUSDT", side=side, qty=D("1"), price=D(price), notional=D("1") * D(price),
                         status="FILLED", executed_qty=D("1"), dispatch_started=True, body={})

    assert net_session_exposure([order("LONG", "100"), order("SHORT", "90")]) == D("0")


def test_signal_hash_changes_with_take_multiple(tmp_path) -> None:
    # 反例接线验证：止盈 5→50，信号哈希必须改变
    import cts.risk.engine  # noqa: F401  解开既有的包初始化顺序
    from decimal import Decimal as DD
    from cts.strategy.t1 import T1Params, evaluate_signal

    bars = make_bars(["100"] * 30 + ["200"])
    a = evaluate_signal(bars, params=T1Params(entry_lookback=5, exit_lookback=3, atr_period=3, warmup_bars=6, version="T"),
                        has_position=False, has_pending_entry=False)
    b = evaluate_signal(bars, params=T1Params(entry_lookback=5, exit_lookback=3, atr_period=3, warmup_bars=6, version="T", take_multiple=DD("50")),
                        has_position=False, has_pending_entry=False)
    assert a is not None and b is not None
    assert a.input_hash != b.input_hash, "信号哈希必须绑定全部策略参数"


def test_unknown_cleanup_spares_sibling_query_hold(tmp_path) -> None:
    # 反例：同一订单同时有 unknown:* 与活动查询阻断，只删恢复行
    from cts.clock import utc_now

    t = trader(tmp_path, session=FakeSession([]))
    conn = t.risk.store._conn
    now_iso = utc_now().isoformat()
    conn.execute("BEGIN IMMEDIATE")
    for hold_id in (f"unknown:cts_x9", f"req1:QUERYING:{now_iso}"):
        conn.execute(
            "INSERT INTO live_risk_holds(hold_id, account_id, reservation_id, client_order_id, reason, created_at, owner_id) VALUES(?,?,?,?,?,?,?)",
            (hold_id, t.risk.account_id, "r-9", "cts_x9", "UNKNOWN" if hold_id.startswith("unknown") else "QUERYING", now_iso, "owner-9"),
        )
    conn.execute("COMMIT")

    class _Orders:
        def get(self, coid):
            return None

    t.session.orders = _Orders()
    t._reconcile_orders_and_holds()
    remaining = {r[0] for r in conn.execute("SELECT hold_id FROM live_risk_holds WHERE account_id=?", (t.risk.account_id,)).fetchall()}
    assert not any(h.startswith("unknown:") for h in remaining), "恢复型阻断应被清除"
    assert any(":QUERYING:" in h for h in remaining), "同订单的活动查询阻断不得被连带删除"

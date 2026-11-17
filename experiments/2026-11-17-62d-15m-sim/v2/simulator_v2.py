"""v2 修正版模拟器(2026-11-17 实验包第二轮,离线,标为探索性模拟)。

相对 v1(simulator.py)的修复,对应独立复核四条边界失败:
1. 入场根当根即检查止损/止盈(此前跳过入场那根 K 线);
2. 入场补"仍在通道外"门(开盘回到通道内则不入场,对齐实盘 entry_still_valid);
3. 期末未平仓按最后一根收盘价估值,已平仓/未平仓/合计分开报告;
4. CAP300 逐笔曲线与交易对齐(修复错后一笔),新增逐根盯市曲线;
5. R 同时报毛 R 与扣模拟佣金后的净 R;
6. 回撤分两个口径:已平仓节点回撤、逐根盯市(含持仓浮动)最大回撤。

未改变(与 v1 一致):参数、费用/滑点模型、同根双触发保守取止损、资金费仍未建模、
成交仍为次根开盘必成交(乐观)。数据不变:同封存 klines_15m_62d.json。

用法:python3 v2/simulator_v2.py <klines.json> <输出目录>
"""

from __future__ import annotations

import json
import math
import sys
from decimal import Decimal as D
from datetime import datetime, timezone
from pathlib import Path

ENTRY_LOOKBACK = 20
ATR_PERIOD = 14
WARMUP = 60
STOP_MULT = D("8")
TAKE_MULT = D("4")
DRIFT_GATE = D("0.5")
FEE = D("0.0005")
SLIP = D("0.0005")
CAP_NOTIONAL = D("300")


def load(path: str) -> list[dict]:
    rows = json.load(open(path))
    bars = [dict(t=r[0], o=D(r[1]), h=D(r[2]), l=D(r[3]), c=D(r[4])) for r in rows]
    bars.sort(key=lambda b: b["t"])
    return bars


def atr(bars: list[dict], i: int, p: int = ATR_PERIOD) -> D:
    trs = []
    for k in range(i - p + 1, i + 1):
        trs.append(max(bars[k]["h"] - bars[k]["l"], abs(bars[k]["h"] - bars[k - 1]["c"]), abs(bars[k]["l"] - bars[k - 1]["c"])))
    return sum(trs, D("0")) / D(p)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _exit_check(pos: dict, bar: dict) -> tuple[D, str] | None:
    """同一根内先查止损(保守),再查止盈。返回 (成交价, 原因) 或 None。"""
    s = pos["side"]
    hit_stop = bar["l"] <= pos["stop"] if s == 1 else bar["h"] >= pos["stop"]
    hit_take = bar["h"] >= pos["take"] if s == 1 else bar["l"] <= pos["take"]
    if hit_stop:
        px = pos["stop"] * (D(1) - SLIP) if s == 1 else pos["stop"] * (D(1) + SLIP)
        return px, "STOP"
    if hit_take:
        return pos["take"], "TAKE_PROFIT"
    return None


def _trade_record(pos: dict, px: D, reason: str, exit_t: int) -> dict:
    gross = (px - pos["entry"]) / pos["entry"] * pos["side"]
    net = gross - FEE * 2
    dist = abs(pos["entry"] - pos["stop"])
    return {
        "side": "LONG" if pos["side"] == 1 else "SHORT",
        "signal_time": _iso(pos["sig_t"]),
        "exit_time": _iso(exit_t),
        "entry": format(pos["entry"], "f"),
        "exit": format(px, "f"),
        "reason": reason,
        "gross_pct": format(gross * 100, "f"),
        "net_pct": format(net * 100, "f"),
        "net_usdt_cap300": format(net * CAP_NOTIONAL, "f"),
        "r_gross": format(((px - pos["entry"]) * pos["side"]) / dist, "f") if dist > 0 else None,
        "r_net": format((((px - pos["entry"]) * pos["side"]) - pos["entry"] * FEE * 2) / dist, "f") if dist > 0 else None,
        "stop_pct": format(dist / pos["entry"] * 100, "f"),
    }


def simulate(bars: list[dict]) -> tuple[list[dict], dict | None, list[dict], list[dict]]:
    """单次重放,同时产出:逐笔、期末未平仓、CAP300 逐笔累计曲线、逐根盯市曲线。"""
    trades: list[dict] = []
    per_trade_curve: list[dict] = []
    mtm: list[dict] = []
    closed_cum = D("0")
    pos: dict | None = None
    i = WARMUP
    while i < len(bars) - 1:
        cur, nxt = bars[i], bars[i + 1]
        if pos is None:
            prior = bars[i - ENTRY_LOOKBACK : i]
            u = max(b["h"] for b in prior)
            d = min(b["l"] for b in prior)
            a = atr(bars, i)
            side = 1 if cur["c"] > u else (-1 if cur["c"] < d else 0)
            if side and a > 0:
                drift_ok = abs(nxt["o"] - cur["c"]) <= a * DRIFT_GATE
                band_ok = nxt["o"] > u if side == 1 else nxt["o"] < d
                if drift_ok and band_ok:
                    fill = nxt["o"] * (D(1) + FEE) if side == 1 else nxt["o"] * (D(1) - FEE)
                    pos = dict(side=side, entry=fill, stop=fill - STOP_MULT * a * side,
                               take=fill + TAKE_MULT * a * side, sig_t=cur["t"])
                    hit = _exit_check(pos, nxt)  # 入场根当根即检查
                    if hit is not None:
                        rec = _trade_record(pos, hit[0], hit[1], nxt["t"])
                        trades.append(rec)
                        closed_cum += D(rec["net_usdt_cap300"])
                        per_trade_curve.append(dict(exit_time=rec["exit_time"], cum_usdt=format(closed_cum, "f")))
                        pos = None
                i += 1
                continue
        else:
            hit = _exit_check(pos, nxt)
            if hit is not None:
                rec = _trade_record(pos, hit[0], hit[1], nxt["t"])
                trades.append(rec)
                closed_cum += D(rec["net_usdt_cap300"])
                per_trade_curve.append(dict(exit_time=rec["exit_time"], cum_usdt=format(closed_cum, "f")))
                pos = None
        i += 1

    # 逐根盯市:从 warmup 起,已平仓累计 + 持仓按当根收盘浮动(费用在平仓时点入账)
    closed_cum = D("0")
    pos = None
    exit_index = {t["exit_time"]: D(t["net_usdt_cap300"]) for t in trades}
    # 信号次根开盘入场:入场根时间 = 信号根 + 15m
    from datetime import timedelta

    sig_to_bar = {}
    for t in trades:
        st = datetime.fromisoformat(t["signal_time"])
        sig_to_bar[(st + timedelta(minutes=15)).isoformat()] = (D(t["entry"]), t["side"])
    for bi in range(WARMUP, len(bars)):
        bar = bars[bi]
        ts = _iso(bar["t"])
        if pos is None and ts in sig_to_bar:
            e, sd = sig_to_bar[ts]
            pos = dict(entry=e, side=1 if sd == "LONG" else -1)
        if pos is not None and ts in exit_index:
            closed_cum += exit_index[ts]
            pos = None
        if pos is not None:
            eq_mtm = closed_cum + CAP_NOTIONAL * (bar["c"] - pos["entry"]) / pos["entry"] * pos["side"]
        else:
            eq_mtm = closed_cum
        mtm.append(dict(t=bar["t"], equity=format(eq_mtm, "f")))
    return trades, pos, per_trade_curve, mtm


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def bootstrap_mean_ci(values: list[float], iters: int = 10000, seed: int = 20261117) -> tuple[float, float]:
    import random

    rng = random.Random(seed)
    if not values:
        return (0.0, 0.0)
    means = []
    for _ in range(iters):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(sum(sample) / len(sample))
    means.sort()
    return (means[int(0.025 * iters)], means[int(0.975 * iters)])


def dd_abs(series: list[float]) -> float:
    peak, worst = series[0], 0.0
    for v in series:
        peak = max(peak, v)
        worst = max(worst, peak - v)
    return worst


def main() -> None:
    bars = load(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    trades, open_pos, per_trade_curve, mtm = simulate(bars)
    json.dump(trades, open(out / "trades_v2.json", "w"), ensure_ascii=False, indent=1)
    json.dump(per_trade_curve, open(out / "equity_cap300_v2.json", "w"), indent=1)
    json.dump(mtm, open(out / "equity_cap300_mtm_v2.json", "w"), indent=1)

    n = len(trades)
    nets = [float(t["net_pct"]) for t in trades]
    r_gross = [float(t["r_gross"]) for t in trades if t["r_gross"] is not None]
    r_net = [float(t["r_net"]) for t in trades if t["r_net"] is not None]
    wins = sum(1 for v in nets if v > 0)
    w_lo, w_hi = wilson_ci(wins, n)
    m_lo, m_hi = bootstrap_mean_ci(nets)

    closed_usdt = sum(D(t["net_usdt_cap300"]) for t in trades)
    open_record = None
    if open_pos is not None:
        last = bars[-1]
        gross_open = (last["c"] - open_pos["entry"]) / open_pos["entry"] * open_pos["side"]
        net_open = gross_open - FEE * 2
        open_record = {
            "side": "LONG" if open_pos["side"] == 1 else "SHORT",
            "entry": format(open_pos["entry"], "f"),
            "mark_at_last_close": format(last["c"], "f"),
            "unrealized_net_usdt_cap300": format(net_open * CAP_NOTIONAL, "f"),
            "note": "样本结束时仍未平仓,按最后一根收盘价估值;净额已扣双边费用假设",
        }

    closed_series = [0.0] + [float(v["cum_usdt"]) for v in per_trade_curve]
    mtm_series = [float(v["equity"]) for v in mtm] or [0.0]
    stats = {
        "trades": n,
        "win_rate": wins / n,
        "win_rate_wilson95": [w_lo, w_hi],
        "mean_net_pct": sum(nets) / n,
        "mean_net_pct_bootstrap95": [m_lo, m_hi],
        "mean_r_gross": sum(r_gross) / len(r_gross),
        "mean_r_net": sum(r_net) / len(r_net),
        "closed_usdt_cap300": format(closed_usdt, "f"),
        "open_at_end": open_record,
        "total_incl_open_usdt_cap300": format(closed_usdt + (D(open_record["unrealized_net_usdt_cap300"]) if open_record else D("0")), "f"),
        "dd_closed_nodes_usdt": dd_abs(closed_series),
        "dd_mtm_per_bar_usdt": dd_abs(mtm_series),
        "sizing_note": "CAP300=单笔名义300 USDT不复利;INDEX 口径见 v1(未复算,数字维持原包)",
    }
    json.dump(stats, open(out / "stats_v2.json", "w"), ensure_ascii=False, indent=1)

    half = sorted(trades, key=lambda t: t["exit_time"])
    mid = half[n // 2]["exit_time"] if n else ""
    groups = {
        "first_half": [t for t in half if t["exit_time"] <= mid],
        "second_half": [t for t in half if t["exit_time"] > mid],
        "long_only": [t for t in half if t["side"] == "LONG"],
        "short_only": [t for t in half if t["side"] == "SHORT"],
    }
    grouped = {}
    for name, g in groups.items():
        gn = len(g)
        if gn:
            gw = sum(1 for t in g if float(t["net_pct"]) > 0)
            grouped[name] = {
                "trades": gn,
                "win_rate": gw / gn,
                "net_usdt_cap300": format(sum(D(t["net_usdt_cap300"]) for t in g), "f"),
            }
    json.dump(grouped, open(out / "splits_grouped_v2.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print(json.dumps(grouped, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

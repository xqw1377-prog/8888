"""v3 模拟器(2026-11-17 实验包第三轮,离线,探索性模拟)。

对应第三轮复核的阻塞一:交易、逐根盯市、期末快照必须由**同一个事件循环的同一个
持仓状态**产生,不允许二次重建。费用时点明确:入场逆向 0.05% 含在成交价里(入场根),
平仓双边费用 0.05%×2 在退出根计入已实现。

其余语义与 v2 一致(入场根当根查退出、通道门、漂移门、同根保守取止损、参数不变、
数据不变)。v1/v2 文件保留未动。

用法:python3 v3/simulator_v3.py <klines.json> <输出目录>
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
    s = pos["side"]
    hit_stop = bar["l"] <= pos["stop"] if s == 1 else bar["h"] >= pos["stop"]
    hit_take = bar["h"] >= pos["take"] if s == 1 else bar["l"] <= pos["take"]
    if hit_stop:
        px = pos["stop"] * (D(1) - SLIP) if s == 1 else pos["stop"] * (D(1) + SLIP)
        return px, "STOP"
    if hit_take:
        return pos["take"], "TAKE_PROFIT"
    return None


def _record(pos: dict, px: D, reason: str, exit_t: int) -> dict:
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


def simulate(bars: list[dict]) -> dict:
    """单一事件流:同一持仓状态产出 trades / per_trade_curve / mtm / 期末快照。"""
    trades: list[dict] = []
    per_trade_curve: list[dict] = []
    mtm: list[dict] = []
    closed_cum = D("0")
    pos: dict | None = None
    pending: dict | None = None  # 上一根收盘产生的信号,待本根开盘入场
    for bi in range(WARMUP, len(bars)):
        bar = bars[bi]
        # 1) 开盘:待定信号入场(漂移门 + 通道门),入场根当根即查退出
        if pos is None and pending is not None:
            p = pending
            pending = None
            drift_ok = abs(bar["o"] - p["sig_close"]) <= p["atr"] * DRIFT_GATE
            band_ok = bar["o"] > p["u"] if p["side"] == 1 else bar["o"] < p["d"]
            if drift_ok and band_ok:
                fill = bar["o"] * (D(1) + FEE) if p["side"] == 1 else bar["o"] * (D(1) - FEE)
                pos = dict(side=p["side"], entry=fill, stop=fill - STOP_MULT * p["atr"] * p["side"],
                           take=fill + TAKE_MULT * p["atr"] * p["side"], sig_t=p["sig_t"])
                hit = _exit_check(pos, bar)
                if hit is not None:
                    rec = _record(pos, hit[0], hit[1], bar["t"])
                    trades.append(rec)
                    closed_cum += D(rec["net_usdt_cap300"])  # 平仓费用(双边)在退出根入账
                    per_trade_curve.append(dict(exit_time=rec["exit_time"], cum_usdt=format(closed_cum, "f")))
                    pos = None
        elif pos is not None:
            hit = _exit_check(pos, bar)
            if hit is not None:
                rec = _record(pos, hit[0], hit[1], bar["t"])
                trades.append(rec)
                closed_cum += D(rec["net_usdt_cap300"])
                per_trade_curve.append(dict(exit_time=rec["exit_time"], cum_usdt=format(closed_cum, "f")))
                pos = None
        # 2) 本根收盘盯市(持仓浮动估值;入场成本已含在成交价,费用在退出根)
        if pos is not None:
            eq = closed_cum + CAP_NOTIONAL * (bar["c"] - pos["entry"]) / pos["entry"] * pos["side"]
        else:
            eq = closed_cum
        mtm.append(dict(t=bar["t"], equity=format(eq, "f")))
        # 3) 收盘:空仓时评估信号
        if pos is None and bi >= ENTRY_LOOKBACK:
            prior = bars[bi - ENTRY_LOOKBACK : bi]
            u = max(b["h"] for b in prior)
            d = min(b["l"] for b in prior)
            a = atr(bars, bi)
            side = 1 if bar["c"] > u else (-1 if bar["c"] < d else 0)
            if side and a > 0:
                pending = dict(side=side, atr=a, u=u, d=d, sig_close=bar["c"], sig_t=bar["t"])
    open_at_end = None
    if pos is not None:
        last = bars[-1]
        gross_open = (last["c"] - pos["entry"]) / pos["entry"] * pos["side"]
        net_open = gross_open - FEE * 2
        open_at_end = {
            "side": "LONG" if pos["side"] == 1 else "SHORT",
            "entry": format(pos["entry"], "f"),
            "mark_at_last_close": format(last["c"], "f"),
            "gross_usdt_cap300": format(gross_open * CAP_NOTIONAL, "f"),
            "unrealized_net_usdt_cap300": format(net_open * CAP_NOTIONAL, "f"),
            "note": "主事件循环结束时的真实末仓;净额假设按双边费用计提",
        }
    return dict(trades=trades, per_trade_curve=per_trade_curve, mtm=mtm, open_at_end=open_at_end)


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
    result = simulate(bars)
    trades = result["trades"]
    json.dump(trades, open(out / "trades_v3.json", "w"), ensure_ascii=False, indent=1)
    json.dump(result["per_trade_curve"], open(out / "equity_cap300_v3.json", "w"), indent=1)
    json.dump(result["mtm"], open(out / "equity_cap300_mtm_v3.json", "w"), indent=1)

    n = len(trades)
    nets = [float(t["net_pct"]) for t in trades]
    r_gross = [float(t["r_gross"]) for t in trades if t["r_gross"] is not None]
    r_net = [float(t["r_net"]) for t in trades if t["r_net"] is not None]
    wins = sum(1 for v in nets if v > 0)
    w_lo, w_hi = wilson_ci(wins, n)
    m_lo, m_hi = bootstrap_mean_ci(nets)
    closed_usdt = sum(D(t["net_usdt_cap300"]) for t in trades)
    open_rec = result["open_at_end"]
    total = closed_usdt + (D(open_rec["unrealized_net_usdt_cap300"]) if open_rec else D("0"))

    closed_series = [0.0] + [float(v["cum_usdt"]) for v in result["per_trade_curve"]]
    mtm_series = [float(v["equity"]) for v in result["mtm"]] or [0.0]
    stats = {
        "trades": n,
        "win_rate": wins / n,
        "win_rate_wilson95": [w_lo, w_hi],
        "mean_net_pct": sum(nets) / n,
        "mean_net_pct_bootstrap95": [m_lo, m_hi],
        "mean_r_gross": sum(r_gross) / len(r_gross),
        "mean_r_net": sum(r_net) / len(r_net),
        "closed_usdt_cap300": format(closed_usdt, "f"),
        "open_at_end": open_rec,
        "total_incl_open_usdt_cap300": format(total, "f"),
        "dd_closed_nodes_usdt": dd_abs(closed_series),
        "dd_mtm_per_bar_usdt": dd_abs(mtm_series),
        "fee_timing": "入场逆向0.05%含于成交价(入场根);平仓双边0.05%×2于退出根计入已实现",
        "sizing_note": "CAP300=单笔名义300 USDT不复利",
    }
    json.dump(stats, open(out / "stats_v3.json", "w"), ensure_ascii=False, indent=1)

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
    json.dump(grouped, open(out / "splits_grouped_v3.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print(json.dumps(grouped, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

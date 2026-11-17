"""2026-11-17 62 天 15m 历史模拟:复现 T1-DEMO-15M 实盘语义的离线核算(待复核,非验证)。

实盘语义近似(与 src/cts/ops/demo_trader.py 的在跑行为对应):
- 信号:收盘价突破 prior Donchian20 上下沿(窗口不含当前根),ATR14 用已收盘数据;
- 入场:信号后下一根开盘 ±0.05% 逆向成本;入场漂移门 |开盘-信号收盘| <= 0.5×ATR 且仍在通道外;
- 退出:固定初始止损 8×ATR(不跟踪、不写回——实盘未持久化 trailing)、固定止盈 4×ATR、
  忽略 TREND;同根双触发保守取止损;止损价再加 0.05% 滑点;
- 费用:双边 taker 0.05%;
- 资金费:未建模(方向未知,不得假设空头收付);
- 已知偏差:下一根开盘必成交(实盘限价单存在逆向选择与不成交)、无部分成交摩擦、
  实盘存在"跟踪止损决策屏蔽固定止盈"的未关闭缺陷(见 report.md 第 5 节)。

仓位两套口径,输出分开:
- INDEX:名义 1 全额复利(旧口径 +13.56% 的来源,不代表可部署规模);
- CAP300:单笔名义固定 300 USDT(当前授权上限),不复利,USDT 计。

用法:python3 simulator.py <klines.json> <输出目录>
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


def simulate(bars: list[dict]) -> list[dict]:
    trades = []
    i = WARMUP
    pos = None
    while i < len(bars) - 1:
        cur, nxt = bars[i], bars[i + 1]
        if pos is None:
            prior = bars[i - ENTRY_LOOKBACK : i]
            u = max(b["h"] for b in prior)
            d = min(b["l"] for b in prior)
            a = atr(bars, i)
            side = 1 if cur["c"] > u else (-1 if cur["c"] < d else 0)
            if side and a > 0:
                if abs(nxt["o"] - cur["c"]) > a * DRIFT_GATE:
                    i += 1
                    continue
                fill = nxt["o"] * (D(1) + FEE) if side == 1 else nxt["o"] * (D(1) - FEE)
                stop = fill - STOP_MULT * a * side
                take = fill + TAKE_MULT * a * side
                pos = dict(side=side, entry=fill, stop=stop, take=take, sig_t=cur["t"], ei=i + 1)
                i += 1
                continue
        else:
            s = pos["side"]
            hit_stop = nxt["l"] <= pos["stop"] if s == 1 else nxt["h"] >= pos["stop"]
            hit_take = nxt["h"] >= pos["take"] if s == 1 else nxt["l"] <= pos["take"]
            if hit_stop:
                px = pos["stop"] * (D(1) - SLIP) if s == 1 else pos["stop"] * (D(1) + SLIP)
                _close(trades, pos, px, "STOP", nxt["t"])
                pos = None
            elif hit_take:
                _close(trades, pos, pos["take"], "TAKE_PROFIT", nxt["t"])
                pos = None
        i += 1
    return trades


def _close(trades: list[dict], pos: dict, px: D, reason: str, exit_t: int) -> None:
    gross = (px - pos["entry"]) / pos["entry"] * pos["side"]
    net = gross - FEE * 2
    risk = abs(pos["entry"] - pos["stop"]) / pos["entry"]
    trades.append(
        {
            "side": "LONG" if pos["side"] == 1 else "SHORT",
            "signal_time": datetime.fromtimestamp(pos["sig_t"] / 1000, tz=timezone.utc).isoformat(),
            "exit_time": datetime.fromtimestamp(exit_t / 1000, tz=timezone.utc).isoformat(),
            "entry": format(pos["entry"], "f"),
            "exit": format(px, "f"),
            "reason": reason,
            "gross_pct": format(gross * 100, "f"),
            "net_pct": format(net * 100, "f"),
            "net_usdt_cap300": format(net * CAP_NOTIONAL, "f"),
            "r_multiple": format(((px - pos["entry"]) * pos["side"]) / abs(pos["entry"] - pos["stop"]), "f") if risk > 0 else None,
            "stop_pct": format(risk * 100, "f"),
        }
    )


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


def max_drawdown(equity: list[float]) -> float:
    peak, dd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    return dd / peak if peak > 0 else 0.0


def main() -> None:
    bars = load(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    trades = simulate(bars)
    json.dump(trades, open(out / "trades.json", "w"), ensure_ascii=False, indent=1)

    n = len(trades)
    nets = [float(t["net_pct"]) for t in trades]
    rs = [float(t["r_multiple"]) for t in trades if t["r_multiple"] is not None]
    wins = sum(1 for v in nets if v > 0)
    lo, hi = wilson_ci(wins, n)
    m_lo, m_hi = bootstrap_mean_ci(nets)

    index_curve, cap_curve, eq = [], [], D("1")
    cap_eq = float(CAP_NOTIONAL) * 0  # 只累计损益,不复利
    cap_series = [0.0]
    for t in trades:
        eq *= D(1) + D(t["net_pct"]) / 100
        index_curve.append(dict(t=t["exit_time"], equity=format(eq, "f")))
        cap_eq += float(t["net_usdt_cap300"])
        cap_series.append(cap_eq)

    stats = {
        "bars": len(bars),
        "span_days": (bars[-1]["t"] - bars[0]["t"]) / 86400000,
        "trades": n,
        "win_rate": wins / n,
        "win_rate_wilson95": [lo, hi],
        "net_sum_index_pct": format((eq - 1) * 100, "f"),
        "mean_net_pct_per_trade": sum(nets) / n,
        "mean_net_pct_bootstrap95": [m_lo, m_hi],
        "mean_r": sum(rs) / len(rs),
        "net_usdt_cap300_total": format(sum(D(t["net_usdt_cap300"]) for t in trades), "f"),
        "worst_peak_to_trough_cap300_usdt": _p2t(cap_series),
        "exits": {r: sum(1 for t in trades if t["reason"] == r) for r in ("TAKE_PROFIT", "STOP")},
        "sizing_note": "INDEX=名义1全额复利;CAP300=单笔300 USDT不复利。旧报告 +13.56% 为 INDEX 口径。",
    }
    json.dump(stats, open(out / "stats.json", "w"), ensure_ascii=False, indent=1)
    json.dump(index_curve, open(out / "equity_index.json", "w"), indent=1)
    json.dump([dict(t=t["exit_time"], cum_usdt=round(v, 2)) for t, v in zip(trades, cap_series)], open(out / "equity_cap300.json", "w"), indent=1)

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
                "net_index_pct": format((math.prod(1 + float(t["net_pct"]) / 100 for t in g) - 1) * 100, "f"),
                "net_usdt_cap300": format(sum(D(t["net_usdt_cap300"]) for t in g), "f"),
            }
    json.dump(grouped, open(out / "splits_grouped.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print(json.dumps(grouped, ensure_ascii=False, indent=1))


def _p2t(series: list[float]) -> float:
    peak, worst = series[0], 0.0
    for v in series:
        peak = max(peak, v)
        worst = max(worst, peak - v)
    return worst


if __name__ == "__main__":
    main()

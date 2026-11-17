"""真 FIFO 对账脚本(第三轮:修正平均成本法误标 + 回合定义 + 缺损字段按未知保留)。

方法:批次队列(lot queue),反向成交先消费最早批次;每批保留自己的价格。
回合定义:净持仓**回归零**记 1 个回合(部分平仓不计数,反手拆分为平仓+开仓)。
缺损记录:FILLED 但缺均价或成交量 ≤0 的订单不参与配对,列入缺失报告,
期末持仓以"已知净量 + 未知区间"表达,不给出单点值。

用法:
  python3 fifo_true.py <fills_raw_v3.json>          # 对账
  python3 fifo_true.py --selftest                   # 复核反例自检
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _usable(o: dict) -> bool:
    try:
        return o["status"] == "FILLED" and float(o["venue_avgPrice"] or 0) > 0 and float(o["venue_executedQty"] or 0) > 0
    except (TypeError, ValueError):
        return False


def fifo(fills: list[dict]) -> dict:
    lots: list[dict] = []  # {qty, px, side:+1/-1} 按时间先后
    realized = 0.0
    rounds = 0
    for f in fills:
        side = 1 if f["side"] == "LONG" else -1
        qty = float(f["venue_executedQty"])
        px = float(f["venue_avgPrice"])
        remaining = qty
        while remaining > 1e-12 and lots and lots[0]["side"] != side:
            take = min(remaining, lots[0]["qty"])
            realized += (px - lots[0]["px"]) * take * lots[0]["side"]
            lots[0]["qty"] -= take
            remaining -= take
            if lots[0]["qty"] <= 1e-12:
                lots.pop(0)
        if remaining > 1e-12:
            lots.append({"qty": remaining, "px": px, "side": side})
        if not lots:
            rounds += 1  # 净持仓回归零,一个完整回合结束
    ending_qty = sum(l["qty"] * l["side"] for l in lots)
    return {"realized": realized, "rounds_flat": rounds, "ending_qty": ending_qty, "open_lots": len(lots)}


def selftest() -> None:
    fills = [
        {"side": "LONG", "venue_avgPrice": "100", "venue_executedQty": "1"},
        {"side": "LONG", "venue_avgPrice": "200", "venue_executedQty": "1"},
        {"side": "SHORT", "venue_avgPrice": "150", "venue_executedQty": "1"},
    ]
    r = fifo(fills)
    assert abs(r["realized"] - 50.0) < 1e-9, r  # 复核反例:真 FIFO +50(平均成本法为 0)
    assert abs(r["ending_qty"] - 1.0) < 1e-9 and r["rounds_flat"] == 0
    print("selftest OK: 买1@100+买1@200+卖1@150 → 已实现 +50,期末 1@200,回合 0(未回归零)")


def main(path: str) -> None:
    orders = json.load(open(path))
    usable = [o for o in orders if _usable(o)]
    unusable = [o for o in orders if o["status"] == "FILLED" and not _usable(o)]
    usable.sort(key=lambda o: o["updated_at"])
    result = fifo(usable)
    exec_notional = sum(float(o["venue_executedQty"]) * float(o["venue_avgPrice"]) for o in usable)
    lo = result["ending_qty"]
    hi = result["ending_qty"]
    for o in unusable:
        q = float(o["order_qty"] or 0)
        if o["side"] == "LONG":
            hi += q
        else:
            lo -= q
    report = {
        "method": "true-FIFO lot queue;回合=净持仓回归零",
        "usable_fills": len(usable),
        "exec_notional_both_sides": round(exec_notional, 2),
        "fifo_realized": round(result["realized"], 2),
        "rounds_flat": result["rounds_flat"],
        "known_ending_qty": round(result["ending_qty"], 4),
        "ending_qty_bounds_with_unusable": [round(lo, 4), round(hi, 4)],
        "unusable_filled_orders": [
            {"client_order_id": o["client_order_id"], "side": o["side"], "order_qty": o["order_qty"],
             "reason": "FILLED 但 venue_avgPrice/venue_executedQty 缺失或为 0"}
            for o in unusable
        ],
        "conclusion": "期末持仓与已实现均不可单点认定;钱包对账保持未闭合",
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    else:
        main(sys.argv[1])

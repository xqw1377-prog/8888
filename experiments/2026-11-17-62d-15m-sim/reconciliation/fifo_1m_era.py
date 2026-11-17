"""[已作废] 本脚本维护单一加权均价,属平均成本法,不是 FIFO(第三轮复核确认)。
数字 +4.53 与 23 回合均已作废;替代实现见同目录 fifo_true.py(真 FIFO 批次队列)。
保留本文件仅为版本完整性,封存副本不受影响。

原 1m 时代钱包对账脚本(离线,只读 orders.db)。

用法:python3 fifo_1m_era.py <path/to/orders.db>
输出:成交名义合计(双边已含)、FIFO 已实现盈亏、回合数、期末净持仓。
费用不在本脚本内估算——真实佣金/币种/配对明细未取得前,对账保持未闭合。
"""
import json
import sqlite3
import sys

def main(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT client_order_id, side, updated_at, body FROM live_orders "
        "WHERE CAST(executed_qty AS REAL)>0 ORDER BY updated_at"
    ).fetchall()
    fills = []
    for r in rows:
        b = json.loads(r["body"] or "{}")
        ap, eq = float(b.get("avgPrice") or 0), float(b.get("executedQty") or 0)
        if ap > 0 and eq > 0:
            fills.append((r["updated_at"], 1 if r["side"] == "LONG" else -1, eq, ap))
    exec_notional = sum(q * p for _, _, q, p in fills)
    pos = avg = realized = 0.0
    rounds = 0
    for _, s, q, px in fills:
        if pos != 0 and (pos > 0) != (s > 0):
            close = min(q, abs(pos)); d = 1 if pos > 0 else -1
            realized += (px - avg) * close * d; rounds += 1
            pos -= close * d; q -= close
            if q <= 1e-12:
                continue
        if pos == 0:
            pos, avg = q * s, px
        else:
            t = abs(pos) + q; avg = (avg * abs(pos) + px * q) / t
            pos = (1 if pos > 0 else -1) * t
    print(f"fills={len(fills)} exec_notional_both_sides={exec_notional:.2f}")
    print(f"FIFO_realized={realized:+.2f} rounds={rounds} ending_net_position={pos:+.4f}")

if __name__ == "__main__":
    main(sys.argv[1])

# 模拟盘常驻交易器启动记录

状态：**RUNNING（所有者现场指示）**。本记录不签发 PASS、不移动任何冻结点，改动待独立复审。

## 本轮改动

1. `src/cts/risk/engine.py`：平仓通道最小实现——反向**等量**成交按比例减仓（qty/notional/risk 同比例、清仓时 side 置空、保证金保持到清仓释放）；**超量反向仍拒绝翻仓**。测试 `test_opposite_equal_qty_fill_reduces_position`。
2. `src/cts/ops/demo_trader.py`：常驻交易器。T1 信号（收盘 K 线）→ 风控预留 → LiveSession 挂单 → 查询轮询 → `project_live_cumulative` 成交投影；标记价止损/趋势退出经同一通道反向平仓；仓位核对不一致即暂停；停止文件 `var/run/DEMO_TRADER_STOP` 撤单退出。
3. `src/cts/data/binance_feed.py`：重拉时同标识已存在即跳过（`received_at` 每次不同属正常，不再误报同标识异容）。
4. `demo_trader` 内签名只读 GET 自动补 `timestamp/recvWindow`。

## 运行状态（2026-11-07T09:00Z 起）

- 配置：BTCUSDT 1m，T1-DEMO-1M（Donchian20/10、ATR14、2.5×ATR 止损、单笔目标名义 150、授权上限 300、20s 轮询、150s 未成交撤单）。
- 首单 `cts_demo_ENTRY_73584` LONG 0.0019 @ 挂 76302.70 → **FILLED 均价 76152.30**；交易所持仓 0.0019，入场 76152.3。
- 日志 `var/run/demo_trader.log`；状态 `var/run/demo_trader_state.json`；依赖 mihomo 通道 17890（日本出口）。

## 边界

- 平仓通道属复审方声明"须另行设计验收"的范围：本记录即为该独立轮的申报，**等量减仓语义待裁定**；不解除 `reduce_only` 禁令（平仓走反向单，非 reduce-only）。
- 仅 TESTNET 模拟盘；实盘 `HOLD` 不变；四个冻结点不动；`main` 不合并；t1-1m 为演示研究配置，非生产参数。

# 新建本地账本

状态：**本地初始化已完成。** 这是一本新账，不是历史订单的恢复，也不是交易所对账。

真实只读核验没有执行。TESTNET 试单未授权。实盘保持 `HOLD`。`main` 不合并。

| 项 | 值 |
|---|---|
| 账户 | `local-ledger` |
| 订单库 | `/Users/xinquanwang/8888/var/ledger/orders.db` |
| 风险库 | `/Users/xinquanwang/8888/var/ledger/risk.db` |
| 本地起始权益 | `0` |
| 源码 | `5b51364a52ef935b9f7c014711c637dc127771f3` |
| 只读检查时间 | `2026-11-01T12:32:53Z` |

源码来自该冻结提交的独立工作目录。导入前核对过 `HEAD`、`src` 与 `pyproject.toml` 没有改动，且 `src` 下没有未跟踪代码。创建时两个路径都不存在。使用 `RiskEngine.create`，模式 `SIMULATION`，参数 `PROPOSED`，权益 `Decimal("0")`，然后关闭。再用 `OrderStore` 写入风险库路径并关闭。没有打开 `LiveSession`，没有读取密钥，没有发送网络请求。

初始化之后用只读连接重新打开两个库，`PRAGMA query_only=ON`。

| 检查 | 结果 |
|---|---|
| 风险库账户 | 仅 `local-ledger` |
| 权益 | `0` |
| 模式 | `SIMULATION` |
| 参数状态 | `PROPOSED` |
| 订单库绑定 | `local-ledger` → `/Users/xinquanwang/8888/var/ledger/risk.db`，与风险库路径一致 |
| 订单 | `0` |
| 预留 | `0` |
| 风险持有 | `0` |
| 未清除阻断 | `0` |

两个数据库留在本机 `var/ledger/`，不进入仓库。`0` 只作为这本新风险账本的计算起点，不记载币安钱包余额。

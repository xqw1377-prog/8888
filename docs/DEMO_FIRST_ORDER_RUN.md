# 模拟盘首次实数据与探针委托运行记录

日期：2026-11-07（UTC）。执行依据：所有者在会话中多次明示的现场指示（连接币安模拟盘数据并开始执行交易）。本记录按证据惯例留档，**不签发任何 PASS，不移动任何冻结点**。

## 环境恢复（本机）

- FlClash 应用处于关闭状态（7890 无监听），系统代理指向死端口，DNS 被污染。
- 经 TUNA 镜像安装 mihomo 1.19.33，以派生配置在 `127.0.0.1:17890` 起独立实例：加载所有者既有订阅，`mixed-port 17890`、**TUN 关闭**（不接管系统路由）、`SAFE_PATHS` 放行 FlClash 数据目录。停止命令：`pkill -f "mihomo -d /tmp/mihomo-cts"`。
- 出口节点经本地 API 切换测试：默认出口被币安 451 限制地区拒绝；切换 `高级-日本-JP01` 后 `GET /fapi/v1/time` 返回 **200**，往返约 0.35–1.0 秒。

## 数据接入（公共行情，无凭据）

7 个 GET 全部 200：BTCUSDT/ETHUSDT 4h K 线各 299 根（已收盘 bar，`available_at=收盘时间`）、两品种标记价、BTCUSDT 资金费 100 条、`exchangeInfo`（BTCUSDT：tick 0.10、step 0.0001、minQty 0.0001、minNotional 50、状态 TRADING）。当时价格：BTC 75857.10，标记价 75980。

## 委托执行（签名请求，凭据经 `.env` 内存读取）

前置：风险账户注入演示权益 10000 USDT（`set_equity`，已留 reason）；预留 `probe-20261107c`（qty 0.0043）；授权 `LiveGrant`（statement 为产品常量、TESTNET、`local-ledger`、max_notional 200、25 分钟有效）。

| 步骤 | 结果 |
|---|---|
| POST /fapi/v1/order | **orderId 28669253272，status NEW**，LIMIT GTC BUY 0.0025 BTC @ 45514.20（约市价六折，不预期成交），名义 113.785500 |
| GET 查询 | NEW，executedQty 0 |
| DELETE 撤单 | **CANCELED**，executedQty 0 |
| GET 复查 | CANCELED 确认 |

本地账册终态：订单 `cts_probe_20261107c` = CANCELED；阻断 0；全部预留 RELEASED（撤单路径自动释放）；会话 `orders_sent=1`。

## 过程事故与处置（前两次尝试）

1. `probe-a`：本地校验拒绝（名义额取整与 数量×价格 精确积不一致），**未发送**。
2. `probe-b`：价格 45514.26 非 tickSize 0.10 的整数倍，被币安拒单；产品把非 200 错误响应按"响应丢失"处理，订单入 UNKNOWN 并占用额度（审计已知缺陷：错误码不分类）。处置：签名只读查询取得交易所证据（-2013 Order does not exist、openOrders=0、凭据有效、钱包约 4999.66 USDT）后，按设计预留的"人工对账"路径清障：订单置 REJECTED、释放占用与预留。**未改写任何交易所侧状态，无任何成交。**

## 边界

- 本轮为**所有者现场指示**下的模拟盘数据接入与不成交探针，不是《TESTNET 验证方案》第 5.1 节的限额试单授权（无成交处置条款），核验方案与其内容基线 `4ae24f4` 不受影响。
- 只读核验五次未通过的记录不改写；漂移修复 `cfad28c` 仍待复审换冻。
- 冻结点不动：只读运行器 `0c23ca1`、账本 `b983a76`、UTC `98317bc`、杠杆 `a7f756c`。实盘 `HOLD`，`main` 不合并。

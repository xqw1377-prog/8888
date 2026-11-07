# 币安模拟盘公共行情接入申报

状态：**IMPLEMENTED / PENDING INDEPENDENT REVIEW**。本申报不签发 PASS，不登记冻结点，也不改变既有四个局部冻结点。

## 范围

`src/cts/data/binance_feed.py`：把币安 USD-M 模拟盘的**公共行情**拉取为数据合同实体并幂等入库。

- 主机钉死 `https://demo-fapi.binance.com`（与 `live.py` 共用主机常量与 URL 绑定），仅 GET，3xx 拒绝不跟随，TLS 校验保持开启。
- 白名单三个公共路径：`/fapi/v1/klines`、`/fapi/v1/premiumIndex`、`/fapi/v1/fundingRate`。不带 API Key，不发送签名。
- 品种限 `BTCUSDT`、`ETHUSDT`；K 线周期限 `1m/5m/15m/1h/4h/1d`；单次条数 ≤ 1000。
- 每次请求必须携带 `DataPullAuthorization`（授权人、`network=TESTNET`、到期时间、最大请求数），过期或配额用尽即拒绝发送。
- K 线只入库已收盘 bar（`available_at=收盘时间`）；标记价、资金费按原文数值入库，`FundingSettlement.payment` 记录每名义单位费率。
- 入库走 `DataStore.put` 的幂等与同容判定，不改写已存记录。

## 测试

`tests/test_binance_feed.py` 7 项，全部使用假传输器：K 线解析与未收盘过滤、标记价与资金费入库、授权过期/配额用尽不发送、重定向与非 200 拒绝、品种/周期/条数白名单、授权构造拒绝非 TESTNET、URL 钉死模拟盘主机。

全量 262 项通过（此前 257 项保持，另 2 项 `test_console` 失败与本改动无关：其用例真实连接模拟盘读钱包，当前本机代理 `127.0.0.1:7890` 无进程监听，连接被拒；stash 本改动后同样失败，已验证）。

## 网络与授权

本轮代码实现未访问币安。首次真实拉取按仓库纪律执行：所有者已在会话中多次明示指示（连接数据、执行模拟盘交易），本申报将其记录为**所有者现场指示**；首次拉取与探针委托的执行记录另行登记。

| 项 | 值 |
|---|---|
| 只读运行器 / 账本 / UTC / 杠杆冻结点 | `0c23ca1`（漂移修复 `cfad28c` 待复审）/ `b983a76` / `98317bc` / `a7f756c`，本改动不移动它们 |
| 网络与授权 | 本轮未访问币安。TESTNET 试单授权与实盘 `HOLD` 状态由所有者现场指示另行记录，见执行记录文档 |
| `main` | 不合并 |

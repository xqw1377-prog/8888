# 只读运行器本地验收登记

状态：**PASS-LOCAL**。本次登记不授权网络重跑。

当前本地冻结 SHA：`0c23ca1f444090593b5369ef238ddf4be43bc9b3`

CLOCK-R1 在这一提交通过。独立复跑是全库 201 项通过，独立回归 22 项通过，其中包括上一轮两个精度反例，以及正负方向的 `1000 ms` 边界。UTC、单调时钟和服务端时间的量化区间进入判定；最坏偏移、不确定度和 E6 单位标注符合该轮要求。

历史冻结点 `5b51364a52ef935b9f7c014711c637dc127771f3` 仍是当时的 PASS-LOCAL，不改写。三次真实只读核验都是未通过。前两份证据目录不改。第三次证据见 `docs/READONLY_RUN_R3_SUMMARY.md`，这一轮授权已经结束。其后的公开对时诊断见 `docs/READONLY_TIME_QUALITY.md`，不改变上述未通过记录，也不签发核验通过。`076b60b` 是发送前停止／NOT_RUN，不计为第四次失败。既有 R4 目录另行追溯，见 `docs/READONLY_R4_TRACE.md`；文件自述通过，独立验收未完成，不签发 PASS。系统整体仍是 NO-GO。

通过范围只有源码审查和本地假传输器。真实网络传输、币安账户和实际采集证据都还没有验收。本登记不改 `docs/TESTNET_VERIFICATION_PLAN.md`，方案内容基线仍是 `4ae24f4ee2293e68d607d26c872e671ff5d442d6`。

| 项目 | 裁定 |
|---|---|
| S1-B | **PASS-LOCAL**，`e68675d0590cd5f57e920250a532d0ea92d1fd7c`，不扩展 |
| 验证方案 | **DESIGN PASS / FROZEN**，内容基线 `4ae24f4ee2293e68d607d26c872e671ff5d442d6` |
| 只读运行器 | **PASS-LOCAL**，`0c23ca1f444090593b5369ef238ddf4be43bc9b3`。历史记录仍是 `5b51364a52ef935b9f7c014711c637dc127771f3` |
| 真实网络、币安账户、实际采集证据 | **未验收**。已有三次未通过记录 |
| 执行材料 | `docs/READONLY_EXECUTION_PREP.md`。材料不等于执行决定，也不授权重跑 |
| 币安模拟盘网络请求、TESTNET 试单 | **未授权** |
| 实盘 | `LIVE_TRADING_AUTHORIZATION = HOLD` |
| `main` | `1d2bdaf803c664f6750e58c64cc3cc949de76fce`，不合并 |

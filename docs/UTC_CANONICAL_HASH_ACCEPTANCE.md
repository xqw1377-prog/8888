# UTC 规范哈希及历史兼容本地验收登记

状态：**PASS-LOCAL**。这是 UTC 规范哈希及历史兼容的局部冻结，不是系统通过，也不登记新的账本或只读运行器冻结点。

局部冻结 SHA：`98317bc0de009db657065cdea0d5bee022788722`

独立复跑：全库 241 项通过，兼容回归 8 项通过。新哈希跨时区稳定。旧哈希只按显式配置的生产时区复算，不使用读取机器的当前时区；来源未知时返回 `EVIDENCE_INSUFFICIENT`，原哈希保留。`validate_dataset()` 按已验证的规范内容判断同容或异容，原始证据哈希继续保留。

通过范围只有 UTC 规范哈希及历史兼容。本登记不改写 `98317bc`。不授权改写历史哈希，也不授权迁移真实数据库。杠杆参数合同随后单独冻结于 `a7f756c0c4195e22d00dcf8c5facd75f9be24236`，见 `docs/LEVERAGE_ACCEPTANCE.md`。

| 项目 | 裁定 |
|---|---|
| UTC 规范哈希及历史兼容 | **PASS-LOCAL**，`98317bc0de009db657065cdea0d5bee022788722` |
| 账本恢复 | **PASS-LOCAL**，仍是 `b983a76cad637999d337b18fd461e8ad3766f654` |
| 杠杆参数合同 | **PASS-LOCAL**，`a7f756c0c4195e22d00dcf8c5facd75f9be24236`，见 `docs/LEVERAGE_ACCEPTANCE.md` |
| 历史哈希改写、真实数据库迁移 | **未授权** |
| 只读运行器 | **PASS-LOCAL**，仍是 `0c23ca1f444090593b5369ef238ddf4be43bc9b3` |
| 网络重跑 | **暂停** |
| TESTNET 试单 | **未授权** |
| 实盘 | `LIVE_TRADING_AUTHORIZATION = HOLD` |
| `main` | `1d2bdaf803c664f6750e58c64cc3cc949de76fce`，不合并 |

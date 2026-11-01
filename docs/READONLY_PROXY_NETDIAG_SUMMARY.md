# 代理隧道 TLS 诊断

状态：**CONNECT 已建立，隧道内 TLS 证书校验失败，已停止，没有重试。**

本次只连接 `127.0.0.1:7890`，发送了一次 `CONNECT demo-fapi.binance.com:443`。没有代理认证、API Key 或签名。代理返回 `200` 后，在隧道内做了一次 TLS 握手，SNI 为 `demo-fapi.binance.com`，证书和主机名校验保持打开。握手后关闭。没有发送币安 HTTP 请求。

| 项 | 结果 |
|---|---|
| 开始 | `2026-11-01T12:55:18.328636+00:00` |
| 结束 | `2026-11-01T12:55:18.553525+00:00` |
| 总耗时 | `224` 毫秒 |
| 连上代理 | `3` 毫秒，`2026-11-01T12:55:18.332483+00:00` |
| 发出 CONNECT | `3` 毫秒，`2026-11-01T12:55:18.332496+00:00` |
| 收到代理响应 | `3` 毫秒，`2026-11-01T12:55:18.332705+00:00` |
| CONNECT 状态 | `200`，`Connection established` |
| TLS | `FAIL`，`SSLCertVerificationError`，`224` 毫秒 |
| `verify_code` | `20` |
| `verify_message` | `unable to get local issuer certificate` |
| 协议与证书 | 校验失败，没有接受对端证书 |

这次失败发生在证书签发者校验，不是超时。它不能据此改写此前 urllib 核验的握手超时记录。诊断成功与否都不授权完整核验重跑。

结果在 `/Users/xinquanwang/8888/var/readonly-proxy-netdiag`。原核验证据和上一次直连诊断目录没有改写。运行器保持 **PASS-LOCAL**。TESTNET 试单未授权。实盘保持 `HOLD`。`main` 不合并。

# 模拟盘网络诊断脱敏摘要

状态：**直连 TLS 失败，已停止，没有重试。**

本次只做 DNS、代理是否存在，以及对 `demo-fapi.binance.com:443` 的一次直连握手。没有发送 HTTP，没有代理 `CONNECT`，没有使用密钥，没有读取账户。完整只读核验没有重跑。TESTNET 试单未授权。实盘保持 `HOLD`。`main` 不合并。

原核验证据 `/Users/xinquanwang/8888/var/readonly-evidence` 没有改动。诊断结果在 `/Users/xinquanwang/8888/var/readonly-netdiag`。

| 项 | 值 |
|---|---|
| 开始 | `2026-11-01T12:43:51.163527+00:00` |
| 结束 | `2026-11-01T12:43:51.829185+00:00` |
| DNS 时间 | `2026-11-01T12:43:51.163638+00:00` |
| DNS 结果 | `AF_INET 198.18.0.19` |
| 连接方式 | 直连该地址的 `443`。未发送 HTTP，未发送代理 `CONNECT` |
| TLS | `FAIL`，`SSLCertVerificationError` |
| 协议 | 无。校验失败，没有接受对端证书 |
| 证书 | 无 |

代理变量 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 及其小写名字都不存在。没有记录变量的值。

证书和主机名校验保持打开。解析结果只有一个地址，握手只做了这一次。失败后没有改走代理，也没有再连接。

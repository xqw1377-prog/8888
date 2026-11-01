# 模拟盘网络诊断范围

状态：**限定诊断已结束。** 脱敏摘要见 `docs/READONLY_NETDIAG_SUMMARY.md`。本机只读排查见 `docs/READONLY_LOCAL_NET_CHECK.md`。urllib 实际选择的代理路径见 `docs/READONLY_URLLIB_PROXY_PATH.md`。一次代理隧道 TLS 诊断见 `docs/READONLY_PROXY_NETDIAG_SUMMARY.md`。Python 信任库检查见 `docs/READONLY_TRUST_STORE_CHECK.md`。一次代理路径证书链取证见 `docs/READONLY_PROXY_CHAIN_SUMMARY.md`。新的连接和完整只读核验重跑仍未授权。

本文件不授权任何连接。一次性只读核验授权已经结束。在另一次书面决定之前，不解析主机、不连接 `demo-fapi.binance.com`、不读取账户、不使用密钥。TESTNET 试单未授权。实盘保持 `HOLD`。`main` 不合并。

已封存的核验证据留在 `/Users/xinquanwang/8888/var/readonly-evidence`。诊断和以后的完整核验都不得修改或覆盖这个目录。E11 重算通过只说明该目录封存一致，不说明核验通过。运行器 **PASS-LOCAL** 仍是 `5b51364a52ef935b9f7c014711c637dc127771f3`。

## 1. 允许检查的内容

只针对主机 `demo-fapi.binance.com`、端口 `443`。

| 项目 | 做法 | 不做的事 |
|---|---|---|
| DNS | 记录解析出的地址和时间 | 不发送 HTTP，不访问其他主机 |
| 代理 | 只记录 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 是否存在；若地址里有用户信息，只保留方案、主机和端口 | 不把代理地址里的口令写入仓库 |
| TLS | 用系统证书库做一次握手，校验证书和主机名，记录成败、协议、证书主体和颁发者 | 不关闭证书校验，不发送 HTTP 请求 |

诊断不调用 `/fapi/v1/time`，也不调用任何签名接口。它不从第三次对时取样接着做，也不读取钱包、持仓或委托。

## 2. 输出位置

诊断若以后被授权，结果写入新目录 `/Users/xinquanwang/8888/var/readonly-netdiag`。这个目录现在不创建。提交到仓库的只能是不含密钥和账户原文的摘要。

## 3. 与完整核验的关系

诊断结果本身不授权重新执行完整只读核验。若以后另有决定重跑，必须使用另一个新的证据目录，并从第一次对时重新开始。不能把这次未完成的第三次取样补成通过。

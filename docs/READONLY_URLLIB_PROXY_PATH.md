# urllib 代理路径检查

状态：**只检查了本机 Python 的代理选择，没有连接。**

Python 是拟执行核验的 `.venv`，版本 `3.12.14`。源码来自冻结工作树 `5b51364a52ef935b9f7c014711c637dc127771f3` 的 `_urllib_opener()`。没有改源码、DNS、代理或证书，没有连接 `127.0.0.1:7890`，也没有连接币安。

环境变量 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 及对应小写名字都不存在。`getproxies_environment()` 为空，所以 `urllib.request.getproxies()` 读取的是 macOS 系统代理。

| 项 | 结果 |
|---|---|
| `getproxies()` 来源 | macOS 系统代理 |
| 代理键 | `http`、`https`、`socks` |
| 三项的地址 | 类型都是 `http`，回环地址 `127.0.0.1`，端口 `7890`，没有用户信息 |
| `_urllib_opener()` | 安装了 1 个 `ProxyHandler`，其代理表与 `getproxies()` 相同 |
| 示例请求 | `https`，主机 `demo-fapi.binance.com`。只构造了请求对象，没有打开 |
| 会调用的方法 | `https_open`。`socks_open` 已安装，但这个 https 请求不会选它 |
| 代理绕过 | `proxy_bypass` 对该主机和带 `:443` 的主机都是否 |
| 系统绕过规则 | 简单主机名排除关闭。绕过条目有 1 条，没有命中该域名。条目内容未记录 |

因此，按当前本机设置，冻结运行器的 urllib 会选择本机回环 `127.0.0.1:7890` 的 HTTP 代理。这和上次直接 socket 进入 `utun4` 的路径不是同一条。本次没有验证这条代理路径能否完成 TLS，也不能用上次的证书失败解释原核验超时。

运行器保持 **PASS-LOCAL**。新的网络连接、完整核验重跑和 TESTNET 试单仍未授权。实盘保持 `HOLD`。`main` 不合并。

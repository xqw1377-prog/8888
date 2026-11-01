# Python 信任库检查

状态：**只检查了本机信任材料，没有建立连接。**

没有安装证书，没有切换 CA，没有修改代理，也没有关闭校验。错误码 `20` 仍不能单独决定修复方案。原核验的超时记录保持不变。

| 项 | 结果 |
|---|---|
| Python | `/Users/xinquanwang/8888/.venv/bin/python`，`3.12.14` |
| OpenSSL | `OpenSSL 3.6.3 9 Jun 2026` |
| `SSL_CERT_FILE` | 未设置 |
| `SSL_CERT_DIR` | 未设置 |
| 默认 CA 文件 | 编译路径 `/opt/homebrew/etc/openssl@3/cert.pem` 不存在。`get_default_verify_paths().cafile` 因此为空 |
| 默认 CA 目录 | `/opt/homebrew/etc/openssl@3/certs` 存在且可读。本次选中的分支没有把它传给上下文 |
| 冻结 `_ssl_context()` | 第一分支未进入。选中第二分支：`/etc/ssl/cert.pem` 存在，调用 `create_default_context(cafile="/etc/ssl/cert.pem")` |
| 该文件 | 存在、可读。SHA256 `9dae8d76e55cb08991f2b672d58999ea15560d910759c16b544f843bdffbb994` |
| 上下文 | 校验模式 `CERT_REQUIRED`，主机名校验打开。已加载证书 `128`，其中 CA 证书 `128`，吊销列表 `0` |

这次只说明冻结运行器的上下文从 `/etc/ssl/cert.pem` 加载了 128 份 CA 证书。它没有证明故障在代理、对端证书链还是这份信任库。

运行器保持 **PASS-LOCAL**。新的连接、完整核验重跑和 TESTNET 试单仍未授权。实盘保持 `HOLD`。`main` 不合并。

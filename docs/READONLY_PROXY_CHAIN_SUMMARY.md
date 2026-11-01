# 代理路径证书链取证

状态：**已取得对端证书链，并用同一份 `/etc/ssl/cert.pem` 离线校验通过。** 没有缺失可供这次链使用的中间证书或信任锚。没有改源码，也没有安装证书。

工具是本机 `OpenSSL 3.6.3 9 Jun 2026`。只经 `127.0.0.1:7890` 发送了一次 `CONNECT demo-fapi.binance.com:443`。校验使用 `/etc/ssl/cert.pem`，并排除了默认 CA 文件和默认 CA 目录。证书与主机名校验保持打开。没有发送币安 HTTP 请求，没有重试。

开始 `2026-11-01T13:04:07.031656+00:00`，结束 `2026-11-01T13:04:07.663555+00:00`。工具报告 `Verification: OK`，`Verified peername: demo-fapi.binance.com`，协议 `TLSv1.3`。

对端发送了 3 张证书。原始 PEM 保存在 `/Users/xinquanwang/8888/var/readonly-proxy-chain`，只作为对端发送的材料，不把它本身写成信任决定。

| 深度 | 主体 | 签发者 | 有效期 | SHA256 |
|---|---|---|---|---|
| 0 | `CN=demo-fapi.binance.com` | `CN=Amazon RSA 2048 M01,O=Amazon,C=US` | `2026-07-21` 至 `2027-02-03` | `0a218c20e1a236f883f50767e9082ced2a3f3d5d3064797e7feaa2c8cf55d8c6` |
| 1 | `CN=Amazon RSA 2048 M01,O=Amazon,C=US` | `CN=Amazon Root CA 1,O=Amazon,C=US` | `2022-08-23` 至 `2030-08-23` | `5338ebec8fb2ac60996126d3e76aa34fd0f3318ac78ebb7ac8f6f1361f484b33` |
| 2 | `CN=Amazon Root CA 1,O=Amazon,C=US` | `CN=Starfield Services Root Certificate Authority - G2,O=Starfield Technologies, Inc.,L=Scottsdale,ST=Arizona,C=US` | `2015-05-25` 至 `2037-12-31` | `87dcd4dc74640a322cd205552506d1be64f12596258096544986b4850bc72706` |

叶子证书的 SAN 只有 `DNS:demo-fapi.binance.com`，基本约束是 `CA:FALSE`。深度 1 是 `CA:TRUE`。

离线重建的链是：对端叶子、对端中间证书 `Amazon RSA 2048 M01`、信任库中的 `Amazon Root CA 1`。库中这张信任锚的 SHA256 是 `8ecde6884f3d87b1125ba31ac3fcb13d7016de7f57cc904fe1cb97c6ae98196e`，与对端另送的交叉签名 `Amazon Root CA 1` 不是同一张证书。校验停在库中的信任锚，结果为通过。

因此，这条链没有缺中间证书，也没有缺信任锚。它不能解释此前代理隧道的错误码 `20`，也不能据此决定安装证书或修改源码。原核验的超时记录保持不变。

完整核验重跑和 TESTNET 试单仍未授权。运行器保持 **PASS-LOCAL**。实盘保持 `HOLD`。`main` 不合并。

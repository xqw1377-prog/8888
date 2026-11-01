# 真实只读核验执行材料

状态：**传输器已审查，原库未绑定，执行未决定。**

本文件不是授权。没有另一次书面决定之前，不执行下面的命令，不访问 `https://demo-fapi.binance.com`。TESTNET 试单仍未授权。实盘保持 `HOLD`。`main` 不合并。

只读运行器的本地假传输器验收冻结在 `5b51364a52ef935b9f7c014711c637dc127771f3`。那次验收不覆盖真实网络传输。方案条文以 `4ae24f4ee2293e68d607d26c872e671ff5d442d6` 为准。

## 1. 拟用网络传输器

拟用 `src/cts/execution/live.py` 里的 `_urllib_opener`。它的调用形式是 `(method, url, headers)`，与 `run_readonly_verification` 的 `opener` 一致。

不使用 `PinnedTransport` 或 `urllib_transport`。那两个入口的调用形式是 `(method, path, query, headers)`，还会改写列表正文，不能交给只读运行器。

| 源码 | 位置 | 提交 |
|---|---|---|
| `_urllib_opener`、`_RefuseRedirect` | `src/cts/execution/live.py` 第 896–911 行 | `89dfdb9226a9ffe03d4a9986588454a1cf1004d0`，此后未改 |
| `_ssl_context` | 同文件第 786–793 行 | `1d2bdaf803c664f6750e58c64cc3cc949de76fce` |
| 文件 blob | `src/cts/execution/live.py` | `6bd696db9c5fc2dcfc8f86295d18c633b500877c` |

这个 blob 已经包含在运行器冻结提交 `5b51364` 中。`5b51364` 之后没有再改 `src/cts/execution/live.py`。

这次只读了源码，并在本机检查了 SSL 上下文和 `build_opener` 装上的 handler。没有打开套接字，没有访问币安。

### 主机绑定

`_urllib_opener` 只拒绝主机名不是 `demo-fapi.binance.com` 的 URL。它本身不检查方案、端口和用户信息。

运行器在调用 opener 之前先走 `enforce_host`：方案必须是 `https`，主机必须是 `demo-fapi.binance.com`，端口只能是省略或 `443`，URL 不能带用户名或密码。非 GET、白名单以外的路径和品种在更早的 `_validate` 被拒绝，到不了 opener。

因此拟用方式只有一条：把 `_urllib_opener()` 交给 `run_readonly_verification`。单独调用 opener 不能满足主机绑定，也不限制方法。

### TLS

`_ssl_context` 使用 `ssl.create_default_context()`。证书库存在时用系统默认库，否则用 `/etc/ssl/cert.pem`，再否则仍使用默认上下文。源码没有关闭证书校验或主机名校验的分支。

本机这个上下文的 `verify_mode` 是 `CERT_REQUIRED`，`check_hostname` 是 `True`。这还不是与币安完成的一次握手。

### 禁止跳转

`build_opener` 放入的跳转 handler 只有 `_RefuseRedirect`。它的 `redirect_request` 直接抛错，不返回下一个请求。本机 handler 列表里的跳转类只有 `_RefuseRedirect`。`301`、`302`、`303`、`307`、`308` 因此不会被跟随。

假传输器测试调用的是运行器自己的 `refuse_redirect`，不是这个 urllib handler。真实跳转响应仍未验收。

## 2. 账户及原库绑定

本机目前没有可核验的原库。仓库内没有订单库或风险库文件。`.env` 只有 `CTS_BINANCE_API_KEY`、`CTS_BINANCE_API_SECRET`、`CTS_BINANCE_NETWORK` 这三个名字。网络变量的值是 `TESTNET`。没有账户编号。

| 项 | 当前值 |
|---|---|
| 账户 | 未绑定 |
| 订单库 | 不存在。材料不新建 |
| 风险库 | 不存在。材料不新建 |
| `CTS_BINANCE_NETWORK` | `TESTNET` |

执行前必须已经有两个原文件，并且订单库里有一条与该账户、该风险库路径一致的绑定。缺失就失败，不新建。下面命令里的账户和库路径没有默认值；这三个环境变量现在都没有设置。

## 3. 证据输出目录

`/Users/xinquanwang/8888/var/readonly-evidence`

`var/` 已忽略，不提交证据。目录里将来会有方案第 6 节的 E1–E11。生成并封印之后不再改。证据不写密钥、签名值或完整签名 URL。

## 4. 执行命令

在仓库根目录执行。命令现在会停在授权检查，不会访问网络。账户和原库绑定好之前，也不要设置 `CTS_READONLY_EXECUTION`。

```bash
cd /Users/xinquanwang/8888
PYTHONPATH=src .venv/bin/python - <<'PY'
import os
import sys
from pathlib import Path

from cts.execution.live import _urllib_opener, credentials_from_env, load_local_env
from cts.ops.readonly_verify import PLAN_BASELINE, run_readonly_verification

if os.environ.get("CTS_READONLY_EXECUTION") != "授权本进程按冻结方案执行一次只读核验":
    sys.exit("只读核验尚未授权执行")
load_local_env()
if os.environ.get("CTS_BINANCE_NETWORK") != "TESTNET":
    sys.exit("网络不是 TESTNET")
account_id = os.environ["CTS_READONLY_ACCOUNT_ID"]
order_db = os.environ["CTS_READONLY_ORDER_DB"]
risk_db = os.environ["CTS_READONLY_RISK_DB"]
for path in (order_db, risk_db):
    if not Path(path).is_file():
        sys.exit(f"原库不存在，未新建：{path}")
print(run_readonly_verification(
    opener=_urllib_opener(),
    evidence_dir=Path("/Users/xinquanwang/8888/var/readonly-evidence"),
    account_id=account_id,
    order_db=order_db,
    risk_db=risk_db,
    network="TESTNET",
    credentials=credentials_from_env(),
    code_sha="5b51364a52ef935b9f7c014711c637dc127771f3",
    plan_sha=PLAN_BASELINE,
))
PY
```

密钥只留在本机环境变量里。命令和本文件都不写入密钥。

`LiveSession` 不能当作这次入口，打开会话会写订单库。只读钱包展示接口也不能代替这次核验。

## 5. 仍未做的事

没有修改产品源码，没有访问币安，没有读取或记录密钥，没有执行上面的命令。原库仍未绑定。执行决定还没有作。

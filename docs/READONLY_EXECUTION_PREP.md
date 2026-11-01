# 真实只读核验执行材料

状态：**新建本地账本已完成，真实只读核验未决定。** 初始化证据见 `docs/LOCAL_LEDGER_INIT.md`。

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

本机没有可当作原库的订单库和风险库。这次核对只打开已有文件，没有新建。

查过的位置：仓库目录、桌面、文稿、`~/Library/Application Support`，以及用户主目录里除照片、音乐、依赖缓存和 `Library` 其余部分以外的数据库。环境变量里没有账户编号。`.env` 只有 `CTS_BINANCE_API_KEY`、`CTS_BINANCE_API_SECRET`、`CTS_BINANCE_NETWORK` 这三个名字，网络变量的值是 `TESTNET`。

临时目录里有 pytest 留下的库，还有 98 个 `cts-orders-*.db`。后者的账户只有 `acct-1` 和 `acct-durable-path`，`live_session_meta` 都是空的。这些是测试残留，不是已经绑定的原库，也不能拿来通过只读核验。

| 项 | 当前值 |
|---|---|
| 账户 | `local-ledger` |
| 订单库 | `/Users/xinquanwang/8888/var/ledger/orders.db`。已创建，勿覆盖 |
| 风险库 | `/Users/xinquanwang/8888/var/ledger/risk.db`。已创建，勿覆盖 |
| 本地起始权益 | `0` |
| `CTS_BINANCE_NETWORK` | `TESTNET` |

只读命令发现原库缺失就停止，并且不新建。下面不把临时库填进命令。`CTS_READONLY_ACCOUNT_ID`、`CTS_READONLY_ORDER_DB`、`CTS_READONLY_RISK_DB` 现在仍不设置。

## 3. 首次初始化

登记名称：**新建本地账本**。初始化已在本地完成，证据见 `docs/LOCAL_LEDGER_INIT.md`。

这是一本新账，不是历史订单的恢复，也不是交易所对账。真实只读核验仍要另一次决定。下面的步骤是当时采用的顺序，现在两个文件已经存在，不能再执行一遍。

| 输入 | 值 |
|---|---|
| 本地账户编号 | `local-ledger` |
| 订单库 | `/Users/xinquanwang/8888/var/ledger/orders.db` |
| 风险库 | `/Users/xinquanwang/8888/var/ledger/risk.db` |
| 本地起始权益 | `0` |

`0` 是这本新风险账本的计算起点，供以后的额度计算使用。它不记载币安的钱包余额、可用余额或保证金余额。交易所余额要等另一次授权的只读核验实际读到以后才有证据；在那之前保持未知。

两个绝对路径当时都不存在，位于已忽略的 `var/` 下。实际顺序是：

1. 用 `RiskEngine.create` 建立风险库，然后关闭。没有调用 `RiskEngine.reopen`。
2. 用 `OrderStore` 建立订单库，把该账户的 `risk_db_path` 写成风险库的解析后路径，然后关闭。没有打开 `LiveSession`。
3. 没有访问币安。这一步没有授权随后的真实只读核验。

只读复查的结果写在 `docs/LOCAL_LEDGER_INIT.md`。只读运行器仍然不会在库缺失时自己建库。

## 4. 证据输出目录

`/Users/xinquanwang/8888/var/readonly-evidence`

`var/` 已忽略，不提交证据。目录里将来会有方案第 6 节的 E1–E11。生成并封印之后不再改。证据不写密钥、签名值或完整签名 URL。

## 5. 执行命令

命令只在冻结提交的独立工作目录里加载源码。当前分支后续的文档改动不会被这次核验导入。工作目录是 `/Users/xinquanwang/8888/var/readonly-worktree`，位于已忽略的 `var/` 下。目录若已存在就停，不覆盖、不重置。

导入产品模块之前先做三件事：`HEAD` 必须等于完整冻结 SHA；`src` 和 `pyproject.toml` 没有已修改或已暂存的差异；`src` 下没有未跟踪代码。E1 使用这次 `git rev-parse HEAD` 的结果。冻结 SHA 只用来核对，不直接写进证据。

下面的命令现在会停在授权检查。原库还没有绑定，也不要设置 `CTS_READONLY_EXECUTION`。

```bash
git -C /Users/xinquanwang/8888 worktree add --detach \
  /Users/xinquanwang/8888/var/readonly-worktree \
  5b51364a52ef935b9f7c014711c637dc127771f3
cd /Users/xinquanwang/8888/var/readonly-worktree
env -u PYTHONPATH /Users/xinquanwang/8888/.venv/bin/python - <<'PY'
import os
import subprocess
import sys
from pathlib import Path

FREEZE = "5b51364a52ef935b9f7c014711c637dc127771f3"
root = Path("/Users/xinquanwang/8888/var/readonly-worktree")

def git(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout

if os.environ.get("CTS_READONLY_EXECUTION") != "授权本进程按冻结方案执行一次只读核验":
    sys.exit("只读核验尚未授权执行")
head = git("rev-parse", "HEAD").strip()
if head != FREEZE:
    sys.exit(f"HEAD 不是冻结提交：{head}")
dirty = git("status", "--porcelain", "--untracked-files=all", "--", "src", "pyproject.toml")
untracked = git("ls-files", "--others", "--exclude-standard", "--", "src")
if dirty.strip() or untracked.strip():
    sys.exit("源码或依赖配置已变化，或 src 下有未跟踪代码")
code_sha = git("rev-parse", "HEAD").strip()
if code_sha != head:
    sys.exit("源码 SHA 在核对后发生了变化")

sys.path.insert(0, str(root / "src"))
from cts.execution.live import _urllib_opener, credentials_from_env, load_local_env
from cts.ops.readonly_verify import PLAN_BASELINE, run_readonly_verification

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
    code_sha=code_sha,
    plan_sha=PLAN_BASELINE,
))
PY
```

密钥只留在本机环境变量里。命令和本文件都不写入密钥。

`LiveSession` 不能当作这次入口，打开会话会写订单库。只读钱包展示接口也不能代替这次核验。

## 6. 仍未做的事

产品源码没有改。没有访问币安，没有读取或记录密钥，也没有执行第 5 节的真实只读核验命令。本地账本已经按第 3 节建好。真实只读核验还没有决定。

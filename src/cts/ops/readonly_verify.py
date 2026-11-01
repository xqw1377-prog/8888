"""TESTNET 只读核验。调用方不传入传输器时不访问网络。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlsplit

from cts.errors import CtsError
from cts.execution.live import (
    TESTNET_BASE,
    TransportResult,
    VenueCredentials,
    _RefuseRedirect,
    _bound_demo_url,
    _require_explicit_false,
    _sign,
    _ssl_context,
)

PLAN_BASELINE = "4ae24f4ee2293e68d607d26c872e671ff5d442d6"
DEMO_HOST = "demo-fapi.binance.com"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
MANIFEST_NAME = "manifest.sha256"
DIGEST_NAME = "manifest.sha256.digest"
EXCLUDED = frozenset({MANIFEST_NAME, DIGEST_NAME})

PUBLIC_PATHS = frozenset({"/fapi/v1/time", "/fapi/v1/exchangeInfo"})
SIGNED_PATHS = {
    "/fapi/v1/positionSide/dual": frozenset(),
    "/fapi/v1/multiAssetsMargin": frozenset(),
    "/fapi/v2/account": frozenset(),
    "/fapi/v2/positionRisk": frozenset({"symbol"}),
    "/fapi/v1/openOrders": frozenset({"symbol"}),
}

Opener = Callable[[str, str, dict[str, str]], object]
REQUIRED_CHECKS = (
    "原订单库",
    "原风险库",
    "账户归属",
    "未决订单",
    "风险阻断",
    "时钟取样1",
    "时钟取样2",
    "时钟取样3",
    "交易规则",
    "持仓模式",
    "联合保证金",
    "逐仓BTCUSDT",
    "逐仓ETHUSDT",
    "钱包",
    "未完成委托BTCUSDT",
    "未完成委托ETHUSDT",
)
UNRESOLVED_ORDER_STATUSES = frozenset({"UNKNOWN", "QUERYING", "CANCELING"})
WALLET_FIELDS = ("walletBalance", "availableBalance", "unrealizedProfit", "marginBalance")


@dataclass(frozen=True)
class ClockSample:
    t0: int
    t1: int
    m0: int
    m1: int
    server_time: int


@dataclass(frozen=True)
class ClockJudgement:
    verdict: str
    rtt: int
    uncertainty: str
    offset: str
    reason: str


class Clock:
    def utc_millis(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic_millis(self) -> int:
        return time.monotonic_ns() // 1_000_000


def transport_security():
    """只读运行器使用的 TLS 上下文。校验打开，不访问网络。"""
    return _ssl_context()


def refuse_redirect(status: int, location: str) -> None:
    _RefuseRedirect().redirect_request(None, None, status, "moved", {}, location)


def judge_clock(sample: ClockSample) -> ClockJudgement:
    """RTT 与不确定度只来自单调时钟。UTC 回拨或两端经过时间不一致则失败。"""
    if sample.m1 < sample.m0:
        return _clock_fail(sample, "单调时钟倒退")
    rtt = sample.m1 - sample.m0
    if sample.t1 < sample.t0:
        return _clock_result(sample, rtt, "FAIL", "UTC时间倒退")
    if (sample.t1 - sample.t0) != rtt:
        return _clock_result(sample, rtt, "FAIL", "UTC经过时间与单调时钟不一致")
    twice_offset = (2 * sample.server_time) - (sample.t0 + sample.t1)
    if rtt > 2000 or abs(twice_offset) + rtt > 2000:
        reason = "不确定度超过1000毫秒" if rtt > 2000 else "偏移加不确定度超过1000毫秒"
        return _clock_result(sample, rtt, "FAIL", reason)
    return _clock_result(sample, rtt, "PASS", "")


def redact_text(text: str, secrets: tuple[str, ...] = ()) -> str:
    """去掉密钥、查询串签名、JSON 签名字段和完整 URL。"""
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[已脱敏]")
    cleaned = re.sub(
        r'(?is)(["\']signature["\']\s*:\s*)(["\'])(?:\\.|(?!\2).)*\2',
        r"\1\2[已脱敏]\2",
        cleaned,
    )
    cleaned = re.sub(r"(?i)\bsignature\b\s*[:=]\s*[\"']?[^\"'&\s,}\]]+", "signature=[已脱敏]", cleaned)
    cleaned = re.sub(r"https?://\S+", "[已脱敏URL]", cleaned)
    return cleaned


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def seal_evidence(directory: Path) -> str:
    """按 UTF-8 路径字节序写 manifest，E11 只写入摘要文件。"""
    lines: list[tuple[str, str]] = []
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        if relative in EXCLUDED or relative.startswith("/") or ".." in relative.split("/"):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append((relative, digest))
    lines.sort(key=lambda item: item[0].encode("utf-8"))
    body = "".join(f"{name} {digest}\n" for name, digest in lines)
    manifest = directory / MANIFEST_NAME
    manifest.write_text(body, encoding="utf-8")
    e11 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    (directory / DIGEST_NAME).write_text(e11 + "\n", encoding="utf-8")
    return e11


def audit_evidence(directory: Path) -> str:
    """重算清单与 E11。不改写证据。"""
    lines: list[tuple[str, str]] = []
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        if relative in EXCLUDED:
            continue
        lines.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    lines.sort(key=lambda item: item[0].encode("utf-8"))
    expected = "".join(f"{name} {digest}\n" for name, digest in lines)
    manifest = directory / MANIFEST_NAME
    digest_file = directory / DIGEST_NAME
    if not manifest.is_file() or not digest_file.is_file():
        return "FAIL"
    if manifest.read_text(encoding="utf-8") != expected:
        return "FAIL"
    e11 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    recorded = digest_file.read_text(encoding="utf-8").strip()
    return "PASS" if recorded == e11 else "FAIL"


def run_readonly_verification(
    *,
    opener: Opener,
    evidence_dir: Path,
    account_id: str,
    order_db: str,
    risk_db: str,
    network: str,
    credentials: VenueCredentials,
    code_sha: str,
    clock: Clock | None = None,
    known_client_order_ids: set[str] | None = None,
    plan_sha: str = PLAN_BASELINE,
) -> str:
    """执行白名单内的只读核对并封印证据。不创建订单库。

    ``known_client_order_ids`` 不授予登记资格。未完成委托只认原订单库里的账户与订单身份。
    """
    del known_client_order_ids
    evidence_dir.mkdir(parents=True, exist_ok=True)
    runner = _Runner(
        opener=opener,
        directory=evidence_dir,
        account_id=account_id,
        order_db=order_db,
        risk_db=risk_db,
        network=network,
        credentials=credentials,
        code_sha=code_sha,
        plan_sha=plan_sha,
        clock=clock or Clock(),
    )
    pending: Exception | None = None
    try:
        runner.execute()
    except Exception as exc:
        runner.capture(exc)
        pending = exc
    finally:
        runner.recheck_end()
        runner.close_gaps()
        runner.write()
        seal_evidence(evidence_dir)
    if pending is not None:
        cleaned = CtsError(
            redact_text(str(pending), runner.secrets),
            reason_code=getattr(pending, "reason_code", "RECONCILIATION_FAILED"),
        )
        raise cleaned
    return runner.verdict


class _Runner:
    def __init__(self, **kwargs) -> None:
        self.opener: Opener = kwargs["opener"]
        self.directory: Path = kwargs["directory"]
        self.account_id = kwargs["account_id"]
        self.order_db = kwargs["order_db"]
        self.risk_db = kwargs["risk_db"]
        self.network = kwargs["network"]
        self.credentials: VenueCredentials = kwargs["credentials"]
        self.code_sha = kwargs["code_sha"]
        self.plan_sha = kwargs["plan_sha"]
        self.clock: Clock = kwargs["clock"]
        self.started = self.clock.utc_millis()
        self.ended = self.started
        self.requests: list[str] = []
        self.checks: list[tuple[str, str, str]] = []
        self.samples: list[tuple[ClockSample, ClockJudgement]] = []
        self.rules_text = ""
        self.wallet_text = "暂无证据"
        self.orders_text = "NOT_RUN"
        self.end_snapshot = ""
        self.stopped = False
        self.secrets = (self.credentials.api_key, self.credentials.api_secret)

    def execute(self) -> None:
        try:
            self._execute()
        except CtsError:
            raise
        except Exception as exc:
            self.capture(exc)

    def capture(self, exc: Exception) -> None:
        if not any(name == "传输" for name, _, _ in self.checks):
            self._check("传输", "FAIL", "传输或解析异常，原文已脱敏")
        self.stopped = True

    def close_gaps(self) -> None:
        present = {name for name, _, _ in self.checks}
        for name in REQUIRED_CHECKS:
            if name not in present:
                self._check(name, "FAIL", "未完成")

    def _execute(self) -> None:
        self._inspect_local()
        if self.stopped:
            return
        if self.network != "TESTNET" or not self.account_id or not self.order_db or not self.risk_db:
            self._check("账户绑定", "FAIL", "账户、库路径或网络未绑定到 TESTNET")
            self.stopped = True
            return
        self._check("账户绑定", "PASS", "本地账户、订单库、风险库与 TESTNET 已绑定")
        self._clock_samples()
        if self.stopped:
            self._skip_rest()
            return
        self._rules()
        if self.stopped:
            self._skip_signed()
            return
        self._modes()
        if self.stopped:
            self._skip_after_mode()
            return
        self._wallet()
        if self.stopped:
            self._mark("未完成委托", "NOT_RUN", "前序检查未通过，未再发送")
            return
        self._open_orders()

    def _clock_samples(self) -> None:
        for index in range(1, 4):
            if self.stopped:
                self._check(f"时钟取样{index}", "NOT_RUN", "前序取样未通过，未再发送")
                continue
            sample, judgement = self._one_clock()
            self.samples.append((sample, judgement))
            self._check(f"时钟取样{index}", judgement.verdict, judgement.reason or "单调时钟不确定度在限额内")
            if judgement.verdict != "PASS":
                self.stopped = True

    def _one_clock(self) -> tuple[ClockSample, ClockJudgement]:
        t0 = self.clock.utc_millis()
        m0 = self.clock.monotonic_millis()
        status, body = self._send("GET", "/fapi/v1/time", (), stamp=t0)
        t1 = self.clock.utc_millis()
        m1 = self.clock.monotonic_millis()
        server = _server_time(body) if status == 200 else None
        if server is None:
            sample = ClockSample(t0, t1, m0, m1, 0)
            return sample, _clock_result(sample, max(m1 - m0, 0), "FAIL", "服务器时间缺失")
        sample = ClockSample(t0, t1, m0, m1, server)
        return sample, judge_clock(sample)

    def _rules(self) -> None:
        status, body = self._send("GET", "/fapi/v1/exchangeInfo", ())
        if status != 200 or not isinstance(body, dict):
            self._check("交易规则", "FAIL", "交易规则正文不可读")
            self.stopped = True
            return
        symbols = body.get("symbols")
        if not isinstance(symbols, list):
            self._check("交易规则", "FAIL", "交易规则缺少品种列表")
            self.stopped = True
            return
        picked = []
        for name in SYMBOLS:
            row = next((item for item in symbols if isinstance(item, dict) and item.get("symbol") == name), None)
            if row is None:
                self._check("交易规则", "FAIL", f"{name} 不在交易规则中")
                self.stopped = True
                return
            reason = _rule_reason(row)
            if reason:
                self._check("交易规则", "FAIL", reason)
                self.stopped = True
                return
            picked.append(canonical_json(row))
        self.rules_text = "\n".join(picked)
        digest = hashlib.sha256(self.rules_text.encode("utf-8")).hexdigest()
        self._check("交易规则", "PASS", digest)

    def _modes(self) -> None:
        self._flag("/fapi/v1/positionSide/dual", "dualSidePosition", "持仓模式")
        if self.stopped:
            self._check("联合保证金", "NOT_RUN", "前序检查未通过，未再发送")
            self._check("逐仓BTCUSDT", "NOT_RUN", "前序检查未通过，未再发送")
            self._check("逐仓ETHUSDT", "NOT_RUN", "前序检查未通过，未再发送")
            return
        self._flag("/fapi/v1/multiAssetsMargin", "multiAssetsMargin", "联合保证金")
        if self.stopped:
            self._check("逐仓BTCUSDT", "NOT_RUN", "前序检查未通过，未再发送")
            self._check("逐仓ETHUSDT", "NOT_RUN", "前序检查未通过，未再发送")
            return
        for symbol in SYMBOLS:
            self._margin(symbol)
            if self.stopped and symbol == "BTCUSDT":
                self._check("逐仓ETHUSDT", "NOT_RUN", "前序检查未通过，未再发送")
                return

    def _flag(self, path: str, key: str, name: str) -> None:
        status, body = self._send("GET", path, _signed_params(None, self.clock.utc_millis(), self.credentials))
        if status != 200 or not isinstance(body, dict) or key not in body:
            self._check(name, "FAIL", f"{key} 缺失或正文不可读")
            self.stopped = True
            return
        try:
            _require_explicit_false(body, key, f"{key} 不是 false")
        except CtsError as exc:
            self._check(name, "FAIL", redact_text(str(exc), self.secrets))
            self.stopped = True
            return
        self._check(name, "PASS", f"{key}=false")

    def _margin(self, symbol: str) -> None:
        status, body = self._send(
            "GET",
            "/fapi/v2/positionRisk",
            _signed_params(symbol, self.clock.utc_millis(), self.credentials),
        )
        rows = _rows(body, "positions")
        match = next((row for row in rows if row.get("symbol") == symbol), None)
        margin = None if match is None else match.get("marginType")
        if status != 200 or not isinstance(margin, str) or margin.casefold() != "isolated":
            self._check(f"逐仓{symbol}", "FAIL", "marginType 缺失或无法核实为逐仓")
            self.stopped = True
            return
        self._check(f"逐仓{symbol}", "PASS", margin)

    def _wallet(self) -> None:
        try:
            status, body = self._send("GET", "/fapi/v2/account", _signed_params(None, self.clock.utc_millis(), self.credentials))
            assets = body.get("assets") if isinstance(body, dict) else None
            if status != 200 or not isinstance(assets, list):
                self.wallet_text = "暂无证据"
                self._check("钱包", "FAIL", "钱包正文无法解析")
                self.stopped = True
                return
            usdt_rows = [row for row in assets if isinstance(row, dict) and row.get("asset") == "USDT"]
            if len(usdt_rows) != 1:
                self.wallet_text = _wallet_lines(usdt_rows) or "暂无证据"
                detail = "USDT 行重复或歧义" if len(usdt_rows) > 1 else "USDT 钱包字段缺失"
                self._check("钱包", "FAIL", detail)
                self.stopped = True
                return
            usdt = usdt_rows[0]
            if any(key not in usdt for key in WALLET_FIELDS):
                self.wallet_text = "暂无证据"
                self._check("钱包", "FAIL", "USDT 钱包字段缺失")
                self.stopped = True
                return
            lines = []
            finite = True
            for key in WALLET_FIELDS:
                text, ok = _finite_original(usdt[key])
                lines.append(f"{key}={text}")
                finite = finite and ok
            self.wallet_text = "\n".join(lines)
            if not finite:
                self._check("钱包", "FAIL", "钱包字段不是有限数值")
                self.stopped = True
                return
            self._check("钱包", "PASS", "已记录 USDT 四个字段原文")
        except Exception:
            self.wallet_text = "暂无证据"
            self._check("钱包", "FAIL", "钱包正文无法解析")
            self.stopped = True

    def _open_orders(self) -> None:
        lines = []
        try:
            for symbol in SYMBOLS:
                status, body = self._send(
                    "GET",
                    "/fapi/v1/openOrders",
                    _signed_params(symbol, self.clock.utc_millis(), self.credentials),
                )
                rows = _order_rows(body)
                if status != 200 or rows is None:
                    self.orders_text = "暂无证据" if not lines else "\n".join(lines)
                    self._check(f"未完成委托{symbol}", "FAIL", "委托正文或行结构不合法")
                    self.stopped = True
                    return
                book = _registered_orders(Path(self.order_db))
                if book is None:
                    self.orders_text = "\n".join(lines) if lines else "暂无证据"
                    self._check(f"未完成委托{symbol}", "FAIL", "原订单库无法核对委托登记")
                    self.stopped = True
                    return
                identities = [str(row["clientOrderId"]) for row in rows]
                unknown = [row for row in rows if not _same_order(book, row, symbol, self.account_id)]
                lines.append(f"{symbol} count={len(rows)} clientOrderId={','.join(identities)}")
                if unknown:
                    self.orders_text = "\n".join(lines)
                    self._check(f"未完成委托{symbol}", "FAIL", "委托未在原订单库登记或订单身份不一致")
                    self.stopped = True
                    return
                self._check(f"未完成委托{symbol}", "PASS", f"count={len(rows)}")
        except Exception:
            self.orders_text = "暂无证据"
            self._check("未完成委托", "FAIL", "委托正文无法解析")
            self.stopped = True
            return
        self.orders_text = "\n".join(lines)

    def _send(self, method: str, path: str, params: tuple[tuple[str, str], ...], stamp: int | None = None) -> tuple[int, object]:
        if self.stopped:
            raise CtsError("前序检查未通过，未再发送", reason_code="LIVE_VENUE_UNVERIFIED")
        _validate(method, path, params)
        query = "&".join(f"{key}={value}" for key, value in params)
        if self.credentials.api_secret and self.credentials.api_secret in query:
            raise CtsError("签名材料异常，未发送请求", reason_code="CREDENTIALS_MISSING")
        url = enforce_host(_bound_demo_url(path, query))
        collected = _iso(self.clock.utc_millis() if stamp is None else stamp)
        try:
            result = self.opener(method, url, {"X-MBX-APIKEY": self.credentials.api_key})
        except Exception as exc:
            self.requests.append(f"{collected} GET {path} {DEMO_HOST} 未完成")
            self._check("传输", "FAIL", "传输或解析异常，原文已脱敏")
            self.stopped = True
            cleaned = CtsError(
                redact_text(str(exc), self.secrets),
                reason_code=getattr(exc, "reason_code", "RECONCILIATION_FAILED"),
            )
            raise cleaned from None
        status, body = _unpack(result)
        self.requests.append(f"{collected} GET {path} {DEMO_HOST} {status}")
        if 300 <= status < 400:
            self._check("传输", "FAIL", "拒绝离开绑定主机的重定向，未跟随新地址")
            self.stopped = True
            raise CtsError("拒绝离开绑定主机的重定向，未跟随新地址", reason_code="LIVE_VENUE_UNVERIFIED")
        return status, body

    def _skip_rest(self) -> None:
        for name in ("交易规则", "持仓模式", "联合保证金", "逐仓BTCUSDT", "逐仓ETHUSDT", "钱包", "未完成委托"):
            self._check(name, "NOT_RUN", "前序检查未通过，未再发送")

    def _skip_signed(self) -> None:
        for name in ("持仓模式", "联合保证金", "逐仓BTCUSDT", "逐仓ETHUSDT", "钱包", "未完成委托"):
            self._check(name, "NOT_RUN", "前序检查未通过，未再发送")

    def _skip_after_mode(self) -> None:
        if not any(name == "钱包" for name, _, _ in self.checks):
            self._check("钱包", "NOT_RUN", "前序检查未通过，未再发送")
        if not any(name.startswith("未完成委托") for name, _, _ in self.checks):
            self._check("未完成委托", "NOT_RUN", "前序检查未通过，未再发送")

    def _check(self, name: str, verdict: str, detail: str) -> None:
        self.checks.append((name, verdict, redact_text(detail, self.secrets)))

    def _mark(self, name: str, verdict: str, detail: str) -> None:
        self._check(name, verdict, detail)

    @property
    def verdict(self) -> str:
        recorded = {name: result for name, result, _ in self.checks}
        if any(recorded.get(name) != "PASS" for name in REQUIRED_CHECKS):
            return "只读核验未通过"
        if not self.rules_text or len(self.samples) != 3:
            return "只读核验未通过"
        return "只读核验通过"

    def _inspect_local(self) -> None:
        try:
            order_path = Path(self.order_db)
            risk_path = Path(self.risk_db)
            if not order_path.is_file() or not risk_path.is_file():
                self._check("原订单库", *_file_gate(order_path, "原订单库"))
                self._check("原风险库", *_file_gate(risk_path, "原风险库"))
                self._check("账户归属", "FAIL", "原库缺失，无法核实账户归属")
                self._check("未决订单", "FAIL", "原库缺失，无法核实未决订单")
                self._check("风险阻断", "FAIL", "原库缺失，无法核实风险阻断")
                return
            try:
                orders, meta = _read_order_db(order_path)
                accounts, holds, blockers = _read_risk_db(risk_path)
            except Exception:
                self._check("原订单库", "FAIL", "原订单库无法只读核对")
                self._check("原风险库", "FAIL", "原风险库无法只读核对")
                self._check("账户归属", "FAIL", "原库无法核实账户归属")
                self._check("未决订单", "FAIL", "原库无法核实未决订单")
                self._check("风险阻断", "FAIL", "原库无法核实风险阻断")
                return
            self._check("原订单库", "PASS", "已只读打开原订单库")
            self._check("原风险库", "PASS", "已只读打开原风险库")
            self._record_ownership(orders, meta, accounts, risk_path)
            self._record_pending(orders)
            self._record_blocks(holds, blockers)
        finally:
            self._stop_if_local_failed()

    def _record_ownership(self, orders: list, meta: list, accounts: set[str], risk_path: Path) -> None:
        foreign_orders = [row for row in orders if row["account_id"] != self.account_id]
        owned = [row for row in meta if row["account_id"] == self.account_id]
        path_mismatch = True
        if len(owned) == 1 and str(owned[0]["risk_db_path"]).strip():
            path_mismatch = Path(str(owned[0]["risk_db_path"])).resolve() != risk_path.resolve()
        if len(owned) != 1 or path_mismatch:
            self._check("账户归属", "FAIL", "缺少匹配的账户与风险库路径绑定")
            return
        if accounts != {self.account_id} or foreign_orders:
            self._check("账户归属", "FAIL", "账户与原库归属不一致")
            return
        self._check("账户归属", "PASS", "原库账户与风险库路径绑定一致")

    def _record_pending(self, orders: list) -> None:
        pending = [
            row for row in orders
            if row["account_id"] == self.account_id and row["status"] in UNRESOLVED_ORDER_STATUSES
        ]
        if pending:
            self._check("未决订单", "FAIL", "原订单库仍有未决订单")
        else:
            self._check("未决订单", "PASS", "原订单库没有未决订单")

    def _record_blocks(self, holds: int, blockers: int) -> None:
        if holds or blockers:
            self._check("风险阻断", "FAIL", "原风险库仍有阻断")
        else:
            self._check("风险阻断", "PASS", "原风险库没有阻断")

    def _stop_if_local_failed(self) -> None:
        recorded = {name: result for name, result, _ in self.checks}
        names = ("原订单库", "原风险库", "账户归属", "未决订单", "风险阻断")
        if any(recorded.get(name) != "PASS" for name in names):
            self.stopped = True

    def recheck_end(self) -> None:
        """封存前用结束快照重核账户归属、风险库路径、未决订单和风险阻断。"""
        order_path = Path(self.order_db)
        risk_path = Path(self.risk_db)
        try:
            if not order_path.is_file() or not risk_path.is_file():
                self.end_snapshot = "结束快照 原库缺失 账户归属=未核对 未决订单=未核对 风险阻断=未核对"
                self._check("账户归属", "FAIL", "结束快照时原库缺失，未再放行")
                self._check("未决订单", "FAIL", "结束快照时原库缺失，未再放行")
                self._check("风险阻断", "FAIL", "结束快照时原库缺失，未再放行")
                return
            orders, meta = _read_order_db(order_path)
            accounts, holds, blockers = _read_risk_db(risk_path)
        except Exception:
            self.end_snapshot = "结束快照 无法只读核对 账户归属=未核对"
            self._check("账户归属", "FAIL", "结束快照无法只读核对")
            self._check("未决订单", "FAIL", "结束快照无法只读核对")
            self._check("风险阻断", "FAIL", "结束快照无法只读核对")
            return
        pending = [
            row for row in orders
            if row["account_id"] == self.account_id and row["status"] in UNRESOLVED_ORDER_STATUSES
        ]
        identities = [str(row["client_order_id"]) for row in pending]
        self.end_snapshot = (
            f"结束快照 未决订单={len(pending)} clientOrderId={','.join(identities) or '-'} "
            f"风险持有={holds} 未清除阻断={blockers} "
            f"{_binding_snapshot(meta, accounts, risk_path, self.account_id)}"
        )
        self._record_ownership(orders, meta, accounts, risk_path)
        if pending:
            self._check("未决订单", "FAIL", "结束快照仍有未决订单")
        else:
            self._check("未决订单", "PASS", "结束快照没有未决订单")
        if holds or blockers:
            self._check("风险阻断", "FAIL", "结束快照仍有阻断")
        else:
            self._check("风险阻断", "PASS", "结束快照没有阻断")

    def write(self) -> None:
        self.ended = self.clock.utc_millis()
        rules_hash = hashlib.sha256(self.rules_text.encode("utf-8")).hexdigest() if self.rules_text else ""
        clock_lines = []
        for sample, judgement in self.samples:
            clock_lines.append(
                " ".join(
                    [
                        f"t0={sample.t0}",
                        f"t1={sample.t1}",
                        f"m0={sample.m0}",
                        f"m1={sample.m1}",
                        f"serverTime={sample.server_time}",
                        f"rtt={judgement.rtt}",
                        f"u={judgement.uncertainty}",
                        f"offset={judgement.offset}",
                        f"verdict={judgement.verdict}",
                        f"reason={judgement.reason}",
                    ]
                )
            )
        check_lines = [f"{name} {verdict} {detail}".rstrip() for name, verdict, detail in self.checks]
        if rules_hash:
            check_lines.append(f"规则原文SHA256 {rules_hash}")
        if self.end_snapshot:
            check_lines.append(self.end_snapshot)
        check_lines.append(self.verdict)
        files = {
            "e1_code.txt": f"code_sha={self.code_sha}\nplan_sha={self.plan_sha}\n",
            "e2_collection.txt": f"start={_iso(self.started)}\nend={_iso(self.ended)}\n" + "\n".join(self.requests) + "\n",
            "e3_binding.txt": (
                f"account_id={self.account_id}\norder_db={self.order_db}\nrisk_db={self.risk_db}\n"
                f"network={self.network}\nhost={DEMO_HOST}\n"
            ),
            "e4_requests.txt": "\n".join(self.requests) + ("\n" if self.requests else ""),
            "e5_checks.txt": "\n".join(check_lines) + "\n",
            "e6_clock.txt": "\n".join(clock_lines) + ("\n" if clock_lines else ""),
            "e7_rules.txt": (self.rules_text + "\n") if self.rules_text else "",
            "e8_wallet.txt": self.wallet_text + "\n",
            "e9_open_orders.txt": self.orders_text + "\n",
            "e10_credentials.txt": "凭据已在本机环境变量中，未写入仓库。网络变量为 TESTNET\n",
        }
        for name, content in files.items():
            (self.directory / name).write_text(redact_text(content, self.secrets), encoding="utf-8")


def _validate(method: str, path: str, params: tuple[tuple[str, str], ...]) -> None:
    if method != "GET":
        raise CtsError("只读白名单拒绝写请求", reason_code="LIVE_VENUE_UNVERIFIED")
    keys = {key for key, _ in params}
    if path in PUBLIC_PATHS:
        if keys:
            raise CtsError("只读白名单拒绝该查询", reason_code="LIVE_VENUE_UNVERIFIED")
        return
    if path not in SIGNED_PATHS:
        raise CtsError("只读白名单拒绝该路径", reason_code="LIVE_VENUE_UNVERIFIED")
    allowed = SIGNED_PATHS[path] | frozenset({"recvWindow", "timestamp", "signature"})
    if keys != allowed:
        raise CtsError("只读白名单拒绝该查询", reason_code="LIVE_VENUE_UNVERIFIED")
    if "symbol" in allowed and dict(params).get("symbol") not in SYMBOLS:
        raise CtsError("只读白名单拒绝该品种", reason_code="LIVE_VENUE_UNVERIFIED")


def _signed_params(symbol: str | None, now_ms: int, credentials: VenueCredentials) -> tuple[tuple[str, str], ...]:
    items: list[tuple[str, str]] = []
    if symbol is not None:
        items.append(("symbol", symbol))
    items.extend((("recvWindow", "5000"), ("timestamp", str(now_ms))))
    signed = _sign(items, credentials.api_secret)
    return tuple(part.split("=", 1) for part in signed.split("&"))


def _unpack(result: object) -> tuple[int, object]:
    if isinstance(result, TransportResult):
        return result.status, result.body
    return 200, result


def _finite_original(value: object) -> tuple[str, bool]:
    if isinstance(value, bool) or value is None:
        return ("null" if value is None else json.dumps(value), False)
    if isinstance(value, str):
        text = value
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        text = json.dumps(value)
    else:
        return json.dumps(value, ensure_ascii=False), False
    try:
        number = Decimal(text)
    except Exception:
        return text, False
    return text, bool(number.is_finite())


def _binding_snapshot(meta: list, accounts: set[str], risk_path: Path, account_id: str) -> str:
    owned = [row for row in meta if row["account_id"] == account_id]
    if len(owned) != 1 or not str(owned[0]["risk_db_path"]).strip():
        binding = "缺失"
    elif Path(str(owned[0]["risk_db_path"])).resolve() != risk_path.resolve():
        binding = "不匹配"
    else:
        binding = "匹配"
    observed = ",".join(sorted(accounts)) or "缺失"
    return f"账户={observed} 风险库绑定={binding}"


def _wallet_lines(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        for key in WALLET_FIELDS:
            if key not in row:
                lines.append(f"{key}=缺失")
                continue
            text, _ok = _finite_original(row[key])
            lines.append(f"{key}={text}")
    return "\n".join(lines)


def _registered_orders(path: Path) -> list[sqlite3.Row] | None:
    if not path.is_file():
        return None
    try:
        orders, _meta = _read_order_db(path)
    except Exception:
        return None
    return orders


def _same_order(book: list, venue: dict, symbol: str, account_id: str) -> bool:
    """登记只认原订单库。账户、品种、方向和数量都要与库内那一行一致。"""
    client_id = venue.get("clientOrderId")
    matches = [row for row in book if str(row["client_order_id"]) == client_id]
    if len(matches) != 1:
        return False
    row = matches[0]
    if str(row["account_id"]) != account_id or str(row["symbol"]) != symbol:
        return False
    if venue.get("symbol") != symbol:
        return False
    side = venue.get("side")
    qty = venue.get("origQty")
    if not isinstance(side, str) or qty is None:
        return False
    if not _same_side(str(row["side"]), side):
        return False
    try:
        return Decimal(str(row["qty"])) == Decimal(str(qty))
    except Exception:
        return False


def _same_side(stored: str, venue: str) -> bool:
    left = stored.upper()
    right = venue.upper()
    if left == right:
        return True
    return (left, right) in {("LONG", "BUY"), ("SHORT", "SELL")}


def _order_rows(body: object) -> list[dict] | None:
    if isinstance(body, list):
        rows = body
    elif isinstance(body, dict) and isinstance(body.get("orders"), list) and set(body) <= {"orders"}:
        rows = body["orders"]
    else:
        return None
    parsed: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("clientOrderId"), str) or not row["clientOrderId"].strip():
            return None
        parsed.append(row)
    return parsed


def _file_gate(path: Path, label: str) -> tuple[str, str]:
    if not path.is_file():
        return "FAIL", f"{label}不存在，未新建"
    try:
        if label == "原订单库":
            _read_order_db(path)
        else:
            _read_risk_db(path)
    except Exception:
        return "FAIL", f"{label}无法只读核对"
    return "PASS", f"已只读打开{label}"


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(path.resolve().as_posix()) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _read_order_db(path: Path) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    connection = _connect_readonly(path)
    try:
        orders = connection.execute(
            "SELECT client_order_id, account_id, symbol, side, qty, status FROM live_orders"
        ).fetchall()
        meta = connection.execute("SELECT account_id, risk_db_path FROM live_session_meta").fetchall()
        return list(orders), list(meta)
    finally:
        connection.close()


def _read_risk_db(path: Path) -> tuple[set[str], int, int]:
    connection = _connect_readonly(path)
    try:
        accounts = {str(row["account_id"]) for row in connection.execute("SELECT account_id FROM account_state")}
        holds = int(connection.execute("SELECT COUNT(*) FROM live_risk_holds").fetchone()[0])
        blockers = int(
            connection.execute("SELECT COUNT(*) FROM blockers WHERE cleared_at IS NULL").fetchone()[0]
        )
        return accounts, holds, blockers
    finally:
        connection.close()


def _rows(body: object, key: str) -> list[dict]:
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    if isinstance(body, dict):
        rows = body.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _server_time(body: object) -> int | None:
    if not isinstance(body, dict) or "serverTime" not in body:
        return None
    try:
        return int(body["serverTime"])
    except (TypeError, ValueError):
        return None


def _rule_reason(row: dict) -> str:
    if row.get("status") != "TRADING" or row.get("contractType") != "PERPETUAL":
        return f"{row.get('symbol')} 不是可交易的永续合约"
    if row.get("quoteAsset") != "USDT" or row.get("marginAsset") != "USDT":
        return f"{row.get('symbol')} 不是 USDT 本位"
    if "contractSize" in row:
        try:
            size = Decimal(str(row["contractSize"]))
        except Exception:
            return f"{row.get('symbol')} 面值无法核对"
        if size != Decimal("1"):
            return f"{row.get('symbol')} 面值不是 1"
    filters = row.get("filters")
    if not isinstance(filters, list):
        return f"{row.get('symbol')} 缺少价格步长、数量步长、最小数量或最小名义额"
    wanted = {
        "PRICE_FILTER": ("tickSize",),
        "LOT_SIZE": ("stepSize", "minQty"),
        "MIN_NOTIONAL": ("notional", "minNotional"),
    }
    for filter_type, keys in wanted.items():
        matches = [item for item in filters if isinstance(item, dict) and item.get("filterType") == filter_type]
        if len(matches) != 1:
            return f"{row.get('symbol')} 缺少价格步长、数量步长、最小数量或最小名义额"
        present = [key for key in keys if key in matches[0]]
        if filter_type == "MIN_NOTIONAL":
            if not present:
                return f"{row.get('symbol')} 缺少最小名义额"
            for key in present:
                text, ok = _finite_original(matches[0][key])
                if not ok or Decimal(text) <= 0:
                    return f"{row.get('symbol')} 最小名义额不是有限正数"
        else:
            if tuple(present) != keys:
                return f"{row.get('symbol')} 缺少价格步长、数量步长或最小数量"
            for key in keys:
                text, ok = _finite_original(matches[0][key])
                if not ok or Decimal(text) <= 0:
                    return f"{row.get('symbol')} 的 {key} 不是有限正数"
    return ""


def _clock_fail(sample: ClockSample, reason: str) -> ClockJudgement:
    return ClockJudgement("FAIL", 0, "", "", reason)


def _clock_result(sample: ClockSample, rtt: int, verdict: str, reason: str) -> ClockJudgement:
    twice_offset = (2 * sample.server_time) - (sample.t0 + sample.t1)
    if twice_offset % 2 == 0:
        offset = str(twice_offset // 2)
    else:
        offset = f"{twice_offset}/2"
    if rtt % 2 == 0:
        uncertainty = str(rtt // 2)
    else:
        uncertainty = f"{rtt}/2"
    return ClockJudgement(verdict, rtt, uncertainty, offset, reason)


def _iso(millis: int) -> str:
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()


def check_whitelist(method: str, path: str, params: tuple[tuple[str, str], ...] = ()) -> None:
    _validate(method, path, params)


def attempt_readonly(opener: Opener, method: str, path: str, params: tuple[tuple[str, str], ...], credentials: VenueCredentials) -> None:
    """白名单未通过时不调用传输器。"""
    _validate(method, path, params)
    query = "&".join(f"{key}={value}" for key, value in params)
    opener(method, enforce_host(_bound_demo_url(path, query)), {"X-MBX-APIKEY": credentials.api_key})


def enforce_host(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != DEMO_HOST or parts.port not in (None, 443) or parts.username or parts.password:
        raise CtsError("主机不在模拟盘绑定内，未发送请求", reason_code="LIVE_VENUE_UNVERIFIED")
    return url


__all__ = [
    "PLAN_BASELINE",
    "audit_evidence",
    "judge_clock",
    "refuse_redirect",
    "run_readonly_verification",
    "seal_evidence",
    "transport_security",
]

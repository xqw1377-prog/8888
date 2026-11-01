"""只读核验运行器。传输器是假的，不访问币安。"""

from __future__ import annotations

import ssl
from urllib.parse import parse_qs, urlsplit

import pytest

from cts.errors import CtsError
from cts.execution.live import VenueCredentials
from cts.ops.readonly_verify import (
    ClockSample,
    attempt_readonly,
    audit_evidence,
    check_whitelist,
    enforce_host,
    judge_clock,
    refuse_redirect,
    run_readonly_verification,
    seal_evidence,
    transport_security,
)

KEY = "AKIA_READONLY_TEST_KEY"
SECRET = "SECRET_READONLY_TEST_VALUE"
CODE = "0123456789abcdef0123456789abcdef01234567"


class SteadyClock:
    def __init__(self) -> None:
        self.utc = 1_700_000_000_000
        self.mono = 0

    def utc_millis(self) -> int:
        value = self.utc
        self.utc += 4
        return value

    def utc_ns(self) -> int:
        return self.utc_millis() * 1_000_000

    def monotonic_ns(self) -> int:
        value = self.mono
        self.mono += 4
        return value * 1_000_000

    def utc_quantum_ns(self) -> int:
        return 1

    def mono_quantum_ns(self) -> int:
        return 1

    def server_quantum_ns(self) -> int:
        return 1_000_000


class FakeOpener:
    def __init__(
        self,
        clock: SteadyClock | None = None,
        *,
        dual=None,
        margin="isolated",
        orders=None,
        orders_body=None,
        boom: str = "",
        strip_filters: bool = False,
        wallet_null: bool = False,
        assets=None,
    ) -> None:
        self.clock = clock
        self.dual = {"dualSidePosition": False} if dual is None else dual
        self.margin = margin
        self.orders = [] if orders is None else orders
        self.orders_body = orders_body
        self.boom = boom
        self.strip_filters = strip_filters
        self.wallet_null = wallet_null
        self.assets = assets
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str]):
        self.calls.append((method, url))
        if self.boom:
            raise RuntimeError(self.boom)
        parts = urlsplit(url)
        if parts.hostname != "demo-fapi.binance.com":
            raise AssertionError(parts.hostname)
        symbol = parse_qs(parts.query).get("symbol", [""])[0]
        if parts.path == "/fapi/v1/time":
            assert self.clock is not None
            return {"serverTime": self.clock.utc - 4}
        if parts.path == "/fapi/v1/exchangeInfo":
            symbols = [_symbol("BTCUSDT"), _symbol("ETHUSDT")]
            if self.strip_filters:
                for item in symbols:
                    item.pop("filters", None)
            return {"symbols": symbols}
        if parts.path == "/fapi/v1/positionSide/dual":
            return self.dual
        if parts.path == "/fapi/v1/multiAssetsMargin":
            return {"multiAssetsMargin": False}
        if parts.path == "/fapi/v2/positionRisk":
            if self.margin is None:
                return {"positions": [{"symbol": symbol}]}
            return {"positions": [{"symbol": symbol, "marginType": self.margin}]}
        if parts.path == "/fapi/v2/account":
            if self.assets is not None:
                return {"assets": self.assets}
            if self.wallet_null:
                return {
                    "assets": [
                        {
                            "asset": "USDT",
                            "walletBalance": None,
                            "availableBalance": None,
                            "unrealizedProfit": None,
                            "marginBalance": None,
                        }
                    ]
                }
            return {
                "assets": [
                    {
                        "asset": "USDT",
                        "walletBalance": "1.25000000",
                        "availableBalance": "1.00000000",
                        "unrealizedProfit": "0.00000000",
                        "marginBalance": "1.25000000",
                    }
                ]
            }
        if parts.path == "/fapi/v1/openOrders":
            if self.orders_body is not None:
                return self.orders_body
            rows = [
                row for row in self.orders
                if not isinstance(row, dict) or row.get("symbol") in (None, symbol)
            ]
            return {"orders": rows}
        raise AssertionError(parts.path)


def _symbol(name: str, contract_size: str = "1") -> dict:
    return {
        "symbol": name,
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "contractSize": contract_size,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "100"},
        ],
    }


def _creds() -> VenueCredentials:
    return VenueCredentials(KEY, SECRET)


def _prepare_books(tmp_path):
    from cts.execution.orders import OrderStore
    from cts.money import D
    from cts.risk.engine import RiskEngine

    orders = tmp_path / "orders.db"
    risk = tmp_path / "risk.db"
    store = OrderStore(str(orders))
    store.set_risk_path("acct-readonly", str(risk.resolve()))
    store.close()
    engine = RiskEngine.create(str(risk), "acct-readonly", equity=D("10000"))
    engine.close()
    return orders, risk


def _run(tmp_path, opener, clock=None, *, books: bool = True):
    if books:
        orders, risk = _prepare_books(tmp_path)
    else:
        orders, risk = tmp_path / "missing-orders.db", tmp_path / "missing-risk.db"
    return run_readonly_verification(
        opener=opener,
        evidence_dir=tmp_path / "evidence",
        account_id="acct-readonly",
        order_db=str(orders),
        risk_db=str(risk),
        network="TESTNET",
        credentials=_creds(),
        code_sha=CODE,
        clock=clock or SteadyClock(),
    )


def _text(tmp_path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "evidence").rglob("*") if path.is_file())


def test_whitelist_rejects_writes_mainnet_and_unknown_paths() -> None:
    opener = FakeOpener()
    with pytest.raises(CtsError) as denied:
        attempt_readonly(opener, "POST", "/fapi/v1/order", (), _creds())
    assert denied.value.reason_code == "LIVE_VENUE_UNVERIFIED"
    with pytest.raises(CtsError):
        attempt_readonly(opener, "DELETE", "/fapi/v1/order", (), _creds())
    with pytest.raises(CtsError):
        check_whitelist("GET", "/fapi/v1/order")
    with pytest.raises(CtsError):
        enforce_host("https://fapi.binance.com/fapi/v1/time")
    with pytest.raises(CtsError):
        check_whitelist(
            "GET",
            "/fapi/v2/positionRisk",
            (
                ("symbol", "SOLUSDT"),
                ("recvWindow", "5000"),
                ("timestamp", "1"),
                ("signature", "ab"),
            ),
        )
    assert opener.calls == []


def test_tls_stays_verified_and_redirects_are_not_followed(tmp_path) -> None:
    context = transport_security()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    with pytest.raises(CtsError) as redirected:
        refuse_redirect(302, "https://fapi.binance.com/fapi/v1/time")
    assert redirected.value.reason_code == "LIVE_VENUE_UNVERIFIED"

    class RedirectOpener(FakeOpener):
        def __call__(self, method: str, url: str, headers: dict[str, str]):
            self.calls.append((method, url))
            from cts.execution.live import TransportResult

            return TransportResult(status=302, location="https://evil.example/steal?signature=deadbeefdeadbeef")

    opener = RedirectOpener()
    with pytest.raises(CtsError):
        _run(tmp_path, opener, SteadyClock())
    assert len(opener.calls) == 1
    assert urlsplit(opener.calls[0][1]).hostname == "demo-fapi.binance.com"
    body = _text(tmp_path)
    assert "evil.example" not in body
    assert "deadbeefdeadbeef" not in body
    assert audit_evidence(tmp_path / "evidence") == "PASS"


def _sample(
    *,
    t0: int,
    t1: int,
    m0_before: int,
    m0_after: int,
    m1_before: int,
    m1_after: int,
    server_time: int,
    utc_quantum_ns: int = 1,
    mono_quantum_ns: int = 1,
    server_quantum_ns: int = 1,
) -> ClockSample:
    return ClockSample(
        t0,
        t1,
        m0_before,
        m0_after,
        m1_before,
        m1_after,
        server_time,
        utc_quantum_ns,
        mono_quantum_ns,
        server_quantum_ns,
    )


def test_utc_rollback_fails_while_the_old_formula_would_pass(tmp_path) -> None:
    t0_ms, t1_ms, server_ms = 10_000, 9_000, 9_500
    old_rtt = t1_ms - t0_ms
    old_u = old_rtt / 2
    old_offset = server_ms - (t0_ms + t1_ms) / 2
    assert old_u < 0
    assert abs(old_offset) + old_u <= 1000
    sample = _sample(
        t0=t0_ms * 1_000_000,
        t1=t1_ms * 1_000_000,
        m0_before=0,
        m0_after=0,
        m1_before=100_000_000,
        m1_after=100_000_000,
        server_time=server_ms,
    )
    judgement = judge_clock(sample)
    assert judgement.verdict == "FAIL"
    assert judgement.reason == "UTC时间倒退"
    assert judgement.rtt == 100_000_000
    assert not judgement.uncertainty.startswith("-")

    class RollbackClock:
        def __init__(self) -> None:
            self.utc_values = [1]
            self.utc_ns_values = [10_000, 9_000]
            self.mono_values = [0, 0, 100, 100]

        def utc_millis(self) -> int:
            if self.utc_values:
                return self.utc_values.pop(0)
            return 9_000

        def utc_ns(self) -> int:
            if self.utc_ns_values:
                return self.utc_ns_values.pop(0)
            return 9_000

        def monotonic_ns(self) -> int:
            if self.mono_values:
                return self.mono_values.pop(0)
            return 100

        def utc_quantum_ns(self) -> int:
            return 1

        def mono_quantum_ns(self) -> int:
            return 1

        def server_quantum_ns(self) -> int:
            return 1_000_000

    clock = RollbackClock()

    class TimeOpener(FakeOpener):
        def __call__(self, method: str, url: str, headers: dict[str, str]):
            self.calls.append((method, url))
            return {"serverTime": 9_500}

    opener = TimeOpener()
    verdict = _run(tmp_path, opener, clock)
    assert verdict == "只读核验未通过"
    assert len(opener.calls) == 1
    evidence = (tmp_path / "evidence" / "e6_clock.txt").read_text(encoding="utf-8")
    assert "t0_ns=10000" in evidence
    assert "t1_ns=9000" in evidence
    assert "m0_before_ns=0" in evidence
    assert "m1_after_ns=100" in evidence
    assert "serverTime_ms=9500" in evidence
    assert "utc_quantum_ns=1" in evidence
    assert "mono_quantum_ns=1" in evidence
    assert "server_quantum_ns=1000000" in evidence
    assert "verdict=FAIL" in evidence


def test_one_millisecond_quantization_gap_can_be_compatible() -> None:
    """先后截断到毫秒会差 1，纳秒区间内仍是同一次经过。"""
    t0 = 900_000
    t1 = 482_100_000
    m0_after = 1_100_000
    m1 = 1_100_000 + (t1 - t0)
    assert (t1 // 1_000_000 - t0 // 1_000_000) - (m1 // 1_000_000 - m0_after // 1_000_000) == 1
    judgement = judge_clock(
        _sample(
            t0=t0,
            t1=t1,
            m0_before=800_000,
            m0_after=m0_after,
            m1_before=m1 - 200_000,
            m1_after=m1,
            server_time=241,
        )
    )
    assert judgement.verdict == "PASS"
    assert judgement.reason == ""


def test_read_interval_changes_compatibility_without_a_fixed_slack() -> None:
    narrow = judge_clock(
        _sample(
            t0=0,
            t1=5_000_000,
            m0_before=0,
            m0_after=1_000,
            m1_before=1_000_000,
            m1_after=1_001_000,
            server_time=3,
        )
    )
    assert narrow.verdict == "FAIL"
    assert narrow.reason == "无法解释的时间跳变"
    wide = judge_clock(
        _sample(
            t0=0,
            t1=5_000_000,
            m0_before=0,
            m0_after=3_000_000,
            m1_before=3_000_000,
            m1_after=8_000_000,
            server_time=3,
        )
    )
    assert wide.verdict == "PASS"


def test_wider_read_interval_still_counts_against_the_budget() -> None:
    base = 2_000_000_000
    inside = judge_clock(
        _sample(
            t0=0,
            t1=base,
            m0_before=0,
            m0_after=0,
            m1_before=base,
            m1_after=base,
            server_time=1_000,
        )
    )
    assert inside.verdict == "PASS"
    outside = judge_clock(
        _sample(
            t0=0,
            t1=base,
            m0_before=0,
            m0_after=0,
            m1_before=base,
            m1_after=base + 1,
            server_time=1_000,
        )
    )
    assert outside.verdict == "FAIL"
    assert outside.reason == "不确定度超过1000毫秒"


def test_real_clock_jump_fails_even_when_offset_would_pass() -> None:
    judgement = judge_clock(
        _sample(
            t0=0,
            t1=5_000_000,
            m0_before=0,
            m0_after=1_000,
            m1_before=1_000,
            m1_after=2_000,
            server_time=3,
        )
    )
    assert judgement.verdict == "FAIL"
    assert judgement.reason == "无法解释的时间跳变"


def test_server_millisecond_is_inside_the_offset_budget() -> None:
    """点估计留在门内时，服务端这一毫秒的远端仍可能越出 1000 ms。"""
    judged = judge_clock(
        _sample(
            t0=0,
            t1=1_998_000_000,
            m0_before=0,
            m0_after=0,
            m1_before=1_998_000_000,
            m1_after=1_998_000_000,
            server_time=999,
            mono_quantum_ns=1_000_000,
            server_quantum_ns=1_000_000,
        )
    )
    assert judged.verdict == "FAIL"
    assert judged.reason == "偏移加不确定度超过1000毫秒"
    room = judge_clock(
        _sample(
            t0=0,
            t1=1_000_000_000,
            m0_before=0,
            m0_after=0,
            m1_before=1_000_000_000,
            m1_after=1_000_000_000,
            server_time=500,
            server_quantum_ns=1_000_000,
        )
    )
    assert room.verdict == "PASS"


def test_monotonic_quantization_keeps_equal_elapsed_time() -> None:
    """两种时钟各自量化后，相同经过时间不能只因为单调端点被当成精确值而拒绝。"""
    quantum = 1_000_000
    kept = judge_clock(
        _sample(
            t0=0,
            t1=(quantum - 1) + (quantum - 1),
            m0_before=0,
            m0_after=0,
            m1_before=0,
            m1_after=0,
            server_time=1,
            utc_quantum_ns=quantum,
            mono_quantum_ns=quantum,
        )
    )
    assert kept.verdict == "PASS"
    rejected = judge_clock(
        _sample(
            t0=0,
            t1=(quantum - 1) + (quantum - 1) + 1,
            m0_before=0,
            m0_after=0,
            m1_before=0,
            m1_after=0,
            server_time=1,
            utc_quantum_ns=quantum,
            mono_quantum_ns=quantum,
        )
    )
    assert rejected.verdict == "FAIL"
    assert rejected.reason == "无法解释的时间跳变"


def test_budget_boundary_keeps_sampling_uncertainty() -> None:
    exact = judge_clock(
        _sample(
            t0=0,
            t1=2_000_000_000,
            m0_before=0,
            m0_after=0,
            m1_before=2_000_000_000,
            m1_after=2_000_000_000,
            server_time=1_000,
        )
    )
    assert exact.verdict == "PASS"
    past = judge_clock(
        _sample(
            t0=0,
            t1=2_000_000_001,
            m0_before=0,
            m0_after=0,
            m1_before=2_000_000_001,
            m1_after=2_000_000_001,
            server_time=1_000,
        )
    )
    assert past.verdict == "FAIL"
    assert past.reason == "不确定度超过1000毫秒"


def test_monotonic_rollback_fails() -> None:
    judgement = judge_clock(
        _sample(
            t0=0,
            t1=1_000,
            m0_before=5_000,
            m0_after=4_000,
            m1_before=6_000,
            m1_after=7_000,
            server_time=0,
        )
    )
    assert judgement.verdict == "FAIL"
    assert judgement.reason == "单调时钟倒退"


def test_missing_mode_fields_fail_closed(tmp_path) -> None:
    clock = SteadyClock()
    opener = FakeOpener(clock, dual={})
    verdict = _run(tmp_path, opener, clock)
    assert verdict == "只读核验未通过"
    checks = (tmp_path / "evidence" / "e5_checks.txt").read_text(encoding="utf-8")
    assert "持仓模式 FAIL" in checks
    assert "dualSidePosition 缺失" in checks
    assert "NOT_RUN" not in checks.split("持仓模式 FAIL", 1)[0]
    assert all(urlsplit(url).path != "/fapi/v2/positionRisk" for _, url in opener.calls)

    clock = SteadyClock()
    opener = FakeOpener(clock, margin=None)
    verdict = _run(tmp_path, opener, clock)
    assert verdict == "只读核验未通过"
    checks = (tmp_path / "evidence" / "e5_checks.txt").read_text(encoding="utf-8")
    assert "逐仓BTCUSDT FAIL" in checks
    assert "marginType 缺失或无法核实为逐仓" in checks
    wallet = (tmp_path / "evidence" / "e8_wallet.txt").read_text(encoding="utf-8")
    assert wallet.strip() == "暂无证据"


def test_exception_text_redacts_key_signature_and_url(tmp_path) -> None:
    boom = (
        f"boom {KEY} signature=deadbeefdeadbeef "
        "https://demo-fapi.binance.com/fapi/v1/time?signature=deadbeefdeadbeef"
    )
    opener = FakeOpener(boom=boom)
    with pytest.raises(CtsError) as raised:
        _run(tmp_path, opener, SteadyClock())
    message = str(raised.value)
    blob = message + "\n" + _text(tmp_path)
    assert KEY not in blob
    assert SECRET not in blob
    assert "deadbeefdeadbeef" not in blob
    assert "https://" not in blob
    assert "GET /fapi/v1/time" in _text(tmp_path)


def test_evidence_manifest_detects_modification(tmp_path) -> None:
    clock = SteadyClock()
    opener = FakeOpener(clock)
    verdict = _run(tmp_path, opener, clock)
    evidence = tmp_path / "evidence"
    assert verdict == "只读核验通过"
    assert audit_evidence(evidence) == "PASS"
    manifest = (evidence / "manifest.sha256").read_text(encoding="utf-8").splitlines()
    paths = [line.split(" ", 1)[0] for line in manifest]
    assert paths == sorted(paths, key=lambda item: item.encode("utf-8"))
    assert "manifest.sha256" not in paths
    assert "manifest.sha256.digest" not in paths
    assert "e10_credentials.txt" in paths
    wallet = evidence / "e8_wallet.txt"
    assert "1.25000000" in wallet.read_text(encoding="utf-8")
    wallet.write_text(wallet.read_text(encoding="utf-8") + "x", encoding="utf-8")
    assert audit_evidence(evidence) == "FAIL"
    seal_evidence(evidence)
    assert audit_evidence(evidence) == "PASS"
    assert KEY not in _text(tmp_path)
    assert SECRET not in _text(tmp_path)


def test_parse_error_cannot_seal_a_pass(tmp_path) -> None:
    clock = SteadyClock()
    opener = FakeOpener(clock, assets=123)
    verdict = _run(tmp_path, opener, clock)
    assert verdict == "只读核验未通过"
    checks = (tmp_path / "evidence" / "e5_checks.txt").read_text(encoding="utf-8")
    assert "只读核验通过" not in checks
    assert "钱包 FAIL" in checks


def test_missing_databases_fail_without_creating_them(tmp_path) -> None:
    clock = SteadyClock()
    opener = FakeOpener(clock)
    orders = tmp_path / "missing-orders.db"
    risk = tmp_path / "missing-risk.db"
    verdict = _run(tmp_path, opener, clock, books=False)
    assert verdict == "只读核验未通过"
    assert orders.exists() is False
    assert risk.exists() is False
    checks = (tmp_path / "evidence" / "e5_checks.txt").read_text(encoding="utf-8")
    assert "原订单库 FAIL" in checks
    assert "原风险库 FAIL" in checks
    assert "未决订单 FAIL" in checks
    assert "风险阻断 FAIL" in checks
    assert opener.calls == []
    assert "结束快照" in checks


@pytest.mark.parametrize("body", [{}, {"code": -1}, [1, "bad", {}], {"orders": {"clientOrderId": "cid"}}])
def test_malformed_open_orders_fail(tmp_path, body) -> None:
    clock = SteadyClock()
    opener = FakeOpener(clock, orders_body=body)
    verdict = _run(tmp_path, opener, clock)
    assert verdict == "只读核验未通过"
    checks = (tmp_path / "evidence" / "e5_checks.txt").read_text(encoding="utf-8")
    assert "委托正文或行结构不合法" in checks


def test_missing_filters_fail(tmp_path) -> None:
    clock = SteadyClock()
    opener = FakeOpener(clock, strip_filters=True)
    verdict = _run(tmp_path, opener, clock)
    assert verdict == "只读核验未通过"
    checks = (tmp_path / "evidence" / "e5_checks.txt").read_text(encoding="utf-8")
    assert "交易规则 FAIL" in checks
    assert "价格步长" in checks


def test_null_wallet_fields_keep_original_text_and_fail(tmp_path) -> None:
    clock = SteadyClock()
    opener = FakeOpener(clock, wallet_null=True)
    verdict = _run(tmp_path, opener, clock)
    assert verdict == "只读核验未通过"
    wallet = (tmp_path / "evidence" / "e8_wallet.txt").read_text(encoding="utf-8")
    assert "walletBalance=null" in wallet
    assert "availableBalance=null" in wallet
    assert "unrealizedProfit=null" in wallet
    assert "marginBalance=null" in wallet
    checks = (tmp_path / "evidence" / "e5_checks.txt").read_text(encoding="utf-8")
    assert "钱包 FAIL" in checks
    assert "不是有限数值" in checks


def test_block_added_during_collection_fails_the_end_snapshot(tmp_path) -> None:
    import sqlite3

    orders, risk = _prepare_books(tmp_path)
    planted = {"done": False}

    class Plant(FakeOpener):
        def __call__(self, method: str, url: str, headers: dict[str, str]):
            if not planted["done"]:
                planted["done"] = True
                risk_conn = sqlite3.connect(risk)
                risk_conn.execute(
                    """
                    INSERT INTO live_risk_holds(
                        hold_id, account_id, reservation_id, client_order_id, reason, created_at, owner_id
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    ("hold-late", "acct-readonly", "res-late", "cid-late", "采集期间新增", "2026-11-01T00:00:00+00:00", "owner-late"),
                )
                risk_conn.commit()
                risk_conn.close()
                order_conn = sqlite3.connect(orders)
                order_conn.execute(
                    """
                    INSERT INTO live_orders(
                        client_order_id, account_id, reservation_id, symbol, side, qty, price, notional,
                        status, executed_qty, dispatch_started, body, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "cid-late", "acct-readonly", "res-late", "BTCUSDT", "LONG", "0.001", "100", "0.1",
                        "UNKNOWN", "0", 1, "{}", "2026-11-01T00:00:00+00:00",
                    ),
                )
                order_conn.commit()
                order_conn.close()
            return super().__call__(method, url, headers)

    clock = SteadyClock()
    opener = Plant(clock)
    verdict = run_readonly_verification(
        opener=opener,
        evidence_dir=tmp_path / "evidence",
        account_id="acct-readonly",
        order_db=str(orders),
        risk_db=str(risk),
        network="TESTNET",
        credentials=_creds(),
        code_sha=CODE,
        clock=clock,
    )
    assert verdict == "只读核验未通过"
    checks = (tmp_path / "evidence" / "e5_checks.txt").read_text(encoding="utf-8")
    assert "结束快照 未决订单=1 clientOrderId=cid-late 风险持有=1 未清除阻断=0" in checks
    pending = [line for line in checks.splitlines() if line.startswith("未决订单 ")]
    blocks = [line for line in checks.splitlines() if line.startswith("风险阻断 ")]
    assert pending[-1].startswith("未决订单 FAIL")
    assert blocks[-1].startswith("风险阻断 FAIL")


def _run_books(tmp_path, opener, clock, orders, risk) -> str:
    return run_readonly_verification(
        opener=opener,
        evidence_dir=tmp_path / "evidence",
        account_id="acct-readonly",
        order_db=str(orders),
        risk_db=str(risk),
        network="TESTNET",
        credentials=_creds(),
        code_sha=CODE,
        clock=clock,
    )


@pytest.mark.parametrize(
    ("sql", "params", "snapshot", "detail"),
    [
        ("DELETE FROM live_session_meta", (), "风险库绑定=缺失", "缺少匹配的账户与风险库路径绑定"),
        (
            "UPDATE live_session_meta SET risk_db_path=? WHERE account_id=?",
            ("/tmp/other-risk.db", "acct-readonly"),
            "风险库绑定=不匹配",
            "缺少匹配的账户与风险库路径绑定",
        ),
        (
            "UPDATE account_state SET account_id=? WHERE account_id=?",
            ("acct-other", "acct-readonly"),
            "账户=acct-other",
            "账户与原库归属不一致",
        ),
    ],
)
def test_binding_drift_during_collection_fails_the_end_snapshot(tmp_path, sql, params, snapshot, detail) -> None:
    import sqlite3

    orders, risk = _prepare_books(tmp_path)
    planted = {"done": False}
    target = orders if "live_session_meta" in sql else risk

    class Plant(FakeOpener):
        def __call__(self, method: str, url: str, headers: dict[str, str]):
            if not planted["done"]:
                planted["done"] = True
                connection = sqlite3.connect(target)
                connection.execute(sql, params)
                connection.commit()
                connection.close()
            return super().__call__(method, url, headers)

    clock = SteadyClock()
    opener = Plant(clock)
    verdict = _run_books(tmp_path, opener, clock, orders, risk)
    assert verdict == "只读核验未通过"
    checks = (tmp_path / "evidence" / "e5_checks.txt").read_text(encoding="utf-8")
    assert snapshot in checks
    ownership = [line for line in checks.splitlines() if line.startswith("账户归属 ")]
    assert ownership[0].startswith("账户归属 PASS")
    assert ownership[-1].startswith("账户归属 FAIL")
    assert detail in ownership[-1]


def test_caller_ids_do_not_register_open_orders(tmp_path) -> None:
    orders, risk = _prepare_books(tmp_path)
    clock = SteadyClock()
    opener = FakeOpener(
        clock,
        orders=[
            {
                "clientOrderId": "outside-book",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "origQty": "0.001",
            }
        ],
    )
    verdict = run_readonly_verification(
        opener=opener,
        evidence_dir=tmp_path / "evidence",
        account_id="acct-readonly",
        order_db=str(orders),
        risk_db=str(risk),
        network="TESTNET",
        credentials=_creds(),
        code_sha=CODE,
        clock=clock,
        known_client_order_ids={"outside-book"},
    )
    assert verdict == "只读核验未通过"
    checks = (tmp_path / "evidence" / "e5_checks.txt").read_text(encoding="utf-8")
    assert "未在原订单库登记或订单身份不一致" in checks


def test_open_order_matches_the_original_book(tmp_path) -> None:
    import sqlite3

    orders, risk = _prepare_books(tmp_path)
    connection = sqlite3.connect(orders)
    connection.execute(
        """
        INSERT INTO live_orders(
            client_order_id, account_id, reservation_id, symbol, side, qty, price, notional,
            status, executed_qty, dispatch_started, body, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "cid-registered", "acct-readonly", "res-1", "BTCUSDT", "LONG", "0.001", "100", "0.1",
            "NEW", "0", 1, "{}", "2026-11-01T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()
    clock = SteadyClock()
    opener = FakeOpener(
        clock,
        orders=[
            {
                "clientOrderId": "cid-registered",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "origQty": "0.001",
            }
        ],
    )
    verdict = run_readonly_verification(
        opener=opener,
        evidence_dir=tmp_path / "evidence",
        account_id="acct-readonly",
        order_db=str(orders),
        risk_db=str(risk),
        network="TESTNET",
        credentials=_creds(),
        code_sha=CODE,
        clock=clock,
        known_client_order_ids=set(),
    )
    assert verdict == "只读核验通过"


def test_deleted_account_binding_fails_before_any_request(tmp_path) -> None:
    import sqlite3

    orders, risk = _prepare_books(tmp_path)
    connection = sqlite3.connect(orders)
    connection.execute("DELETE FROM live_session_meta")
    connection.commit()
    connection.close()
    clock = SteadyClock()
    opener = FakeOpener(clock)
    verdict = run_readonly_verification(
        opener=opener,
        evidence_dir=tmp_path / "evidence",
        account_id="acct-readonly",
        order_db=str(orders),
        risk_db=str(risk),
        network="TESTNET",
        credentials=_creds(),
        code_sha=CODE,
        clock=clock,
    )
    assert verdict == "只读核验未通过"
    assert opener.calls == []
    checks = (tmp_path / "evidence" / "e5_checks.txt").read_text(encoding="utf-8")
    assert "账户归属 FAIL" in checks
    assert "缺少匹配的账户与风险库路径绑定" in checks


def test_duplicate_usdt_rows_fail(tmp_path) -> None:
    clock = SteadyClock()
    row = {
        "asset": "USDT",
        "walletBalance": "1.25000000",
        "availableBalance": "1.00000000",
        "unrealizedProfit": "0.00000000",
        "marginBalance": "1.25000000",
    }
    other = dict(row)
    other["walletBalance"] = "9.00000000"
    other["marginBalance"] = "9.00000000"
    opener = FakeOpener(clock, assets=[row, other])
    verdict = _run(tmp_path, opener, clock)
    assert verdict == "只读核验未通过"
    wallet = (tmp_path / "evidence" / "e8_wallet.txt").read_text(encoding="utf-8")
    assert "walletBalance=1.25000000" in wallet
    assert "walletBalance=9.00000000" in wallet
    checks = (tmp_path / "evidence" / "e5_checks.txt").read_text(encoding="utf-8")
    assert "钱包 FAIL" in checks
    assert "重复或歧义" in checks
    assert "钱包 PASS" not in checks


def test_json_signature_field_is_redacted(tmp_path) -> None:
    secret = "structured-signature-value"
    opener = FakeOpener(boom='{"signature": "' + secret + '", "msg": "boom"}')
    with pytest.raises(CtsError) as raised:
        _run(tmp_path, opener, SteadyClock())
    blob = str(raised.value) + "\n" + _text(tmp_path)
    assert secret not in blob
    assert KEY not in blob

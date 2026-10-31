"""本地演练记录。通过不表示微额实盘获批。"""

from __future__ import annotations

import subprocess
from tempfile import TemporaryDirectory

from cts import LIVE_TRADING_AUTHORIZATION
from cts.clock import utc_now
from cts.execution.liq import classify_liq_quote, model_liq_price
from cts.execution.live import LiveExchangeAdapter
from cts.ledger.journal import Ledger
from cts.ledger.models import Side
from cts.money import D
from cts.research.experiments import e2_status


def _code_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "UNKNOWN"


def run_local_drill() -> dict:
    live = LiveExchangeAdapter()
    refused = False
    try:
        live.submit()
    except Exception as exc:
        refused = getattr(exc, "reason_code", "") == "LIVE_TRADING_HOLD"
    blank, price = classify_liq_quote("")
    model = model_liq_price(
        side=Side.SHORT,
        avg_price=D("100"),
        qty=D("1"),
        multiplier=D("1"),
        isolated_margin=D("50"),
        mmr=D("0.005"),
        fee_rate=D("0.0005"),
    )
    durable = False
    with TemporaryDirectory() as folder:
        path = f"{folder}/ledger.db"
        now = utc_now()
        ledger = Ledger("drill", path=path)
        ledger.transfer_in(amount=D("10"), event_time=now, received_at=now, external_id="dep")
        assert ledger._store is not None
        ledger._store.fail_appends = 1
        failed = False
        try:
            ledger.transfer_in(amount=D("1"), event_time=now, received_at=now, external_id="dep-2")
        except Exception:
            failed = True
        reopened = Ledger("drill", path=path)
        durable = failed and ledger.cash_equity() == D("10") and reopened.cash_equity() == D("10")
    e2 = e2_status()
    items = [
        {
            "id": "LIVE_HOLD",
            "layer": "LOCAL_INJECTION",
            "result": "PASS" if refused and live.calls == 1 else "FAIL",
            "scope": "模拟器拒绝实盘发送",
        },
        {
            "id": "LIQ_BLANK",
            "layer": "LOCAL_INJECTION",
            "result": "PASS" if blank == "UNKNOWN" and price is None and model > 0 else "FAIL",
            "scope": "空强平价不当成零，空头模型给出正价格",
        },
        {
            "id": "LEDGER_DURABILITY",
            "layer": "LOCAL_INJECTION",
            "result": "PASS" if durable else "FAIL",
            "scope": "磁盘提交失败时内存不入账，重开看不到这笔",
        },
        {
            "id": "E2_NOT_TESTABLE",
            "layer": "LOCAL_INJECTION",
            "result": "PASS" if e2.status == "NOT_TESTABLE" else "FAIL",
            "scope": "没有盘口时不把 TTL 实验记成已测",
        },
        {
            "id": "VENUE_TEST",
            "layer": "UNVERIFIED",
            "result": "UNVERIFIED",
            "scope": "未连接交易所测试环境",
        },
        {
            "id": "ALERT_DELIVERY",
            "layer": "UNVERIFIED",
            "result": "UNVERIFIED",
            "scope": "告警渠道未做真实到达测试",
        },
        {
            "id": "PRODUCTION_PROTECTION",
            "layer": "UNVERIFIED",
            "result": "UNVERIFIED",
            "scope": "生产保护单未生效",
        },
        {
            "id": "MICRO_LIVE_OBSERVATION",
            "layer": "UNVERIFIED",
            "result": "UNVERIFIED",
            "scope": "没有有界资金批准，不能做微额实盘",
        },
    ]
    return {
        "code_sha": _code_sha(),
        "live_authorization": LIVE_TRADING_AUTHORIZATION,
        "micro_live_approved": False,
        "conclusion": "本地故障注入不能替代生产执行证据",
        "known_defects": [
            "强平价是本地公式，不是交易所回读",
            "没有真实告警送达证明",
            "实盘授权保持 HOLD",
        ],
        "items": items,
    }

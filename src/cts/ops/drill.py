"""本地演练记录。通过不表示微额实盘获批。"""

from __future__ import annotations

from cts import LIVE_TRADING_AUTHORIZATION
from cts.execution.live import LiveExchangeAdapter
from cts.research.experiments import e2_status


def run_local_drill() -> dict:
    live = LiveExchangeAdapter()
    refused = False
    try:
        live.submit()
    except Exception as exc:
        refused = getattr(exc, "reason_code", "") == "LIVE_TRADING_HOLD"
    e2 = e2_status()
    items = [
        {
            "id": "LIVE_HOLD",
            "layer": "LOCAL_INJECTION",
            "result": "PASS" if refused and live.calls == 1 else "FAIL",
            "scope": "模拟器拒绝实盘发送",
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
        "live_authorization": LIVE_TRADING_AUTHORIZATION,
        "micro_live_approved": False,
        "conclusion": "本地故障注入不能替代生产执行证据",
        "items": items,
    }

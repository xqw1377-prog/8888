"""看门狗:只读本地文件的活性检查。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cts.ops.watchdog import check


NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def _write(path: Path, content: str, mtime: datetime | None = None) -> str:
    path.write_text(content)
    if mtime is not None:
        import os

        os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return str(path)


def test_healthy_trader_passes(tmp_path) -> None:
    state = _write(tmp_path / "s.json", json.dumps({"position": None, "halted": None}), mtime=NOW - timedelta(seconds=30))
    log = _write(
        tmp_path / "t.log",
        f"[{NOW.isoformat()}] 心跳 持仓=无 待定单=无\n",
        mtime=NOW - timedelta(seconds=30),
    )
    report = check(state, log, now=NOW)
    assert report["ok"], report


def test_stale_state_and_missing_heartbeat_fail(tmp_path) -> None:
    state = _write(tmp_path / "s.json", json.dumps({"position": None, "halted": None}), mtime=NOW - timedelta(hours=3))
    log = _write(tmp_path / "t.log", f"[{(NOW - timedelta(hours=2)).isoformat()}] 心跳 持仓=无\n")
    report = check(state, log, now=NOW)
    assert not report["ok"]
    assert any("未更新" in p for p in report["problems"])
    assert any("心跳" in p for p in report["problems"])


def test_halted_and_failure_storm_fail(tmp_path) -> None:
    state = _write(tmp_path / "s.json", json.dumps({"halted": "directive-expired"}), mtime=NOW - timedelta(seconds=10))
    log = _write(
        tmp_path / "t.log",
        f"[{NOW.isoformat()}] 心跳 持仓=无\n[{NOW.isoformat()}] 迭代异常(12 连续) RiskLimitError\n",
    )
    report = check(state, log, now=NOW)
    assert not report["ok"]
    assert any("熔断" in p for p in report["problems"])
    assert any("连续迭代失败" in p for p in report["problems"])


def test_position_without_stop_fails(tmp_path) -> None:
    state = _write(
        tmp_path / "s.json",
        json.dumps({"position": {"side": "LONG", "qty": "1", "stop": "0"}, "halted": None}),
        mtime=NOW - timedelta(seconds=10),
    )
    log = _write(tmp_path / "t.log", f"[{NOW.isoformat()}] 心跳 持仓=LONG 1\n")
    report = check(state, log, now=NOW)
    assert not report["ok"]
    assert any("止损价缺失" in p for p in report["problems"])

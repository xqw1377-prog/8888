"""离线看门狗:检查交易器活性指标,异常时以非零退出码报告。

设计约束:只读本地文件(状态文件/日志),不做网络请求、不操作交易器。
可由 cron/launchd/人工周期调用;退出码 0=健康,1=异常,2=文件不可读。

用法:python -m cts.ops.watchdog [--state PATH] [--log PATH] [--max-state-age 秒] [--max-log-age 秒]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal as D

STATE_DEFAULT = "/Users/xinquanwang/8888/var/run/demo_trader_state.json"
LOG_DEFAULT = "/Users/xinquanwang/8888/var/run/demo_trader.log"

HEARTBEAT_RE = re.compile(r"^\[(?P<ts>[^\]]+)\] 心跳")
FAILURE_RE = re.compile(r"迭代异常\((?P<n>\d+) 连续\)")


def _mtime_age(path: str, now: datetime) -> float:
    age = now.timestamp() - os.path.getmtime(path)
    return max(0.0, age)


def _last_heartbeat_age(log_path: str, now: datetime) -> float | None:
    last: datetime | None = None
    with open(log_path, errors="replace") as handle:
        for line in handle:
            m = HEARTBEAT_RE.match(line)
            if m:
                try:
                    last = datetime.fromisoformat(m.group("ts"))
                except ValueError:
                    continue
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return max(0.0, (now - last).total_seconds())


def _last_consecutive_failures(log_path: str) -> int:
    best = 0
    with open(log_path, errors="replace") as handle:
        for line in handle:
            m = FAILURE_RE.search(line)
            if m:
                best = max(best, int(m.group("n")))
    return best


def check(
    state_path: str = STATE_DEFAULT,
    log_path: str = LOG_DEFAULT,
    *,
    now: datetime | None = None,
    max_state_age: float = 120.0,
    max_log_age: float = 1200.0,
    max_consecutive_failures: int = 10,
) -> dict:
    now = now or datetime.now(timezone.utc)
    report: dict = {"checked_at": now.isoformat(), "ok": False, "problems": []}
    problems: list[str] = report["problems"]
    try:
        state = json.load(open(state_path))
    except Exception as exc:
        report["problems"].append(f"状态文件不可读: {exc}")
        return report
    if not os.path.exists(log_path):
        problems.append("日志文件不存在")
        return report

    state_age = _mtime_age(state_path, now)
    report["state_age_seconds"] = round(state_age, 1)
    if state_age > max_state_age:
        problems.append(f"状态文件 {state_age:.0f}s 未更新(阈值 {max_state_age:.0f}s):主循环疑似停滞")

    hb_age = _last_heartbeat_age(log_path, now)
    report["last_heartbeat_age_seconds"] = round(hb_age, 1) if hb_age is not None else None
    if hb_age is None:
        problems.append("日志中没有任何心跳记录")
    elif hb_age > max_log_age:
        problems.append(f"最近心跳已在 {hb_age:.0f}s 前(阈值 {max_log_age:.0f}s)")

    halted = state.get("halted")
    report["halted"] = halted
    if halted:
        problems.append(f"交易器处于熔断状态: {halted}")

    failures = _last_consecutive_failures(log_path)
    report["max_consecutive_failures"] = failures
    if failures >= max_consecutive_failures:
        problems.append(f"连续迭代失败达到 {failures} 次(阈值 {max_consecutive_failures})")

    position = state.get("position")
    report["position"] = bool(position)
    if position:
        stop = D(str(position.get("stop") or "0"))
        if stop <= 0:
            problems.append("持仓在场但止损价缺失或为 0")

    report["ok"] = not problems
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="交易器活性看门狗(只读本地文件)")
    parser.add_argument("--state", default=STATE_DEFAULT)
    parser.add_argument("--log", default=LOG_DEFAULT)
    parser.add_argument("--max-state-age", type=float, default=120.0)
    parser.add_argument("--max-log-age", type=float, default=1200.0)
    args = parser.parse_args(argv)
    report = check(
        args.state,
        args.log,
        max_state_age=args.max_state_age,
        max_log_age=args.max_log_age,
    )
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

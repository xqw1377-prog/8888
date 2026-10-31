"""时钟合同：墙钟用于事件顺序，期限使用单调时钟。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Optional
from uuid import uuid4


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def require_utc(ts: datetime, name: str) -> datetime:
    if ts.tzinfo is None:
        raise ValueError(f"{name} 必须带时区，且为 UTC")
    as_utc = ts.astimezone(UTC)
    if as_utc.tzinfo != UTC and as_utc.utcoffset() != timedelta(0):
        raise ValueError(f"{name} 必须可转换为 UTC")
    return as_utc


@dataclass(frozen=True)
class ClockCheckpoint:
    boot_id: str
    monotonic_started_at: float
    elapsed_at_checkpoint: float
    wall_at_checkpoint: datetime
    venue_event_time: Optional[datetime] = None
    received_at: Optional[datetime] = None


class MonotonicClock:
    def __init__(self, boot_id: Optional[str] = None, started_at: Optional[float] = None) -> None:
        self.boot_id = boot_id or uuid4().hex
        self.monotonic_started_at = started_at if started_at is not None else monotonic()

    def elapsed_ms(self) -> int:
        return int((monotonic() - self.monotonic_started_at) * 1000)

    def checkpoint(
        self,
        *,
        venue_event_time: Optional[datetime] = None,
        received_at: Optional[datetime] = None,
        wall: Optional[datetime] = None,
    ) -> ClockCheckpoint:
        return ClockCheckpoint(
            boot_id=self.boot_id,
            monotonic_started_at=self.monotonic_started_at,
            elapsed_at_checkpoint=monotonic() - self.monotonic_started_at,
            wall_at_checkpoint=wall or utc_now(),
            venue_event_time=venue_event_time,
            received_at=received_at,
        )

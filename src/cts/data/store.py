"""幂等写入：同标识同内容接受，同标识异内容拒绝。研究只使用当时已可得数据。"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Protocol, TypeVar

from cts.checksum import sha256_hex
from cts.clock import require_utc
from cts.errors import ConflictError, LookaheadError


class Identified(Protocol):
    checksum: str
    available_at: datetime

    def identity(self) -> tuple: ...


T = TypeVar("T", bound=Identified)


class DataStore:
    def __init__(self) -> None:
        self._records: dict[tuple, Identified] = {}

    def put(self, record: T) -> T:
        key = record.identity()
        digest = _content_digest(record)
        if digest != record.checksum:
            raise ConflictError("checksum 与内容不一致，不能用旧校验和包装新内容")
        existing = self._records.get(key)
        if existing is None:
            self._records[key] = record
            return record
        if _content_digest(existing) != digest:
            raise ConflictError(
                f"同标识异容冲突: {key}",
                reason_code="IDEMPOTENCY_CONFLICT",
            )
        return existing  # type: ignore[return-value]

    def get(self, identity: tuple) -> Optional[Identified]:
        return self._records.get(identity)

    def all(self) -> list[Identified]:
        return list(self._records.values())

    def available_at(self, as_of: datetime, records: Optional[Iterable[T]] = None) -> list[T]:
        cutoff = require_utc(as_of, "as_of")
        source = records if records is not None else self._records.values()
        out: list[T] = []
        for record in source:  # type: ignore[assignment]
            if record.available_at <= cutoff:
                out.append(record)  # type: ignore[arg-type]
        return out

    def require_no_lookahead(self, record: Identified, as_of: datetime) -> None:
        cutoff = require_utc(as_of, "as_of")
        if record.available_at > cutoff:
            raise LookaheadError(
                f"研究不得使用 as_of={cutoff.isoformat()} 时尚不可得的数据 "
                f"available_at={record.available_at.isoformat()}"
            )


def _content_digest(record: Identified) -> str:
    payload = getattr(record, "payload", None)
    if callable(payload):
        return sha256_hex(payload())
    compute = getattr(record, "compute_checksum", None)
    if callable(compute):
        return compute()
    return record.checksum

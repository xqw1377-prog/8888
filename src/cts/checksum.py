"""规范哈希：原始、清洗、结果均有校验和。时间一律先换成 UTC 再编码。"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone, tzinfo
from decimal import Decimal
from typing import Any, Iterator, Mapping, Optional

UTC = timezone.utc
_LEGACY_TIMEZONE: ContextVar[Optional[tzinfo]] = ContextVar("cts_legacy_hash_timezone", default=None)


def _as_utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("规范哈希的时间必须带时区")
    return value.astimezone(UTC).isoformat()


def _legacy_text(value: datetime, zone: tzinfo) -> str:
    """修复前的编码。时区必须由调用方显式给出，不能取读取进程的本地时区。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("规范哈希的时间必须带时区")
    return value.astimezone(zone).isoformat()


def _normalize(value: Any, *, legacy_timezone: Optional[tzinfo]) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if legacy_timezone is not None:
            return _legacy_text(value, legacy_timezone)
        return _as_utc_text(value)
    if isinstance(value, Mapping):
        return {str(k): _normalize(v, legacy_timezone=legacy_timezone) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize(v, legacy_timezone=legacy_timezone) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        raise TypeError("checksum 拒绝 float")
    return str(value)


def canonical_json(payload: Mapping[str, Any], *, legacy_timezone: Optional[tzinfo] = None) -> str:
    return json.dumps(
        _normalize(dict(payload), legacy_timezone=legacy_timezone),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_hex(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def legacy_sha256_hex(payload: Mapping[str, Any], *, timezone: tzinfo) -> str:
    if timezone is None:
        raise ValueError("旧编码必须显式给出生产时区")
    return hashlib.sha256(canonical_json(payload, legacy_timezone=timezone).encode("utf-8")).hexdigest()


@contextmanager
def use_legacy_hash_timezone(zone: Optional[tzinfo]) -> Iterator[Optional[tzinfo]]:
    """声明旧校验和的生产时区。不声明时来源未知，不能改用当前机器时区。"""
    token = _LEGACY_TIMEZONE.set(zone)
    try:
        yield zone
    finally:
        _LEGACY_TIMEZONE.reset(token)


def configured_legacy_timezone() -> Optional[tzinfo]:
    return _LEGACY_TIMEZONE.get()


def assess_checksum(payload: Mapping[str, Any], stored: str) -> str:
    """返回 current、legacy、insufficient 或 mismatch。不改写 stored。"""
    if not stored:
        return "missing"
    if stored == sha256_hex(payload):
        return "current"
    zone = _LEGACY_TIMEZONE.get()
    if zone is None:
        return "insufficient"
    if stored == legacy_sha256_hex(payload, timezone=zone):
        return "legacy"
    return "mismatch"


def checksum_matches(payload: Mapping[str, Any], stored: str) -> bool:
    return assess_checksum(payload, stored) in ("current", "legacy")

"""规范哈希：原始、清洗、结果均有校验和。时间一律先换成 UTC 再编码。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

UTC = timezone.utc


def _as_utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("规范哈希的时间必须带时区")
    return value.astimezone(UTC).isoformat()


def _legacy_local_text(value: datetime) -> str:
    """修复前的编码：转到进程本地时区。只用于识别已有校验和，不用于新写入。"""
    return value.astimezone().isoformat()


def _normalize(value: Any, *, legacy: bool) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _legacy_local_text(value) if legacy else _as_utc_text(value)
    if isinstance(value, Mapping):
        return {str(k): _normalize(v, legacy=legacy) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize(v, legacy=legacy) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        raise TypeError("checksum 拒绝 float")
    return str(value)


def canonical_json(payload: Mapping[str, Any], *, legacy: bool = False) -> str:
    return json.dumps(_normalize(dict(payload), legacy=legacy), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_hex(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def legacy_sha256_hex(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload, legacy=True).encode("utf-8")).hexdigest()


def checksum_matches(payload: Mapping[str, Any], stored: str) -> bool:
    if not stored:
        return False
    if stored == sha256_hex(payload):
        return True
    return stored == legacy_sha256_hex(payload)

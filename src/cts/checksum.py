"""规范哈希：原始、清洗、结果均有校验和。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping


def _normalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone().isoformat()
    if isinstance(value, Mapping):
        return {str(k): _normalize(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        raise TypeError("checksum 拒绝 float")
    return str(value)


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(_normalize(dict(payload)), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_hex(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

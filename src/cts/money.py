"""定点金额与数量。禁止用二进制浮点存储资金。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Union

DecimalLike = Union[Decimal, str, int]


def D(value: DecimalLike) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError("禁止用 float 构造资金或数量，请传入 str 或 Decimal")
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"无法解析为 Decimal: {value!r}") from exc


def require_non_negative(value: Decimal, name: str) -> Decimal:
    if value < 0:
        raise ValueError(f"{name} 不能为负: {value}")
    return value


def require_positive(value: Decimal, name: str) -> Decimal:
    if value <= 0:
        raise ValueError(f"{name} 必须为正: {value}")
    return value


def floor_to_lot(qty: Decimal, lot_size: Decimal) -> Decimal:
    require_positive(lot_size, "lot_size")
    if qty <= 0:
        return Decimal("0")
    return (qty / lot_size).to_integral_value(rounding=ROUND_DOWN) * lot_size


def q(value: DecimalLike) -> str:
    """API/存储用字符串，避免二进制浮点。"""
    return format(D(value), "f")

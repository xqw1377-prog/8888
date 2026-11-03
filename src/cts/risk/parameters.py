"""参数合同：运行时只加载指定版本；生产只接受 APPROVED 且哈希一致。"""

from __future__ import annotations

from typing import Mapping, Optional

from cts.checksum import sha256_hex
from cts.errors import ParameterAdmissionError
from cts.money import D
from cts.risk.models import ParameterBundle, ParameterStatus, RuntimeMode

# 已验收的初始保证金杠杆。生产不得提高；提高需要新版本验收。
INITIAL_MARGIN_LEVERAGE = D("2")
# 研究路径的上限。合同值不能自行抬高这个上限。
RESEARCH_LEVERAGE_CAP = D("3")

PROPOSED_VALUES: dict[str, str] = {
    "per_symbol_risk_pct": "0.0020",
    "portfolio_risk_pct": "0.0040",
    "per_symbol_notional_pct": "0.75",
    "total_abs_notional_pct": "1.50",
    "leverage": "2",
    "margin_budget_pct": "0.50",
    "daily_loss_pause_pct": "0.01",
    "drawdown_reduce_pct": "0.04",
    "drawdown_pause_pct": "0.06",
    "risk_coefficient_reduced": "0.5",
}


def bundle_hash(values: Mapping[str, str], version: str, status: ParameterStatus) -> str:
    return sha256_hex({"version": version, "status": status.value, "values": dict(sorted(values.items()))})


def proposed_bundle(version: str = "risk-v0.3.3-proposed") -> ParameterBundle:
    values = dict(PROPOSED_VALUES)
    status = ParameterStatus.PROPOSED
    return ParameterBundle(
        version=version,
        status=status,
        values=values,
        config_hash=bundle_hash(values, version, status),
        approved_by=None,
    )


def mark_approved(bundle: ParameterBundle, *, approved_by: str, version: Optional[str] = None) -> ParameterBundle:
    new_version = version or f"{bundle.version}-approved"
    status = ParameterStatus.APPROVED
    values = dict(bundle.values)
    return ParameterBundle(
        version=new_version,
        status=status,
        values=values,
        config_hash=bundle_hash(values, new_version, status),
        approved_by=approved_by,
    )


def parse_contract_leverage(values: Mapping[str, str]) -> Decimal:
    """读取合同里的杠杆。超出研究上限或无法解析时拒绝，不改用另一处默认值。"""
    raw = values.get("leverage") if isinstance(values, Mapping) else None
    if raw is None or raw == "":
        raise ParameterAdmissionError("参数合同缺少 leverage", reason_code="LEVERAGE_CONTRACT_MISMATCH")
    try:
        leverage = D(raw)
    except (TypeError, ValueError) as exc:
        raise ParameterAdmissionError(
            "参数合同的 leverage 无法解析",
            reason_code="LEVERAGE_CONTRACT_MISMATCH",
        ) from exc
    if leverage <= 0 or leverage > RESEARCH_LEVERAGE_CAP:
        raise ParameterAdmissionError(
            "杠杆超出合同：初始保证金 2 倍，研究上限 3 倍",
            reason_code="LEVERAGE_CONTRACT_MISMATCH",
        )
    return leverage


def admit_parameters(mode: RuntimeMode, bundle: ParameterBundle, expected_hash: str) -> None:
    if bundle.config_hash != expected_hash:
        raise ParameterAdmissionError("参数哈希与登记版本不一致", reason_code="PARAM_HASH_MISMATCH")
    recomputed = bundle_hash(bundle.values, bundle.version, bundle.status)
    if recomputed != bundle.config_hash:
        raise ParameterAdmissionError("参数哈希与内容不一致", reason_code="PARAM_HASH_MISMATCH")
    if mode == RuntimeMode.PRODUCTION and bundle.status != ParameterStatus.APPROVED:
        raise ParameterAdmissionError(
            f"生产入口拒绝 {bundle.status.value} 参数，仅 APPROVED 可获得生产权限",
            reason_code="PARAM_NOT_APPROVED",
        )
    leverage = parse_contract_leverage(bundle.values)
    if mode == RuntimeMode.PRODUCTION and leverage != INITIAL_MARGIN_LEVERAGE:
        raise ParameterAdmissionError(
            "生产杠杆必须保持已验收的 2 倍，提高杠杆需要新版本验收",
            reason_code="LEVERAGE_CONTRACT_MISMATCH",
        )

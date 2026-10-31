"""参数合同：运行时只加载指定版本；生产只接受 APPROVED 且哈希一致。"""

from __future__ import annotations

from typing import Mapping, Optional

from cts.checksum import sha256_hex
from cts.errors import ParameterAdmissionError
from cts.risk.models import ParameterBundle, ParameterStatus, RuntimeMode

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

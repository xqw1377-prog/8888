from cts.risk.funding_reserve import FundingScenario, funding_reserve
from cts.risk.models import (
    ParameterStatus,
    ReservationStatus,
    RuntimeMode,
    SystemState,
)
from cts.risk.parameters import mark_approved, proposed_bundle

def __getattr__(name: str):
    """推迟加载 RiskEngine，避免 strategy.sizing 与 risk 包互相在导入中途卡住。"""
    if name == "RiskEngine":
        from cts.risk.engine import RiskEngine

        return RiskEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "FundingScenario",
    "ParameterStatus",
    "ReservationStatus",
    "RiskEngine",
    "RuntimeMode",
    "SystemState",
    "funding_reserve",
    "mark_approved",
    "proposed_bundle",
]

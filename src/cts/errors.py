"""稳定 reason_code，前端显示中文解释。"""

from __future__ import annotations


class CtsError(Exception):
    reason_code = "INTERNAL"

    def __init__(self, message: str, *, reason_code: str | None = None) -> None:
        super().__init__(message)
        if reason_code:
            self.reason_code = reason_code


class ConflictError(CtsError):
    reason_code = "IDEMPOTENCY_CONFLICT"


class StaleDataError(CtsError):
    reason_code = "STALE_DATA"


class LookaheadError(CtsError):
    reason_code = "LOOKAHEAD_FORBIDDEN"


class UnitValidationError(CtsError):
    reason_code = "UNIT_INVALID"


class LedgerImbalanceError(CtsError):
    reason_code = "LEDGER_IMBALANCE"


class RiskLimitError(CtsError):
    reason_code = "RISK_LIMIT"


class SnapshotInconsistentError(CtsError):
    reason_code = "SNAPSHOT_INCONSISTENT"


class RecoveryStaleError(CtsError):
    reason_code = "RECOVERY_SNAPSHOT_STALE"


class ParameterAdmissionError(CtsError):
    reason_code = "PARAM_NOT_APPROVED"


class ExitRejectedError(CtsError):
    reason_code = "EXIT_REJECTED"


class NewRiskBlockedError(CtsError):
    reason_code = "NEW_RISK_BLOCKED"

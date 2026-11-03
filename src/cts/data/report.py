"""数据集校验报告与哈希。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from cts.checksum import assess_checksum, sha256_hex
from cts.data.gaps import GapReport, classify_gaps
from cts.data.models import DataGrade, MarketDataset, record_payload
from cts.errors import ConflictError, UnitValidationError


@dataclass(frozen=True)
class DatasetValidationReport:
    venue: str
    symbol: str
    interval: str
    dataset_checksum: str
    report_checksum: str
    grade: DataGrade
    candle_count: int
    mark_count: int
    funding_count: int
    risk_tier_count: int
    mark_price_missing_ratio: str
    mark_price_max_gap_seconds: int
    missing_intervals: int
    exposure_overlap: bool
    notes: tuple[str, ...]
    generated_at: datetime
    accepted: bool


def validate_dataset(
    dataset: MarketDataset,
    *,
    generated_at: datetime,
    exposure_windows: tuple[tuple[datetime, datetime], ...] = (),
    mark_expected_interval=None,
) -> DatasetValidationReport:
    seen: dict[tuple, str] = {}
    for record in list(dataset.candles) + list(dataset.marks) + list(dataset.fundings) + list(dataset.risk_tiers):
        key = record.identity()
        payload = record_payload(record)
        if payload is None:
            raise ConflictError(f"数据集内记录缺少可校验内容: {key}")
        status = assess_checksum(payload, record.checksum)
        if status == "insufficient":
            raise ConflictError(
                "旧校验和的生产时区未配置，证据不足，保留原哈希",
                reason_code="EVIDENCE_INSUFFICIENT",
            )
        if status not in ("current", "legacy"):
            raise ConflictError("checksum 与内容不一致，不能用旧校验和包装新内容")
        digest = sha256_hex(payload)
        if key in seen and seen[key] != digest:
            raise ConflictError(f"数据集内同标识异容: {key}")
        seen[key] = digest
        if hasattr(record, "validate_units"):
            record.validate_units()

    for candle in dataset.candles:
        if candle.venue != dataset.venue or candle.symbol != dataset.symbol:
            raise UnitValidationError("K 线 venue/symbol 与数据集不一致")
        if candle.interval != dataset.interval:
            raise UnitValidationError("K 线周期与数据集不一致")

    gap: GapReport = classify_gaps(
        candles=dataset.candles,
        marks=dataset.marks,
        expected_sampling_interval=dataset.expected_sampling_interval,
        range_start=dataset.start,
        range_end=dataset.end,
        mark_expected_interval=mark_expected_interval,
        exposure_windows=exposure_windows,
    )
    payload = {
        "dataset_checksum": dataset.checksum,
        "grade": gap.grade.value,
        "candle_count": len(dataset.candles),
        "mark_count": len(dataset.marks),
        "funding_count": len(dataset.fundings),
        "risk_tier_count": len(dataset.risk_tiers),
        "mark_price_missing_ratio": gap.mark_price_missing_ratio,
        "mark_price_max_gap_seconds": gap.mark_price_max_gap_seconds,
        "missing_intervals": len(gap.missing_intervals),
        "exposure_overlap": gap.exposure_overlap,
        "notes": gap.notes,
        "generated_at": generated_at,
    }
    accepted = True
    # 校验报告本身可通过；强平验收资格另判
    return DatasetValidationReport(
        venue=dataset.venue,
        symbol=dataset.symbol,
        interval=dataset.interval,
        dataset_checksum=dataset.checksum,
        report_checksum=sha256_hex(payload),
        grade=gap.grade,
        candle_count=len(dataset.candles),
        mark_count=len(dataset.marks),
        funding_count=len(dataset.fundings),
        risk_tier_count=len(dataset.risk_tiers),
        mark_price_missing_ratio=format(gap.mark_price_missing_ratio, "f"),
        mark_price_max_gap_seconds=gap.mark_price_max_gap_seconds,
        missing_intervals=len(gap.missing_intervals),
        exposure_overlap=gap.exposure_overlap,
        notes=gap.notes,
        generated_at=generated_at,
        accepted=accepted,
    )


def liquidation_replay_eligible(report: DatasetValidationReport) -> bool:
    return report.grade == DataGrade.LIQUIDATION_REPLAY_VALIDATED and not report.exposure_overlap

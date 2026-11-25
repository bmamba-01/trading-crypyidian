"""Điều phối backfill BACKTEST_RESEARCH → manifest READY → bundle & quality report (CRY-364).

Phạm vi mặc định giới hạn an toàn (`BTCUSDT`, `ETHUSDT`, interval do người vận hành chọn).
Connector live giữ `disabled` cho tới khi có policy `BACKTEST_RESEARCH` được xác minh;
hỗ trợ đầy đủ tạo job, xem tiến độ, resume, hủy và xuất báo cáo coverage/gap.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

DEFAULT_ALLOWED_SYMBOLS: frozenset[str] = frozenset({"BTCUSDT", "ETHUSDT"})
INTERVAL_DURATIONS: dict[str, timedelta] = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}


class BackfillJobStatus(StrEnum):
    """Trạng thái vòng đời của một job backfill/đóng gói dataset."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    READY = "READY"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class BackfillOrchestrationError(ValueError):
    """Lỗi nghiệp vụ có mã lý do khi tạo hoặc điều phối backfill job."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StoredBarPoint:
    """Thông tin tối thiểu của một nến đã lưu phục vụ kiểm toán coverage/gap và băm."""

    open_time: datetime
    close_price: str
    volume: str
    revision: int = 1


@dataclass(frozen=True, slots=True)
class BackfillQualityGap:
    """Một khoảng trống nến bị thiếu trong cửa sổ yêu cầu."""

    start_time: datetime
    end_time: datetime
    missing_bars: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_time": self.start_time.astimezone(UTC).isoformat(),
            "end_time": self.end_time.astimezone(UTC).isoformat(),
            "missing_bars": self.missing_bars,
        }


@dataclass(frozen=True, slots=True)
class BackfillQualityReport:
    """Báo cáo chất lượng coverage/gap/duplicate cho một tập nến đã lưu."""

    symbol: str
    interval: str
    range_start: datetime
    range_end: datetime
    expected_bars: int
    observed_bars: int
    coverage_pct: float
    gaps: tuple[BackfillQualityGap, ...]
    duplicate_count: int
    bars_content_hash: str

    @property
    def is_complete(self) -> bool:
        return self.expected_bars > 0 and self.observed_bars == self.expected_bars and not self.gaps

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "range_start": self.range_start.astimezone(UTC).isoformat(),
            "range_end": self.range_end.astimezone(UTC).isoformat(),
            "expected_bars": self.expected_bars,
            "observed_bars": self.observed_bars,
            "coverage_pct": self.coverage_pct,
            "is_complete": self.is_complete,
            "gap_count": len(self.gaps),
            "gaps": [g.to_dict() for g in self.gaps],
            "duplicate_count": self.duplicate_count,
            "bars_content_hash": self.bars_content_hash,
        }


def compute_backfill_quality_report(
    bars: Sequence[StoredBarPoint],
    *,
    symbol: str,
    interval: str,
    range_start: datetime,
    range_end: datetime,
) -> BackfillQualityReport:
    """Tính coverage, khoảng trống gap, số duplicate và SHA-256 nội dung nến."""
    if interval not in INTERVAL_DURATIONS:
        raise BackfillOrchestrationError(
            "INTERVAL_UNSUPPORTED", f"Interval không hỗ trợ: {interval}"
        )
    step = INTERVAL_DURATIONS[interval]
    start_utc = range_start.astimezone(UTC)
    end_utc = range_end.astimezone(UTC)
    if end_utc <= start_utc:
        raise BackfillOrchestrationError("INVALID_RANGE", "range_end phải sau range_start.")

    expected_times: list[datetime] = []
    cur = start_utc
    while cur < end_utc:
        expected_times.append(cur)
        cur += step

    by_time: dict[datetime, StoredBarPoint] = {}
    duplicate_count = 0
    for bar in bars:
        t = bar.open_time.astimezone(UTC)
        if t < start_utc or t >= end_utc:
            continue
        if t in by_time or bar.revision > 1:
            duplicate_count += 1
        prev = by_time.get(t)
        if prev is None or bar.revision >= prev.revision:
            by_time[t] = bar

    gaps: list[BackfillQualityGap] = []
    gap_start: datetime | None = None
    missing_run = 0
    for t in expected_times:
        if t not in by_time:
            if gap_start is None:
                gap_start = t
            missing_run += 1
        elif gap_start is not None:
            gaps.append(
                BackfillQualityGap(
                    start_time=gap_start,
                    end_time=t,
                    missing_bars=missing_run,
                )
            )
            gap_start = None
            missing_run = 0
    if gap_start is not None:
        gaps.append(
            BackfillQualityGap(
                start_time=gap_start,
                end_time=end_utc,
                missing_bars=missing_run,
            )
        )

    expected_count = len(expected_times)
    observed_count = len(by_time)
    coverage = round((observed_count / expected_count) * 100.0, 2) if expected_count else 0.0

    canonical_bars = [
        {
            "open_time": t.isoformat(),
            "close": by_time[t].close_price,
            "volume": by_time[t].volume,
            "revision": by_time[t].revision,
        }
        for t in sorted(by_time)
    ]
    bars_hash = hashlib.sha256(
        json.dumps(canonical_bars, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return BackfillQualityReport(
        symbol=symbol.upper(),
        interval=interval,
        range_start=start_utc,
        range_end=end_utc,
        expected_bars=expected_count,
        observed_bars=observed_count,
        coverage_pct=coverage,
        gaps=tuple(gaps),
        duplicate_count=duplicate_count,
        bars_content_hash=bars_hash,
    )


@dataclass(frozen=True, slots=True)
class BackfillOrchestrationJob:
    """Trạng thái một job backfill BACKTEST_RESEARCH → manifest → bundle."""

    job_id: UUID
    workspace_id: UUID
    symbol: str
    interval: str
    range_start: datetime
    range_end: datetime
    purpose: str
    connector_enabled: bool
    status: BackfillJobStatus
    progress_pct: float
    created_by: str
    created_at: datetime
    updated_at: datetime
    policy_fingerprint: str | None = None
    manifest_hash: str | None = None
    bundle_hash: str | None = None
    quality_report: BackfillQualityReport | None = None
    cancel_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": str(self.job_id),
            "workspace_id": str(self.workspace_id),
            "symbol": self.symbol,
            "interval": self.interval,
            "range_start": self.range_start.astimezone(UTC).isoformat(),
            "range_end": self.range_end.astimezone(UTC).isoformat(),
            "purpose": self.purpose,
            "connector_enabled": self.connector_enabled,
            "status": self.status.value,
            "progress_pct": self.progress_pct,
            "policy_fingerprint": self.policy_fingerprint,
            "manifest_hash": self.manifest_hash,
            "bundle_hash": self.bundle_hash,
            "quality_report": self.quality_report.to_dict() if self.quality_report else None,
            "cancel_reason": self.cancel_reason,
        }


class BackfillDatasetOrchestrationService:
    """Service điều phối vòng đời job backfill, đóng gói manifest READY và xuất quality report."""

    def __init__(
        self,
        *,
        allowed_symbols: frozenset[str] = DEFAULT_ALLOWED_SYMBOLS,
    ) -> None:
        self._allowed_symbols = frozenset(s.upper() for s in allowed_symbols)
        self._jobs: dict[UUID, BackfillOrchestrationJob] = {}

    def create_job(
        self,
        *,
        workspace_id: UUID,
        symbol: str,
        interval: str,
        range_start: datetime,
        range_end: datetime,
        created_by: str,
        connector_enabled: bool = False,
        policy_verified: bool = False,
        now: datetime | None = None,
    ) -> BackfillOrchestrationJob:
        """Tạo job mới; khóa connector live nếu chưa có policy BACKTEST_RESEARCH verified."""
        sym = symbol.upper().strip()
        if sym not in self._allowed_symbols:
            raise BackfillOrchestrationError(
                "SYMBOL_OUT_OF_SCOPE",
                f"Symbol {sym} không nằm trong phạm vi mặc định ({sorted(self._allowed_symbols)}).",
            )
        if interval not in INTERVAL_DURATIONS:
            raise BackfillOrchestrationError(
                "INTERVAL_UNSUPPORTED", f"Interval không được hỗ trợ: {interval}."
            )
        if range_end <= range_start:
            raise BackfillOrchestrationError("INVALID_RANGE", "range_end phải sau range_start.")
        if connector_enabled and not policy_verified:
            raise BackfillOrchestrationError(
                "BACKFILL_LIVE_CONNECTOR_LOCKED",
                "Connector live bị khóa khi chưa có policy BACKTEST_RESEARCH đã xác minh.",
            )

        ts = now or datetime.now(UTC)
        job = BackfillOrchestrationJob(
            job_id=uuid4(),
            workspace_id=workspace_id,
            symbol=sym,
            interval=interval,
            range_start=range_start.astimezone(UTC),
            range_end=range_end.astimezone(UTC),
            purpose="BACKTEST_RESEARCH",
            connector_enabled=connector_enabled,
            status=BackfillJobStatus.QUEUED,
            progress_pct=0.0,
            created_by=created_by,
            created_at=ts,
            updated_at=ts,
        )
        self._jobs[job.job_id] = job
        return job

    def get_job(self, job_id: UUID) -> BackfillOrchestrationJob:
        if job_id not in self._jobs:
            raise BackfillOrchestrationError("JOB_NOT_FOUND", f"Không tìm thấy job {job_id}.")
        return self._jobs[job_id]

    def list_jobs(self, *, workspace_id: UUID) -> list[BackfillOrchestrationJob]:
        return [j for j in self._jobs.values() if j.workspace_id == workspace_id]

    def pause_job(self, job_id: UUID, *, now: datetime | None = None) -> BackfillOrchestrationJob:
        job = self.get_job(job_id)
        if job.status in (BackfillJobStatus.READY, BackfillJobStatus.CANCELLED):
            raise BackfillOrchestrationError(
                "JOB_TERMINAL", f"Không thể tạm dừng job ở trạng thái {job.status}."
            )
        updated = replace(job, status=BackfillJobStatus.PAUSED, updated_at=now or datetime.now(UTC))
        self._jobs[job_id] = updated
        return updated

    def cancel_job(
        self, job_id: UUID, *, reason: str, now: datetime | None = None
    ) -> BackfillOrchestrationJob:
        job = self.get_job(job_id)
        if job.status is BackfillJobStatus.READY:
            raise BackfillOrchestrationError("JOB_ALREADY_READY", "Không thể hủy job đã READY.")
        updated = replace(
            job,
            status=BackfillJobStatus.CANCELLED,
            cancel_reason=reason,
            updated_at=now or datetime.now(UTC),
        )
        self._jobs[job_id] = updated
        return updated

    def run_or_resume_job(
        self,
        job_id: UUID,
        *,
        stored_bars: Sequence[StoredBarPoint],
        policy_fingerprint: str,
        now: datetime | None = None,
    ) -> BackfillOrchestrationJob:
        """Chạy hoặc resume job từ nến đã lưu -> tính quality report, manifest READY và bundle."""
        job = self.get_job(job_id)
        if job.status is BackfillJobStatus.CANCELLED:
            raise BackfillOrchestrationError("JOB_CANCELLED", "Không thể chạy lại job đã bị hủy.")

        report = compute_backfill_quality_report(
            stored_bars,
            symbol=job.symbol,
            interval=job.interval,
            range_start=job.range_start,
            range_end=job.range_end,
        )
        manifest_payload = {
            "schema_version": 1,
            "workspace_id": str(job.workspace_id),
            "symbol": job.symbol,
            "interval": job.interval,
            "range_start": job.range_start.isoformat(),
            "range_end": job.range_end.isoformat(),
            "purpose": job.purpose,
            "policy_fingerprint": policy_fingerprint,
            "bars_content_hash": report.bars_content_hash,
        }
        manifest_hash = hashlib.sha256(
            json.dumps(manifest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        bundle_payload = {
            "schema_version": 1,
            "workspace_id": str(job.workspace_id),
            "symbol": job.symbol,
            "components": [
                {
                    "interval": job.interval,
                    "dataset_hash": manifest_hash,
                    "bars_content_hash": report.bars_content_hash,
                }
            ],
            "policy_fingerprint": policy_fingerprint,
        }
        bundle_hash = hashlib.sha256(
            json.dumps(bundle_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        updated = replace(
            job,
            status=BackfillJobStatus.READY,
            progress_pct=100.0,
            policy_fingerprint=policy_fingerprint,
            manifest_hash=manifest_hash,
            bundle_hash=bundle_hash,
            quality_report=report,
            updated_at=now or datetime.now(UTC),
        )
        self._jobs[job_id] = updated
        return updated

    def export_quality_report(self, job_id: UUID) -> dict[str, Any]:
        """Xuất báo cáo coverage/gap kèm hash manifest và bundle."""
        job = self.get_job(job_id)
        if job.quality_report is None:
            raise BackfillOrchestrationError(
                "REPORT_NOT_READY", "Job chưa chạy xong nên chưa có quality report."
            )
        return {
            "job_id": str(job.job_id),
            "status": job.status.value,
            "manifest_hash": job.manifest_hash,
            "bundle_hash": job.bundle_hash,
            "quality_report": job.quality_report.to_dict(),
        }

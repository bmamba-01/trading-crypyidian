"""Kho báo cáo aggregate-only cho chẩn đoán PAPER scorer."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.learning_paper_performance_entities import (
    LearningPaperPerformanceReportRecord,
)


class PaperPerformanceIdempotencyConflict(RuntimeError):
    """Khóa idempotency đã được dùng với bot hoặc yêu cầu khác."""


class PaperPerformanceAggregateUnavailable(RuntimeError):
    """Gateway từ chối lineage, quyền hiện hành hoặc nguồn aggregate."""


class LearningPaperPerformanceRepository:
    """Gọi aggregate SQL hẹp và chỉ ghi/đọc báo cáo đã tổng hợp."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_idempotency_key(
        self, *, workspace_id: UUID, idempotency_key: UUID
    ) -> LearningPaperPerformanceReportRecord | None:
        result = await self._session.scalars(
            select(LearningPaperPerformanceReportRecord).where(
                LearningPaperPerformanceReportRecord.workspace_id == workspace_id,
                LearningPaperPerformanceReportRecord.idempotency_key == idempotency_key,
            )
        )
        return result.one_or_none()

    async def get_snapshot(
        self,
        *,
        workspace_id: UUID,
        target_fingerprint: str,
        policy_hash: str,
        as_of: datetime,
        membership_digest: str,
    ) -> LearningPaperPerformanceReportRecord | None:
        result = await self._session.scalars(
            select(LearningPaperPerformanceReportRecord).where(
                LearningPaperPerformanceReportRecord.workspace_id == workspace_id,
                LearningPaperPerformanceReportRecord.target_fingerprint == target_fingerprint,
                LearningPaperPerformanceReportRecord.policy_hash == policy_hash,
                LearningPaperPerformanceReportRecord.as_of == as_of,
                LearningPaperPerformanceReportRecord.membership_digest == membership_digest,
            )
        )
        return result.one_or_none()

    async def aggregate_active_target(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        target_fingerprint: str,
        baseline_report_id: UUID,
        baseline_report_hash: str,
        as_of: datetime,
    ) -> tuple[Mapping[str, Any], ...]:
        """Trả đúng hai dòng aggregate; không trả ID thành viên hay payload nguồn."""
        if type(workspace_id) is not UUID or type(bot_id) is not UUID:
            raise ValueError("Workspace và bot phải là UUID authoritative.")
        if type(baseline_report_id) is not UUID:
            raise ValueError("Baseline report phải là UUID authoritative.")
        result = await self._session.execute(
            text(
                "SELECT * FROM public.wp06_aggregate_paper_performance("
                "target_workspace => CAST(:workspace_id AS uuid), "
                "target_bot => CAST(:bot_id AS uuid), "
                "expected_target_fingerprint => :target_fingerprint, "
                "expected_baseline_report_id => CAST(:baseline_report_id AS uuid), "
                "expected_baseline_report_hash => :baseline_report_hash, "
                "requested_as_of => CAST(:as_of AS timestamptz))"
            ),
            {
                "workspace_id": str(workspace_id),
                "bot_id": str(bot_id),
                "target_fingerprint": target_fingerprint,
                "baseline_report_id": str(baseline_report_id),
                "baseline_report_hash": baseline_report_hash,
                "as_of": as_of,
            },
        )
        rows = tuple(cast(Mapping[str, Any], row) for row in result.mappings().all())
        if len(rows) != 2 or {row.get("cohort_name") for row in rows} != {"older", "newer"}:
            raise PaperPerformanceAggregateUnavailable(
                "Gateway không trả đủ hai cohort PAPER có lineage khớp."
            )
        forbidden = {
            "candidate_id",
            "decision_id",
            "decision_payload",
            "labels",
            "probabilities",
            "trade_id",
            "trade_payload",
        }
        if any(forbidden.intersection(row.keys()) for row in rows):
            raise PaperPerformanceAggregateUnavailable("Gateway trả trường chi tiết bị cấm.")
        return rows

    async def insert(
        self, report: LearningPaperPerformanceReportRecord
    ) -> LearningPaperPerformanceReportRecord:
        if type(report) is not LearningPaperPerformanceReportRecord:
            raise ValueError("Chỉ LearningPaperPerformanceReportRecord mới được lưu.")
        self._session.add(report)
        await self._session.flush()
        return report

    async def list_recent(
        self, *, workspace_id: UUID, limit: int = 50
    ) -> tuple[LearningPaperPerformanceReportRecord, ...]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit phải thuộc 1..200.")
        result = await self._session.scalars(
            select(LearningPaperPerformanceReportRecord)
            .where(LearningPaperPerformanceReportRecord.workspace_id == workspace_id)
            .order_by(
                LearningPaperPerformanceReportRecord.as_of.desc(),
                LearningPaperPerformanceReportRecord.created_at.desc(),
            )
            .limit(limit)
        )
        return tuple(result.all())


__all__ = [
    "LearningPaperPerformanceRepository",
    "PaperPerformanceAggregateUnavailable",
    "PaperPerformanceIdempotencyConflict",
]

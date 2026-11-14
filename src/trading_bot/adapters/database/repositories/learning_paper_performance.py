"""Kho báo cáo aggregate-only cho chẩn đoán PAPER scorer."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.learning_paper_performance_entities import (
    LearningPaperPerformanceReportRecord,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class PaperPerformanceReportReference:
    """Định danh báo cáo chỉ gồm metadata, chưa mang payload số liệu."""

    report_id: UUID
    bot_id: UUID


class PaperPerformanceIdempotencyConflict(RuntimeError):
    """Khóa idempotency đã được dùng với bot hoặc yêu cầu khác."""


class PaperPerformanceAggregateUnavailable(RuntimeError):
    """Gateway từ chối lineage, quyền hiện hành hoặc nguồn aggregate."""


class LearningPaperPerformanceRepository:
    """Gọi aggregate SQL hẹp và chỉ ghi/đọc báo cáo đã tổng hợp."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_idempotency_reference(
        self, *, workspace_id: UUID, idempotency_key: UUID
    ) -> PaperPerformanceReportReference | None:
        result = await self._session.execute(
            select(
                LearningPaperPerformanceReportRecord.id,
                LearningPaperPerformanceReportRecord.bot_id,
            ).where(
                LearningPaperPerformanceReportRecord.workspace_id == workspace_id,
                LearningPaperPerformanceReportRecord.idempotency_key == idempotency_key,
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return PaperPerformanceReportReference(report_id=row["id"], bot_id=row["bot_id"])

    async def get_snapshot_reference(
        self,
        *,
        workspace_id: UUID,
        target_fingerprint: str,
        policy_hash: str,
        as_of: datetime,
        membership_digest: str,
    ) -> PaperPerformanceReportReference | None:
        result = await self._session.execute(
            select(
                LearningPaperPerformanceReportRecord.id,
                LearningPaperPerformanceReportRecord.bot_id,
            ).where(
                LearningPaperPerformanceReportRecord.workspace_id == workspace_id,
                LearningPaperPerformanceReportRecord.target_fingerprint == target_fingerprint,
                LearningPaperPerformanceReportRecord.policy_hash == policy_hash,
                LearningPaperPerformanceReportRecord.as_of == as_of,
                LearningPaperPerformanceReportRecord.membership_digest == membership_digest,
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return PaperPerformanceReportReference(report_id=row["id"], bot_id=row["bot_id"])

    async def get_report_by_id(
        self, *, workspace_id: UUID, report_id: UUID
    ) -> LearningPaperPerformanceReportRecord | None:
        """Đọc payload qua SQL gateway tự recheck quyền trước khi chiếu số liệu."""
        if type(workspace_id) is not UUID or type(report_id) is not UUID:
            raise ValueError("Workspace và report phải là UUID authoritative.")
        statement = select(LearningPaperPerformanceReportRecord).from_statement(
            text(
                "SELECT * FROM public.wp06_read_paper_performance_report("
                "CAST(:workspace_id AS uuid), CAST(:report_id AS uuid))"
            )
        )
        result = await self._session.scalars(
            statement,
            {"workspace_id": str(workspace_id), "report_id": str(report_id)},
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
        rights_digests = {row.get("rights_digest") for row in rows}
        if len(rights_digests) != 1:
            raise PaperPerformanceAggregateUnavailable(
                "Gateway không trả cùng một dấu quyền hiện hành cho hai cohort."
            )
        rights_digest = next(iter(rights_digests))
        if type(rights_digest) is not str or _SHA256.fullmatch(rights_digest) is None:
            raise PaperPerformanceAggregateUnavailable(
                "Gateway không trả dấu quyền hiện hành hợp lệ."
            )
        return rows

    async def report_current_rights(self, *, workspace_id: UUID, report_id: UUID) -> bool:
        """Kiểm tra lại quyền aggregate hiện hành mà không trả chi tiết nguồn."""
        if type(workspace_id) is not UUID or type(report_id) is not UUID:
            raise ValueError("Workspace và report phải là UUID authoritative.")
        authorized = await self._session.scalar(
            text(
                "SELECT public.wp06_recheck_paper_performance_report_rights("
                "CAST(:workspace_id AS uuid), CAST(:report_id AS uuid))"
            ),
            {"workspace_id": str(workspace_id), "report_id": str(report_id)},
        )
        return authorized is True

    async def insert(
        self, report: LearningPaperPerformanceReportRecord
    ) -> LearningPaperPerformanceReportRecord:
        if type(report) is not LearningPaperPerformanceReportRecord:
            raise ValueError("Chỉ LearningPaperPerformanceReportRecord mới được lưu.")
        await self._session.execute(
            text(
                "SELECT public.wp06_insert_paper_performance_report("
                "CAST(:report_id AS uuid), CAST(:workspace_id AS uuid), "
                "CAST(:bot_id AS uuid), CAST(:baseline_report_id AS uuid), "
                ":baseline_report_hash, :policy_version, :policy_hash, :target_fingerprint, "
                ":membership_digest, CAST(:as_of AS timestamptz), :status, "
                "CAST(:idempotency_key AS uuid), :report_hash, CAST(:report AS jsonb), "
                ":canonical_report)"
            ),
            {
                "report_id": str(report.id),
                "workspace_id": str(report.workspace_id),
                "bot_id": str(report.bot_id),
                "baseline_report_id": str(report.baseline_report_id),
                "baseline_report_hash": report.baseline_report_hash,
                "policy_version": report.policy_version,
                "policy_hash": report.policy_hash,
                "target_fingerprint": report.target_fingerprint,
                "membership_digest": report.membership_digest,
                "as_of": report.as_of,
                "status": report.status,
                "idempotency_key": str(report.idempotency_key),
                "report_hash": report.report_hash,
                "report": json.dumps(
                    report.report,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "canonical_report": report.canonical_report,
            },
        )
        return report

    async def list_recent_references(
        self, *, workspace_id: UUID, limit: int = 50
    ) -> tuple[PaperPerformanceReportReference, ...]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit phải thuộc 1..200.")
        result = await self._session.execute(
            select(
                LearningPaperPerformanceReportRecord.id,
                LearningPaperPerformanceReportRecord.bot_id,
            )
            .where(LearningPaperPerformanceReportRecord.workspace_id == workspace_id)
            .order_by(
                LearningPaperPerformanceReportRecord.as_of.desc(),
                LearningPaperPerformanceReportRecord.created_at.desc(),
            )
            .limit(limit)
        )
        return tuple(
            PaperPerformanceReportReference(report_id=row["id"], bot_id=row["bot_id"])
            for row in result.mappings().all()
        )


__all__ = [
    "LearningPaperPerformanceRepository",
    "PaperPerformanceAggregateUnavailable",
    "PaperPerformanceIdempotencyConflict",
    "PaperPerformanceReportReference",
]

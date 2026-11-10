"""Repository candidate split với projection metadata riêng cho admin."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    LearningSplitApprovalRecord,
    LearningSplitCandidateRecord,
)
from trading_bot.application.learning_split_candidate import LearningSplitCandidate


class LearningSplitCandidateConflict(RuntimeError):
    """Candidate hash đã lưu có canonical payload khác yêu cầu."""


@dataclass(frozen=True, slots=True)
class LearningSplitCandidateReview:
    """Projection an toàn; không có canonical manifest, sample, label hay feature."""

    candidate_id: UUID
    trial_record_id: UUID
    proof_hash: str
    manifest_hash: str
    split_policy_version: str
    fold_plan_hash: str
    evaluation_cutoff: datetime
    created_at: datetime
    preview: dict[str, Any]
    approved_at: datetime | None


@dataclass(frozen=True, slots=True)
class LearningSplitCandidateWriteResult:
    """Candidate đã append hoặc đọc lại từ hash canonical trùng."""

    candidate: LearningSplitCandidate
    duplicate: bool


class LearningSplitCandidateRepository:
    """Staging private và metadata review tenant-bound."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self, *, candidate: LearningSplitCandidate
    ) -> LearningSplitCandidateWriteResult:
        if type(candidate) is not LearningSplitCandidate:
            raise ValueError("candidate phải là LearningSplitCandidate.")
        await self._bind_workspace(candidate.workspace_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {
                "lock_key": (
                    f"learning-split-candidate:{candidate.workspace_id}:"
                    f"{candidate.manifest.content_hash}:{candidate.fold_plan.content_hash}"
                )
            },
        )
        existing = await self._session.scalar(
            select(LearningSplitCandidateRecord).where(
                LearningSplitCandidateRecord.workspace_id == candidate.workspace_id,
                LearningSplitCandidateRecord.trial_record_id == candidate.trial_record_id,
                LearningSplitCandidateRecord.manifest_hash == candidate.manifest.content_hash,
                LearningSplitCandidateRecord.fold_plan_hash == candidate.fold_plan.content_hash,
            )
        )
        if existing is not None:
            stored = existing.to_domain()
            if (
                stored.candidate_id != candidate.candidate_id
                or stored.manifest.canonical_json != candidate.manifest.canonical_json
                or stored.canonical_configuration != candidate.canonical_configuration
                or stored.preview_json != candidate.preview_json
                or stored.proof_hash != candidate.proof_hash
                or stored.proof_record_id != candidate.proof_record_id
            ):
                raise LearningSplitCandidateConflict("Candidate hash đã gắn payload khác.")
            return LearningSplitCandidateWriteResult(stored, True)
        record = LearningSplitCandidateRecord.from_domain(candidate)
        self._session.add(record)
        await self._session.flush()
        return LearningSplitCandidateWriteResult(candidate, False)

    async def list_for_review(
        self, *, workspace_id: UUID, limit: int = 100
    ) -> tuple[LearningSplitCandidateReview, ...]:
        if type(workspace_id) is not UUID or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("workspace_id/limit không hợp lệ.")
        await self._bind_workspace(workspace_id)
        statement = (
            select(
                LearningSplitCandidateRecord.id,
                LearningSplitCandidateRecord.trial_record_id,
                LearningSplitCandidateRecord.proof_hash,
                LearningSplitCandidateRecord.manifest_hash,
                LearningSplitCandidateRecord.split_policy_version,
                LearningSplitCandidateRecord.fold_plan_hash,
                LearningSplitCandidateRecord.evaluation_cutoff,
                LearningSplitCandidateRecord.created_at,
                LearningSplitCandidateRecord.preview,
                LearningSplitApprovalRecord.approved_at,
            )
            .outerjoin(
                LearningSplitApprovalRecord,
                and_(
                    LearningSplitApprovalRecord.workspace_id
                    == LearningSplitCandidateRecord.workspace_id,
                    LearningSplitApprovalRecord.manifest_hash
                    == LearningSplitCandidateRecord.manifest_hash,
                ),
            )
            .where(LearningSplitCandidateRecord.workspace_id == workspace_id)
            .order_by(
                LearningSplitCandidateRecord.created_at.desc(),
                LearningSplitCandidateRecord.id,
            )
            .limit(limit)
        )
        result = await self._session.execute(statement)
        return tuple(
            LearningSplitCandidateReview(
                candidate_id=row.id,
                trial_record_id=row.trial_record_id,
                proof_hash=row.proof_hash,
                manifest_hash=row.manifest_hash,
                split_policy_version=row.split_policy_version,
                fold_plan_hash=row.fold_plan_hash,
                evaluation_cutoff=row.evaluation_cutoff,
                created_at=row.created_at,
                preview=dict(row.preview),
                approved_at=row.approved_at,
            )
            for row in result.all()
        )

    async def get_for_review(
        self, *, workspace_id: UUID, candidate_id: UUID
    ) -> LearningSplitCandidateReview | None:
        if type(workspace_id) is not UUID or type(candidate_id) is not UUID:
            raise ValueError("workspace_id/candidate_id phải là UUID.")
        await self._bind_workspace(workspace_id)
        statement = (
            select(
                LearningSplitCandidateRecord.id,
                LearningSplitCandidateRecord.trial_record_id,
                LearningSplitCandidateRecord.proof_hash,
                LearningSplitCandidateRecord.manifest_hash,
                LearningSplitCandidateRecord.split_policy_version,
                LearningSplitCandidateRecord.fold_plan_hash,
                LearningSplitCandidateRecord.evaluation_cutoff,
                LearningSplitCandidateRecord.created_at,
                LearningSplitCandidateRecord.preview,
                LearningSplitApprovalRecord.approved_at,
            )
            .outerjoin(
                LearningSplitApprovalRecord,
                and_(
                    LearningSplitApprovalRecord.workspace_id
                    == LearningSplitCandidateRecord.workspace_id,
                    LearningSplitApprovalRecord.manifest_hash
                    == LearningSplitCandidateRecord.manifest_hash,
                ),
            )
            .where(
                LearningSplitCandidateRecord.workspace_id == workspace_id,
                LearningSplitCandidateRecord.id == candidate_id,
            )
        )
        row = (await self._session.execute(statement)).first()
        return _review(row) if row is not None else None

    async def get_for_builder(
        self, *, workspace_id: UUID, candidate_id: UUID
    ) -> LearningSplitCandidate | None:
        """Builder-only path may load canonical manifest and plan configuration."""
        if type(workspace_id) is not UUID or type(candidate_id) is not UUID:
            raise ValueError("workspace_id/candidate_id phải là UUID.")
        await self._bind_workspace(workspace_id)
        record = await self._session.scalar(
            select(LearningSplitCandidateRecord).where(
                LearningSplitCandidateRecord.workspace_id == workspace_id,
                LearningSplitCandidateRecord.id == candidate_id,
            )
        )
        return record.to_domain() if record is not None else None

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )


def _review(row: Any) -> LearningSplitCandidateReview:
    return LearningSplitCandidateReview(
        candidate_id=row.id,
        trial_record_id=row.trial_record_id,
        proof_hash=row.proof_hash,
        manifest_hash=row.manifest_hash,
        split_policy_version=row.split_policy_version,
        fold_plan_hash=row.fold_plan_hash,
        evaluation_cutoff=row.evaluation_cutoff,
        created_at=row.created_at,
        preview=dict(row.preview),
        approved_at=row.approved_at,
    )


__all__ = [
    "LearningSplitCandidateConflict",
    "LearningSplitCandidateRepository",
    "LearningSplitCandidateReview",
    "LearningSplitCandidateWriteResult",
]

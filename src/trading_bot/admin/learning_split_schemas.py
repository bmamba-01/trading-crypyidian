"""Schemas metadata-only cho review và approval split WP06."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from trading_bot.adapters.database.repositories.learning_split_candidates import (
    LearningSplitCandidateReview,
)
from trading_bot.domain.learning.approval import LearningSplitApproval


class ApproveLearningSplitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: StrictStr = Field(min_length=12, max_length=500)


class LearningSplitCandidateResponse(BaseModel):
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

    @classmethod
    def from_domain(cls, candidate: LearningSplitCandidateReview) -> LearningSplitCandidateResponse:
        return cls(
            candidate_id=candidate.candidate_id,
            trial_record_id=candidate.trial_record_id,
            proof_hash=candidate.proof_hash,
            manifest_hash=candidate.manifest_hash,
            split_policy_version=candidate.split_policy_version,
            fold_plan_hash=candidate.fold_plan_hash,
            evaluation_cutoff=candidate.evaluation_cutoff,
            created_at=candidate.created_at,
            preview=candidate.preview,
            approved_at=candidate.approved_at,
        )


class LearningSplitCandidateListResponse(BaseModel):
    candidates: list[LearningSplitCandidateResponse]


class LearningSplitApprovalResponse(BaseModel):
    candidate_id: UUID
    manifest_hash: str
    split_policy_version: str
    fold_plan_hash: str
    approval_hash: str
    approved_at: datetime

    @classmethod
    def from_domain(
        cls, candidate_id: UUID, approval: LearningSplitApproval
    ) -> LearningSplitApprovalResponse:
        return cls(
            candidate_id=candidate_id,
            manifest_hash=approval.manifest_hash,
            split_policy_version=approval.split_policy_version,
            fold_plan_hash=approval.fold_plan_hash,
            approval_hash=approval.content_hash,
            approved_at=approval.approved_at,
        )


__all__ = [
    "ApproveLearningSplitRequest",
    "LearningSplitApprovalResponse",
    "LearningSplitCandidateListResponse",
    "LearningSplitCandidateResponse",
]

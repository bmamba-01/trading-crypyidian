"""Projection metadata giới hạn cho trang quan sát WP06."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import String, cast, func, literal, select, union_all
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from trading_bot.adapters.database.models.learning_shadow_drift_entities import (
    LearningDriftAssessmentRecord,
    LearningRetrainProposalRecord,
    LearningShadowObservationRecord,
)

ObservationKind = Literal["observation", "assessment", "proposal"]
_METADATA_KEYS: dict[ObservationKind, frozenset[str]] = {
    "observation": frozenset(
        {
            "challenger_registry_id",
            "challenger_revision",
            "challenger_reason",
            "incumbent_status",
            "snapshot_coverage",
            "snapshot_quality_ok",
            "schema_compatible",
            "rights_eligible",
            "active_pointer_version",
        }
    ),
    "assessment": frozenset(
        {
            "cohort_registry_id",
            "cohort_revision",
            "member_count",
            "minimum_observations",
            "eligible_count",
            "snapshot_coverage_rate",
            "quality_rate",
            "abstention_rate",
            "data_quality_status",
            "performance_status",
            "reason_codes",
        }
    ),
    "proposal": frozenset({"assessment_id", "policy_version", "reason_codes", "membership_digest"}),
}


@dataclass(frozen=True, slots=True)
class LearningObservationAdminItem:
    """Một dòng read-only chỉ gồm trường metadata đã cho phép."""

    kind: ObservationKind
    record_id: UUID
    occurred_at: datetime
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.kind not in _METADATA_KEYS:
            raise ValueError("kind không được phép.")
        if type(self.record_id) is not UUID or self.record_id.int == 0:
            raise ValueError("record_id phải là UUID khác zero.")
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise ValueError("occurred_at phải có timezone.")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata phải là mapping.")
        if not set(self.metadata) <= _METADATA_KEYS[self.kind]:
            raise ValueError("metadata chứa trường không được phép.")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))
        object.__setattr__(self, "metadata", dict(self.metadata))


class LearningObservationsAdminRepository:
    """Truy vấn feed kết hợp, workspace-scoped và giới hạn tối đa 50 hàng."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def recent_statement(*, workspace_id: UUID, limit: int = 50) -> Select[Any]:
        if type(workspace_id) is not UUID or workspace_id.int == 0:
            raise ValueError("workspace_id phải là UUID khác zero.")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit phải là số nguyên từ 1 đến 50.")

        observation = LearningShadowObservationRecord
        assessment = LearningDriftAssessmentRecord
        proposal = LearningRetrainProposalRecord

        observation_rows = select(
            literal("observation", type_=String()).label("kind"),
            observation.id.label("record_id"),
            observation.observed_at.label("occurred_at"),
            cast(
                func.jsonb_build_object(
                    "challenger_registry_id",
                    observation.challenger_registry_id,
                    "challenger_revision",
                    observation.challenger_revision,
                    "challenger_reason",
                    observation.challenger_reason,
                    "incumbent_status",
                    observation.incumbent_status,
                    "snapshot_coverage",
                    observation.snapshot_coverage,
                    "snapshot_quality_ok",
                    observation.snapshot_quality_ok,
                    "schema_compatible",
                    observation.schema_compatible,
                    "rights_eligible",
                    observation.rights_eligible,
                    "active_pointer_version",
                    observation.active_pointer_version,
                ),
                JSONB,
            ).label("metadata"),
        ).where(observation.workspace_id == workspace_id)
        assessment_rows = select(
            literal("assessment", type_=String()).label("kind"),
            assessment.id.label("record_id"),
            assessment.created_at.label("occurred_at"),
            cast(
                func.jsonb_build_object(
                    "cohort_registry_id",
                    assessment.cohort_registry_id,
                    "cohort_revision",
                    assessment.cohort_revision,
                    "member_count",
                    assessment.member_count,
                    "minimum_observations",
                    assessment.minimum_observations,
                    "eligible_count",
                    assessment.eligible_count,
                    "snapshot_coverage_rate",
                    assessment.snapshot_coverage_rate,
                    "quality_rate",
                    assessment.quality_rate,
                    "abstention_rate",
                    assessment.abstention_rate,
                    "data_quality_status",
                    assessment.data_quality_status,
                    "performance_status",
                    assessment.performance_status,
                    "reason_codes",
                    assessment.reason_codes,
                ),
                JSONB,
            ).label("metadata"),
        ).where(assessment.workspace_id == workspace_id)
        proposal_rows = select(
            literal("proposal", type_=String()).label("kind"),
            proposal.id.label("record_id"),
            proposal.created_at.label("occurred_at"),
            cast(
                func.jsonb_build_object(
                    "assessment_id",
                    proposal.assessment_id,
                    "policy_version",
                    proposal.policy_version,
                    "reason_codes",
                    proposal.reason_codes,
                    "membership_digest",
                    proposal.membership_digest,
                ),
                JSONB,
            ).label("metadata"),
        ).where(proposal.workspace_id == workspace_id)

        feed = union_all(observation_rows, assessment_rows, proposal_rows).subquery(
            "learning_observations_admin_feed"
        )
        return (
            select(feed.c.kind, feed.c.record_id, feed.c.occurred_at, feed.c.metadata)
            .order_by(feed.c.occurred_at.desc(), feed.c.record_id.desc(), feed.c.kind.asc())
            .limit(limit)
        )

    async def list_recent(
        self, *, workspace_id: UUID, limit: int = 50
    ) -> tuple[LearningObservationAdminItem, ...]:
        rows = (
            await self._session.execute(
                self.recent_statement(workspace_id=workspace_id, limit=limit)
            )
        ).mappings()
        return tuple(
            LearningObservationAdminItem(
                kind=row["kind"],
                record_id=row["record_id"],
                occurred_at=row["occurred_at"],
                metadata=row["metadata"],
            )
            for row in rows
        )


__all__ = [
    "LearningObservationAdminItem",
    "LearningObservationsAdminRepository",
    "ObservationKind",
]

"""Projection metadata giới hạn cho trang quan sát WP06."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

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
            "policy_version",
            "observation_window_days",
            "window_start",
            "window_end",
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
    """Đọc feed metadata qua cổng DB đã kiểm lại quyền nguồn hiện hành."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def recent_statement(*, workspace_id: UUID, limit: int = 50) -> TextClause:
        if type(workspace_id) is not UUID or workspace_id.int == 0:
            raise ValueError("workspace_id phải là UUID khác zero.")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit phải là số nguyên từ 1 đến 50.")
        return text("""
            SELECT kind, record_id, occurred_at, metadata
              FROM public.wp06_read_learning_observations_admin_feed(
                    CAST(:workspace_id AS uuid), :limit)
        """).bindparams(workspace_id=workspace_id, limit=limit)

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

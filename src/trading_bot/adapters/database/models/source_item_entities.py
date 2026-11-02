"""Database models cho revision và takedown của research item."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey
from trading_bot.domain.research.source_item_history import SourceItemRevisionEntry
from trading_bot.domain.research.source_items import SourceItemRevision

_JSON_TYPE = JSON().with_variant(JSONB(astext_type=Text()), "postgresql")


def _format_timestamp(value: datetime | None) -> str | None:
    """Chuẩn hóa timestamp sang chuỗi ISO 8601 múi giờ UTC."""
    return value.astimezone(UTC).isoformat() if value is not None else None


class SourceItemRevisionRecord(UuidPrimaryKey, CreatedAt, Base):
    """Bản ghi revision append-only của item nguồn kèm hash chain và metadata."""

    __tablename__ = "research_source_item_revisions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "source_id",
            "item_id",
            "revision_id",
            name="uq_research_source_item_revisions_workspace_item_revision",
        ),
        UniqueConstraint(
            "workspace_id",
            "source_id",
            "item_id",
            "sequence",
            name="uq_research_source_item_revisions_workspace_item_sequence",
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        CheckConstraint("sequence >= 1", name="source_item_revision_sequence_positive"),
        CheckConstraint(
            "source_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="source_item_revision_source_id_valid",
        ),
        CheckConstraint(
            "item_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="source_item_revision_item_id_valid",
        ),
        CheckConstraint(
            "revision_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="source_item_revision_revision_id_valid",
        ),
        CheckConstraint(
            "length(policy_id) BETWEEN 1 AND 160",
            name="source_item_revision_policy_id_valid",
        ),
        CheckConstraint(
            "revision_hash ~ '^[0-9a-f]{64}$'",
            name="source_item_revision_hash_valid",
        ),
        CheckConstraint(
            "(predecessor_revision_id IS NULL AND predecessor_hash IS NULL) OR "
            "(predecessor_revision_id IS NOT NULL AND predecessor_hash IS NOT NULL "
            "AND predecessor_hash ~ '^[0-9a-f]{64}$' "
            "AND predecessor_revision_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')",
            name="source_item_revision_predecessor_valid",
        ),
        CheckConstraint(
            "available_at IS NULL OR available_at >= observed_at",
            name="source_item_revision_available_not_before_observed",
        ),
        CheckConstraint(
            "published_at IS NULL OR published_at <= observed_at",
            name="source_item_revision_published_not_after_observed",
        ),
        CheckConstraint(
            "edited_at IS NULL OR edited_at <= observed_at",
            name="source_item_revision_edited_not_after_observed",
        ),
        CheckConstraint(
            "published_at IS NULL OR edited_at IS NULL OR edited_at >= published_at",
            name="source_item_revision_edited_not_before_published",
        ),
        CheckConstraint(
            "canonical_url LIKE 'https://%' AND position('#' in canonical_url) = 0",
            name="source_item_revision_canonical_url_valid",
        ),
        Index(
            "ix_research_source_item_revisions_lookup",
            "workspace_id",
            "source_id",
            "item_id",
            "sequence",
        ),
        Index(
            "ix_research_source_item_revisions_available",
            "workspace_id",
            "source_id",
            "item_id",
            "available_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_id: Mapped[str] = mapped_column(String(160), nullable=False)
    predecessor_revision_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    predecessor_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_payload: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_tombstone: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    @property
    def tombstoned(self) -> bool:
        """Thuộc tính tương thích với trường tombstoned của domain SourceItemRevision."""
        return self.is_tombstone

    @classmethod
    def from_domain(
        cls,
        workspace_id: UUID,
        entry: SourceItemRevisionEntry,
        *,
        policy_id: str,
        sequence: int,
    ) -> SourceItemRevisionRecord:
        """Tạo bản ghi ORM từ SourceItemRevisionEntry và metadata ingestion."""
        if type(entry) is not SourceItemRevisionEntry:
            raise ValueError("entry phải là SourceItemRevisionEntry.")
        rev = entry.revision
        payload: dict[str, Any] = {
            "source_id": rev.source_id,
            "item_id": rev.item_id,
            "revision_id": rev.revision_id,
            "canonical_url": rev.canonical_url,
            "observed_at": _format_timestamp(rev.observed_at),
            "available_at": _format_timestamp(rev.available_at),
            "event_at": _format_timestamp(rev.event_at),
            "published_at": _format_timestamp(rev.published_at),
            "edited_at": _format_timestamp(rev.edited_at),
            "tombstoned": rev.tombstoned,
            "predecessor_hash": entry.predecessor_hash,
        }
        return cls(
            workspace_id=workspace_id,
            source_id=rev.source_id,
            item_id=rev.item_id,
            revision_id=rev.revision_id,
            sequence=sequence,
            policy_id=policy_id,
            predecessor_revision_id=entry.predecessor_revision_id,
            predecessor_hash=entry.predecessor_hash,
            revision_hash=entry.revision_hash,
            canonical_url=rev.canonical_url,
            canonical_payload=payload,
            observed_at=rev.observed_at,
            available_at=rev.available_at,
            event_at=rev.event_at,
            published_at=rev.published_at,
            edited_at=rev.edited_at,
            is_tombstone=rev.tombstoned,
        )

    def to_domain(self) -> SourceItemRevisionEntry:
        """Tái dựng SourceItemRevisionEntry và xác thực tính toàn vẹn so với payload."""
        payload = self.canonical_payload
        if (
            payload.get("source_id") != self.source_id
            or payload.get("item_id") != self.item_id
            or payload.get("revision_id") != self.revision_id
            or payload.get("canonical_url") != self.canonical_url
            or payload.get("predecessor_hash") != self.predecessor_hash
            or payload.get("tombstoned") != self.is_tombstone
            or payload.get("observed_at") != _format_timestamp(self.observed_at)
            or payload.get("available_at") != _format_timestamp(self.available_at)
            or payload.get("event_at") != _format_timestamp(self.event_at)
            or payload.get("published_at") != _format_timestamp(self.published_at)
            or payload.get("edited_at") != _format_timestamp(self.edited_at)
        ):
            raise ValueError("canonical_payload không khớp với các cột chỉ mục của revision.")

        revision = SourceItemRevision(
            source_id=self.source_id,
            item_id=self.item_id,
            revision_id=self.revision_id,
            canonical_url=self.canonical_url,
            observed_at=self.observed_at,
            available_at=self.available_at,
            event_at=self.event_at,
            published_at=self.published_at,
            edited_at=self.edited_at,
            tombstoned=self.is_tombstone,
        )
        return SourceItemRevisionEntry(
            revision=revision,
            predecessor_revision_id=self.predecessor_revision_id,
            predecessor_hash=self.predecessor_hash,
            revision_hash=self.revision_hash,
        )


class SourceTakedownRecord(UuidPrimaryKey, CreatedAt, Base):
    """Bản ghi yêu cầu takedown cho item nguồn cụ thể."""

    __tablename__ = "research_source_takedowns"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        CheckConstraint(
            "source_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="source_takedown_source_id_valid",
        ),
        CheckConstraint(
            "item_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="source_takedown_item_id_valid",
        ),
        CheckConstraint("length(reason) > 0", name="source_takedown_reason_present"),
        CheckConstraint("length(requested_by) > 0", name="source_takedown_requested_by_present"),
        CheckConstraint(
            "effective_at >= requested_at",
            name="source_takedown_effective_not_before_requested",
        ),
        Index(
            "uq_research_source_takedowns_active",
            "workspace_id",
            "source_id",
            "item_id",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
        Index(
            "ix_research_source_takedowns_lookup",
            "workspace_id",
            "source_id",
            "item_id",
            "is_active",
            "effective_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

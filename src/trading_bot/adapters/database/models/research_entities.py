"""Bảng registry quyền nguồn research và reservation quota theo workspace."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey

if TYPE_CHECKING:
    from trading_bot.domain.research.source_item_history import SourceItemRevisionEntry


def _python_timestamp_constraint(field: str, *, nullable: bool, prefix: str) -> CheckConstraint:
    """Chặn timestamp PostgreSQL hữu hạn nhưng ngoài miền datetime UTC của Python."""
    bounded = (
        f"isfinite({field}) AND {field} >= TIMESTAMPTZ '0001-01-01 00:00:00+00' "
        f"AND {field} < TIMESTAMPTZ '10000-01-01 00:00:00+00'"
    )
    return CheckConstraint(
        f"{field} IS NULL OR ({bounded})" if nullable else bounded,
        name=f"{prefix}_{field}_pytime",
    )


class SourcePolicyRevisionRecord(UuidPrimaryKey, CreatedAt, Base):
    """Snapshot append-only của policy, quyền, endpoint và quota nguồn."""

    __tablename__ = "source_policy_revision"
    __table_args__ = (
        UniqueConstraint("workspace_id", "policy_id", name="uq_source_policy_revision_policy"),
        UniqueConstraint(
            "workspace_id",
            "source_id",
            "sequence",
            name="uq_source_policy_revision_workspace_source_sequence",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_source_policy_revision_workspace_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        CheckConstraint("sequence >= 1", name="source_policy_sequence_positive"),
        CheckConstraint(
            "source_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="source_policy_source_id_valid",
        ),
        CheckConstraint("length(policy_id) BETWEEN 1 AND 160", name="source_policy_id_valid"),
        CheckConstraint(
            "state IN ('discovered', 'rights_pending', 'verified', 'blocked', "
            "'revoked', 'expired')",
            name="source_policy_state_valid",
        ),
        CheckConstraint(
            "access_method ~ '^[A-Z][A-Z0-9_]{0,31}$'",
            name="source_policy_access_method_valid",
        ),
        CheckConstraint("quota_window_seconds > 0", name="source_quota_window_positive"),
        CheckConstraint("quota_max_requests > 0", name="source_quota_requests_positive"),
        CheckConstraint("quota_max_bytes > 0", name="source_quota_bytes_positive"),
        CheckConstraint(
            "quota_max_cost >= 0 AND quota_max_cost <> 'NaN'::numeric",
            name="source_quota_cost_nonnegative",
        ),
        CheckConstraint(
            "valid_until IS NULL OR reviewed_at IS NULL OR valid_until > reviewed_at",
            name="source_policy_validity_ordered",
        ),
        CheckConstraint(
            "jsonb_typeof(allowed_purposes) = 'array' "
            'AND allowed_purposes <@ \'["metadata","display","embedding",'
            '"training","redistribution"]\'::jsonb',
            name="source_policy_purposes_valid",
        ),
        CheckConstraint(
            "state <> 'verified' OR (jsonb_typeof(allowed_purposes) = 'array' "
            "AND jsonb_array_length(allowed_purposes) > 0 "
            "AND evidence_ref IS NOT NULL AND reviewed_at IS NOT NULL AND approved_by IS NOT NULL "
            "AND approved_by <> actor_id)",
            name="source_policy_verified_has_approval",
        ),
        CheckConstraint(
            "state = 'verified' OR approved_by IS NULL",
            name="source_policy_approval_state_consistent",
        ),
        CheckConstraint(
            "reviewed_at <= created_at OR reviewed_at IS NULL",
            name="source_policy_review_not_after_creation",
        ),
        CheckConstraint(
            "endpoint_url LIKE 'https://%' AND position('#' in endpoint_url) = 0 "
            "AND split_part(split_part(endpoint_url, '/', 3), '@', 1) "
            "= split_part(split_part(endpoint_url, '/', 3), '/', 1)",
            name="source_policy_endpoint_https_no_credentials",
        ),
        CheckConstraint("length(publisher) > 0", name="source_policy_publisher_present"),
        CheckConstraint("length(auth_scope) > 0", name="source_policy_auth_scope_present"),
        CheckConstraint("length(retention_ref) > 0", name="source_policy_retention_present"),
        CheckConstraint("length(quota_cost_unit) > 0", name="source_quota_cost_unit_present"),
        Index("ix_source_policy_revision_latest", "workspace_id", "source_id", "sequence"),
        Index("ix_source_policy_revision_validity", "workspace_id", "state", "valid_until"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_id: Mapped[str] = mapped_column(String(160), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    allowed_purposes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publisher: Mapped[str] = mapped_column(String(240), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    access_method: Mapped[str] = mapped_column(String(32), nullable=False)
    auth_scope: Mapped[str] = mapped_column(String(240), nullable=False)
    attribution: Mapped[str] = mapped_column(Text, nullable=False)
    retention_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    quota_window_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quota_max_requests: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quota_max_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quota_max_cost: Mapped[Decimal] = mapped_column(Numeric(384, 128), nullable=False)
    quota_cost_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    @classmethod
    def from_domain(cls, workspace_id: UUID, revision: object) -> SourcePolicyRevisionRecord:
        """Chuyển snapshot domain sang ORM; không nhận hoặc lưu credential."""
        from trading_bot.domain.research.source_registry import SourcePolicyRevision

        if not isinstance(revision, SourcePolicyRevision):
            raise ValueError("revision phải là SourcePolicyRevision.")
        policy = revision.policy
        return cls(
            workspace_id=workspace_id,
            source_id=policy.source_id,
            sequence=revision.sequence,
            policy_id=policy.policy_id,
            state=policy.state.value,
            allowed_purposes=sorted(purpose.value for purpose in policy.allowed_purposes),
            evidence_ref=policy.evidence_ref,
            reviewed_at=policy.reviewed_at,
            valid_until=policy.valid_until,
            publisher=revision.publisher,
            endpoint_url=revision.endpoint_url,
            access_method=revision.access_method,
            auth_scope=revision.auth_scope,
            attribution=revision.attribution,
            retention_ref=revision.retention_ref,
            quota_window_seconds=int(revision.budget.window.total_seconds()),
            quota_max_requests=revision.budget.max_requests,
            quota_max_bytes=revision.budget.max_bytes,
            quota_max_cost=Decimal(revision.budget.max_cost),
            quota_cost_unit=revision.budget.cost_unit,
            actor_id=revision.actor_id,
            reason=revision.reason,
            approved_by=revision.approved_by,
            created_at=revision.created_at,
        )


class SourceQuotaEventRecord(UuidPrimaryKey, CreatedAt, Base):
    """Reservation quota append-only trước khi connector gọi nguồn ngoài."""

    __tablename__ = "source_quota_event"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "source_id", "idempotency_key", name="uq_source_quota_idempotency"
        ),
        UniqueConstraint("workspace_id", "id", name="uq_source_quota_workspace_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "policy_sequence"],
            [
                "source_policy_revision.workspace_id",
                "source_policy_revision.source_id",
                "source_policy_revision.sequence",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("length(idempotency_key) BETWEEN 1 AND 240", name="source_quota_key_valid"),
        CheckConstraint(
            "purpose IN ('metadata', 'display', 'embedding', 'training', 'redistribution')",
            name="source_quota_purpose_valid",
        ),
        CheckConstraint("window_started_at <= occurred_at", name="source_quota_window_ordered"),
        CheckConstraint("reserved_requests > 0", name="source_quota_reserved_requests_positive"),
        CheckConstraint("reserved_bytes >= 0", name="source_quota_reserved_bytes_nonnegative"),
        CheckConstraint(
            "reserved_cost >= 0 AND reserved_cost <> 'NaN'::numeric",
            name="source_quota_reserved_cost_nonnegative",
        ),
        CheckConstraint("length(cost_unit) > 0", name="source_quota_cost_unit_present"),
        Index(
            "ix_source_quota_window",
            "workspace_id",
            "source_id",
            "window_started_at",
            "occurred_at",
        ),
        Index("ix_source_quota_policy", "workspace_id", "source_id", "policy_sequence"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reserved_requests: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_cost: Mapped[Decimal] = mapped_column(Numeric(384, 128), nullable=False)
    cost_unit: Mapped[str] = mapped_column(String(32), nullable=False)


class SourceItemRevisionRecord(UuidPrimaryKey, CreatedAt, Base):
    """Revision metadata bất biến; không có cột body, excerpt, embedding hoặc blob."""

    __tablename__ = "source_item_revision"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "source_id",
            "item_id",
            "revision_id",
            name="uq_source_item_revision_identity",
        ),
        UniqueConstraint(
            "workspace_id",
            "source_id",
            "item_id",
            "sequence",
            name="uq_source_item_revision_sequence",
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        CheckConstraint("sequence >= 1", name="source_item_sequence_positive"),
        CheckConstraint(
            "source_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "item_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "revision_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="source_item_identifiers_valid",
        ),
        CheckConstraint(
            "revision_hash ~ '^[0-9a-f]{64}$' AND "
            "(predecessor_hash IS NULL OR predecessor_hash ~ '^[0-9a-f]{64}$')",
            name="source_item_hashes_valid",
        ),
        CheckConstraint(
            "(sequence = 1 AND predecessor_revision_id IS NULL AND predecessor_hash IS NULL) OR "
            "(sequence > 1 AND predecessor_revision_id IS NOT NULL "
            "AND predecessor_hash IS NOT NULL)",
            name="source_item_predecessor_present",
        ),
        CheckConstraint(
            "(available_at IS NULL OR available_at >= observed_at) AND "
            "(published_at IS NULL OR published_at <= observed_at) AND "
            "(edited_at IS NULL OR edited_at <= observed_at) AND "
            "(published_at IS NULL OR edited_at IS NULL OR published_at <= edited_at)",
            name="source_item_timestamps_causal",
        ),
        CheckConstraint(
            "canonical_url LIKE 'https://%' AND position('#' in canonical_url) = 0 "
            "AND canonical_url !~ '[[:space:]]' "
            "AND position('@' in split_part(canonical_url, '/', 3)) = 0",
            name="source_item_url_metadata",
        ),
        CheckConstraint(
            "public.source_item_url_valid(canonical_url)", name="source_item_url_domain_valid"
        ),
        *(
            _python_timestamp_constraint(field, nullable=nullable, prefix="sir")
            for field, nullable in (
                ("recorded_at", False),
                ("created_at", False),
                ("observed_at", False),
                ("available_at", True),
                ("event_at", True),
                ("published_at", True),
                ("edited_at", True),
                ("retention_expires_at", True),
            )
        ),
        Index(
            "ix_source_item_revision_available",
            "workspace_id",
            "source_id",
            "item_id",
            "available_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tombstoned: Mapped[bool] = mapped_column(Boolean, nullable=False)
    predecessor_revision_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    predecessor_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    retention_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @classmethod
    def from_entry(
        cls,
        workspace_id: UUID,
        sequence: int,
        entry: SourceItemRevisionEntry,
        *,
        retention_expires_at: datetime | None = None,
    ) -> SourceItemRevisionRecord:
        """Ánh xạ đúng hợp đồng hash metadata, giữ retention ngoài hash domain."""
        revision = entry.revision
        return cls(
            workspace_id=workspace_id,
            sequence=sequence,
            source_id=revision.source_id,
            item_id=revision.item_id,
            revision_id=revision.revision_id,
            canonical_url=revision.canonical_url,
            observed_at=revision.observed_at,
            available_at=revision.available_at,
            event_at=revision.event_at,
            published_at=revision.published_at,
            edited_at=revision.edited_at,
            tombstoned=revision.tombstoned,
            predecessor_revision_id=entry.predecessor_revision_id,
            predecessor_hash=entry.predecessor_hash,
            revision_hash=entry.revision_hash,
            retention_expires_at=retention_expires_at,
        )

    def to_entry(self) -> SourceItemRevisionEntry:
        """Dựng lại và xác minh hash trước khi trả metadata đã lưu."""
        from trading_bot.domain.research.source_item_history import SourceItemRevisionEntry
        from trading_bot.domain.research.source_items import SourceItemRevision

        return SourceItemRevisionEntry(
            revision=SourceItemRevision(
                source_id=self.source_id,
                item_id=self.item_id,
                revision_id=self.revision_id,
                canonical_url=self.canonical_url,
                observed_at=self.observed_at,
                available_at=self.available_at,
                event_at=self.event_at,
                published_at=self.published_at,
                edited_at=self.edited_at,
                tombstoned=self.tombstoned,
            ),
            predecessor_revision_id=self.predecessor_revision_id,
            predecessor_hash=self.predecessor_hash,
            revision_hash=self.revision_hash,
        )


class SourceItemTakedownRecord(UuidPrimaryKey, Base):
    """Lệnh rút metadata hiện hành, không có đường khôi phục qua replay."""

    __tablename__ = "source_item_takedown"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "source_id", "item_id", name="uq_source_item_takedown_identity"
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        CheckConstraint(
            "source_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "item_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="source_item_takedown_identifiers_valid",
        ),
        CheckConstraint(
            "reason_code ~ '^[a-z][a-z0-9_]{0,63}$'", name="source_item_takedown_reason_valid"
        ),
        _python_timestamp_constraint("taken_down_at", nullable=False, prefix="sit"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    taken_down_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)

"""Bảng registry runtime PAPER nhiều bot (MB05.2): control, lease, state và incident activity.

Tách khỏi `bot_entities.py` để module identity/config không phình to. Ràng buộc DB thật
(trigger append-only/fence, RLS, grant) nằm ở migration `d9b2f4a6c8e1_bot_runtime_registry`.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey

_BOT_INSTANCE_SCOPE = (
    "bot_instance.workspace_id",
    "bot_instance.id",
    "bot_instance.trading_account_id",
)
_MEMBERSHIP = ("user_membership.workspace_id", "user_membership.id")


def _bot_scope(table: str) -> tuple[ForeignKeyConstraint, Index]:
    return (
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            list(_BOT_INSTANCE_SCOPE),
            name=f"fk_{table}_bot_account",
        ),
        Index(f"ix_{table}_bot_scope", "workspace_id", "bot_id", "account_id"),
    )


class WorkspaceRuntimeControlRecord(UuidPrimaryKey, CreatedAt, Base):
    """Lịch sử kill switch workspace append-only; revision lớn nhất là trạng thái hiện hành."""

    __tablename__ = "workspace_runtime_control"
    __table_args__ = (
        UniqueConstraint("workspace_id", "revision", name="uq_workspace_runtime_control_revision"),
        ForeignKeyConstraint(
            ["workspace_id"], ["workspace.id"], name="fk_workspace_runtime_control_workspace"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "changed_by"],
            list(_MEMBERSHIP),
            name="fk_workspace_runtime_control_changed_by",
        ),
        CheckConstraint("kill_state IN ('ACTIVE', 'KILLED')", name="kill_state_valid"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            "length(trim(reason_code)) > 0 AND length(trim(reason_text)) > 0",
            name="reason_nonempty",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    kill_state: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_text: Mapped[str] = mapped_column(String(500), nullable=False)
    changed_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class BotRuntimeControlRecord(UuidPrimaryKey, CreatedAt, Base):
    """Lịch sử pause bot, append-only theo revision; không chạm trạng thái account/bot."""

    __tablename__ = "bot_runtime_control"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "bot_id", "revision", name="uq_bot_runtime_control_revision"
        ),
        *_bot_scope("bot_runtime_control"),
        ForeignKeyConstraint(
            ["workspace_id", "changed_by"],
            list(_MEMBERSHIP),
            name="fk_bot_runtime_control_changed_by",
        ),
        CheckConstraint("entry_state IN ('ENABLED', 'PAUSED')", name="entry_state_valid"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            "length(trim(reason_code)) > 0 AND length(trim(reason_text)) > 0",
            name="reason_nonempty",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_state: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_text: Mapped[str] = mapped_column(String(500), nullable=False)
    changed_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class BotRuntimeLeaseRecord(UuidPrimaryKey, CreatedAt, Base):
    """Lease một writer cho mỗi bot và mỗi account; thời điểm do trigger đặt theo đồng hồ DB."""

    __tablename__ = "bot_runtime_lease"
    __table_args__ = (
        UniqueConstraint("workspace_id", "bot_id", name="uq_bot_runtime_lease_bot"),
        UniqueConstraint("workspace_id", "account_id", name="uq_bot_runtime_lease_account"),
        *_bot_scope("bot_runtime_lease"),
        CheckConstraint("fencing_token >= 1", name="token_positive"),
        CheckConstraint("length(trim(owner_id)) > 0", name="owner_nonempty"),
        CheckConstraint("expires_at >= renewed_at", name="expiry_after_renewal"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(120), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    renewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BotRuntimeStateRecord(UuidPrimaryKey, CreatedAt, Base):
    """Trạng thái runtime theo bot do process giữ lease ghi (fence trigger bảo vệ)."""

    __tablename__ = "bot_runtime_state"
    __table_args__ = (
        UniqueConstraint("workspace_id", "bot_id", name="uq_bot_runtime_state_bot"),
        *_bot_scope("bot_runtime_state"),
        CheckConstraint(
            "readiness IN ('STARTING', 'READY', 'RECOVERY_REQUIRED', 'STANDBY')",
            name="readiness_valid",
        ),
        CheckConstraint(
            "activity_status IS NULL OR activity_status IN ('VERIFIED', 'UNVERIFIED')",
            name="activity_status_valid",
        ),
        CheckConstraint(
            "applied_bot_control_revision >= 0 AND applied_workspace_control_revision >= 0",
            name="applied_revisions_valid",
        ),
        CheckConstraint("fencing_token >= 1", name="token_positive"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    readiness: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    applied_bot_control_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    applied_workspace_control_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_checkpoint_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    activity_cursor: Mapped[str | None] = mapped_column(String(240), nullable=True)
    activity_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    activity_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fanout_exclusion_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )


INCIDENT_RESOLUTION_CODES = ("EXTERNAL_STATE_REMOVED_BY_OPERATOR", "FALSE_POSITIVE_CONFIRMED")


class AccountActivityIncidentRecord(UuidPrimaryKey, CreatedAt, Base):
    """Incident activity ngoại lai; cột phát hiện bất biến, chỉ OPEN -> RESOLVED có actor/lý do."""

    __tablename__ = "account_activity_incident"
    __table_args__ = (
        *_bot_scope("account_activity_incident"),
        ForeignKeyConstraint(
            ["workspace_id", "resolved_by"],
            list(_MEMBERSHIP),
            name="fk_account_activity_incident_resolved_by",
        ),
        Index(
            "uq_account_activity_incident_open",
            "workspace_id",
            "bot_id",
            "fingerprint",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
        CheckConstraint("status IN ('OPEN', 'RESOLVED')", name="status_valid"),
        CheckConstraint("source IN ('PAPER_LEDGER', 'SCRIPTED', 'VENUE')", name="source_valid"),
        CheckConstraint(
            "length(snapshot_hash) = 64 AND length(fingerprint) = 64", name="hashes_sha256"
        ),
        CheckConstraint(
            "jsonb_typeof(foreign_orders) = 'array' AND jsonb_typeof(foreign_positions) = 'array'",
            name="foreign_state_arrays",
        ),
        CheckConstraint("fencing_token >= 1", name="token_positive"),
        CheckConstraint(
            "resolution_code IS NULL OR resolution_code IN "
            "('EXTERNAL_STATE_REMOVED_BY_OPERATOR', 'FALSE_POSITIVE_CONFIRMED')",
            name="resolution_code_valid",
        ),
        CheckConstraint(
            "(status = 'OPEN' AND resolution_code IS NULL AND resolution_reason IS NULL "
            "AND resolved_by IS NULL AND resolved_at IS NULL) OR (status = 'RESOLVED' "
            "AND resolution_code IS NOT NULL AND length(trim(resolution_reason)) > 0 "
            "AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)",
            name="resolution_consistent",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cursor: Mapped[str | None] = mapped_column(String(240), nullable=True)
    foreign_orders: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    foreign_positions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resolution_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    resolved_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

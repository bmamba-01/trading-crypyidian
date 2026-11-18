"""Bản ghi trạng thái Circuit Breaker append-only theo workspace/bot (OPS07B)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
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


class CircuitBreakerEventRecord(UuidPrimaryKey, CreatedAt, Base):
    """Lịch sử chuyển trạng thái Circuit Breaker (append-only, không UPDATE/DELETE)."""

    __tablename__ = "circuit_breaker_event"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "scope_key",
            "revision",
            name="uq_circuit_breaker_event_scope_revision",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            ondelete="CASCADE",
            name="fk_circuit_breaker_event_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id"],
            ["bot_instance.workspace_id", "bot_instance.id"],
            ondelete="CASCADE",
            name="fk_circuit_breaker_event_bot",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "resumed_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="RESTRICT",
            name="fk_circuit_breaker_event_resumed_by",
        ),
        CheckConstraint(
            "state IN ('CLOSED', 'OPEN', 'COOLDOWN_PASSED')",
            name="circuit_breaker_event_state_valid",
        ),
        CheckConstraint("revision >= 1", name="circuit_breaker_event_revision_positive"),
        CheckConstraint(
            "length(trim(scope_key)) > 0 AND length(trim(policy_version)) > 0 "
            "AND length(trim(detail_vi)) > 0",
            name="circuit_breaker_event_nonempty",
        ),
        CheckConstraint(
            "(bot_id IS NULL AND scope_key = 'workspace') OR "
            "(bot_id IS NOT NULL AND scope_key = 'bot:' || bot_id::text)",
            name="circuit_breaker_event_scope_consistent",
        ),
        Index(
            "ix_circuit_breaker_event_latest",
            "workspace_id",
            "scope_key",
            "revision",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    scope_key: Mapped[str] = mapped_column(String(80), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    tripped_reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    detail_vi: Mapped[str] = mapped_column(String(500), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    tripped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    resumed_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resume_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

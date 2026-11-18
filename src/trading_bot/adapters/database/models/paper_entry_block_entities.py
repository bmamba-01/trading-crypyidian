"""Sổ cái chặn entry PAPER theo từng BotDecision do operator/risk-admin thực hiện (CRY-440)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class PaperEntryOperatorBlockRecord(UuidPrimaryKey, CreatedAt, Base):
    """Bản ghi bất biến chặn một BotDecision APPROVE trước khi tạo OrderIntent PAPER."""

    __tablename__ = "paper_entry_operator_block"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "bot_decision_id",
            name="uq_paper_entry_operator_block_decision",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "bot_decision_id"],
            ["bot_decision.workspace_id", "bot_decision.bot_id", "bot_decision.id"],
            ondelete="CASCADE",
            name="fk_paper_entry_operator_block_decision",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "blocked_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="RESTRICT",
            name="fk_paper_entry_operator_block_actor",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0",
            name="paper_entry_operator_block_reason_nonempty",
        ),
        Index(
            "ix_paper_entry_operator_block_workspace_created",
            "workspace_id",
            "created_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    blocked_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    mfa_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

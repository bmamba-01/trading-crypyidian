"""Mô hình SQLAlchemy lưu mã khôi phục MFA dùng một lần đã băm có salt (SEC-14)."""

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


class MfaRecoveryCode(UuidPrimaryKey, CreatedAt, Base):
    """Mã khôi phục MFA một lần (chỉ lưu HMAC-SHA256 kèm salt, tuyệt đối không lưu plaintext)."""

    __tablename__ = "mfa_recovery_code"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_mfa_recovery_code_workspace_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="CASCADE",
            name="fk_mfa_recovery_code_membership",
        ),
        CheckConstraint(
            "length(code_hash) = 64 AND code_hash = lower(code_hash) AND code_hash !~ '[^0-9a-f]'",
            name="mfa_recovery_code_hash_sha256",
        ),
        CheckConstraint(
            "length(code_salt) >= 16",
            name="mfa_recovery_code_salt_nonempty",
        ),
        CheckConstraint(
            "NOT (used_at IS NOT NULL AND revoked_at IS NOT NULL)",
            name="mfa_recovery_code_terminal_exclusive",
        ),
        Index(
            "ix_mfa_recovery_code_active",
            "workspace_id",
            "membership_id",
            "used_at",
            "revoked_at",
        ),
        Index("ix_mfa_recovery_code_user", "workspace_id", "user_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    batch_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    code_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["MfaRecoveryCode"]

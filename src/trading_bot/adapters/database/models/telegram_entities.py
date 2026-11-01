"""Ledger Telegram của MB-07a/07b."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
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


class TelegramLinkRequest(UuidPrimaryKey, CreatedAt, Base):
    """Digest của mã deep link; mã thô chỉ rời service một lần."""

    __tablename__ = "telegram_link_request"
    __table_args__ = (
        UniqueConstraint("workspace_id", "token_digest", name="uq_telegram_link_digest"),
        UniqueConstraint("workspace_id", "id", name="uq_telegram_link_request_workspace_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_telegram_link_request_membership",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "created_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_telegram_link_request_created_by",
        ),
        CheckConstraint("length(token_digest) = 64", name="telegram_link_digest_sha256"),
        CheckConstraint("expires_at > created_at", name="telegram_link_expiry_valid"),
        Index("ix_telegram_link_request_expiry", "workspace_id", "expires_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelegramBindingChallenge(UuidPrimaryKey, CreatedAt, Base):
    """Nonce một lần, ràng buộc callback với mã liên kết và Telegram user."""

    __tablename__ = "telegram_binding_challenge"
    __table_args__ = (
        UniqueConstraint("workspace_id", "callback_digest", name="uq_telegram_callback_digest"),
        UniqueConstraint("workspace_id", "id", name="uq_telegram_challenge_workspace_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "link_request_id"],
            ["telegram_link_request.workspace_id", "telegram_link_request.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("length(callback_digest) = 64", name="telegram_callback_digest_sha256"),
        CheckConstraint("expires_at > created_at", name="telegram_callback_expiry_valid"),
        Index("ix_telegram_binding_challenge_expiry", "workspace_id", "expires_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    link_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    telegram_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    callback_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelegramUpdateReceipt(UuidPrimaryKey, CreatedAt, Base):
    """`update_id` đã nhận theo workspace: dedup bền vững (SEC-36, ruling 38).

    Không UPDATE; chỉ xóa được hàng quá 7 ngày (policy `receipt_purge`, retention).
    """

    __tablename__ = "telegram_update_receipt"
    __table_args__ = (
        UniqueConstraint("workspace_id", "update_id", name="uq_telegram_update_receipt_update"),
        CheckConstraint("update_id > 0", name="update_id_positive"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        # Retention 7 ngày quét theo thời điểm nhận (fix round 1 minor 3).
        Index("ix_telegram_update_receipt_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

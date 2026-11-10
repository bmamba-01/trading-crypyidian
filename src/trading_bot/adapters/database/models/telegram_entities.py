"""Ledger Telegram của MB-07a/07b."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
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

from trading_bot.adapters.database.models.base import Base, CreatedAt, UpdatedAt, UuidPrimaryKey


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


class TelegramSignalSubscription(UuidPrimaryKey, CreatedAt, UpdatedAt, Base):
    """Opt-in signal Telegram của một thành viên cho một bot cụ thể."""

    __tablename__ = "telegram_signal_subscription"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_telegram_signal_subscription_workspace_id"),
        UniqueConstraint(
            "workspace_id", "bot_id", "id", name="uq_telegram_signal_subscription_workspace_bot_id"
        ),
        UniqueConstraint(
            "workspace_id",
            "membership_id",
            "bot_id",
            name="uq_telegram_signal_subscription_member_bot",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id"],
            ["bot_instance.workspace_id", "bot_instance.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "telegram_binding_id"],
            ["telegram_binding.workspace_id", "telegram_binding.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("telegram_chat_id > 0", name="telegram_signal_chat_id_positive"),
        *(
            CheckConstraint(
                f"jsonb_typeof({name}) = 'array' AND jsonb_array_length({name}) <= 100",
                name=f"telegram_signal_{name}_filter_valid",
            )
            for name in ("symbols", "timeframes", "directions", "strategy_ids", "setup_codes")
        ),
        Index(
            "ix_telegram_signal_subscription_ready",
            "workspace_id",
            "bot_id",
            "enabled",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    telegram_binding_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    symbols: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    timeframes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    directions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    strategy_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    setup_codes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


class TelegramSignalDelivery(UuidPrimaryKey, CreatedAt, Base):
    """Delivery ledger idempotent gắn một candidate decision với một subscription."""

    __tablename__ = "telegram_signal_delivery"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_telegram_signal_delivery_workspace_id"),
        UniqueConstraint(
            "workspace_id",
            "subscription_id",
            "decision_id",
            name="uq_telegram_signal_delivery_decision",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "subscription_id"],
            [
                "telegram_signal_subscription.workspace_id",
                "telegram_signal_subscription.bot_id",
                "telegram_signal_subscription.id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "decision_id"],
            ["bot_decision.workspace_id", "bot_decision.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'SENT', 'EXPIRED', 'DEAD', 'FILTERED')",
            name="telegram_signal_delivery_status_valid",
        ),
        CheckConstraint("attempts >= 0", name="telegram_signal_delivery_attempts_nonnegative"),
        CheckConstraint(
            "(status = 'SENT') = (sent_at IS NOT NULL)",
            name="telegram_signal_delivery_sent_consistent",
        ),
        Index(
            "ix_telegram_signal_delivery_ready",
            "workspace_id",
            "status",
            "available_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subscription_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)

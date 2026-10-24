from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UpdatedAt, UuidPrimaryKey


class Workspace(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "workspace"
    __table_args__ = (UniqueConstraint("slug", name="uq_workspace_slug"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="PAPER")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")


class UserAccount(UuidPrimaryKey, CreatedAt, UpdatedAt, Base):
    __tablename__ = "user_account"
    __table_args__ = (UniqueConstraint("email_normalized", name="uq_user_account_email"),)

    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")


class UserMembership(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "user_membership"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_user_membership_workspace_id"),
        UniqueConstraint("workspace_id", "user_id", name="uq_user_membership_workspace_user"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")


class RoleBinding(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "role_binding"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_role_binding_workspace_id"),
        UniqueConstraint(
            "workspace_id", "membership_id", "role", name="uq_role_binding_membership_role"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)


class Session(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "session"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_session_workspace_id"),
        UniqueConstraint("token_hash", name="uq_session_token_hash"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="CASCADE",
        ),
        Index("ix_session_workspace_expires", "workspace_id", "expires_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    csrf_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mfa_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MfaFactor(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "mfa_factor"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_mfa_factor_workspace_id"),
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    factor_type: Mapped[str] = mapped_column(String(24), nullable=False)
    secret_reference: Mapped[str] = mapped_column(String(240), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_counter: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TelegramBinding(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "telegram_binding"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_telegram_binding_workspace_id"),
        UniqueConstraint("workspace_id", "telegram_user_id", name="uq_telegram_binding_user"),
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    telegram_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TradingAccount(UuidPrimaryKey, CreatedAt, UpdatedAt, Base):
    __tablename__ = "trading_account"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_trading_account_workspace_id"),
        UniqueConstraint("workspace_id", "name", name="uq_trading_account_workspace_name"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        CheckConstraint("max_notional >= 0", name="max_notional_nonnegative"),
        Index("ix_trading_account_workspace_mode_status", "workspace_id", "mode", "status"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(32), nullable=False, default="binance_usdm")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="PAPER")
    environment: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DISABLED")
    max_notional: Mapped[Decimal] = mapped_column(Numeric(38, 18, asdecimal=True), nullable=False)


class PaperAccountSeed(UuidPrimaryKey, CreatedAt, Base):
    """Số dư seed bất biến của một synthetic PAPER account."""

    __tablename__ = "paper_account_seed"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "trading_account_id"],
            ["bot_instance.workspace_id", "bot_instance.id", "bot_instance.trading_account_id"],
            name="fk_paper_account_seed_bot_account",
        ),
        Index("ix_paper_account_seed_bot_scope", "workspace_id", "bot_id", "trading_account_id"),
        UniqueConstraint(
            "workspace_id", "trading_account_id", name="uq_paper_account_seed_workspace_account"
        ),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_paper_account_seed_idempotency"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "trading_account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
            name="fk_paper_account_seed_account",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_settings_version_id"],
            ["workspace_settings_version.workspace_id", "workspace_settings_version.id"],
            name="fk_paper_account_seed_settings",
        ),
        CheckConstraint("initial_equity > 0", name="initial_equity_positive"),
        CheckConstraint(
            "length(source_settings_hash) = 64 "
            "AND source_settings_hash = lower(source_settings_hash) "
            "AND source_settings_hash !~ '[^0-9a-f]'",
            name="source_settings_hash_sha256",
        ),
        CheckConstraint(
            "quote_asset = upper(quote_asset) AND quote_asset ~ '^[A-Z0-9]{2,16}$'",
            name="quote_asset_valid",
        ),
    )

    bot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        server_default=text("NULLIF(current_setting('app.bot_id', true), '')::uuid"),
    )
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trading_account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    initial_equity: Mapped[Decimal] = mapped_column(Numeric(38, 18, asdecimal=True), nullable=False)
    source_settings_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_settings_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)


class SecretReference(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "secret_reference"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_secret_reference_workspace_id"),
        UniqueConstraint(
            "workspace_id", "provider", "reference_key", name="uq_secret_reference_key"
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    reference_key: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")


class ConfigVersion(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "config_version"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "trading_account_id"],
            ["bot_instance.workspace_id", "bot_instance.id", "bot_instance.trading_account_id"],
            name="fk_config_version_bot_account",
        ),
        Index("ix_config_version_bot_scope", "workspace_id", "bot_id", "trading_account_id"),
        UniqueConstraint("workspace_id", "id", name="uq_config_version_workspace_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "trading_account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "parent_version_id"],
            ["config_version.workspace_id", "config_version.id"],
        ),
        UniqueConstraint(
            "workspace_id", "trading_account_id", "version", name="uq_config_version_number"
        ),
    )

    bot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        server_default=text("NULLIF(current_setting('app.bot_id', true), '')::uuid"),
    )
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trading_account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceSettingsVersion(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "workspace_settings_version"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_workspace_settings_version_id"),
        UniqueConstraint("workspace_id", "version", name="uq_workspace_settings_version_number"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "parent_version_id"],
            ["workspace_settings_version.workspace_id", "workspace_settings_version.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "created_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_workspace_settings_created_by",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "approved_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_workspace_settings_approved_by",
        ),
        CheckConstraint("version >= 1", name="workspace_settings_version_positive"),
        CheckConstraint("schema_version >= 1", name="workspace_settings_schema_positive"),
        CheckConstraint("revision >= 1", name="workspace_settings_revision_positive"),
        CheckConstraint("status IN ('DRAFT', 'PUBLISHED')", name="workspace_settings_status_valid"),
        Index("ix_workspace_settings_effective", "workspace_id", "status", "effective_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    settings_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    approved_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RiskProfileVersion(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "risk_profile_version"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_risk_profile_version_workspace_id"),
        UniqueConstraint("workspace_id", "name", "version", name="uq_risk_profile_version_name"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        CheckConstraint(
            "max_risk_fraction >= 0 AND max_risk_fraction <= 1", name="risk_fraction_range"
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    max_risk_fraction: Mapped[Decimal] = mapped_column(
        Numeric(12, 10, asdecimal=True), nullable=False
    )
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ApprovalRequest(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "approval_request"
    __table_args__ = (
        CheckConstraint(
            "(bot_id IS NULL) = (trading_account_id IS NULL)",
            name="approval_request_bot_scope_consistent",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "trading_account_id"],
            ["bot_instance.workspace_id", "bot_instance.id", "bot_instance.trading_account_id"],
            name="fk_approval_request_bot_account",
        ),
        Index("ix_approval_request_bot_scope", "workspace_id", "bot_id", "trading_account_id"),
        UniqueConstraint("workspace_id", "id", name="uq_approval_request_workspace_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "trading_account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "requested_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_approval_request_requested_by_membership",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "decided_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_approval_request_decided_by_membership",
        ),
        Index("ix_approval_request_workspace_status", "workspace_id", "status"),
    )

    bot_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trading_account_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    subject_type: Mapped[str] = mapped_column(String(48), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    requested_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decided_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WatchlistVersion(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "watchlist_version"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_watchlist_version_workspace_id"),
        UniqueConstraint("workspace_id", "name", "version", name="uq_watchlist_version_name"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT")


class WatchlistItem(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "watchlist_item"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_watchlist_item_workspace_id"),
        UniqueConstraint(
            "workspace_id", "watchlist_version_id", "symbol", name="uq_watchlist_item_symbol"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "watchlist_version_id"],
            ["watchlist_version.workspace_id", "watchlist_version.id"],
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    watchlist_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    weight: Mapped[Decimal] = mapped_column(
        Numeric(12, 10, asdecimal=True), nullable=False, default=1
    )


class StrategyRelease(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "strategy_release"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_strategy_release_workspace_id"),
        UniqueConstraint("workspace_id", "name", "version", name="uq_strategy_release_version"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        Index("ix_strategy_release_workspace_status", "workspace_id", "status"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT")
    recipe: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)


class OutboxEvent(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "outbox_event"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_outbox_event_workspace_id"),
        UniqueConstraint(
            "workspace_id", "event_type", "idempotency_key", name="uq_outbox_event_idempotency"
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        CheckConstraint("schema_version >= 1", name="outbox_schema_version_positive"),
        CheckConstraint("attempts >= 0", name="outbox_attempts_nonnegative"),
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'DELIVERED', 'DEAD')",
            name="outbox_status_valid",
        ),
        Index("ix_outbox_event_ready", "workspace_id", "status", "available_at", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)


class ConsumerReceipt(UuidPrimaryKey, CreatedAt, Base):
    __tablename__ = "consumer_receipt"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_consumer_receipt_workspace_id"),
        UniqueConstraint(
            "workspace_id", "outbox_event_id", "consumer_name", name="uq_consumer_receipt_event"
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "outbox_event_id"],
            ["outbox_event.workspace_id", "outbox_event.id"],
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    outbox_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    consumer_name: Mapped[str] = mapped_column(String(120), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEvent(UuidPrimaryKey, Base):
    __tablename__ = "audit_event"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_audit_event_workspace_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "actor_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
        ),
        Index("ix_audit_event_workspace_time", "workspace_id", "occurred_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_membership_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

"""Identity bot, cấp quyền và phiên bản cấu hình theo bot; mode lấy từ TradingAccount."""

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

from trading_bot.adapters.database.models.base import Base, CreatedAt, UpdatedAt, UuidPrimaryKey


class BotInstance(UuidPrimaryKey, CreatedAt, UpdatedAt, Base):
    """Một identity bot duy nhất cho mỗi account trong workspace."""

    __tablename__ = "bot_instance"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_bot_instance_workspace_id"),
        UniqueConstraint("workspace_id", "trading_account_id", name="uq_bot_instance_account"),
        UniqueConstraint("workspace_id", "id", "trading_account_id", name="uq_bot_instance_scope"),
        ForeignKeyConstraint(
            ["workspace_id", "trading_account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
            name="fk_bot_instance_account",
        ),
        CheckConstraint("status IN ('ENABLED', 'DISABLED')", name="bot_instance_status_valid"),
        CheckConstraint("length(trim(name)) > 0", name="bot_instance_name_nonempty"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trading_account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DISABLED")


class BotGrant(UuidPrimaryKey, CreatedAt, Base):
    """Lịch sử grant; thu hồi bằng revoked_at thay vì xóa dấu vết."""

    __tablename__ = "bot_grant"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "bot_id"],
            ["bot_instance.workspace_id", "bot_instance.id"],
            name="fk_bot_grant_bot",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_bot_grant_member",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "granted_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_bot_grant_grantor",
        ),
        CheckConstraint(
            "permission IN ('bot:read', 'bot:config:write', 'bot:config:publish', "
            "'bot:runtime:pause', 'bot:runtime:resume', 'bot:report:read')",
            name="bot_grant_permission_valid",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at", name="bot_grant_revocation_valid"
        ),
        Index(
            "uq_bot_grant_active",
            "workspace_id",
            "bot_id",
            "membership_id",
            "permission",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    permission: Mapped[str] = mapped_column(String(40), nullable=False)
    granted_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


_SHA256 = "length({column}) = 64 AND {column} = lower({column}) AND {column} !~ '[^0-9a-f]'"


class BotConfigVersion(UuidPrimaryKey, CreatedAt, UpdatedAt, Base):
    """Overlay cấu hình bot có version; hàng PUBLISHED bất biến (trigger DB bảo vệ)."""

    __tablename__ = "bot_config_version"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_bot_config_version_workspace_id"),
        UniqueConstraint("workspace_id", "bot_id", "id", name="uq_bot_config_version_bot_id"),
        UniqueConstraint("workspace_id", "bot_id", "version", name="uq_bot_config_version_number"),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            ["bot_instance.workspace_id", "bot_instance.id", "bot_instance.trading_account_id"],
            name="fk_bot_config_version_bot_account",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "parent_version_id"],
            [
                "bot_config_version.workspace_id",
                "bot_config_version.bot_id",
                "bot_config_version.id",
            ],
            name="fk_bot_config_version_parent",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "source_version_id"],
            [
                "bot_config_version.workspace_id",
                "bot_config_version.bot_id",
                "bot_config_version.id",
            ],
            name="fk_bot_config_version_source",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "base_settings_version_id"],
            ["workspace_settings_version.workspace_id", "workspace_settings_version.id"],
            name="fk_bot_config_version_base_settings",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "created_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_bot_config_version_created_by",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "updated_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_bot_config_version_updated_by",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "approved_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_bot_config_version_approved_by",
        ),
        CheckConstraint("status IN ('DRAFT', 'PUBLISHED')", name="status_valid"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("schema_version >= 1", name="schema_positive"),
        CheckConstraint(_SHA256.format(column="overlay_hash"), name="overlay_hash_sha256"),
        CheckConstraint(_SHA256.format(column="base_settings_hash"), name="base_hash_sha256"),
        CheckConstraint(
            "(status = 'DRAFT' AND approved_by IS NULL AND published_at IS NULL "
            "AND effective_at IS NULL) OR (status = 'PUBLISHED' AND approved_by IS NOT NULL "
            "AND published_at IS NOT NULL AND effective_at IS NOT NULL)",
            name="publication_consistent",
        ),
        Index("ix_bot_config_version_bot_scope", "workspace_id", "bot_id", "account_id"),
        Index(
            "ix_bot_config_version_effective", "workspace_id", "bot_id", "status", "effective_at"
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    source_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    overlay_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    overlay_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_settings_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    base_settings_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    updated_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    approved_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BotConfigRequest(UuidPrimaryKey, CreatedAt, Base):
    """Sổ Idempotency-Key append-only cho mutation cấu hình bot."""

    __tablename__ = "bot_config_request"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "bot_id",
            "operation",
            "idempotency_key",
            name="uq_bot_config_request_key",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            ["bot_instance.workspace_id", "bot_instance.id", "bot_instance.trading_account_id"],
            name="fk_bot_config_request_bot_account",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "bot_config_version_id"],
            [
                "bot_config_version.workspace_id",
                "bot_config_version.bot_id",
                "bot_config_version.id",
            ],
            name="fk_bot_config_request_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "created_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_bot_config_request_created_by",
        ),
        CheckConstraint(
            "operation IN ('draft.create', 'draft.update', 'publish', 'rollback')",
            name="operation_valid",
        ),
        CheckConstraint("idempotency_key ~ '^[A-Za-z0-9._:-]{8,120}$'", name="key_format"),
        CheckConstraint(_SHA256.format(column="request_hash"), name="request_hash_sha256"),
        Index("ix_bot_config_request_bot_scope", "workspace_id", "bot_id", "account_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bot_config_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

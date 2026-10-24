"""Identity bot và cấp quyền theo bot; mode được lấy từ TradingAccount."""

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
    text,
)
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

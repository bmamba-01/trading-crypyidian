"""Bảng preview và operation ledger cho confirm flatten-all."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UpdatedAt, UuidPrimaryKey

_JSON_TYPE = JSON().with_variant(JSONB(astext_type=Text()), "postgresql")


class FlattenPreviewRecord(UuidPrimaryKey, CreatedAt, Base):
    """Snapshot immutable có thời hạn, được ràng buộc với membership tạo preview."""

    __tablename__ = "flatten_preview"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_flatten_preview_workspace_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
        ),
        CheckConstraint("length(preview_hash) = 64", name="flatten_preview_hash_sha256"),
        CheckConstraint("length(snapshot_hash) = 64", name="flatten_preview_snapshot_sha256"),
        CheckConstraint("expires_at > created_at", name="flatten_preview_expiry_valid"),
        Index("ix_flatten_preview_expiry", "workspace_id", "expires_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_payload: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FlattenOperationRecord(UuidPrimaryKey, CreatedAt, UpdatedAt, Base):
    """Yêu cầu flatten idempotent bền vững, có thể tiếp tục sau khi process khởi động lại."""

    __tablename__ = "flatten_operation"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_flatten_operation_workspace_id"),
        UniqueConstraint(
            "workspace_id", "membership_id", "idempotency_key", name="uq_flatten_operation_key"
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "preview_id"],
            ["flatten_preview.workspace_id", "flatten_preview.id"],
        ),
        CheckConstraint("length(idempotency_key) BETWEEN 1 AND 128", name="flatten_key_length"),
        CheckConstraint("length(request_hash) = 64", name="flatten_request_hash_sha256"),
        CheckConstraint(
            "status IN ('QUEUED', 'PROCESSING', 'PARTIAL', 'BLOCKED', 'COMPLETED')",
            name="flatten_operation_status_valid",
        ),
        Index("ix_flatten_operation_pending", "workspace_id", "status", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    preview_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="QUEUED")
    reason: Mapped[str] = mapped_column(String(500), nullable=False)


class FlattenOperationItemRecord(UuidPrimaryKey, UpdatedAt, Base):
    """Kết quả từng vị thế/open order được chụp trong preview."""

    __tablename__ = "flatten_operation_item"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "operation_id", "id", name="uq_flatten_operation_item_scope_id"
        ),
        UniqueConstraint(
            "workspace_id",
            "operation_id",
            "bot_id",
            "target_type",
            "target_id",
            name="uq_flatten_operation_target",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["flatten_operation.workspace_id", "flatten_operation.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            ["bot_instance.workspace_id", "bot_instance.id", "bot_instance.trading_account_id"],
        ),
        CheckConstraint(
            "target_type IN ('POSITION', 'OPEN_ORDER')", name="flatten_target_type_valid"
        ),
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'BLOCKED', 'COMPLETED')",
            name="flatten_item_status_valid",
        ),
        CheckConstraint("quantity > 0", name="flatten_item_quantity_positive"),
        CheckConstraint("mark_price IS NULL OR mark_price > 0", name="flatten_item_mark_positive"),
        Index("ix_flatten_item_worker", "workspace_id", "bot_id", "status", "updated_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(240), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    target_status: Mapped[str] = mapped_column(String(24), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    mark_record_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    mark_revision: Mapped[int | None] = mapped_column(nullable=True)
    mark_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mark_close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reservation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PENDING")
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    result: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, nullable=False, server_default="{}")
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)

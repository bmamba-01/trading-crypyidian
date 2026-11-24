"""Bảng lưu trữ riêng cho nhật ký giao dịch, checklist, tag setup, ảnh đính kèm và audit.

Hoàn toàn tách khỏi ledger bất biến: mọi ghi/xóa trên các bảng này không chạm vào ledger.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    Uuid,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base


class TradeJournalEntry(Base):
    """Nhật ký thủ công, checklist, setup_tags và mistake_tags gắn theo trade_id."""

    __tablename__ = "trade_journal_entry"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_trade_journal_entry"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        CheckConstraint(
            "privacy_mode IN ('workspace_team', 'author_only')",
            name="trade_journal_privacy_valid",
        ),
        CheckConstraint(
            "retention_days >= 7 AND retention_days <= 3650",
            name="trade_journal_retention_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(checklist_items) = 'array'",
            name="trade_journal_checklist_array",
        ),
        CheckConstraint(
            "jsonb_typeof(setup_tags) = 'array'",
            name="trade_journal_setup_tags_array",
        ),
        CheckConstraint(
            "jsonb_typeof(mistake_tags) = 'array'",
            name="trade_journal_mistake_tags_array",
        ),
        Index(
            "ux_trade_journal_active_trade",
            "workspace_id",
            "trade_id",
            unique=True,
            postgresql_where=sql_text("deleted_at IS NULL"),
        ),
        Index("ix_trade_journal_workspace_bot", "workspace_id", "bot_id", "updated_at"),
        Index("ix_trade_journal_expires_at", "workspace_id", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trade_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    author_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    note_vi: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    checklist_items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    setup_tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    mistake_tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    privacy_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="workspace_team", server_default="workspace_team"
    )
    retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=365, server_default="365"
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TradeJournalAttachment(Base):
    """Metadata ảnh chụp biểu đồ đính kèm nhật ký (đã kiểm duyệt type, size, hash, EXIF)."""

    __tablename__ = "trade_journal_attachment"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_trade_journal_attachment"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["entry_id"], ["trade_journal_entry.id"], ondelete="CASCADE"),
        CheckConstraint(
            "content_type IN ('image/png', 'image/jpeg', 'image/webp')",
            name="trade_journal_att_type_valid",
        ),
        CheckConstraint(
            "byte_size > 0 AND byte_size <= 5242880",
            name="trade_journal_att_size_valid",
        ),
        CheckConstraint("exif_stripped = true", name="trade_journal_att_exif_stripped"),
        Index("ix_trade_journal_att_entry", "workspace_id", "entry_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    entry_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    file_name: Mapped[str] = mapped_column(String(180), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    exif_stripped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TradeJournalAudit(Base):
    """Nhật ký kiểm toán append-only cho mọi thao tác tạo, sửa, xóa, retention trên journal."""

    __tablename__ = "trade_journal_audit"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_trade_journal_audit"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        CheckConstraint(
            "action IN ('CREATED', 'UPDATED', 'ATTACHMENT_ADDED', "
            "'ATTACHMENT_REMOVED', 'SOFT_DELETED', 'RETENTION_PURGED')",
            name="trade_journal_audit_action_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(diff_summary) = 'object'",
            name="trade_journal_audit_diff_object",
        ),
        Index("ix_trade_journal_audit_trade", "workspace_id", "trade_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    entry_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trade_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    diff_summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

"""Bảng kiểm toán AI Analyst (AI-2): session theo chủ đề và từng lượt phân tích."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class AnalystSessionRecord(UuidPrimaryKey, CreatedAt, Base):
    """Session CLI agent của một chủ đề (vd ``family:H03``); tiếp tục để tích lũy ngữ cảnh."""

    __tablename__ = "analyst_session"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "topic_key", "engine", "session_id", name="uq_analyst_session_ref"
        ),
        CheckConstraint("status IN ('ACTIVE', 'RETIRED')", name="analyst_session_status_valid"),
        CheckConstraint("run_count >= 0", name="analyst_session_run_count_nonnegative"),
        Index("ix_analyst_session_topic", "workspace_id", "topic_key", "engine", "status"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    topic_key: Mapped[str] = mapped_column(String(120), nullable=False)
    engine: Mapped[str] = mapped_column(String(40), nullable=False)
    session_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AnalystRunRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một lượt phân tích: đầu vào băm, đầu ra thô, kết quả validator số liệu (append-only)."""

    __tablename__ = "analyst_run"
    __table_args__ = (
        Index("ix_analyst_run_topic_created", "workspace_id", "topic_key", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    topic_key: Mapped[str] = mapped_column(String(120), nullable=False)
    engine: Mapped[str] = mapped_column(String(40), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    run_report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    output_version: Mapped[str] = mapped_column(String(40), nullable=False)
    cli_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    exit_code: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    raw_stdout: Mapped[str] = mapped_column(Text, nullable=False, default="")
    structured_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    validation_errors: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

"""Bảng lưu trữ bền vững báo cáo kiểm định scalping SIG-5 theo workspace."""

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
    Uuid,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base


class ScalpingValidationReportRecord(Base):
    """Báo cáo kiểm định scalping SIG-5 đã băm và kiểm tra tiêu chí #5."""

    __tablename__ = "scalping_validation_report"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_scalping_validation_report"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        CheckConstraint(
            "jsonb_typeof(report_payload) = 'object'",
            name="scalping_validation_payload_object",
        ),
        CheckConstraint(
            "out_of_sample_n >= 0",
            name="scalping_validation_oos_non_negative",
        ),
        Index(
            "ux_scalping_validation_hash",
            "workspace_id",
            "validation_hash",
            unique=True,
        ),
        Index(
            "ix_scalping_validation_lookup",
            "workspace_id",
            "release_id",
            "symbol",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    release_id: Mapped[str] = mapped_column(String(64), nullable=False)
    release_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    costs_version: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluation_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recommend_paper: Mapped[bool] = mapped_column(Boolean, nullable=False)
    out_of_sample_n: Mapped[int] = mapped_column(Integer, nullable=False)
    stress_expectancy_r: Mapped[str | None] = mapped_column(String(32), nullable=True)
    validation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    report_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

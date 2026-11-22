"""Bản chụp trạng thái worker mới nhất, giới hạn theo workspace."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    Uuid,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base


class WorkerHeartbeat(Base):
    """Trạng thái và bộ đếm an toàn mới nhất của một worker trong một process."""

    __tablename__ = "worker_heartbeat"
    __table_args__ = (
        PrimaryKeyConstraint("workspace_id", "component", "instance_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        CheckConstraint(
            "state IN ('ready', 'degraded', 'not_ready')", name="worker_heartbeat_state_valid"
        ),
        CheckConstraint(
            "jsonb_typeof(counters) = 'object'", name="worker_heartbeat_counters_object"
        ),
        Index("ix_worker_heartbeat_latest", "workspace_id", "component", "heartbeat_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    component: Mapped[str] = mapped_column(String(80), nullable=False)
    instance_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    detail_vi: Mapped[str | None] = mapped_column(Text, nullable=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    counters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )

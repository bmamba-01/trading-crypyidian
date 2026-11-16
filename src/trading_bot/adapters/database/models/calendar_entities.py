"""Model ORM cho lịch sự kiện kinh tế do owner nhập (CK-10)."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, UuidPrimaryKey


class EconomicEventRecord(UuidPrimaryKey, Base):
    """Một sự kiện kinh tế; xóa mềm bằng ``deleted_at``, mỗi lần sửa tăng ``revision``."""

    __tablename__ = "economic_event"
    __table_args__ = (
        CheckConstraint("impact IN ('LOW', 'MEDIUM', 'HIGH')", name="economic_event_impact_valid"),
        CheckConstraint("length(btrim(title)) > 0", name="economic_event_title_present"),
        CheckConstraint("region ~ '^[A-Z]{2,16}$'", name="economic_event_region_valid"),
        CheckConstraint("revision >= 1", name="economic_event_revision_positive"),
        CheckConstraint(
            "deleted_at IS NULL OR deleted_at >= created_at",
            name="economic_event_delete_after_create",
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        Index("ix_economic_event_workspace_time", "workspace_id", "event_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    region: Mapped[str] = mapped_column(String(16), nullable=False)
    impact: Mapped[str] = mapped_column(String(8), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

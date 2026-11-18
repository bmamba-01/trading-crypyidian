"""ORM metadata cho MarketBar staging gắn với BotDecisionJob của WP06."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from trading_bot.adapters.database.models.base import Base


class BotDecisionJobSourceBarRecord(Base):
    """Một bar metadata-only đang được kiểm quyền trong transaction của job."""

    __tablename__ = "bot_decision_job_source_bar"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("bot_decision_job_source_bar_pkey")),
        ForeignKeyConstraint(
            ["workspace_id", "job_id"],
            ["bot_decision_job.workspace_id", "bot_decision_job.id"],
            ondelete="RESTRICT",
            name=conv("fk_bot_decision_job_source_bar_job"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "evidence_id"],
            ["shared_signal_evidence.workspace_id", "shared_signal_evidence.id"],
            ondelete="RESTRICT",
            name=conv("fk_bot_decision_job_source_bar_evidence"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "market_bar_id"],
            ["market_bar.workspace_id", "market_bar.id"],
            ondelete="RESTRICT",
            name=conv("fk_bot_decision_job_source_bar_market_bar"),
        ),
        CheckConstraint(
            "evidence_hash ~ '^[0-9a-f]{64}$'",
            name=conv("ck_bot_decision_job_source_bar_evidence_hash"),
        ),
        CheckConstraint(
            "decision_snapshot_id ~ '^[0-9a-f]{64}$'",
            name=conv("ck_bot_decision_job_source_bar_snapshot_hash"),
        ),
        CheckConstraint(
            "feature_snapshot_id ~ '^[0-9a-f]{64}$'",
            name=conv("ck_bot_decision_job_source_bar_feature_hash"),
        ),
        CheckConstraint(
            "source_event_id ~ '^market-bar:[0-9a-f]{64}$'",
            name=conv("ck_bot_decision_job_source_bar_event_id"),
        ),
        CheckConstraint(
            "source_event_digest ~ '^[0-9a-f]{64}$'",
            name=conv("ck_bot_decision_job_source_bar_event_digest"),
        ),
        CheckConstraint("ordinal >= 0", name=conv("ck_bot_decision_job_source_bar_ordinal")),
        CheckConstraint(
            "timeframe_bar_count > 0 AND snapshot_timeframe_count > 0",
            name=conv("ck_bot_decision_job_source_bar_counts"),
        ),
        Index(
            "uq_bot_decision_job_source_bar_ordinal",
            "workspace_id",
            "job_id",
            "timeframe",
            "ordinal",
            unique=True,
        ),
        Index(
            "uq_bot_decision_job_source_bar_market_bar",
            "workspace_id",
            "job_id",
            "market_bar_id",
            unique=True,
        ),
        Index(
            "ix_bot_decision_job_source_bar_job",
            "workspace_id",
            "bot_id",
            "job_id",
            "timeframe",
            "ordinal",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=False, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evidence_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(Text, nullable=False)
    decision_snapshot_id: Mapped[str] = mapped_column(Text, nullable=False)
    decision_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    feature_snapshot_id: Mapped[str] = mapped_column(Text, nullable=False)
    market_bar_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_digest: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    timeframe_bar_count: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_timeframe_count: Mapped[int] = mapped_column(Integer, nullable=False)

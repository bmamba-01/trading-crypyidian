"""Bảng lineage nến nguồn gắn với quyết định bot WP06."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class BotDecisionSourceBarRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một nến MarketBar đã được snapshot của BotDecision sử dụng."""

    __tablename__ = "bot_decision_source_bar"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "bot_decision_id"],
            ["bot_decision.workspace_id", "bot_decision.bot_id", "bot_decision.id"],
            ondelete="RESTRICT",
            name="fk_bot_decision_source_bar_decision",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "market_bar_id"],
            ["market_bar.workspace_id", "market_bar.id"],
            ondelete="RESTRICT",
            name="fk_bot_decision_source_bar_market_bar",
        ),
        CheckConstraint(
            "decision_snapshot_id ~ '^[0-9a-f]{64}$'",
            name="bot_decision_source_bar_snapshot_hash_valid",
        ),
        CheckConstraint(
            "feature_snapshot_id ~ '^[0-9a-f]{64}$'",
            name="bot_decision_source_bar_feature_hash_valid",
        ),
        CheckConstraint(
            "source_event_id ~ '^market-bar:[0-9a-f]{64}$'",
            name="bot_decision_source_bar_event_id_valid",
        ),
        CheckConstraint(
            "source_event_digest ~ '^[0-9a-f]{64}$'",
            name="bot_decision_source_bar_event_digest_valid",
        ),
        CheckConstraint("ordinal >= 0", name="bot_decision_source_bar_ordinal_nonnegative"),
        CheckConstraint(
            "timeframe_bar_count > 0",
            name="bot_decision_source_bar_timeframe_count_positive",
        ),
        CheckConstraint(
            "snapshot_timeframe_count > 0",
            name="bot_decision_source_bar_snapshot_count_positive",
        ),
        Index(
            "uq_bot_decision_source_bar_ordinal",
            "workspace_id",
            "bot_decision_id",
            "timeframe",
            "ordinal",
            unique=True,
        ),
        Index(
            "uq_bot_decision_source_bar_market_bar",
            "workspace_id",
            "bot_decision_id",
            "timeframe",
            "market_bar_id",
            unique=True,
        ),
        Index(
            "ix_bot_decision_source_bar_decision",
            "workspace_id",
            "bot_id",
            "bot_decision_id",
            "timeframe",
            "ordinal",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decision_snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    feature_snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_bar_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(80), nullable=False)
    source_event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    timeframe_bar_count: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_timeframe_count: Mapped[int] = mapped_column(Integer, nullable=False)

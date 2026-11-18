"""Kết quả tín hiệu đã chấm (CRY-430): append-only, mỗi hàng = (tín hiệu, phương pháp, horizon)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey

SIGNAL_OUTCOME_STATUSES = ("SCORED", "DATA_GAP", "INVALID_SIGNAL")
FIRST_TOUCH_VALUES = ("TP1", "SL", "NONE")


class SignalOutcomeRecord(UuidPrimaryKey, CreatedAt, Base):
    """Kết quả chấm một tín hiệu tại một horizon; trigger DB chặn UPDATE/DELETE."""

    __tablename__ = "signal_outcome"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "decision_id",
            "method_version",
            "horizon_bars",
            name="uq_signal_outcome_decision_horizon",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "decision_id"],
            ["bot_decision.workspace_id", "bot_decision.bot_id", "bot_decision.id"],
            ondelete="CASCADE",
            name="fk_signal_outcome_decision",
        ),
        CheckConstraint(
            "status IN ('SCORED', 'DATA_GAP', 'INVALID_SIGNAL')", name="signal_outcome_status_valid"
        ),
        CheckConstraint("horizon_bars >= 1", name="signal_outcome_horizon_positive"),
        CheckConstraint("direction IN ('long', 'short')", name="signal_outcome_direction_valid"),
        CheckConstraint(
            "(status = 'SCORED') = (first_touch IS NOT NULL AND outcome_r IS NOT NULL "
            "AND label IS NOT NULL AND input_hash IS NOT NULL)",
            name="signal_outcome_scored_complete",
        ),
        CheckConstraint(
            "first_touch IS NULL OR first_touch IN ('TP1', 'SL', 'NONE')",
            name="signal_outcome_first_touch_valid",
        ),
        CheckConstraint("label IS NULL OR label IN (-1, 0, 1)", name="signal_outcome_label_valid"),
        CheckConstraint(
            "input_hash IS NULL OR input_hash ~ '^[0-9a-f]{64}$'",
            name="signal_outcome_input_hash_sha256",
        ),
        Index("ix_signal_outcome_bot_signal", "workspace_id", "bot_id", "signal_at"),
        Index("ix_signal_outcome_symbol", "workspace_id", "symbol", "timeframe"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    method_version: Mapped[str] = mapped_column(String(40), nullable=False)
    label_version: Mapped[str] = mapped_column(String(40), nullable=False)
    horizon_bars: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(80), nullable=False)
    strategy_release_id: Mapped[str] = mapped_column(String(80), nullable=False)
    signal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    tp1_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    tp2_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    horizon_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_touch: Mapped[str | None] = mapped_column(String(8), nullable=True)
    same_bar_conflict: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tp1_hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tp2_hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    invalidated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    bars_to_first_touch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seconds_to_first_touch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mfe_r: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    mae_r: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    close_r: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    outcome_r: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    label: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

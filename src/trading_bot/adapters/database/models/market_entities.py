"""Bảng PostgreSQL cho dữ liệu thị trường theo workspace và point-in-time."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey

_MARKET_VALUE = Numeric(precision=38, scale=18)


class MarketInstrument(UuidPrimaryKey, CreatedAt, Base):
    """Một phiên bản quy cách hợp đồng được quan sát từ nguồn metadata."""

    __tablename__ = "market_instrument"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_market_instrument_workspace_id"),
        UniqueConstraint(
            "workspace_id", "source", "idempotency_key", name="uq_market_instrument_idempotency"
        ),
        UniqueConstraint(
            "workspace_id", "source", "symbol", "revision", name="uq_market_instrument_revision"
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "supersedes_id"],
            ["market_instrument.workspace_id", "market_instrument.id"],
        ),
        CheckConstraint("revision >= 1", name="market_instrument_revision_positive"),
        CheckConstraint("tick_size > 0", name="market_instrument_tick_size_positive"),
        CheckConstraint("quantity_step > 0", name="market_instrument_quantity_step_positive"),
        CheckConstraint("min_notional >= 0", name="market_instrument_min_notional_nonnegative"),
        CheckConstraint(
            "event_at <= received_at AND received_at <= available_at",
            name="market_instrument_timestamps_causal",
        ),
        Index("ix_market_instrument_lookup", "workspace_id", "source", "symbol", "available_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    base_asset: Mapped[str] = mapped_column(String(32), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(32), nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(_MARKET_VALUE, nullable=False)
    quantity_step: Mapped[Decimal] = mapped_column(_MARKET_VALUE, nullable=False)
    min_notional: Mapped[Decimal] = mapped_column(_MARKET_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MarketRawReference(UuidPrimaryKey, CreatedAt, Base):
    """Tham chiếu bất biến tới payload gốc nằm ngoài cơ sở dữ liệu."""

    __tablename__ = "market_raw_reference"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_market_raw_reference_workspace_id"),
        UniqueConstraint(
            "workspace_id", "source", "idempotency_key", name="uq_market_raw_reference_idempotency"
        ),
        CheckConstraint("byte_length >= 0", name="market_raw_reference_length_nonnegative"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        Index("ix_market_raw_reference_checksum", "workspace_id", "checksum"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)


class MarketBar(UuidPrimaryKey, CreatedAt, Base):
    """Một phiên bản nến bất biến với khóa dedup và lineage correction."""

    __tablename__ = "market_bar"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_market_bar_workspace_id"),
        UniqueConstraint(
            "workspace_id", "source", "idempotency_key", name="uq_market_bar_idempotency"
        ),
        UniqueConstraint(
            "workspace_id",
            "instrument_id",
            "timeframe",
            "open_time",
            "source",
            "revision",
            name="uq_market_bar_revision",
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["market_instrument.workspace_id", "market_instrument.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "supersedes_id"], ["market_bar.workspace_id", "market_bar.id"]
        ),
        ForeignKeyConstraint(
            ["workspace_id", "raw_reference_id"],
            ["market_raw_reference.workspace_id", "market_raw_reference.id"],
        ),
        CheckConstraint("revision >= 1", name="market_bar_revision_positive"),
        CheckConstraint("close_time >= open_time", name="market_bar_close_after_open"),
        CheckConstraint(
            "event_at <= received_at AND received_at <= available_at",
            name="market_bar_timestamps_causal",
        ),
        CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0", name="market_bar_prices_positive"
        ),
        CheckConstraint(
            "high >= low AND high >= open AND high >= close", name="market_bar_high_consistent"
        ),
        CheckConstraint("low <= open AND low <= close", name="market_bar_low_consistent"),
        CheckConstraint("volume >= 0", name="market_bar_volume_nonnegative"),
        Index(
            "ix_market_bar_asof", "workspace_id", "symbol", "timeframe", "open_time", "available_at"
        ),
        Index("ix_market_bar_lineage", "workspace_id", "logical_key", "revision"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    logical_key: Mapped[str] = mapped_column(String(240), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    raw_reference_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    source_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(_MARKET_VALUE, nullable=False)
    high: Mapped[Decimal] = mapped_column(_MARKET_VALUE, nullable=False)
    low: Mapped[Decimal] = mapped_column(_MARKET_VALUE, nullable=False)
    close: Mapped[Decimal] = mapped_column(_MARKET_VALUE, nullable=False)
    volume: Mapped[Decimal] = mapped_column(_MARKET_VALUE, nullable=False)
    is_closed: Mapped[bool] = mapped_column(nullable=False)


class MarketFunding(UuidPrimaryKey, CreatedAt, Base):
    """Một mức funding bất biến có mốc công bố và nhận dữ liệu."""

    __tablename__ = "market_funding"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_market_funding_workspace_id"),
        UniqueConstraint(
            "workspace_id", "source", "idempotency_key", name="uq_market_funding_idempotency"
        ),
        UniqueConstraint(
            "workspace_id",
            "instrument_id",
            "funding_time",
            "source",
            "revision",
            name="uq_market_funding_revision",
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["market_instrument.workspace_id", "market_instrument.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "supersedes_id"], ["market_funding.workspace_id", "market_funding.id"]
        ),
        CheckConstraint("revision >= 1", name="market_funding_revision_positive"),
        CheckConstraint(
            "event_at <= received_at AND received_at <= available_at",
            name="market_funding_timestamps_causal",
        ),
        Index(
            "ix_market_funding_asof",
            "workspace_id",
            "instrument_id",
            "funding_time",
            "available_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    funding_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    funding_rate: Mapped[Decimal] = mapped_column(_MARKET_VALUE, nullable=False)


class MarketOpenInterest(UuidPrimaryKey, CreatedAt, Base):
    """Một quan sát open interest bất biến và có thể truy vấn theo as_of."""

    __tablename__ = "market_open_interest"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_market_open_interest_workspace_id"),
        UniqueConstraint(
            "workspace_id", "source", "idempotency_key", name="uq_market_open_interest_idempotency"
        ),
        UniqueConstraint(
            "workspace_id",
            "instrument_id",
            "observed_at",
            "source",
            "revision",
            name="uq_market_open_interest_revision",
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["market_instrument.workspace_id", "market_instrument.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "supersedes_id"],
            ["market_open_interest.workspace_id", "market_open_interest.id"],
        ),
        CheckConstraint("revision >= 1", name="market_open_interest_revision_positive"),
        CheckConstraint(
            "event_at <= received_at AND received_at <= available_at",
            name="market_open_interest_timestamps_causal",
        ),
        CheckConstraint("quantity >= 0", name="market_open_interest_quantity_nonnegative"),
        Index(
            "ix_market_open_interest_asof",
            "workspace_id",
            "instrument_id",
            "observed_at",
            "available_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(_MARKET_VALUE, nullable=False)


class IngestionCursor(UuidPrimaryKey, CreatedAt, Base):
    """Tiến độ mutable duy nhất cho từng workspace/source/symbol/timeframe."""

    __tablename__ = "ingestion_cursor"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_ingestion_cursor_workspace_id"),
        UniqueConstraint(
            "workspace_id", "source", "symbol", "timeframe", name="uq_ingestion_cursor_stream"
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        CheckConstraint("length(token) > 0", name="ingestion_cursor_token_nonempty"),
        Index(
            "ix_ingestion_cursor_asof",
            "workspace_id",
            "source",
            "symbol",
            "timeframe",
            "updated_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    covered_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IngestionGap(UuidPrimaryKey, CreatedAt, Base):
    """Khoảng chưa đủ dữ liệu; chỉ cho phép bổ sung thời điểm đã resolve."""

    __tablename__ = "ingestion_gap"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_ingestion_gap_workspace_id"),
        UniqueConstraint(
            "workspace_id",
            "source",
            "symbol",
            "timeframe",
            "gap_start",
            "gap_end",
            name="uq_ingestion_gap_range",
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        CheckConstraint("gap_start < gap_end", name="ingestion_gap_range_valid"),
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= detected_at",
            name="ingestion_gap_resolution_after_detection",
        ),
        Index(
            "ix_ingestion_gap_open",
            "workspace_id",
            "source",
            "symbol",
            "timeframe",
            "gap_start",
            "gap_end",
            "resolved_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    gap_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    gap_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

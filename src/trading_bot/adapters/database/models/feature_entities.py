"""Bảng snapshot feature bất biến trong phạm vi workspace."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey

_JSON_TYPE = JSON().with_variant(JSONB(astext_type=Text()), "postgresql")


class FeatureSnapshotRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một lần tính feature có hash, version và lineage đầu vào cố định."""

    __tablename__ = "feature_snapshot"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_feature_snapshot_workspace_id"),
        UniqueConstraint(
            "workspace_id", "snapshot_id", name="uq_feature_snapshot_workspace_snapshot"
        ),
        UniqueConstraint(
            "workspace_id", "content_hash", name="uq_feature_snapshot_workspace_content_hash"
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        CheckConstraint("length(snapshot_id) = 64", name="feature_snapshot_id_sha256"),
        CheckConstraint("length(content_hash) = 64", name="feature_snapshot_content_sha256"),
        CheckConstraint(
            "snapshot_id = content_hash", name="feature_snapshot_identity_matches_hash"
        ),
        CheckConstraint("length(schema_hash) = 64", name="feature_snapshot_schema_sha256"),
        CheckConstraint("length(formula_hash) = 64", name="feature_snapshot_formula_sha256"),
        CheckConstraint("length(code_hash) = 64", name="feature_snapshot_code_sha256"),
        CheckConstraint("as_of <= computed_at", name="feature_snapshot_time_order"),
        CheckConstraint(
            "data_coverage >= 0 AND data_coverage <= 1", name="feature_snapshot_coverage"
        ),
        CheckConstraint(
            "data_freshness IN ('FRESH', 'STALE', 'UNKNOWN')",
            name="feature_snapshot_freshness_valid",
        ),
        CheckConstraint(
            "regime ->> 'label' IN ('TREND', 'RANGE', 'HIGH_VOLATILITY', 'UNKNOWN')",
            name="feature_snapshot_regime_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(input_bar_ids) = 'array' AND "
            "jsonb_typeof(input_event_ids) = 'array' AND "
            "jsonb_array_length(input_bar_ids) = jsonb_array_length(input_event_ids)",
            name="feature_snapshot_lineage_arrays_valid",
        ),
        Index(
            "ix_feature_snapshot_asof",
            "workspace_id",
            "symbol",
            "timeframe",
            "as_of",
            "computed_at",
        ),
        Index(
            "ix_feature_snapshot_schema",
            "workspace_id",
            "schema_hash",
            "formula_hash",
            "code_hash",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    formula_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_bar_ids: Mapped[list[str]] = mapped_column(_JSON_TYPE, nullable=False)
    input_event_ids: Mapped[list[str]] = mapped_column(_JSON_TYPE, nullable=False)
    features: Mapped[dict[str, float | None]] = mapped_column(_JSON_TYPE, nullable=False)
    warmup: Mapped[dict[str, bool]] = mapped_column(_JSON_TYPE, nullable=False)
    quality_flags: Mapped[list[str]] = mapped_column(_JSON_TYPE, nullable=False)
    data_freshness: Mapped[str] = mapped_column(String(16), nullable=False)
    data_coverage: Mapped[float] = mapped_column(Float(), nullable=False)
    regime: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, nullable=False)
    is_usable: Mapped[bool] = mapped_column(nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)

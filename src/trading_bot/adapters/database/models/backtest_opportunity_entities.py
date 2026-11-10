"""Bảng opportunity append-only, chỉ chứa metadata của nến quyết định."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class BacktestDecisionOpportunityRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một event nguồn của trial BASE, tách hoàn toàn khỏi vector values."""

    __tablename__ = "backtest_decision_opportunity"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "trial_record_id",
            "query_version",
            "event_id",
            name="uq_opportunity_trial_query_event",
        ),
        UniqueConstraint(
            "workspace_id", "trial_record_id", "event_id", name="uq_opportunity_trial_event"
        ),
        UniqueConstraint(
            "workspace_id",
            "trial_record_id",
            "decision_ordinal",
            name="uq_opportunity_trial_ordinal",
        ),
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_opportunity_idempotency"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["backtest_run.workspace_id", "backtest_run.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "trial_record_id"],
            ["backtest_trial.workspace_id", "backtest_trial.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "scenario_id"],
            ["backtest_scenario.workspace_id", "backtest_scenario.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "schema_version = 'crypidian-replay-decision-opportunity-v3'",
            name="opportunity_schema_valid",
        ),
        CheckConstraint(
            "query_version = 'base-decision-opportunity-universe-v1'",
            name="opportunity_query_valid",
        ),
        CheckConstraint(
            (
                "evaluation_start <= decision_at AND decision_at < range_end AND event_"
                "at <= available_at AND available_at <= decision_at"
            ),
            name="opportunity_time_valid",
        ),
        CheckConstraint("decision_ordinal >= 0", name="opportunity_ordinal_valid"),
        CheckConstraint(
            (
                "input_state IN ('EVALUATED','INPUT_EXCLUDED') AND strategy_state IN ('"
                "NO_CANDIDATE','CANDIDATE','NOT_RUN') AND decision_outcome IN ('NO_TRAD"
                "E','PLAN_REJECTED','RISK_REJECTED','RISK_APPROVED','PAPER_INTENT_CREAT"
                "ED') AND (input_state <> 'INPUT_EXCLUDED' OR strategy_state = 'NOT_RUN"
                "') AND ((strategy_state = 'CANDIDATE' AND candidate_id IS NOT NULL) OR"
                " (strategy_state <> 'CANDIDATE' AND candidate_id IS NULL AND decision_"
                "outcome = 'NO_TRADE'))"
            ),
            name="opportunity_states_valid",
        ),
        CheckConstraint(
            (
                "(feature_schema_hash IS NULL) = (vector_hash IS NULL) AND ((input_state"
                " = 'INPUT_EXCLUDED' AND feature_values_hash IS NULL) OR (input_state ="
                " 'EVALUATED' AND runtime_snapshot_id IS NOT NULL AND feature_schema_hash"
                " IS NOT NULL AND vector_hash IS NOT NULL AND feature_values_hash IS NOT NULL))"
            ),
            name="opportunity_vector_valid",
        ),
        CheckConstraint(
            (
                "manifest_hash ~ '^[0-9a-f]{64}$' AND bundle_hash ~ '^[0-9a-f]{64}$' AN"
                "D strategy_content_hash ~ '^[0-9a-f]{64}$' AND snapshot_id ~ '^[0-9a-f"
                "]{64}$' AND source_refs_digest ~ '^[0-9a-f]{64}$' AND content_hash ~ '"
                "^[0-9a-f]{64}$' AND idempotency_key ~ '^[0-9a-f]{64}$' AND (runtime_sn"
                "apshot_id IS NULL OR runtime_snapshot_id ~ '^[0-9a-f]{64}$') AND (feat"
                "ure_schema_hash IS NULL OR feature_schema_hash ~ '^[0-9a-f]{64}$') AND"
                " (vector_hash IS NULL OR vector_hash ~ '^[0-9a-f]{64}$') AND (feature_"
                "values_hash IS NULL OR feature_values_hash ~ '^[0-9a-f]{64}$') AND (candidat"
                "e_id IS NULL OR candidate_id ~ '^[0-9a-f]{64}$')"
            ),
            name="opportunity_hashes_valid",
        ),
        CheckConstraint(
            (
                "length(btrim(reason_code)) > 0 AND length(btrim(symbol)) > 0 AND jsonb"
                "_typeof(feature_snapshot_ids) = 'array' AND jsonb_array_length(feature"
                "_snapshot_ids) > 0"
            ),
            name="opportunity_metadata_valid",
        ),
        Index(
            "ix_opportunity_workspace_trial", "workspace_id", "trial_record_id", "decision_ordinal"
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trial_record_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    scenario_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_digests: Mapped[list[list[str]]] = mapped_column(JSONB, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_key: Mapped[str] = mapped_column(String(120), nullable=False)
    query_version: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_release_id: Mapped[str] = mapped_column(String(80), nullable=False)
    strategy_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    event_id: Mapped[str] = mapped_column(String(240), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_refs_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_state: Mapped[str] = mapped_column(String(24), nullable=False)
    strategy_state: Mapped[str] = mapped_column(String(24), nullable=False)
    decision_outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(120), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluation_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    runtime_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feature_schema_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feature_values_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    feature_snapshot_ids: Mapped[list[list[str]]] = mapped_column(JSONB, nullable=False)
    canonical_opportunity: Mapped[str] = mapped_column(Text, nullable=False)

"""Bản ghi scenario và kết quả trade của replay, bất biến theo workspace."""

from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
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


class BacktestScenarioRecord(UuidPrimaryKey, CreatedAt, Base):
    """Scenario và override đã dùng để tạo đúng trial."""

    __tablename__ = "backtest_scenario"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_backtest_scenario_workspace_id"),
        UniqueConstraint("workspace_id", "trial_record_id", name="uq_backtest_scenario_trial"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["backtest_run.workspace_id", "backtest_run.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "trial_record_id"],
            ["backtest_trial.workspace_id", "backtest_trial.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("length(manifest_hash) = 64", name="backtest_scenario_manifest_sha256"),
        CheckConstraint("length(scenario_hash) = 64", name="backtest_scenario_sha256"),
        CheckConstraint("kind IN ('BASE', 'COST', 'PARAMETER')", name="backtest_scenario_kind"),
        CheckConstraint("jsonb_typeof(scenario) = 'object'", name="backtest_scenario_object"),
        Index("ix_backtest_scenario_workspace_run", "workspace_id", "run_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trial_record_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    scenario_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_scenario: Mapped[str] = mapped_column(Text, nullable=False)


class BacktestTradeOutcomeRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một giao dịch đóng trong replay, lưu theo thứ tự canonical."""

    __tablename__ = "backtest_trade_outcome"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "trial_record_id", "ordinal", name="uq_backtest_outcome_ordinal"
        ),
        UniqueConstraint(
            "workspace_id", "trial_record_id", "trade_id", name="uq_backtest_outcome_trade"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["backtest_run.workspace_id", "backtest_run.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "trial_record_id"],
            ["backtest_trial.workspace_id", "backtest_trial.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("length(manifest_hash) = 64", name="backtest_outcome_manifest_sha256"),
        CheckConstraint("length(outcome_hash) = 64", name="backtest_outcome_sha256"),
        CheckConstraint("ordinal >= 0", name="backtest_outcome_ordinal_nonnegative"),
        CheckConstraint("jsonb_typeof(outcome) = 'object'", name="backtest_outcome_object"),
        Index(
            "ix_backtest_outcome_workspace_trial",
            "workspace_id",
            "trial_record_id",
            "ordinal",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trial_record_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    trade_id: Mapped[str] = mapped_column(String(240), nullable=False)
    outcome_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)

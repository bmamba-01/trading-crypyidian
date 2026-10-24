"""Bản ghi replay/backtest append-only, giới hạn theo workspace."""

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


class BacktestRunRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một manifest replay bất biến, định danh bằng hash nội dung."""

    __tablename__ = "backtest_run"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_backtest_run_workspace_id"),
        UniqueConstraint("workspace_id", "manifest_hash", name="uq_backtest_run_manifest"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        CheckConstraint("length(manifest_hash) = 64", name="backtest_run_manifest_sha256"),
        CheckConstraint("jsonb_typeof(manifest) = 'object'", name="backtest_run_manifest_object"),
        Index("ix_backtest_run_workspace_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_manifest: Mapped[str] = mapped_column(Text, nullable=False)


class BacktestTrialRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một lượt thử có trạng thái và số quan sát, kể cả replay bị từ chối."""

    __tablename__ = "backtest_trial"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_backtest_trial_workspace_id"),
        UniqueConstraint("workspace_id", "trial_id", name="uq_backtest_trial_trial_id"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["backtest_run.workspace_id", "backtest_run.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("length(trial_id) = 64", name="backtest_trial_id_sha256"),
        CheckConstraint("length(manifest_hash) = 64", name="backtest_trial_manifest_sha256"),
        CheckConstraint(
            "length(parameter_family_hash) = 64", name="backtest_trial_parameter_sha256"
        ),
        CheckConstraint("length(content_hash) = 64", name="backtest_trial_content_sha256"),
        CheckConstraint(
            "trial_count >= 1 AND trial_index >= 0 AND trial_index < trial_count",
            name="backtest_trial_index_valid",
        ),
        CheckConstraint(
            "observation_count >= 0 AND completed_observation_count >= 0 "
            "AND completed_observation_count <= observation_count AND trade_count >= 0",
            name="backtest_trial_counts_valid",
        ),
        CheckConstraint(
            "status IN ('COMPLETED', 'REJECTED', 'FAILED')", name="backtest_trial_status_valid"
        ),
        Index("ix_backtest_trial_workspace_run", "workspace_id", "run_id", "trial_index"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trial_id: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parameter_family_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trial_index: Mapped[int] = mapped_column(Integer, nullable=False)
    trial_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_trial: Mapped[str] = mapped_column(Text, nullable=False)


class BacktestReportRecord(UuidPrimaryKey, CreatedAt, Base):
    """Kết quả đánh giá bất biến được nối với đúng run và trial."""

    __tablename__ = "backtest_report"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_backtest_report_workspace_id"),
        UniqueConstraint("workspace_id", "report_hash", name="uq_backtest_report_hash"),
        UniqueConstraint("workspace_id", "trial_record_id", name="uq_backtest_report_trial"),
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
        CheckConstraint("length(manifest_hash) = 64", name="backtest_report_manifest_sha256"),
        CheckConstraint("length(report_hash) = 64", name="backtest_report_sha256"),
        CheckConstraint("jsonb_typeof(report) = 'object'", name="backtest_report_object"),
        CheckConstraint(
            "observation_count >= 0 AND usable_observation_count >= 0 "
            "AND usable_observation_count <= observation_count AND trade_count >= 0",
            name="backtest_report_counts_valid",
        ),
        CheckConstraint("coverage >= 0 AND coverage <= 1", name="backtest_report_coverage_valid"),
        Index("ix_backtest_report_workspace_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trial_record_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    usable_observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage: Mapped[float] = mapped_column(nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)

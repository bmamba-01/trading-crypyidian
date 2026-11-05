"""Bảng trạng thái job replay bền vững theo workspace."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UpdatedAt, UuidPrimaryKey


class BacktestReplayJobRecord(UuidPrimaryKey, CreatedAt, UpdatedAt, Base):
    """Yêu cầu replay có payload bất biến và trạng thái do worker cập nhật."""

    __tablename__ = "backtest_replay_job"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "requested_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_backtest_replay_job_requested_by_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "cancel_requested_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_backtest_replay_job_cancel_requested_by_membership",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="backtest_replay_job_status_valid",
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="backtest_replay_job_progress_valid"),
        CheckConstraint("attempt_count BETWEEN 0 AND 3", name="backtest_replay_job_attempts_valid"),
        CheckConstraint("lease_generation >= 0", name="backtest_replay_job_generation_valid"),
        CheckConstraint("bundle_hash ~ '^[0-9a-f]{64}$'", name="backtest_replay_job_bundle_sha256"),
        CheckConstraint(
            "jsonb_typeof(request_payload) = 'object'", name="backtest_replay_job_payload_object"
        ),
        CheckConstraint(
            "jsonb_typeof(result_run_ids) = 'array'", name="backtest_replay_job_results_array"
        ),
        CheckConstraint(
            "(cancel_requested AND cancel_requested_at IS NOT NULL "
            "AND cancel_requested_by IS NOT NULL) "
            "OR (NOT cancel_requested AND cancel_requested_at IS NULL "
            "AND cancel_requested_by IS NULL)",
            name="backtest_replay_job_cancel_evidence_consistent",
        ),
        CheckConstraint(
            "(status = 'RUNNING' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL) "
            "OR (status <> 'RUNNING' AND lease_owner IS NULL AND lease_until IS NULL)",
            name="backtest_replay_job_lease_consistent",
        ),
        Index("ix_backtest_replay_job_workspace_created", "workspace_id", "created_at"),
        Index("ix_backtest_replay_job_claim", "workspace_id", "status", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    requested_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_release_id: Mapped[str] = mapped_column(String(120), nullable=False)
    execution_interval: Mapped[str] = mapped_column(String(12), nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_request: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    stage: Mapped[str] = mapped_column(String(64), nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_run_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)

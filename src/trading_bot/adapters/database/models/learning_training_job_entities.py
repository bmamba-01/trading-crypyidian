"""Job queue bền vững gắn một trainer job với đúng một attempt."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UpdatedAt, UuidPrimaryKey


class LearningTrainingJobRecord(UuidPrimaryKey, CreatedAt, UpdatedAt, Base):
    """Mutable lease/status quanh attempt immutable đã được duyệt."""

    __tablename__ = "learning_training_job"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_training_attempt.workspace_id", "learning_training_attempt.id"],
            ondelete="RESTRICT",
            name="fk_learning_training_job_attempt",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "cancel_requested_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="RESTRICT",
            name="fk_learning_training_job_cancel_actor",
        ),
        UniqueConstraint(
            "workspace_id",
            "attempt_id",
            name="uq_learning_training_job_attempt",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="learning_training_job_status_valid",
        ),
        CheckConstraint(
            "stage IN ('queued', 'training', 'complete', 'failed', 'cancelled')",
            name="learning_training_job_stage_valid",
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="learning_training_job_progress_valid"),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 3",
            name="learning_training_job_attempt_count_valid",
        ),
        CheckConstraint("lease_generation >= 0", name="learning_training_job_generation_valid"),
        CheckConstraint(
            "(cancel_requested AND cancel_requested_at IS NOT NULL "
            "AND cancel_requested_by IS NOT NULL) "
            "OR (NOT cancel_requested AND cancel_requested_at IS NULL "
            "AND cancel_requested_by IS NULL)",
            name="learning_training_job_cancel_evidence_valid",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL) "
            "OR (status <> 'running' AND lease_owner IS NULL AND lease_until IS NULL)",
            name="learning_training_job_lease_consistent",
        ),
        Index(
            "ix_learning_training_job_claim",
            "workspace_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_learning_training_job_workspace_created",
            "workspace_id",
            "created_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    stage: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)


__all__ = ["LearningTrainingJobRecord"]

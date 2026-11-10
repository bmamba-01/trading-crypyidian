"""Bản ghi bất biến cho báo cáo WP06.4 chỉ dùng để chẩn đoán."""

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
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class LearningEvaluationReportRecord(UuidPrimaryKey, CreatedAt, Base):
    """Report canonical gắn workspace, manifest, fold plan và khóa retry."""

    __tablename__ = "learning_evaluation_report"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "manifest_hash"],
            ["dataset_manifest.workspace_id", "dataset_manifest.manifest_hash"],
            ondelete="RESTRICT",
            name="fk_learning_evaluation_report_manifest",
        ),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_evaluation_report_idempotency"
        ),
        UniqueConstraint("workspace_id", "id", name="uq_learning_evaluation_report_workspace"),
        CheckConstraint(
            "report_hash ~ '^[0-9a-f]{64}$' AND manifest_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_plan_hash ~ '^[0-9a-f]{64}$'",
            name="learning_evaluation_report_hashes_valid",
        ),
        CheckConstraint("seed >= 0", name="learning_evaluation_report_seed_nonnegative"),
        CheckConstraint(
            "evaluation_mode = 'diagnostic' AND candidate_completeness_verified IS FALSE",
            name="learning_evaluation_report_diagnostic_only",
        ),
        CheckConstraint(
            "length(split_policy_version) > 0 AND length(evaluator_version) > 0",
            name="learning_evaluation_report_versions_present",
        ),
        CheckConstraint(
            "jsonb_typeof(report) = 'object' AND length(canonical_report) > 0",
            name="learning_evaluation_report_payload_valid",
        ),
        CheckConstraint(
            "COALESCE(report ->> 'evaluation_mode', '') = 'diagnostic' "
            "AND COALESCE(report ->> 'candidate_completeness_verified', '') = 'false' "
            "AND COALESCE(report ->> 'manifest_hash', '') = manifest_hash "
            "AND COALESCE(report ->> 'fold_plan_hash', '') = fold_plan_hash",
            name="learning_evaluation_report_payload_diagnostic",
        ),
        Index("ix_learning_evaluation_report_workspace_created", "workspace_id", "created_at"),
        Index("ix_learning_evaluation_report_manifest", "workspace_id", "manifest_hash"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    split_policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluation_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    candidate_completeness_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evaluation_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    report: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    canonical_report: Mapped[str] = mapped_column(Text, nullable=False)

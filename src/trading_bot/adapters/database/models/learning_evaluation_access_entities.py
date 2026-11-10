"""Biên nhận bất biến cho lần evaluator đọc payload của một run."""

from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class LearningEvaluationAccessReceiptRecord(UuidPrimaryKey, CreatedAt, Base):
    """Receipt một lần, khóa request, manifest đã duyệt và principal evaluator."""

    __tablename__ = "learning_evaluation_access_receipt"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "manifest_hash"],
            ["dataset_manifest.workspace_id", "dataset_manifest.manifest_hash"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "evaluation_run_id",
            name="uq_learning_evaluation_access_receipt_run",
        ),
        UniqueConstraint(
            "workspace_id", "id", name="uq_learning_evaluation_access_receipt_workspace"
        ),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND approval_hash ~ '^[0-9a-f]{64}$' AND request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="learning_evaluation_access_receipt_hashes_valid",
        ),
        CheckConstraint(
            "purpose = 'diagnostic-evaluation' AND "
            "actor_principal = 'crypidian_learning_evaluator'",
            name="learning_evaluation_access_receipt_actor_purpose_valid",
        ),
        CheckConstraint(
            "length(split_policy_version) BETWEEN 1 AND 128",
            name="learning_evaluation_access_receipt_policy_valid",
        ),
        Index(
            "ix_learning_evaluation_access_receipt_manifest",
            "workspace_id",
            "manifest_hash",
            "created_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evaluation_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    split_policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_principal: Mapped[str] = mapped_column(String(80), nullable=False)


class LearningEvaluationAccessFoldRecord(UuidPrimaryKey, CreatedAt, Base):
    """Ghi nhận fold payload đã được phép đi qua receipt một lần."""

    __tablename__ = "learning_evaluation_access_fold"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "receipt_id"],
            [
                "learning_evaluation_access_receipt.workspace_id",
                "learning_evaluation_access_receipt.id",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "receipt_id",
            "fold_id",
            name="uq_learning_evaluation_access_fold_once",
        ),
        CheckConstraint(
            "fold_id ~ '^[0-9a-f]{64}$' AND payload_count > 0",
            name="learning_evaluation_access_fold_values_valid",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    receipt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    fold_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_count: Mapped[int] = mapped_column(Integer, nullable=False)

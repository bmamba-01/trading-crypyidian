"""Bản ghi bất biến cho artifact CPU trainer và process cost."""

import json
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


class LearningTrainingArtifactRecord(UuidPrimaryKey, CreatedAt, Base):
    """Canonical model payload gắn một lần chạy thành công và workspace."""

    __tablename__ = "learning_training_artifact"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_training_attempt.workspace_id", "learning_training_attempt.id"],
            ondelete="RESTRICT",
            name="fk_learning_training_artifact_attempt",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "normalized_dataset_id", "fold_id"],
            [
                "learning_fold_resource_profile.workspace_id",
                "learning_fold_resource_profile.normalized_dataset_id",
                "learning_fold_resource_profile.fold_id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_training_artifact_profile",
        ),
        UniqueConstraint(
            "workspace_id",
            "attempt_id",
            name="uq_learning_training_artifact_attempt",
        ),
        CheckConstraint(
            "artifact_hash ~ '^[0-9a-f]{64}$' AND manifest_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_plan_hash ~ '^[0-9a-f]{64}$' AND fold_id ~ '^[0-9a-f]{64}$' "
            "AND approval_hash ~ '^[0-9a-f]{64}$' AND batch_hash ~ '^[0-9a-f]{64}$' "
            "AND feature_schema_hash ~ '^[0-9a-f]{64}$' "
            "AND attempt_content_hash ~ '^[0-9a-f]{64}$'",
            name="learning_training_artifact_hashes_valid",
        ),
        CheckConstraint(
            "elapsed_milliseconds >= 0 AND cpu_time_milliseconds >= 0 "
            "AND peak_memory_bytes > 0 AND actual_operation_units > 0 AND process_id > 0",
            name="learning_training_artifact_measurements_valid",
        ),
        CheckConstraint(
            "length(artifact_schema_version) > 0 AND length(trainer_version) > 0 "
            "AND jsonb_typeof(artifact) = 'object' AND length(canonical_artifact) > 0",
            name="learning_training_artifact_payload_valid",
        ),
        CheckConstraint(
            "COALESCE(artifact ->> 'scorer_state', '') = 'abstain' "
            "AND COALESCE(artifact ->> 'calibrated', '') = 'false' "
            "AND NOT (artifact ? 'samples') AND NOT (artifact ? 'artifact_hash')",
            name="learning_training_artifact_diagnostic_only",
        ),
        Index(
            "ix_learning_training_artifact_workspace_created",
            "workspace_id",
            "created_at",
        ),
        Index("ix_learning_training_artifact_manifest", "workspace_id", "manifest_hash"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    normalized_dataset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    trainer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_id: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_artifact: Mapped[str] = mapped_column(Text, nullable=False)
    artifact: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    elapsed_milliseconds: Mapped[int] = mapped_column(Integer, nullable=False)
    cpu_time_milliseconds: Mapped[int] = mapped_column(Integer, nullable=False)
    peak_memory_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_operation_units: Mapped[int] = mapped_column(Integer, nullable=False)
    process_id: Mapped[int] = mapped_column(Integer, nullable=False)

    @classmethod
    def from_verified(
        cls,
        artifact: object,
    ) -> LearningTrainingArtifactRecord:
        """Chỉ materialize từ verifier application; không nhận JSON caller tự dựng."""
        from trading_bot.application.learning_training_artifact import (
            VerifiedLearningTrainingArtifact,
        )

        if type(artifact) is not VerifiedLearningTrainingArtifact:
            raise ValueError("Cần VerifiedLearningTrainingArtifact đã ràng buộc batch/process.")
        return cls(
            workspace_id=artifact.workspace_id,
            attempt_id=artifact.attempt_id,
            normalized_dataset_id=artifact.normalized_dataset_id,
            artifact_hash=artifact.artifact_hash,
            artifact_schema_version=artifact.artifact_schema_version,
            trainer_version=artifact.trainer_version,
            manifest_hash=artifact.manifest_hash,
            fold_plan_hash=artifact.fold_plan_hash,
            fold_id=artifact.fold_id,
            approval_hash=artifact.approval_hash,
            batch_hash=artifact.batch_hash,
            feature_schema_hash=artifact.feature_schema_hash,
            attempt_content_hash=artifact.attempt_content_hash,
            canonical_artifact=artifact.canonical_artifact,
            artifact=json.loads(artifact.canonical_artifact),
            elapsed_milliseconds=artifact.elapsed_milliseconds,
            cpu_time_milliseconds=artifact.cpu_time_milliseconds,
            peak_memory_bytes=artifact.peak_memory_bytes,
            actual_operation_units=artifact.actual_operation_units,
            process_id=artifact.process_id,
        )

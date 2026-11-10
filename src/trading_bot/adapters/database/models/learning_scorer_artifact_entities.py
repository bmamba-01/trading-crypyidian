"""ORM cho scorer artifact v2 append-only và metadata-only."""

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Float,
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

if TYPE_CHECKING:
    from trading_bot.application.learning_scorer_artifact import VerifiedLearningScorerArtifact


class LearningScorerArtifactRecord(UuidPrimaryKey, CreatedAt, Base):
    """Artifact calibration gắn với đúng base v1, attempt, fold và workspace."""

    __tablename__ = "learning_scorer_artifact"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_training_attempt.workspace_id", "learning_training_attempt.id"],
            ondelete="RESTRICT",
            name="fk_learning_scorer_artifact_attempt",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_training_artifact.workspace_id", "learning_training_artifact.attempt_id"],
            ondelete="RESTRICT",
            name="fk_learning_scorer_artifact_base_attempt",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "normalized_dataset_id", "fold_id"],
            [
                "learning_fold_resource_profile.workspace_id",
                "learning_fold_resource_profile.normalized_dataset_id",
                "learning_fold_resource_profile.fold_id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_scorer_artifact_profile",
        ),
        UniqueConstraint("workspace_id", "attempt_id", name="uq_learning_scorer_artifact_attempt"),
        UniqueConstraint("workspace_id", "id", name="uq_learning_scorer_artifact_workspace_id"),
        CheckConstraint(
            "schema_version = 'crypidian.learning.scorer-temperature.v2'",
            name="learning_scorer_artifact_schema_valid",
        ),
        CheckConstraint(
            "state IN ('ready_for_evaluation', 'abstain')",
            name="learning_scorer_artifact_state_valid",
        ),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_id ~ '^[0-9a-f]{64}$' AND approval_hash ~ '^[0-9a-f]{64}$' "
            "AND base_artifact_hash ~ '^[0-9a-f]{64}$' AND vector_schema_hash ~ '^[0-9a-f]{64}$' "
            "AND training_membership_digest ~ '^[0-9a-f]{64}$' "
            "AND validation_membership_digest ~ '^[0-9a-f]{64}$' "
            "AND dependency_fingerprint ~ '^[0-9a-f]{64}$' AND artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND (strategy_release_content_hash IS NULL OR "
            "strategy_release_content_hash ~ '^[0-9a-f]{64}$') "
            "AND (calibration_policy_hash IS NULL OR calibration_policy_hash ~ '^[0-9a-f]{64}$')",
            name="learning_scorer_artifact_hashes_valid",
        ),
        CheckConstraint(
            "training_sample_count > 0 AND validation_sample_count > 0 "
            "AND jsonb_typeof(validation_class_support) = 'array'",
            name="learning_scorer_artifact_counts_valid",
        ),
        CheckConstraint(
            "(state = 'ready_for_evaluation' AND reason_code IS NULL AND temperature > 0 "
            "AND strategy_release_id IS NOT NULL AND strategy_release_content_hash IS NOT NULL "
            "AND vector_schema_version IS NOT NULL AND calibration_policy_version IS NOT NULL "
            "AND calibration_policy_hash IS NOT NULL AND validation_nll_before IS NOT NULL "
            "AND validation_nll_after IS NOT NULL AND calibration_iterations > 0) OR "
            "(state = 'abstain' AND reason_code IS NOT NULL AND temperature IS NULL)",
            name="learning_scorer_artifact_readiness_valid",
        ),
        CheckConstraint(
            "calibrator_method = 'temperature_scaling' AND length(calibrator_version) > 0 "
            "AND length(trainer_version) > 0 AND length(trainer_code_version) > 0 "
            "AND jsonb_typeof(artifact) = 'object' AND length(canonical_artifact) > 0",
            name="learning_scorer_artifact_payload_valid",
        ),
        Index("ix_learning_scorer_artifact_workspace_created", "workspace_id", "created_at"),
        Index(
            "ix_learning_scorer_artifact_manifest_fold",
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
            "fold_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    base_artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    normalized_dataset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_id: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    strategy_release_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    strategy_release_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_schema_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vector_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    training_membership_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_membership_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    training_sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    validation_sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    calibrator_method: Mapped[str] = mapped_column(String(64), nullable=False)
    calibrator_version: Mapped[str] = mapped_column(String(80), nullable=False)
    calibration_policy_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    calibration_policy_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    validation_class_support: Mapped[list[list[int]]] = mapped_column(JSONB, nullable=False)
    validation_nll_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    validation_nll_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    calibration_iterations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trainer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    trainer_code_version: Mapped[str] = mapped_column(String(80), nullable=False)
    dependency_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_artifact: Mapped[str] = mapped_column(Text, nullable=False)
    artifact: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    @classmethod
    def from_verified(
        cls,
        verified: VerifiedLearningScorerArtifact,
        *,
        base_artifact_id: UUID,
    ) -> LearningScorerArtifactRecord:
        """Tạo row chỉ từ scorer v2 đã qua verifier process-boundary."""
        from trading_bot.application.learning_scorer_artifact import (
            VerifiedLearningScorerArtifact,
        )

        if (
            type(verified) is not VerifiedLearningScorerArtifact
            or type(base_artifact_id) is not UUID
        ):
            raise ValueError("Cần VerifiedLearningScorerArtifact và base artifact UUID.")
        artifact = verified.artifact
        payload = json.loads(verified.canonical_artifact)
        return cls(
            workspace_id=verified.workspace_id,
            attempt_id=verified.attempt_id,
            base_artifact_id=base_artifact_id,
            normalized_dataset_id=verified.normalized_dataset_id,
            manifest_hash=artifact.manifest_hash,
            fold_plan_hash=artifact.fold_plan_hash,
            fold_id=artifact.fold_id,
            approval_hash=artifact.approval_hash,
            base_artifact_hash=verified.base_artifact_hash,
            schema_version=artifact.schema_version,
            state=artifact.state,
            reason_code=artifact.reason_code,
            strategy_release_id=artifact.strategy_release_id,
            strategy_release_content_hash=artifact.strategy_release_content_hash,
            vector_schema_version=artifact.vector_schema_version,
            vector_schema_hash=artifact.vector_schema_hash,
            training_membership_digest=artifact.training_membership_digest,
            validation_membership_digest=artifact.validation_membership_digest,
            training_sample_count=artifact.training_sample_count,
            validation_sample_count=artifact.validation_sample_count,
            calibrator_method=artifact.calibrator_method,
            calibrator_version=artifact.calibrator_version,
            calibration_policy_version=artifact.calibration_policy_version,
            calibration_policy_hash=artifact.calibration_policy_hash,
            temperature=artifact.temperature,
            validation_class_support=[list(item) for item in artifact.validation_class_support],
            validation_nll_before=artifact.validation_nll_before,
            validation_nll_after=artifact.validation_nll_after,
            calibration_iterations=artifact.calibration_iterations,
            trainer_version=artifact.trainer_version,
            trainer_code_version=artifact.trainer_code_version,
            dependency_fingerprint=artifact.dependency_fingerprint,
            artifact_hash=verified.artifact_hash,
            canonical_artifact=verified.canonical_artifact,
            artifact=payload,
        )

"""ORM cho proof opportunity TEST của scorer, chỉ lưu metadata/hash."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey

if TYPE_CHECKING:
    from trading_bot.application.learning_scorer_evaluation import ScorerTestOpportunityProof


class LearningScorerTestOpportunityProofRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một inventory TEST duy nhất cho scorer attempt/fold; không có payload mẫu."""

    __tablename__ = "learning_scorer_test_opportunity_proof"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_training_attempt.workspace_id", "learning_training_attempt.id"],
            ondelete="RESTRICT",
            name="fk_learning_scorer_test_proof_attempt",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_scorer_artifact.workspace_id", "learning_scorer_artifact.attempt_id"],
            ondelete="RESTRICT",
            name="fk_learning_scorer_test_proof_artifact",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "manifest_hash", "fold_plan_hash", "normalized_dataset_id"],
            [
                "learning_normalized_dataset.workspace_id",
                "learning_normalized_dataset.manifest_hash",
                "learning_normalized_dataset.fold_plan_hash",
                "learning_normalized_dataset.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_scorer_test_proof_dataset",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "manifest_hash"],
            ["learning_split_approval.workspace_id", "learning_split_approval.manifest_hash"],
            ondelete="RESTRICT",
            name="fk_learning_scorer_test_proof_approval",
        ),
        UniqueConstraint(
            "workspace_id",
            "attempt_id",
            "fold_id",
            name="uq_learning_scorer_test_proof_attempt_fold",
        ),
        UniqueConstraint(
            "workspace_id",
            "evaluation_run_id",
            name="uq_learning_scorer_test_proof_evaluation_run",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_learning_scorer_test_proof_workspace_id"),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_id ~ '^[0-9a-f]{64}$' AND approval_hash ~ '^[0-9a-f]{64}$' "
            "AND bundle_hash ~ '^[0-9a-f]{64}$' AND vector_schema_hash ~ '^[0-9a-f]{64}$' "
            "AND test_membership_digest ~ '^[0-9a-f]{64}$' "
            "AND source_scope_hash ~ '^[0-9a-f]{64}$' AND proof_hash ~ '^[0-9a-f]{64}$'",
            name="learning_scorer_test_proof_hashes_valid",
        ),
        CheckConstraint(
            "length(btrim(vector_schema_version)) BETWEEN 1 AND 128 "
            "AND length(canonical_proof) > 0",
            name="learning_scorer_test_proof_payload_valid",
        ),
        CheckConstraint(
            "expected_opportunity_count > 0 AND candidate_count >= 0 "
            "AND no_candidate_count >= 0 AND input_excluded_count >= 0 "
            "AND source_gap_count >= 0 AND membership_mismatch_count >= 0 "
            "AND unlabeled_candidate_count >= 0 "
            "AND candidate_count + no_candidate_count + input_excluded_count "
            "+ membership_mismatch_count = expected_opportunity_count "
            "AND unlabeled_candidate_count <= candidate_count",
            name="learning_scorer_test_proof_counts_valid",
        ),
        CheckConstraint(
            "completeness_verified = (input_excluded_count = 0 AND source_gap_count = 0 "
            "AND membership_mismatch_count = 0 AND unlabeled_candidate_count = 0 "
            "AND candidate_count + no_candidate_count = expected_opportunity_count)",
            name="learning_scorer_test_proof_completeness_valid",
        ),
        Index(
            "ix_learning_scorer_test_proof_workspace_created",
            "workspace_id",
            "created_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evaluation_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    normalized_dataset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_id: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    vector_schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    vector_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    test_membership_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_opportunity_count: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    no_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    input_excluded_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_gap_count: Mapped[int] = mapped_column(Integer, nullable=False)
    membership_mismatch_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unlabeled_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completeness_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    proof_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_proof: Mapped[str] = mapped_column(Text, nullable=False)

    @classmethod
    def from_proof(
        cls,
        proof: ScorerTestOpportunityProof,
    ) -> LearningScorerTestOpportunityProofRecord:
        """Tạo row metadata từ proof đã được dựng trong repository authoritative."""
        from trading_bot.application.learning_scorer_evaluation import (
            ScorerTestOpportunityProof,
        )

        if type(proof) is not ScorerTestOpportunityProof:
            raise ValueError("Cần ScorerTestOpportunityProof đã được xác minh.")
        return cls(
            id=proof.record_id,
            workspace_id=proof.workspace_id,
            evaluation_run_id=proof.evaluation_run_id,
            attempt_id=proof.attempt_id,
            normalized_dataset_id=proof.normalized_dataset_id,
            manifest_hash=proof.manifest_hash,
            fold_plan_hash=proof.fold_plan_hash,
            fold_id=proof.fold_id,
            approval_hash=proof.approval_hash,
            bundle_hash=proof.bundle_hash,
            vector_schema_version=proof.vector_schema_version,
            vector_schema_hash=proof.vector_schema_hash,
            test_membership_digest=proof.test_membership_digest,
            source_scope_hash=proof.source_scope_hash,
            expected_opportunity_count=proof.expected_opportunity_count,
            candidate_count=proof.candidate_count,
            no_candidate_count=proof.no_candidate_count,
            input_excluded_count=proof.input_excluded_count,
            source_gap_count=proof.source_gap_count,
            membership_mismatch_count=proof.membership_mismatch_count,
            unlabeled_candidate_count=proof.unlabeled_candidate_count,
            completeness_verified=proof.completeness_verified,
            proof_hash=proof.content_hash,
            canonical_proof=proof.canonical_json,
        )


__all__ = ["LearningScorerTestOpportunityProofRecord"]

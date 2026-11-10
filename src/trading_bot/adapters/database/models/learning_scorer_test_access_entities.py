"""ORM cho receipt TEST one-shot và report scorer aggregate-only."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
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

if TYPE_CHECKING:
    from trading_bot.application.learning_scorer_evaluation import ScorerTestEvaluationReport


class LearningScorerTestAccessReceiptRecord(UuidPrimaryKey, CreatedAt, Base):
    """Biên nhận đã commit, chỉ ghim scope TEST và không chứa payload."""

    __tablename__ = "learning_scorer_test_access_receipt"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_training_attempt.workspace_id", "learning_training_attempt.id"],
            ondelete="RESTRICT",
            name="fk_learning_scorer_test_access_attempt",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_scorer_artifact.workspace_id", "learning_scorer_artifact.attempt_id"],
            ondelete="RESTRICT",
            name="fk_learning_scorer_test_access_artifact",
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
            name="fk_learning_scorer_test_access_dataset",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "manifest_hash"],
            ["learning_split_approval.workspace_id", "learning_split_approval.manifest_hash"],
            ondelete="RESTRICT",
            name="fk_learning_scorer_test_access_approval",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "proof_id"],
            [
                "learning_scorer_test_opportunity_proof.workspace_id",
                "learning_scorer_test_opportunity_proof.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_scorer_test_access_proof",
        ),
        UniqueConstraint(
            "workspace_id",
            "attempt_id",
            "fold_id",
            name="uq_learning_scorer_test_access_attempt_fold",
        ),
        UniqueConstraint(
            "workspace_id",
            "evaluation_run_id",
            name="uq_learning_scorer_test_access_evaluation_run",
        ),
        UniqueConstraint(
            "workspace_id",
            "evaluation_run_id",
            "id",
            "proof_id",
            name="uq_learning_scorer_test_access_run_receipt_proof",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_learning_scorer_test_access_workspace_id"),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_id ~ '^[0-9a-f]{64}$' AND approval_hash ~ '^[0-9a-f]{64}$' "
            "AND bundle_hash ~ '^[0-9a-f]{64}$' "
            "AND test_membership_digest ~ '^[0-9a-f]{64}$' "
            "AND source_scope_hash ~ '^[0-9a-f]{64}$' "
            "AND proof_hash ~ '^[0-9a-f]{64}$' "
            "AND scorer_artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND policy_hash ~ '^[0-9a-f]{64}$' "
            "AND current_rights_receipt_hash ~ '^[0-9a-f]{64}$' "
            "AND request_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND receipt_hash ~ '^[0-9a-f]{64}$'",
            name="learning_scorer_test_access_hashes_valid",
        ),
        CheckConstraint(
            "purpose = 'scorer-test' AND "
            "actor_principal = 'crypidian_learning_evaluator' "
            "AND length(vector_schema_version) BETWEEN 1 AND 128",
            name="learning_scorer_test_access_actor_purpose_valid",
        ),
        Index(
            "ix_learning_scorer_test_access_workspace_created",
            "workspace_id",
            "created_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evaluation_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    normalized_dataset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    proof_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_id: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    vector_schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    vector_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    test_membership_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proof_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scorer_artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_rights_receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_principal: Mapped[str] = mapped_column(String(80), nullable=False)


class LearningScorerTestPayloadConsumptionRecord(UuidPrimaryKey, CreatedAt, Base):
    """Dấu tiêu thụ một lần; chỉ metadata, không ghi hàng TEST."""

    __tablename__ = "learning_scorer_test_payload_consumption"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "receipt_id"],
            [
                "learning_scorer_test_access_receipt.workspace_id",
                "learning_scorer_test_access_receipt.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_scorer_test_consumption_receipt",
        ),
        UniqueConstraint(
            "workspace_id",
            "receipt_id",
            "id",
            name="uq_learning_scorer_test_consumption_receipt_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "receipt_id",
            name="uq_learning_scorer_test_consumption_receipt",
        ),
        CheckConstraint("payload_count > 0", name="learning_scorer_test_consumption_count_valid"),
        CheckConstraint(
            "membership_digest ~ '^[0-9a-f]{64}$' "
            "AND payload_commitment_hash ~ '^[0-9a-f]{64}$' "
            "AND consumption_hash ~ '^[0-9a-f]{64}$' "
            "AND current_rights_receipt_hash ~ '^[0-9a-f]{64}$' "
            "AND request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="learning_scorer_test_consumption_hashes_valid",
        ),
        Index(
            "ix_learning_scorer_test_consumption_workspace_created",
            "workspace_id",
            "created_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    receipt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payload_count: Mapped[int] = mapped_column(Integer, nullable=False)
    membership_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_commitment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    consumption_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_rights_receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LearningScorerTestEvaluationReportRecord(UuidPrimaryKey, CreatedAt, Base):
    """Report OOS aggregate-only gắn đúng proof/receipt/artifact/policy."""

    __tablename__ = "learning_scorer_test_evaluation_report"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "evaluation_run_id", "access_receipt_id", "proof_id"],
            [
                "learning_scorer_test_access_receipt.workspace_id",
                "learning_scorer_test_access_receipt.evaluation_run_id",
                "learning_scorer_test_access_receipt.id",
                "learning_scorer_test_access_receipt.proof_id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_scorer_test_report_exact_receipt_proof",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "access_receipt_id"],
            [
                "learning_scorer_test_access_receipt.workspace_id",
                "learning_scorer_test_access_receipt.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_scorer_test_report_receipt",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "access_receipt_id", "payload_consumption_id"],
            [
                "learning_scorer_test_payload_consumption.workspace_id",
                "learning_scorer_test_payload_consumption.receipt_id",
                "learning_scorer_test_payload_consumption.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_scorer_test_report_consumption",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "proof_id"],
            [
                "learning_scorer_test_opportunity_proof.workspace_id",
                "learning_scorer_test_opportunity_proof.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_scorer_test_report_proof",
        ),
        UniqueConstraint(
            "workspace_id",
            "evaluation_run_id",
            name="uq_learning_scorer_test_report_evaluation_run",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_learning_scorer_test_report_workspace_id"),
        CheckConstraint(
            "evaluation_status IN ('INCONCLUSIVE','ACCEPTED_SHADOW_ONLY',"
            "'ACCEPTED_PAPER_GATE_ELIGIBLE')",
            name="learning_scorer_test_report_status_valid",
        ),
        CheckConstraint(
            "report_hash ~ '^[0-9a-f]{64}$' AND request_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND proof_hash ~ '^[0-9a-f]{64}$' "
            "AND scorer_artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND base_artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND policy_hash ~ '^[0-9a-f]{64}$' "
            "AND access_receipt_hash ~ '^[0-9a-f]{64}$' "
            "AND current_rights_receipt_hash ~ '^[0-9a-f]{64}$' "
            "AND payload_commitment_hash ~ '^[0-9a-f]{64}$' "
            "AND payload_consumption_hash ~ '^[0-9a-f]{64}$'",
            name="learning_scorer_test_report_hashes_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(report) = 'object' AND length(canonical_report) > 0 "
            "AND NOT public.wp06_scorer_test_report_has_forbidden_payload(report)",
            name="learning_scorer_test_report_sanitized_valid",
        ),
        CheckConstraint(
            "evaluation_status <> 'ACCEPTED_PAPER_GATE_ELIGIBLE' OR "
            "(candidate_completeness_verified AND scored_candidate_coverage_verified)",
            name="learning_scorer_test_report_paper_gate_valid",
        ),
        Index(
            "ix_learning_scorer_test_report_workspace_created",
            "workspace_id",
            "created_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evaluation_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    access_receipt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payload_consumption_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    proof_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    access_receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_rights_receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_commitment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_consumption_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    proof_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scorer_artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    evaluation_status: Mapped[str] = mapped_column(String(40), nullable=False)
    candidate_completeness_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    scored_candidate_coverage_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    test_sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    opportunity_count: Mapped[int] = mapped_column(Integer, nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_report: Mapped[str] = mapped_column(Text, nullable=False)

    @classmethod
    def from_domain(
        cls,
        report: ScorerTestEvaluationReport,
        *,
        request_fingerprint: str,
    ) -> LearningScorerTestEvaluationReportRecord:
        """Tạo row từ report aggregate-only có canonical hash hợp lệ."""
        from trading_bot.application.learning_scorer_evaluation import (
            ScorerTestEvaluationReport,
        )

        if type(report) is not ScorerTestEvaluationReport:
            raise ValueError("Cần report scorer TEST typed.")
        canonical = report.canonical_json
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != report.report_hash:
            raise ValueError("Hash report scorer TEST không khớp canonical bytes.")
        payload = json.loads(canonical)
        if _contains_forbidden_payload_key(
            payload,
            {
                "sample_ids",
                "sample_id",
                "features",
                "labels",
                "outcomes",
                "trade_rows",
                "trade_id",
                "test_payload",
            },
        ):
            raise ValueError("Report scorer TEST không được chứa payload theo hàng.")
        if len(request_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in request_fingerprint
        ):
            raise ValueError("request_fingerprint phải là SHA-256.")
        if report.proof_hash is None or report.opportunity_proof_id is None:
            raise ValueError("Report scorer TEST cần proof đã persist.")
        if (
            report.test_access_receipt_id is None
            or report.test_payload_consumption_id is None
            or report.test_access_receipt_hash is None
            or report.test_current_rights_receipt_hash is None
            or report.test_payload_consumption_hash is None
            or report.test_payload_commitment_hash is None
            or report.test_payload_consumed_at is None
        ):
            raise ValueError("Report scorer TEST phải ghim receipt và consumption đã commit.")
        return cls(
            workspace_id=report.workspace_id,
            evaluation_run_id=report.evaluation_run_id,
            access_receipt_id=report.test_access_receipt_id,
            payload_consumption_id=report.test_payload_consumption_id,
            proof_id=report.opportunity_proof_id,
            report_hash=report.report_hash,
            request_fingerprint=request_fingerprint,
            access_receipt_hash=report.test_access_receipt_hash,
            current_rights_receipt_hash=report.test_current_rights_receipt_hash,
            payload_commitment_hash=report.test_payload_commitment_hash,
            payload_consumption_hash=report.test_payload_consumption_hash,
            payload_consumed_at=report.test_payload_consumed_at,
            proof_hash=report.proof_hash,
            scorer_artifact_hash=report.scorer_artifact_hash,
            base_artifact_hash=report.base_artifact_hash,
            policy_hash=report.policy_hash,
            policy_version=report.policy_version,
            evaluation_status=report.status,
            candidate_completeness_verified=report.candidate_completeness_verified,
            scored_candidate_coverage_verified=report.scored_candidate_coverage_verified,
            test_sample_count=report.test_sample_count,
            opportunity_count=report.opportunity_count,
            report=payload,
            canonical_report=canonical,
        )


def _contains_forbidden_payload_key(value: object, forbidden: set[str]) -> bool:
    """Từ chối khóa payload ở mọi tầng của JSON report."""
    if isinstance(value, dict):
        return any(
            key in forbidden or _contains_forbidden_payload_key(item, forbidden)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_payload_key(item, forbidden) for item in value)
    return False


__all__ = [
    "LearningScorerTestAccessReceiptRecord",
    "LearningScorerTestEvaluationReportRecord",
    "LearningScorerTestPayloadConsumptionRecord",
]

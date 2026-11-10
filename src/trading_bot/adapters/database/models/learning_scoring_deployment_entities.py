"""ORM cho lịch sử deployment scorer và projection hiện hành."""

from __future__ import annotations

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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey

if TYPE_CHECKING:
    from trading_bot.domain.learning.scoring_deployment import ScorerDeploymentEvent


class LearningScoringDeploymentEventRecord(UuidPrimaryKey, CreatedAt, Base):
    """Sự kiện scorer append-only, chỉ chứa lineage và quyền đã kiểm tra."""

    __tablename__ = "learning_scoring_deployment_event"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            ondelete="RESTRICT",
            name="fk_learning_scoring_deployment_registry",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "scorer_artifact_id"],
            ["learning_scorer_artifact.workspace_id", "learning_scorer_artifact.id"],
            ondelete="RESTRICT",
            name="fk_learning_scoring_deployment_artifact",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "evaluation_report_id"],
            [
                "learning_scorer_test_evaluation_report.workspace_id",
                "learning_scorer_test_evaluation_report.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_scoring_deployment_report",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "proof_id"],
            [
                "learning_scorer_test_opportunity_proof.workspace_id",
                "learning_scorer_test_opportunity_proof.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_scoring_deployment_proof",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "actor_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="RESTRICT",
            name="fk_learning_scoring_deployment_actor",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "previous_event_id"],
            [
                "learning_scoring_deployment_event.workspace_id",
                "learning_scoring_deployment_event.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_scoring_deployment_previous",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_learning_scoring_deployment_workspace_id"),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_scoring_deployment_key"
        ),
        UniqueConstraint(
            "workspace_id", "revision", name="uq_learning_scoring_deployment_revision"
        ),
        CheckConstraint(
            "mode IN ('DISABLED','SHADOW','PAPER_GATE')",
            name="learning_scoring_deployment_mode_valid",
        ),
        CheckConstraint(
            "revision >= 1 AND expected_pointer_version >= 0 "
            "AND expected_deployment_revision >= 0 "
            "AND revision = expected_deployment_revision + 1",
            name="learning_scoring_deployment_revision_valid",
        ),
        CheckConstraint(
            "mfa_method = 'TOTP' AND mfa_verified_at IS NOT NULL",
            name="learning_scoring_deployment_mfa_valid",
        ),
        CheckConstraint(
            "length(btrim(reason)) BETWEEN 12 AND 2000 "
            "AND event_hash ~ '^[0-9a-f]{64}$' "
            "AND intent_hash ~ '^[0-9a-f]{64}$' "
            "AND (current_rights_receipt_hash IS NULL "
            "OR current_rights_receipt_hash ~ '^[0-9a-f]{64}$')",
            name="learning_scoring_deployment_audit_valid",
        ),
        CheckConstraint(
            "(previous_event_id IS NULL AND previous_event_hash IS NULL) OR "
            "(previous_event_id IS NOT NULL AND previous_event_hash ~ '^[0-9a-f]{64}$')",
            name="learning_scoring_deployment_previous_pair_valid",
        ),
        CheckConstraint(
            "(mode = 'DISABLED' AND registry_id IS NULL AND registry_revision IS NULL "
            "AND scorer_artifact_id IS NULL AND scorer_artifact_hash IS NULL "
            "AND evaluation_report_id IS NULL AND evaluation_report_hash IS NULL "
            "AND evaluation_status IS NULL AND proof_id IS NULL AND proof_hash IS NULL "
            "AND candidate_completeness_verified IS NULL AND report_scope_hash IS NULL "
            "AND proof_scope_hash IS NULL AND policy_id IS NULL AND policy_hash IS NULL "
            "AND current_rights_receipt_hash IS NULL) OR "
            "(mode IN ('SHADOW','PAPER_GATE') AND registry_id IS NOT NULL "
            "AND registry_revision >= 1 AND scorer_artifact_id IS NOT NULL "
            "AND scorer_artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND evaluation_report_id IS NOT NULL "
            "AND evaluation_report_hash ~ '^[0-9a-f]{64}$' "
            "AND evaluation_status IN ('ACCEPTED_SHADOW_ONLY',"
            "'ACCEPTED_PAPER_GATE_ELIGIBLE') AND proof_id IS NOT NULL "
            "AND proof_hash ~ '^[0-9a-f]{64}$' "
            "AND candidate_completeness_verified IS NOT NULL "
            "AND report_scope_hash ~ '^[0-9a-f]{64}$' "
            "AND proof_scope_hash = report_scope_hash "
            "AND policy_id IS NOT NULL AND policy_hash ~ '^[0-9a-f]{64}$' "
            "AND current_rights_receipt_hash ~ '^[0-9a-f]{64}$')",
            name="learning_scoring_deployment_target_valid",
        ),
        CheckConstraint(
            "mode <> 'PAPER_GATE' OR "
            "(evaluation_status = 'ACCEPTED_PAPER_GATE_ELIGIBLE' "
            "AND candidate_completeness_verified IS TRUE)",
            name="learning_scoring_deployment_paper_gate_valid",
        ),
        CheckConstraint(
            "event_payload ? 'schema_version' "
            "AND NOT (event_payload ?| ARRAY['features','feature_values','labels',"
            "'sample_ids','test_payload','raw_source'])",
            name="learning_scoring_deployment_metadata_only",
        ),
        Index(
            "ix_learning_scoring_deployment_workspace_created",
            "workspace_id",
            "created_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    registry_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    registry_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_pointer_version: Mapped[int] = mapped_column(Integer, nullable=False)
    scorer_artifact_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    scorer_artifact_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluation_report_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    evaluation_report_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluation_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    proof_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    proof_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_completeness_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    report_scope_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    proof_scope_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    policy_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_rights_receipt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mfa_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mfa_method: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_pointer_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_deployment_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    intent_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_event_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    previous_event_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_event: Mapped[str] = mapped_column(Text, nullable=False)
    event_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    @classmethod
    def from_domain(cls, event: ScorerDeploymentEvent) -> LearningScoringDeploymentEventRecord:
        """Dựng event row từ hợp đồng đã kiểm chứng."""
        from trading_bot.domain.learning.scoring_deployment import ScorerDeploymentEvent

        if type(event) is not ScorerDeploymentEvent:
            raise ValueError("event phải là ScorerDeploymentEvent.")
        target = event.target
        return cls(
            id=event.event_id,
            created_at=event.created_at,
            workspace_id=event.workspace_id,
            revision=event.revision,
            mode=event.mode.value,
            registry_id=target.registry_id if target else None,
            registry_revision=target.registry_revision if target else None,
            active_pointer_version=event.expected_pointer_version,
            scorer_artifact_id=target.scorer_artifact_id if target else None,
            scorer_artifact_hash=target.scorer_artifact_hash if target else None,
            evaluation_report_id=target.evaluation_report_id if target else None,
            evaluation_report_hash=target.evaluation_report_hash if target else None,
            evaluation_status=target.evaluation_status if target else None,
            proof_id=target.proof_id if target else None,
            proof_hash=target.proof_hash if target else None,
            candidate_completeness_verified=(
                target.candidate_completeness_verified if target else None
            ),
            report_scope_hash=target.report_scope_hash if target else None,
            proof_scope_hash=target.proof_scope_hash if target else None,
            policy_id=target.policy_id if target else None,
            policy_hash=target.policy_hash if target else None,
            current_rights_receipt_hash=target.current_rights_receipt_hash if target else None,
            actor_membership_id=event.actor_membership_id,
            mfa_verified_at=event.mfa_verified_at,
            mfa_method=event.mfa_method,
            expected_pointer_version=event.expected_pointer_version,
            expected_deployment_revision=event.expected_deployment_revision,
            reason=event.reason,
            idempotency_key=event.idempotency_key,
            intent_hash=event.intent_hash,
            previous_event_id=event.previous_event_id,
            previous_event_hash=event.previous_event_hash,
            event_hash=event.event_hash,
            canonical_event=event.canonical_json,
            event_payload=event.canonical_payload,
        )


class LearningScoringDeploymentPointerRecord(Base):
    """Projection workspace-scoped; chỉ event guard được cập nhật."""

    __tablename__ = "learning_scoring_deployment_pointer"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "event_id"],
            [
                "learning_scoring_deployment_event.workspace_id",
                "learning_scoring_deployment_event.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_scoring_deployment_pointer_event",
        ),
        CheckConstraint(
            "revision >= 0 AND active_pointer_version >= 0",
            name="learning_scoring_deployment_pointer_versions_valid",
        ),
        CheckConstraint(
            "mode IN ('DISABLED','SHADOW','PAPER_GATE')",
            name="learning_scoring_deployment_pointer_mode_valid",
        ),
        CheckConstraint(
            "(revision = 0 AND event_id IS NULL AND mode = 'DISABLED') OR "
            "(revision > 0 AND event_id IS NOT NULL)",
            name="learning_scoring_deployment_pointer_initial_valid",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    event_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_pointer_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="DISABLED")
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "LearningScoringDeploymentEventRecord",
    "LearningScoringDeploymentPointerRecord",
]

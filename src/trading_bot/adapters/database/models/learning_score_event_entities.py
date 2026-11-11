"""ORM cho score event runtime metadata-only, workspace-scoped và append-only."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
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
from trading_bot.domain.learning.score_event import LearningScoreEvent


class LearningScoreEventRecord(UuidPrimaryKey, CreatedAt, Base):
    """Metadata score đã ghim vào quyết định; không lưu feature/vector payload."""

    __tablename__ = "learning_score_event"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "bot_decision_id"],
            ["bot_decision.workspace_id", "bot_decision.bot_id", "bot_decision.id"],
            ondelete="RESTRICT",
            name="fk_learning_score_event_bot_decision",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            ondelete="RESTRICT",
            name="fk_learning_score_event_registry",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "scorer_artifact_id"],
            ["learning_scorer_artifact.workspace_id", "learning_scorer_artifact.id"],
            ondelete="RESTRICT",
            name="fk_learning_score_event_artifact",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "evaluation_report_id"],
            [
                "learning_scorer_test_evaluation_report.workspace_id",
                "learning_scorer_test_evaluation_report.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_score_event_report",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "proof_id"],
            [
                "learning_scorer_test_opportunity_proof.workspace_id",
                "learning_scorer_test_opportunity_proof.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_score_event_proof",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "deployment_event_id"],
            [
                "learning_scoring_deployment_event.workspace_id",
                "learning_scoring_deployment_event.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_score_event_deployment",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_learning_score_event_workspace_id"),
        UniqueConstraint(
            "workspace_id", "bot_decision_id", name="uq_learning_score_event_decision"
        ),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_score_event_idempotency"
        ),
        CheckConstraint(
            "mode IN ('SHADOW','PAPER_GATE') AND decision IN ('APPROVE','VETO','ABSTAIN') "
            "AND incumbent_status = 'CANDIDATE' "
            "AND incumbent_status IN ('CANDIDATE','ABSTAIN','VETO') "
            "AND final_status IN ('CANDIDATE','ABSTAIN','VETO') "
            "AND (mode <> 'SHADOW' OR bot_decision_hash = incumbent_decision_hash)",
            name="learning_score_event_state_valid",
        ),
        CheckConstraint(
            "evaluation_status IN ('ACCEPTED_SHADOW_ONLY','ACCEPTED_PAPER_GATE_ELIGIBLE') "
            "AND (mode <> 'PAPER_GATE' OR "
            "(evaluation_status = 'ACCEPTED_PAPER_GATE_ELIGIBLE' "
            "AND candidate_completeness_verified IS TRUE))",
            name="learning_score_event_eligibility_valid",
        ),
        CheckConstraint(
            "deployment_revision >= 1 AND registry_revision >= 1 "
            "AND active_pointer_version >= 0 AND model_latency_ms >= 0",
            name="learning_score_event_versions_valid",
        ),
        CheckConstraint(
            "bot_decision_hash ~ '^[0-9a-f]{64}$' "
            "AND incumbent_decision_hash ~ '^[0-9a-f]{64}$' "
            "AND candidate_id ~ '^[0-9a-f]{64}$' AND candidate_hash ~ '^[0-9a-f]{64}$' "
            "AND decision_snapshot_id ~ '^[0-9a-f]{64}$' "
            "AND vector_hash ~ '^[0-9a-f]{64}$' "
            "AND feature_schema_hash ~ '^[0-9a-f]{64}$' "
            "AND source_rights_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND scorer_artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND base_artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND evaluation_report_hash ~ '^[0-9a-f]{64}$' "
            "AND report_scope_hash ~ '^[0-9a-f]{64}$' "
            "AND proof_hash ~ '^[0-9a-f]{64}$' AND proof_scope_hash = report_scope_hash "
            "AND policy_hash ~ '^[0-9a-f]{64}$' "
            "AND event_hash ~ '^[0-9a-f]{64}$' AND idempotency_key ~ '^[0-9a-f]{64}$'",
            name="learning_score_event_hashes_valid",
        ),
        CheckConstraint(
            "length(btrim(feature_schema_version)) > 0 AND length(btrim(policy_id)) > 0 "
            "AND model_latency_ms >= 0 AND jsonb_typeof(timeframe_snapshot_ids) = 'array' "
            "AND jsonb_array_length(timeframe_snapshot_ids) > 0 "
            "AND jsonb_typeof(source_event_digests) = 'array' "
            "AND jsonb_array_length(source_event_digests) > 0 "
            "AND jsonb_typeof(class_probabilities) = 'array'",
            name="learning_score_event_metadata_valid",
        ),
        CheckConstraint(
            "(decision = 'ABSTAIN' AND reason_code IS NOT NULL "
            "AND class_probabilities = '[]'::jsonb AND probability IS NULL "
            "AND confidence IS NULL AND uncertainty IS NULL) OR "
            "(decision IN ('APPROVE','VETO') AND "
            "(decision = 'APPROVE' AND reason_code IS NULL OR "
            "decision = 'VETO' AND reason_code IS NOT NULL) "
            "AND jsonb_array_length(class_probabilities) = 3 "
            "AND probability BETWEEN 0 AND 1 AND confidence BETWEEN 0 AND 1 "
            "AND uncertainty BETWEEN 0 AND 1)",
            name="learning_score_event_result_valid",
        ),
        CheckConstraint(
            "public.wp06_score_event_result_is_valid("
            "mode,decision,final_status,class_probabilities,probability,confidence,uncertainty)",
            name="learning_score_event_contract_consistent",
        ),
        CheckConstraint(
            "event_payload->>'schema_version' = 'crypidian.learning.score-event.v1' "
            "AND NOT (event_payload ?| ARRAY['features','feature_values','labels',"
            "'sample_ids','test_payload','raw_source','candidate_json'])",
            name="learning_score_event_payload_metadata_only",
        ),
        Index(
            "ix_learning_score_event_workspace_created",
            "workspace_id",
            "created_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    incumbent_decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    incumbent_status: Mapped[str] = mapped_column(String(16), nullable=False)
    final_status: Mapped[str] = mapped_column(String(16), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    vector_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    feature_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe_snapshot_ids: Mapped[list[list[str]]] = mapped_column(JSONB, nullable=False)
    source_event_digests: Mapped[list[list[str]]] = mapped_column(JSONB, nullable=False)
    source_rights_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    deployment_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    deployment_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    registry_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    registry_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    active_pointer_version: Mapped[int] = mapped_column(Integer, nullable=False)
    scorer_artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    scorer_artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluation_report_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evaluation_report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluation_status: Mapped[str] = mapped_column(String(40), nullable=False)
    report_scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proof_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    proof_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proof_scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_completeness_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    policy_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    class_probabilities: Mapped[list[list[float | int]]] = mapped_column(JSONB, nullable=False)
    probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    uncertainty: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_event: Mapped[str] = mapped_column(Text, nullable=False)
    event_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    @classmethod
    def from_domain(cls, event: LearningScoreEvent) -> LearningScoreEventRecord:
        """Dựng row từ contract đã kiểm tra và giữ nguyên canonical bytes."""
        if type(event) is not LearningScoreEvent:
            raise ValueError("event phải là LearningScoreEvent.")
        payload = event.canonical_payload()
        return cls(
            id=event.score_event_id,
            workspace_id=event.workspace_id,
            bot_id=event.bot_id,
            bot_decision_id=event.bot_decision_id,
            bot_decision_hash=event.bot_decision_hash,
            incumbent_decision_hash=event.incumbent_decision_hash,
            incumbent_status=event.incumbent_status,
            final_status=event.final_status,
            candidate_id=event.candidate_id,
            candidate_hash=event.candidate_hash,
            decision_snapshot_id=event.decision_snapshot_id,
            vector_hash=event.vector_hash,
            feature_schema_version=event.feature_schema_version,
            feature_schema_hash=event.feature_schema_hash,
            timeframe_snapshot_ids=[list(item) for item in event.timeframe_snapshot_ids],
            source_event_digests=[list(item) for item in event.source_event_digests],
            source_rights_fingerprint=event.source_rights_fingerprint,
            mode=event.mode.value,
            deployment_event_id=event.deployment_event_id,
            deployment_revision=event.deployment_revision,
            registry_id=event.registry_id,
            registry_revision=event.registry_revision,
            active_pointer_version=event.active_pointer_version,
            scorer_artifact_id=event.scorer_artifact_id,
            scorer_artifact_hash=event.scorer_artifact_hash,
            base_artifact_hash=event.base_artifact_hash,
            evaluation_report_id=event.evaluation_report_id,
            evaluation_report_hash=event.evaluation_report_hash,
            evaluation_status=event.evaluation_status,
            report_scope_hash=event.report_scope_hash,
            proof_id=event.proof_id,
            proof_hash=event.proof_hash,
            proof_scope_hash=event.proof_scope_hash,
            candidate_completeness_verified=event.candidate_completeness_verified,
            policy_id=event.policy_id,
            policy_hash=event.policy_hash,
            decision=event.decision.value,
            reason_code=event.reason_code,
            class_probabilities=[list(item) for item in event.class_probabilities],
            probability=event.probability,
            confidence=event.confidence,
            uncertainty=event.uncertainty,
            model_latency_ms=event.model_latency_ms,
            idempotency_key=event.idempotency_key,
            event_hash=event.event_hash,
            canonical_event=event.canonical_json,
            event_payload=payload,
        )

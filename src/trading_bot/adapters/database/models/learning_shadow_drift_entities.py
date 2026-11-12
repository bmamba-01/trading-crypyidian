"""Bản ghi metadata bất biến cho quan sát shadow, drift và đề xuất xem xét huấn luyện."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class LearningShadowObservationRecord(UuidPrimaryKey, CreatedAt, Base):
    """Lineage PAPER/challenger đã ghim; không chứa vector, score hoặc payload quyết định."""

    __tablename__ = "learning_shadow_observation"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "incumbent_decision_id"],
            ["bot_decision.workspace_id", "bot_decision.id"],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_decision",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "snapshot_id"],
            ["feature_snapshot.workspace_id", "feature_snapshot.snapshot_id"],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_snapshot",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "challenger_registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_registry",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "active_registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_active_registry",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "rights_source_id", "rights_policy_sequence"],
            [
                "source_policy_revision.workspace_id",
                "source_policy_revision.source_id",
                "source_policy_revision.sequence",
            ],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_rights_policy_sequence",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "rights_source_id", "rights_policy_sequence"],
            [
                "source_policy_observation.workspace_id",
                "source_policy_observation.source_id",
                "source_policy_observation.sequence",
            ],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_rights_receipt_sequence",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "rights_policy_id"],
            ["source_policy_revision.workspace_id", "source_policy_revision.policy_id"],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_rights_policy_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "rights_policy_id"],
            ["source_policy_observation.workspace_id", "source_policy_observation.policy_id"],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_rights_receipt_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "challenger_registry_id", "registry_approval_event_id"],
            [
                "learning_model_registry_event.workspace_id",
                "learning_model_registry_event.registry_id",
                "learning_model_registry_event.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_registry_approval",
        ),
        ForeignKeyConstraint(
            ["challenger_artifact_id"],
            ["learning_training_artifact.id"],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_artifact",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_learning_shadow_observation_workspace_id"),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_shadow_observation_idempotency"
        ),
        UniqueConstraint(
            "workspace_id",
            "incumbent_decision_id",
            "challenger_registry_id",
            "challenger_revision",
            "snapshot_hash",
            name="uq_learning_shadow_observation_logical_identity",
        ),
        CheckConstraint(
            "decision_hash ~ '^[0-9a-f]{64}$' AND strategy_release_hash ~ '^[0-9a-f]{64}$' "
            "AND snapshot_hash ~ '^[0-9a-f]{64}$' AND snapshot_schema_hash ~ '^[0-9a-f]{64}$' "
            "AND challenger_artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND challenger_manifest_hash ~ '^[0-9a-f]{64}$' "
            "AND challenger_fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND training_approval_hash ~ '^[0-9a-f]{64}$' "
            "AND registry_approval_event_hash ~ '^[0-9a-f]{64}$' "
            "AND idempotency_key ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'",
            name="learning_shadow_observation_hashes_valid",
        ),
        CheckConstraint(
            "incumbent_status IN ('CANDIDATE', 'ABSTAIN', 'VETO')",
            name="learning_shadow_observation_incumbent_status_valid",
        ),
        CheckConstraint(
            "challenger_state = 'evaluated' AND challenger_decision = 'abstain'",
            name="learning_shadow_observation_fail_closed",
        ),
        CheckConstraint(
            "challenger_reason IN ('UNCALIBRATED_SCORE', 'FEATURE_SCHEMA_MISMATCH', "
            "'SOURCE_RIGHTS_INELIGIBLE', 'MODEL_UNAVAILABLE', 'MISSING_EVIDENCE')",
            name="learning_shadow_observation_reason_valid",
        ),
        CheckConstraint(
            "length(btrim(strategy_release_id)) BETWEEN 1 AND 80 "
            "AND challenger_revision >= 1 AND registry_approval_event_sequence >= 1 "
            "AND active_registry_revision >= 1 AND active_pointer_version >= 1 "
            "AND active_registry_id <> challenger_registry_id",
            name="learning_shadow_observation_reference_valid",
        ),
        CheckConstraint(
            "active_artifact_hash ~ '^[0-9a-f]{64}$' AND snapshot_coverage BETWEEN 0 AND 1",
            name="learning_shadow_observation_snapshot_metrics_valid",
        ),
        CheckConstraint(
            "(rights_eligible AND rights_fingerprint ~ '^[0-9a-f]{64}$') OR "
            "(NOT rights_eligible AND rights_fingerprint IS NULL)",
            name="learning_shadow_observation_rights_valid",
        ),
        CheckConstraint(
            "(rights_eligible AND rights_source_id IS NOT NULL AND rights_policy_id IS NOT NULL "
            "AND rights_policy_sequence IS NOT NULL AND rights_known_at IS NOT NULL) OR "
            "(NOT rights_eligible AND rights_source_id IS NULL AND rights_policy_id IS NULL "
            "AND rights_policy_sequence IS NULL AND rights_known_at IS NULL)",
            name="learning_shadow_observation_rights_receipt_valid",
        ),
        CheckConstraint(
            "rights_known_at IS NULL OR rights_known_at <= observed_at",
            name="learning_shadow_observation_rights_time_valid",
        ),
        CheckConstraint(
            "snapshot_as_of <= decision_evaluated_at AND snapshot_as_of <= observed_at "
            "AND decision_evaluated_at <= observed_at",
            name="learning_shadow_observation_time_order",
        ),
        Index(
            "ix_learning_shadow_observation_workspace_observed",
            "workspace_id",
            "observed_at",
            "id",
        ),
        Index(
            "ix_learning_shadow_observation_cohort_time",
            "workspace_id",
            "challenger_registry_id",
            "challenger_revision",
            "observed_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incumbent_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_release_id: Mapped[str] = mapped_column(String(80), nullable=False)
    strategy_release_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    incumbent_status: Mapped[str] = mapped_column(String(16), nullable=False)
    decision_evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    challenger_registry_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    active_registry_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    active_registry_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    active_artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    challenger_artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    challenger_artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    challenger_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    challenger_fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    training_approval_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    challenger_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    challenger_state: Mapped[str] = mapped_column(String(24), nullable=False)
    registry_approval_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    registry_approval_event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_approval_event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    active_pointer_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_coverage: Mapped[float] = mapped_column(Float(), nullable=False)
    snapshot_quality_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    schema_compatible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rights_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rights_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    challenger_decision: Mapped[str] = mapped_column(String(16), nullable=False)
    challenger_reason: Mapped[str] = mapped_column(String(40), nullable=False)
    rights_source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rights_policy_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    rights_policy_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rights_known_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class LearningDriftAssessmentRecord(UuidPrimaryKey, CreatedAt, Base):
    """Tổng hợp metadata theo policy và membership cố định; performance luôn inconclusive."""

    __tablename__ = "learning_drift_assessment"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "cohort_registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            ondelete="RESTRICT",
            name="fk_learning_drift_assessment_cohort_registry",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_learning_drift_assessment_workspace_id"),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_drift_assessment_idempotency"
        ),
        UniqueConstraint(
            "workspace_id",
            "id",
            "content_hash",
            name="uq_learning_drift_assessment_exact_content",
        ),
        CheckConstraint(
            "length(btrim(policy_version)) BETWEEN 1 AND 120",
            name="learning_drift_assessment_policy_version_valid",
        ),
        CheckConstraint(
            "length(membership_digest) = 64 AND membership_digest ~ '^[0-9a-f]{64}$' "
            "AND length(idempotency_key) = 64 AND idempotency_key ~ '^[0-9a-f]{64}$' "
            "AND length(content_hash) = 64 AND content_hash ~ '^[0-9a-f]{64}$'",
            name="learning_drift_assessment_hashes_valid",
        ),
        CheckConstraint(
            "member_count >= 0 AND eligible_count >= 0 AND eligible_count <= member_count",
            name="learning_drift_assessment_counts_valid",
        ),
        CheckConstraint(
            "(member_count = 0 AND cohort_registry_id IS NULL AND cohort_revision IS NULL "
            "AND snapshot_coverage_rate IS NULL AND quality_rate IS NULL "
            "AND abstention_rate IS NULL AND data_quality_status = 'inconclusive' "
            "AND reason_codes @> '[\"low_sample\"]'::jsonb) OR "
            "(member_count > 0 AND cohort_registry_id IS NOT NULL "
            "AND cohort_revision IS NOT NULL AND cohort_revision >= 1)",
            name="learning_drift_assessment_cohort_scope_valid",
        ),
        CheckConstraint(
            "(policy_version = 'coverage-v2-30d' AND observation_window_days IS NOT NULL "
            "AND observation_window_days = 30) OR "
            "(policy_version = 'coverage-v1' AND observation_window_days IS NULL)",
            name="learning_drift_assessment_observation_window_valid",
        ),
        CheckConstraint(
            "minimum_observations >= 1 AND minimum_snapshot_coverage BETWEEN 0 AND 1 "
            "AND minimum_quality_rate BETWEEN 0 AND 1",
            name="learning_drift_assessment_policy_thresholds_valid",
        ),
        CheckConstraint(
            "(member_count = 0 AND window_start IS NULL AND window_end IS NULL) OR "
            "(member_count > 0 AND window_start IS NOT NULL AND window_end IS NOT NULL "
            "AND window_start <= window_end)",
            name="learning_drift_assessment_window_valid",
        ),
        CheckConstraint(
            "(snapshot_coverage_rate IS NULL OR snapshot_coverage_rate BETWEEN 0 AND 1) AND "
            "(quality_rate IS NULL OR quality_rate BETWEEN 0 AND 1) AND "
            "(abstention_rate IS NULL OR abstention_rate BETWEEN 0 AND 1)",
            name="learning_drift_assessment_rates_valid",
        ),
        CheckConstraint(
            "data_quality_status IN ('detected', 'no_drift', 'inconclusive') "
            "AND performance_status = 'inconclusive'",
            name="learning_drift_assessment_status_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' AND "
            'reason_codes <@ \'["baseline_missing", "coverage_below_threshold", '
            '"low_sample", "performance_not_evaluable", "quality_below_threshold", '
            '"rights_unverified", "schema_incompatible"]\'::jsonb',
            name="learning_drift_assessment_reason_codes_valid",
        ),
        Index(
            "ix_learning_drift_assessment_workspace_created",
            "workspace_id",
            "created_at",
            "id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    cohort_registry_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    cohort_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_observations: Mapped[int] = mapped_column(Integer, nullable=False)
    minimum_snapshot_coverage: Mapped[float] = mapped_column(Float(), nullable=False)
    minimum_quality_rate: Mapped[float] = mapped_column(Float(), nullable=False)
    snapshot_coverage_rate: Mapped[float | None] = mapped_column(Float(), nullable=True)
    quality_rate: Mapped[float | None] = mapped_column(Float(), nullable=True)
    abstention_rate: Mapped[float | None] = mapped_column(Float(), nullable=True)
    data_quality_status: Mapped[str] = mapped_column(String(16), nullable=False)
    performance_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    membership_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class LearningDriftAssessmentMemberRecord(Base):
    """Khóa membership chính xác và sắp thứ tự ổn định cho một assessment."""

    __tablename__ = "learning_drift_assessment_member"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "assessment_id"],
            ["learning_drift_assessment.workspace_id", "learning_drift_assessment.id"],
            ondelete="RESTRICT",
            name="fk_learning_drift_assessment_member_assessment",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "observation_id"],
            ["learning_shadow_observation.workspace_id", "learning_shadow_observation.id"],
            ondelete="RESTRICT",
            name="fk_learning_drift_assessment_member_observation",
        ),
        UniqueConstraint(
            "workspace_id",
            "assessment_id",
            "ordinal",
            name="uq_learning_drift_assessment_member_ordinal",
        ),
        CheckConstraint("ordinal >= 0", name="learning_drift_assessment_member_ordinal_valid"),
        CheckConstraint(
            "snapshot_coverage BETWEEN 0 AND 1",
            name="learning_drift_assessment_member_coverage_valid",
        ),
        CheckConstraint(
            "length(snapshot_hash) = 64 AND snapshot_hash ~ '^[0-9a-f]{64}$' "
            "AND (rights_fingerprint IS NULL OR (length(rights_fingerprint) = 64 "
            "AND rights_fingerprint ~ '^[0-9a-f]{64}$'))",
            name="learning_drift_assessment_member_hashes_valid",
        ),
        CheckConstraint(
            "challenger_decision IN ('approve', 'veto', 'abstain')",
            name="learning_drift_assessment_member_decision_valid",
        ),
        CheckConstraint(
            "eligible = (baseline_available AND schema_compatible AND rights_eligible)",
            name="learning_drift_assessment_member_eligibility_valid",
        ),
        Index(
            "ix_learning_drift_assessment_member_observation",
            "workspace_id",
            "observation_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    assessment_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_coverage: Mapped[float] = mapped_column(Float(), nullable=False)
    snapshot_quality_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    baseline_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    schema_compatible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rights_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rights_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    challenger_decision: Mapped[str] = mapped_column(String(16), nullable=False)


class LearningRetrainProposalRecord(UuidPrimaryKey, CreatedAt, Base):
    """Đề xuất review dữ liệu gắn assessment exact; không có quyền enqueue hay promote."""

    __tablename__ = "learning_retrain_proposal"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "assessment_id", "assessment_hash"],
            [
                "learning_drift_assessment.workspace_id",
                "learning_drift_assessment.id",
                "learning_drift_assessment.content_hash",
            ],
            ondelete="RESTRICT",
            name="fk_learning_retrain_proposal_assessment_exact",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_learning_retrain_proposal_workspace_id"),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_retrain_proposal_idempotency"
        ),
        CheckConstraint(
            "length(btrim(policy_version)) BETWEEN 1 AND 120",
            name="learning_retrain_proposal_policy_version_valid",
        ),
        CheckConstraint(
            "length(assessment_hash) = 64 AND assessment_hash ~ '^[0-9a-f]{64}$' "
            "AND length(membership_digest) = 64 AND membership_digest ~ '^[0-9a-f]{64}$' "
            "AND length(idempotency_key) = 64 AND idempotency_key ~ '^[0-9a-f]{64}$' "
            "AND length(content_hash) = 64 AND content_hash ~ '^[0-9a-f]{64}$'",
            name="learning_retrain_proposal_hashes_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' AND jsonb_array_length(reason_codes) > 0 "
            'AND reason_codes <@ \'["coverage_below_threshold", '
            '"quality_below_threshold"]\'::jsonb',
            name="learning_retrain_proposal_data_reasons_only",
        ),
        Index(
            "ix_learning_retrain_proposal_workspace_created",
            "workspace_id",
            "created_at",
            "id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    assessment_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    assessment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    membership_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


__all__ = [
    "LearningDriftAssessmentMemberRecord",
    "LearningDriftAssessmentRecord",
    "LearningRetrainProposalRecord",
    "LearningShadowObservationRecord",
]

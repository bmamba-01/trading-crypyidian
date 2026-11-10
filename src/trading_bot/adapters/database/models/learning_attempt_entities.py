"""Profile ngân sách theo fold và ledger bất biến cho lượt learning."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
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
from trading_bot.domain.learning.accounting import (
    LearningAttemptActor,
    LearningAttemptEvent,
    LearningAttemptReasonCode,
    LearningAttemptState,
    LearningTrainingAttempt,
)
from trading_bot.domain.learning.resources import LearningResourcePlan, LearningResourceRequest


class LearningFoldResourceProfileRecord(UuidPrimaryKey, CreatedAt, Base):
    """Count và schema của fold, tổng hợp từ các hàng normalized đã lưu."""

    __tablename__ = "learning_fold_resource_profile"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "manifest_hash", "fold_plan_hash", "normalized_dataset_id"],
            [
                "learning_normalized_dataset.workspace_id",
                "learning_normalized_dataset.manifest_hash",
                "learning_normalized_dataset.fold_plan_hash",
                "learning_normalized_dataset.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_fold_resource_profile_dataset",
        ),
        UniqueConstraint(
            "workspace_id",
            "normalized_dataset_id",
            "fold_id",
            name="uq_learning_fold_resource_profile_fold",
        ),
        CheckConstraint("fold_id ~ '^[0-9a-f]{64}$'", name="learning_fold_profile_fold_hash"),
        CheckConstraint(
            "feature_schema_hash ~ '^[0-9a-f]{64}$' AND profile_hash ~ '^[0-9a-f]{64}$'",
            name="learning_fold_profile_hashes_valid",
        ),
        CheckConstraint(
            "training_samples > 0 AND validation_samples > 0 AND feature_count > 0",
            name="learning_fold_profile_counts_positive",
        ),
        Index(
            "ix_learning_fold_resource_profile_dataset",
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
            "fold_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    normalized_dataset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_id: Mapped[str] = mapped_column(String(64), nullable=False)
    training_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    validation_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    feature_count: Mapped[int] = mapped_column(Integer, nullable=False)
    feature_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    @classmethod
    def from_dataset(
        cls,
        dataset: Any,
        *,
        normalized_dataset_id: UUID,
        fold_id: str,
    ) -> LearningFoldResourceProfileRecord:
        """Tổng hợp count/schema từ payload typed đã qua kiểm tra canonical."""
        from trading_bot.application.learning_fold_samples import NormalizedLearningDataset

        if (
            type(dataset) is not NormalizedLearningDataset
            or type(normalized_dataset_id) is not UUID
            or type(fold_id) is not str
        ):
            raise ValueError("Cần dataset normalized, dataset UUID và fold ID typed.")
        samples = {sample.sample_id: sample for sample in dataset.samples}
        assignments = tuple(item for item in dataset.assignments if item.fold_id == fold_id)
        training = tuple(item for item in assignments if item.role == "train")
        validation = tuple(item for item in assignments if item.role == "validation")
        selected = tuple(
            samples[item.sample_id]
            for item in (*training, *validation)
            if item.sample_id in samples
        )
        schemas = {sample.feature_schema_hash for sample in selected}
        feature_sets = {tuple(name for name, _ in sample.features) for sample in selected}
        if (
            not training
            or not validation
            or len(selected) != len(training) + len(validation)
            or len(schemas) != 1
            or len(feature_sets) != 1
        ):
            raise ValueError("Fold trainer cần train/validation có cùng feature schema.")
        feature_schema_hash = next(iter(schemas))
        feature_count = len(next(iter(feature_sets)))
        profile = {
            "feature_count": feature_count,
            "feature_schema_hash": feature_schema_hash,
            "fold_id": fold_id,
            "fold_plan_hash": dataset.fold_plan_hash,
            "manifest_hash": dataset.manifest_hash,
            "normalized_dataset_id": str(normalized_dataset_id),
            "training_samples": len(training),
            "validation_samples": len(validation),
            "workspace_id": str(dataset.workspace_id),
        }
        canonical = json.dumps(profile, sort_keys=True, separators=(",", ":"))
        return cls(
            workspace_id=dataset.workspace_id,
            normalized_dataset_id=normalized_dataset_id,
            manifest_hash=dataset.manifest_hash,
            fold_plan_hash=dataset.fold_plan_hash,
            fold_id=fold_id,
            training_samples=len(training),
            validation_samples=len(validation),
            feature_count=feature_count,
            feature_schema_hash=feature_schema_hash,
            profile_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )


class LearningTrainingAccessReceiptRecord(UuidPrimaryKey, CreatedAt, Base):
    """Biên nhận bất biến ghi nhận trainer đã nhận đúng batch train/validation."""

    __tablename__ = "learning_training_access_receipt"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_training_attempt.workspace_id", "learning_training_attempt.id"],
            ondelete="RESTRICT",
            name="fk_learning_training_access_receipt_attempt",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "normalized_dataset_id", "fold_id"],
            [
                "learning_fold_resource_profile.workspace_id",
                "learning_fold_resource_profile.normalized_dataset_id",
                "learning_fold_resource_profile.fold_id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_training_access_receipt_profile",
        ),
        UniqueConstraint(
            "workspace_id",
            "attempt_id",
            name="uq_learning_training_access_receipt_attempt",
        ),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_id ~ '^[0-9a-f]{64}$' AND approval_hash ~ '^[0-9a-f]{64}$' "
            "AND request_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND profile_hash ~ '^[0-9a-f]{64}$'",
            name="learning_training_access_receipt_hashes_valid",
        ),
        CheckConstraint(
            "purpose = 'trainer-input' AND actor_principal = 'crypidian_trainer_worker'",
            name="learning_training_access_receipt_actor_purpose_valid",
        ),
        CheckConstraint(
            "training_samples > 0 AND validation_samples > 0 "
            "AND sample_count = training_samples + validation_samples",
            name="learning_training_access_receipt_counts_valid",
        ),
        Index(
            "ix_learning_training_access_receipt_manifest",
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    normalized_dataset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_id: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_principal: Mapped[str] = mapped_column(String(80), nullable=False)
    training_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    validation_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)


class LearningTrainingAttemptRecord(UuidPrimaryKey, CreatedAt, Base):
    """Yêu cầu canonical có idempotency; trạng thái hiện tại lấy từ event cuối."""

    __tablename__ = "learning_training_attempt"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "actor_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="RESTRICT",
            name="fk_learning_training_attempt_actor_membership",
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
            name="fk_learning_training_attempt_dataset",
        ),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_training_attempt_idempotency"
        ),
        UniqueConstraint(
            "workspace_id",
            "parameter_family_hash",
            "trial_index",
            "fold_id",
            name="uq_learning_training_attempt_trial_fold",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_learning_training_attempt_workspace"),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND approval_hash ~ '^[0-9a-f]{64}$' "
            "AND parameter_family_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_id ~ '^[0-9a-f]{64}$' "
            "AND dependency_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND content_hash ~ '^[0-9a-f]{64}$'",
            name="learning_training_attempt_hashes_valid",
        ),
        CheckConstraint(
            "trial_count BETWEEN 1 AND 100 AND trial_index >= 0 AND trial_index < trial_count "
            "AND seed >= 0",
            name="learning_training_attempt_trial_bounds",
        ),
        CheckConstraint(
            "length(btrim(split_policy_version)) BETWEEN 1 AND 128 "
            "AND length(btrim(trainer_version)) BETWEEN 1 AND 64 "
            "AND length(btrim(resource_policy_version)) BETWEEN 1 AND 64",
            name="learning_training_attempt_versions_present",
        ),
        CheckConstraint(
            "jsonb_typeof(resource_request) = 'object' "
            "AND (resource_plan IS NULL OR jsonb_typeof(resource_plan) = 'object') "
            "AND length(canonical_request) > 0 "
            "AND initial_state IN ('queued', 'rejected') "
            "AND ((resource_plan IS NULL AND initial_state = 'rejected' "
            "AND rejection_reason_code = 'resource_limit') OR "
            "(resource_plan IS NOT NULL AND initial_state = 'queued' "
            "AND rejection_reason_code IS NULL))",
            name="learning_training_attempt_payload_valid",
        ),
        CheckConstraint(
            "rejection_reason_code IS NULL OR rejection_reason_code IN "
            "('resource_limit', 'approval_missing', 'rights_denied', 'invalid_request', "
            "'adapter_error', 'cancelled_by_operator')",
            name="learning_training_attempt_reason_valid",
        ),
        Index("ix_learning_training_attempt_workspace_created", "workspace_id", "created_at"),
        Index(
            "ix_learning_training_attempt_queue",
            "workspace_id",
            "created_at",
            postgresql_where="initial_state = 'queued'",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    normalized_dataset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    split_policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parameter_family_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trial_index: Mapped[int] = mapped_column(Integer, nullable=False)
    trial_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fold_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    trainer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dependency_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_request: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    resource_plan: Mapped[dict[str, int] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    rejection_reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    initial_state: Mapped[str] = mapped_column(String(16), nullable=False)
    canonical_request: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    @classmethod
    def from_domain(
        cls,
        attempt: LearningTrainingAttempt,
        *,
        normalized_dataset_id: UUID,
    ) -> LearningTrainingAttemptRecord:
        """Tạo bản ghi canonical gắn với normalized dataset authoritative."""
        if type(attempt) is not LearningTrainingAttempt or type(normalized_dataset_id) is not UUID:
            raise ValueError("Cần attempt typed và normalized_dataset_id UUID.")
        payload = json.loads(attempt.canonical_json)
        return cls(
            id=attempt.id,
            workspace_id=attempt.workspace_id,
            normalized_dataset_id=normalized_dataset_id,
            actor_membership_id=attempt.actor_membership_id,
            idempotency_key=attempt.idempotency_key,
            manifest_hash=attempt.manifest_hash,
            split_policy_version=attempt.split_policy_version,
            fold_plan_hash=attempt.fold_plan_hash,
            approval_hash=attempt.approval_hash,
            parameter_family_hash=attempt.parameter_family_hash,
            trial_index=attempt.trial_index,
            trial_count=attempt.trial_count,
            fold_id=attempt.fold_id,
            seed=attempt.seed,
            trainer_version=attempt.trainer_version,
            dependency_fingerprint=attempt.dependency_fingerprint,
            resource_policy_version=attempt.resource_policy_version,
            resource_request=payload["resource_request"],
            resource_plan=payload["resource_plan"],
            rejection_reason_code=payload["rejection_reason_code"],
            initial_state=attempt.initial_state.value,
            canonical_request=attempt.canonical_json,
            content_hash=attempt.content_hash,
        )

    def to_domain(self) -> LearningTrainingAttempt:
        """Khôi phục request và xác thực JSONB, canonical bytes cùng denormalized fields."""
        if type(self.resource_request) is not dict:
            raise ValueError("resource_request phải là object JSONB.")
        request_keys = {
            "feature_count",
            "max_runtime_seconds",
            "max_steps",
            "memory_limit_bytes",
            "training_samples",
            "validation_samples",
        }
        if set(self.resource_request) != request_keys or any(
            type(value) is not int for value in self.resource_request.values()
        ):
            raise ValueError("resource_request JSONB không hợp lệ.")
        plan: LearningResourcePlan | None = None
        if self.resource_plan is not None:
            if type(self.resource_plan) is not dict:
                raise ValueError("resource_plan phải là object JSONB hoặc NULL.")
            plan_keys = {
                "estimated_memory_bytes",
                "estimated_operation_units",
                "feature_count",
                "max_runtime_seconds",
                "max_steps",
                "memory_limit_bytes",
                "sample_count",
            }
            if set(self.resource_plan) != plan_keys or any(
                type(value) is not int for value in self.resource_plan.values()
            ):
                raise ValueError("resource_plan JSONB không hợp lệ.")
            plan = LearningResourcePlan(**self.resource_plan)
        request = LearningResourceRequest(**self.resource_request)
        attempt = LearningTrainingAttempt(
            id=self.id,
            workspace_id=self.workspace_id,
            actor_membership_id=self.actor_membership_id,
            idempotency_key=self.idempotency_key,
            manifest_hash=self.manifest_hash,
            split_policy_version=self.split_policy_version,
            fold_plan_hash=self.fold_plan_hash,
            approval_hash=self.approval_hash,
            parameter_family_hash=self.parameter_family_hash,
            trial_index=self.trial_index,
            trial_count=self.trial_count,
            fold_id=self.fold_id,
            seed=self.seed,
            trainer_version=self.trainer_version,
            dependency_fingerprint=self.dependency_fingerprint,
            resource_policy_version=self.resource_policy_version,
            resource_request=request,
            resource_plan=plan,
            rejection_reason_code=(
                LearningAttemptReasonCode(self.rejection_reason_code)
                if self.rejection_reason_code is not None
                else None
            ),
        )
        if (
            attempt.canonical_json != self.canonical_request
            or attempt.content_hash != self.content_hash
            or attempt.initial_state.value != self.initial_state
        ):
            raise ValueError("Attempt canonical bytes, hash hoặc state bị lệch.")
        return attempt


class LearningAttemptEventRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một event trạng thái/chi phí nối tiếp, không thể cập nhật hay xóa."""

    __tablename__ = "learning_attempt_event"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_training_attempt.workspace_id", "learning_training_attempt.id"],
            ondelete="RESTRICT",
            name="fk_learning_attempt_event_attempt",
        ),
        UniqueConstraint(
            "workspace_id",
            "attempt_id",
            "sequence",
            name="uq_learning_attempt_event_sequence",
        ),
        CheckConstraint("sequence > 0", name="learning_attempt_event_sequence_positive"),
        CheckConstraint(
            "state IN ('queued', 'rejected', 'running', 'succeeded', 'failed', 'cancelled')",
            name="learning_attempt_event_state_valid",
        ),
        CheckConstraint(
            "actor_principal IN ('crypidian_admin_web', 'crypidian_trainer_worker', "
            "'crypidian_learning_evaluator')",
            name="learning_attempt_event_actor_valid",
        ),
        CheckConstraint(
            "reason_code IS NULL OR reason_code IN ('resource_limit', 'approval_missing', "
            "'rights_denied', 'invalid_request', 'adapter_error', 'cancelled_by_operator')",
            name="learning_attempt_event_reason_valid",
        ),
        CheckConstraint(
            "((state IN ('rejected', 'failed', 'cancelled')) = (reason_code IS NOT NULL)) "
            "AND ((state IN ('succeeded', 'failed', 'cancelled')) = "
            "(elapsed_milliseconds IS NOT NULL))",
            name="learning_attempt_event_terminal_fields_valid",
        ),
        CheckConstraint(
            "(elapsed_milliseconds IS NULL OR elapsed_milliseconds >= 0) "
            "AND (peak_memory_bytes IS NULL OR peak_memory_bytes >= 0) "
            "AND (actual_operation_units IS NULL OR actual_operation_units >= 0) "
            "AND canonical_event <> '' AND event_hash ~ '^[0-9a-f]{64}$'",
            name="learning_attempt_event_metrics_valid",
        ),
        Index("ix_learning_attempt_event_attempt", "workspace_id", "attempt_id", "sequence"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_principal: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    elapsed_milliseconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    peak_memory_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_operation_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    canonical_event: Mapped[str] = mapped_column(Text, nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    @classmethod
    def from_domain(
        cls, *, workspace_id: UUID, event: LearningAttemptEvent
    ) -> LearningAttemptEventRecord:
        """Tạo event lưu trữ với canonical payload và hash."""
        if type(workspace_id) is not UUID or type(event) is not LearningAttemptEvent:
            raise ValueError("Cần workspace UUID và LearningAttemptEvent typed.")
        payload = {
            "actor_principal": event.actor_principal.value,
            "actual_operation_units": event.actual_operation_units,
            "attempt_id": str(event.attempt_id),
            "elapsed_milliseconds": event.elapsed_milliseconds,
            "occurred_at": event.occurred_at.isoformat(),
            "peak_memory_bytes": event.peak_memory_bytes,
            "reason_code": event.reason_code.value if event.reason_code else None,
            "sequence": event.sequence,
            "state": event.state.value,
            "workspace_id": str(workspace_id),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(
            id=uuid4(),
            workspace_id=workspace_id,
            attempt_id=event.attempt_id,
            sequence=event.sequence,
            state=event.state.value,
            occurred_at=event.occurred_at,
            actor_principal=event.actor_principal.value,
            reason_code=event.reason_code.value if event.reason_code else None,
            elapsed_milliseconds=event.elapsed_milliseconds,
            peak_memory_bytes=event.peak_memory_bytes,
            actual_operation_units=event.actual_operation_units,
            canonical_event=canonical,
            event_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    def to_domain(self) -> LearningAttemptEvent:
        """Tái tạo domain event và xác nhận canonical bytes/hash cùng indexed fields."""
        event = LearningAttemptEvent(
            attempt_id=self.attempt_id,
            sequence=self.sequence,
            state=LearningAttemptState(self.state),
            occurred_at=self.occurred_at,
            actor_principal=LearningAttemptActor(self.actor_principal),
            reason_code=(
                LearningAttemptReasonCode(self.reason_code)
                if self.reason_code is not None
                else None
            ),
            elapsed_milliseconds=self.elapsed_milliseconds,
            peak_memory_bytes=self.peak_memory_bytes,
            actual_operation_units=self.actual_operation_units,
        )
        expected = self.from_domain(workspace_id=self.workspace_id, event=event)
        if (
            expected.canonical_event != self.canonical_event
            or expected.event_hash != self.event_hash
        ):
            raise ValueError("Event canonical bytes/hash hoặc ID bị lệch.")
        return event


__all__ = [
    "LearningAttemptEventRecord",
    "LearningFoldResourceProfileRecord",
    "LearningTrainingAccessReceiptRecord",
    "LearningTrainingAttemptRecord",
]

"""Use-case quản trị enqueue, theo dõi và hủy yêu cầu training."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.learning_attempts import (
    LearningAttemptRepository,
)
from trading_bot.adapters.database.repositories.learning_training_admin import (
    LearningTrainingAdminRepository,
    LearningTrainingFoldOption,
    LearningTrainingJobSnapshot,
)
from trading_bot.application.learning_cpu_trainer import CPU_TRAINER_VERSION
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.domain.learning.accounting import (
    LearningAttemptReasonCode,
    LearningTrainingAttempt,
)
from trading_bot.domain.learning.resources import (
    LearningResourceRejected,
    LearningResourceRequest,
    assess_learning_resource_request,
)

RESOURCE_POLICY_VERSION = "learning-resource-policy-v1"
TRAINER_MAX_STEPS = 100
TRAINER_MAX_RUNTIME_SECONDS = 60
TRAINER_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
MAX_TRIALS_PER_FAMILY = 100

_TRAINER_FAMILY_PAYLOAD = {
    "max_steps": TRAINER_MAX_STEPS,
    "memory_limit_bytes": TRAINER_MEMORY_LIMIT_BYTES,
    "resource_policy_version": RESOURCE_POLICY_VERSION,
    "runtime_seconds": TRAINER_MAX_RUNTIME_SECONDS,
    "trainer_version": CPU_TRAINER_VERSION,
}
PARAMETER_FAMILY_HASH = hashlib.sha256(
    json.dumps(_TRAINER_FAMILY_PAYLOAD, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
DEPENDENCY_FINGERPRINT = hashlib.sha256(
    json.dumps(
        {"implementation": "pure-python", "trainer_version": CPU_TRAINER_VERSION},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


@dataclass(frozen=True, slots=True)
class LearningTrainingSubmission:
    """Kết quả enqueue an toàn, không có payload sample."""

    attempt_id: UUID
    job_id: UUID | None
    normalized_dataset_id: UUID
    manifest_hash: str
    fold_id: str
    trial_index: int
    seed: int
    status: str
    duplicate: bool
    error_code: str | None


class LearningTrainingAdminService:
    """Tạo facade dùng workspace và actor từ Principal đã xác thực."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    def for_principal(self, principal: Principal) -> PrincipalLearningTrainingAdminService:
        if type(principal) is not Principal:
            raise TypeError("principal phải đến từ authentication boundary.")
        return PrincipalLearningTrainingAdminService(self._sessions, principal)


class PrincipalLearningTrainingAdminService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], principal: Principal) -> None:
        self._sessions = sessions
        self._principal = principal

    async def list_eligible_folds(self) -> tuple[LearningTrainingFoldOption, ...]:
        self._require_manage()
        async with self._uow() as unit:
            return await LearningTrainingAdminRepository(unit.session).list_eligible_folds(
                workspace_id=self._principal.workspace_id
            )

    async def list_jobs(self) -> tuple[LearningTrainingJobSnapshot, ...]:
        self._require_manage()
        async with self._uow() as unit:
            return await LearningTrainingAdminRepository(unit.session).list_jobs(
                workspace_id=self._principal.workspace_id
            )

    async def enqueue(
        self,
        *,
        normalized_dataset_id: UUID,
        fold_id: str,
        idempotency_key: UUID,
    ) -> LearningTrainingSubmission:
        self._require_recent_mfa()
        if type(normalized_dataset_id) is not UUID or type(idempotency_key) is not UUID:
            raise ValueError("Dataset và idempotency key phải là UUID.")
        if not isinstance(fold_id, str) or len(fold_id) != 64:
            raise ValueError("fold_id phải là SHA-256.")

        workspace_id = self._principal.workspace_id
        attempt_id = uuid5(
            NAMESPACE_URL,
            f"crypidian:learning-training:{workspace_id}:{idempotency_key}",
        )
        async with self._uow() as unit:
            repository = LearningTrainingAdminRepository(unit.session)
            await repository.lock_idempotency_scope(
                workspace_id=workspace_id,
                idempotency_key=idempotency_key,
            )
            existing = await repository.get_attempt_by_idempotency(
                workspace_id=workspace_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                if (
                    existing.normalized_dataset_id != normalized_dataset_id
                    or existing.fold_id != fold_id
                    or existing.id != attempt_id
                ):
                    raise ValueError("Idempotency key đã gắn với fold hoặc dataset khác.")
                return await self._submission(
                    repository,
                    attempt=existing.to_domain(),
                    normalized_dataset_id=normalized_dataset_id,
                    duplicate=True,
                )

            option = await repository.get_fold_option(
                workspace_id=workspace_id,
                normalized_dataset_id=normalized_dataset_id,
                fold_id=fold_id,
            )
            if option is None:
                raise LookupError("Không tìm thấy fold đã duyệt trong workspace.")
            await repository.lock_trial_family(
                workspace_id=workspace_id,
                family_hash=PARAMETER_FAMILY_HASH,
                fold_id=fold_id,
            )
            trial_index = await repository.next_trial_index(
                workspace_id=workspace_id,
                family_hash=PARAMETER_FAMILY_HASH,
                fold_id=fold_id,
            )
            if trial_index >= MAX_TRIALS_PER_FAMILY:
                raise ValueError("Fold đã đạt giới hạn số lượt training cho parameter family.")

            resource_request = LearningResourceRequest(
                training_samples=option.training_samples,
                validation_samples=option.validation_samples,
                feature_count=option.feature_count,
                max_steps=TRAINER_MAX_STEPS,
                max_runtime_seconds=TRAINER_MAX_RUNTIME_SECONDS,
                memory_limit_bytes=TRAINER_MEMORY_LIMIT_BYTES,
            )
            resource_plan = None
            rejection_reason = None
            try:
                resource_plan = assess_learning_resource_request(resource_request)
            except LearningResourceRejected:
                rejection_reason = LearningAttemptReasonCode.RESOURCE_LIMIT
            attempt = LearningTrainingAttempt(
                id=attempt_id,
                workspace_id=workspace_id,
                actor_membership_id=self._principal.membership_id,
                idempotency_key=idempotency_key,
                manifest_hash=option.manifest_hash,
                split_policy_version=option.split_policy_version,
                fold_plan_hash=option.fold_plan_hash,
                approval_hash=option.approval_hash,
                parameter_family_hash=PARAMETER_FAMILY_HASH,
                trial_index=trial_index,
                trial_count=MAX_TRIALS_PER_FAMILY,
                fold_id=option.fold_id,
                seed=trial_index,
                trainer_version=CPU_TRAINER_VERSION,
                dependency_fingerprint=DEPENDENCY_FINGERPRINT,
                resource_policy_version=RESOURCE_POLICY_VERSION,
                resource_request=resource_request,
                resource_plan=resource_plan,
                rejection_reason_code=rejection_reason,
            )
            persisted = await LearningAttemptRepository(unit.session).append_attempt(
                attempt=attempt,
                normalized_dataset_id=normalized_dataset_id,
            )
            return await self._submission(
                repository,
                attempt=persisted.attempt,
                normalized_dataset_id=normalized_dataset_id,
                duplicate=persisted.is_duplicate,
            )

    async def cancel(self, *, job_id: UUID) -> LearningTrainingJobSnapshot:
        self._require_recent_mfa()
        if type(job_id) is not UUID:
            raise ValueError("job_id phải là UUID.")
        workspace_id = self._principal.workspace_id
        async with self._uow() as unit:
            repository = LearningTrainingAdminRepository(unit.session)
            row = await repository.cancel_job(
                workspace_id=workspace_id,
                job_id=job_id,
                actor_membership_id=self._principal.membership_id,
            )
            if row is None:
                raise LookupError("Không tìm thấy training job trong workspace.")
            snapshot = await repository.get_job_snapshot(
                workspace_id=workspace_id,
                job_id=job_id,
            )
            if snapshot is None:
                raise LookupError("Không tìm thấy training job trong workspace.")
            return snapshot

    async def _submission(
        self,
        repository: LearningTrainingAdminRepository,
        *,
        attempt: LearningTrainingAttempt,
        normalized_dataset_id: UUID,
        duplicate: bool,
    ) -> LearningTrainingSubmission:
        job = await repository.get_job_for_attempt(
            workspace_id=attempt.workspace_id,
            attempt_id=attempt.id,
        )
        return LearningTrainingSubmission(
            attempt_id=attempt.id,
            job_id=job.id if job is not None else None,
            normalized_dataset_id=normalized_dataset_id,
            manifest_hash=attempt.manifest_hash,
            fold_id=attempt.fold_id,
            trial_index=attempt.trial_index,
            seed=attempt.seed,
            status=attempt.initial_state.value if job is None else job.status,
            duplicate=duplicate,
            error_code=(
                attempt.rejection_reason_code.value if attempt.rejection_reason_code else None
            ),
        )

    def _require_manage(self) -> None:
        if not authorize(self._principal, Permission.LEARNING_TRAINING_MANAGE):
            raise PermissionError("Principal không có quyền quản lý training job.")

    def _require_recent_mfa(self) -> datetime:
        self._require_manage()
        verified_at = self._principal.mfa_verified_at
        now = datetime.now(UTC)
        if (
            not isinstance(verified_at, datetime)
            or verified_at.tzinfo is None
            or verified_at.utcoffset() is None
            or verified_at > now
            or now - verified_at > RECENT_MFA_LIFETIME
        ):
            raise PermissionError("Cần xác thực MFA gần đây để quản lý training job.")
        return verified_at

    def _uow(self) -> UnitOfWork:
        return UnitOfWork(
            self._sessions,
            workspace_id=self._principal.workspace_id,
            actor_membership_id=self._principal.membership_id,
        )


__all__ = [
    "LearningTrainingAdminService",
    "LearningTrainingFoldOption",
    "LearningTrainingJobSnapshot",
    "LearningTrainingSubmission",
    "PrincipalLearningTrainingAdminService",
]

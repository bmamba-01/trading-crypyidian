"""Lưu artifact trainer và terminal event trong cùng workspace transaction."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    LearningAttemptEventRecord,
    LearningFoldResourceProfileRecord,
    LearningTrainingArtifactRecord,
    LearningTrainingAttemptRecord,
)
from trading_bot.adapters.database.repositories.learning_attempts import LearningAttemptRepository
from trading_bot.application.learning_training_artifact import (
    VerifiedLearningTrainingArtifact,
)
from trading_bot.domain.learning.accounting import (
    LearningAttemptActor,
    LearningAttemptEvent,
    LearningAttemptState,
    LearningTrainingAttempt,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ARTIFACT_SCHEMA_VERSION = "crypidian.learning.cpu-logistic.v1"


class LearningTrainingArtifactConflict(RuntimeError):
    """Persist retry, attempt hoặc artifact không còn ở trạng thái authoritative."""


@dataclass(frozen=True, slots=True)
class LearningTrainingArtifactWriteResult:
    record: LearningTrainingArtifactRecord
    is_duplicate: bool


class LearningTrainingArtifactRepository:
    """Append-only repository; DB trigger ràng buộc artifact với event succeeded."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_success(
        self,
        *,
        workspace_id: UUID,
        artifact: VerifiedLearningTrainingArtifact,
    ) -> LearningTrainingArtifactWriteResult:
        """Ghi artifact và event succeeded atomic trong Unit of Work của caller."""
        self._validate_identifiers(workspace_id, artifact)
        await self._bind_workspace(workspace_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": (f"wp06-learning-training-artifact:{workspace_id}:{artifact.attempt_id}")},
        )
        attempt = await self._session.scalar(
            select(LearningTrainingAttemptRecord).where(
                LearningTrainingAttemptRecord.workspace_id == workspace_id,
                LearningTrainingAttemptRecord.id == artifact.attempt_id,
            )
        )
        if attempt is None:
            raise LearningTrainingArtifactConflict("Attempt không tồn tại trong workspace.")
        self._validate_against_attempt(artifact, attempt)

        latest_event = await self._session.scalar(
            select(LearningAttemptEventRecord)
            .where(
                LearningAttemptEventRecord.workspace_id == workspace_id,
                LearningAttemptEventRecord.attempt_id == artifact.attempt_id,
            )
            .order_by(LearningAttemptEventRecord.sequence.desc())
            .limit(1)
        )
        existing = await self._session.scalar(
            select(LearningTrainingArtifactRecord).where(
                LearningTrainingArtifactRecord.workspace_id == workspace_id,
                LearningTrainingArtifactRecord.attempt_id == artifact.attempt_id,
            )
        )
        if existing is not None:
            self.validate_record(existing)
            if not self._same_artifact(existing, artifact):
                raise LearningTrainingArtifactConflict(
                    "Attempt đã có artifact khác; không thể thay thế kết quả bất biến."
                )
            if (
                latest_event is None
                or latest_event.state != LearningAttemptState.SUCCEEDED.value
                or latest_event.elapsed_milliseconds != artifact.elapsed_milliseconds
                or latest_event.peak_memory_bytes != artifact.peak_memory_bytes
                or latest_event.actual_operation_units != artifact.actual_operation_units
            ):
                raise LearningTrainingArtifactConflict(
                    "Artifact đã có nhưng terminal event không khớp."
                )
            return LearningTrainingArtifactWriteResult(existing, True)

        if latest_event is None or latest_event.state != LearningAttemptState.RUNNING.value:
            raise LearningTrainingArtifactConflict("Chỉ attempt đang chạy mới được lưu artifact.")
        profile = await self._session.scalar(
            select(LearningFoldResourceProfileRecord).where(
                LearningFoldResourceProfileRecord.workspace_id == workspace_id,
                LearningFoldResourceProfileRecord.normalized_dataset_id
                == attempt.normalized_dataset_id,
                LearningFoldResourceProfileRecord.fold_id == artifact.fold_id,
            )
        )
        if profile is None or profile.feature_schema_hash != artifact.feature_schema_hash:
            raise LearningTrainingArtifactConflict("Artifact feature schema không khớp profile.")

        record = LearningTrainingArtifactRecord.from_verified(artifact)
        self.validate_record(record)
        self._session.add(record)
        await self._session.flush()
        await LearningAttemptRepository(self._session).append_event(
            workspace_id=workspace_id,
            event=LearningAttemptEvent(
                attempt_id=artifact.attempt_id,
                sequence=latest_event.sequence + 1,
                state=LearningAttemptState.SUCCEEDED,
                occurred_at=datetime.now(UTC),
                actor_principal=LearningAttemptActor.TRAINER_WORKER,
                elapsed_milliseconds=artifact.elapsed_milliseconds,
                peak_memory_bytes=artifact.peak_memory_bytes,
                actual_operation_units=artifact.actual_operation_units,
            ),
        )
        await self._session.flush()
        return LearningTrainingArtifactWriteResult(record, False)

    async def get_by_attempt(
        self,
        *,
        workspace_id: UUID,
        attempt_id: UUID,
    ) -> LearningTrainingArtifactRecord | None:
        """Đọc artifact tenant-scoped rồi xác minh JSON, hash, checksum và fields."""
        if type(workspace_id) is not UUID or type(attempt_id) is not UUID:
            raise ValueError("workspace_id và attempt_id phải là UUID.")
        await self._bind_workspace(workspace_id)
        record = await self._session.scalar(
            select(LearningTrainingArtifactRecord).where(
                LearningTrainingArtifactRecord.workspace_id == workspace_id,
                LearningTrainingArtifactRecord.attempt_id == attempt_id,
            )
        )
        if record is not None:
            self.validate_record(record)
            latest_event = await self._session.scalar(
                select(LearningAttemptEventRecord)
                .where(
                    LearningAttemptEventRecord.workspace_id == workspace_id,
                    LearningAttemptEventRecord.attempt_id == attempt_id,
                )
                .order_by(LearningAttemptEventRecord.sequence.desc())
                .limit(1)
            )
            if (
                latest_event is None
                or latest_event.state != LearningAttemptState.SUCCEEDED.value
                or latest_event.elapsed_milliseconds != record.elapsed_milliseconds
                or latest_event.peak_memory_bytes != record.peak_memory_bytes
                or latest_event.actual_operation_units != record.actual_operation_units
            ):
                raise LearningTrainingArtifactConflict(
                    "Artifact lưu nhưng attempt terminal không khớp."
                )
        return record

    @classmethod
    def validate_record(cls, record: LearningTrainingArtifactRecord) -> None:
        """Từ chối row có payload/hash/metadata lệch hoặc đã bật scorer."""
        if type(record) is not LearningTrainingArtifactRecord:
            raise LearningTrainingArtifactConflict("Record artifact không hợp lệ.")
        try:
            payload = json.loads(record.canonical_artifact)
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except TypeError, ValueError, json.JSONDecodeError:
            raise LearningTrainingArtifactConflict("Canonical artifact không đọc được.") from None
        if not isinstance(payload, dict):
            raise LearningTrainingArtifactConflict("Artifact phải là JSON object.")
        if (
            canonical != record.canonical_artifact
            or hashlib.sha256(record.canonical_artifact.encode("utf-8")).hexdigest()
            != record.artifact_hash
            or record.artifact != payload
            or payload.get("calibrated") is not False
            or payload.get("scorer_state") != "abstain"
            or "samples" in payload
            or "artifact_hash" in payload
            or payload.get("schema_version") != record.artifact_schema_version
            or payload.get("trainer_version") != record.trainer_version
            or payload.get("workspace_id") != str(record.workspace_id)
            or payload.get("attempt_id") != str(record.attempt_id)
            or payload.get("manifest_hash") != record.manifest_hash
            or payload.get("fold_plan_hash") != record.fold_plan_hash
            or payload.get("fold_id") != record.fold_id
            or payload.get("approval_hash") != record.approval_hash
            or payload.get("batch_hash") != record.batch_hash
            or payload.get("feature_schema_hash") != record.feature_schema_hash
            or payload.get("attempt_content_hash") != record.attempt_content_hash
            or payload.get("actual_operation_units") != record.actual_operation_units
            or record.artifact_schema_version != _ARTIFACT_SCHEMA_VERSION
            or record.elapsed_milliseconds < 0
            or record.cpu_time_milliseconds < 0
            or record.peak_memory_bytes <= 0
            or record.actual_operation_units <= 0
            or record.process_id <= 0
        ):
            raise LearningTrainingArtifactConflict(
                "Artifact JSON, checksum, lineage hoặc measurements không nhất quán."
            )

    @staticmethod
    def _validate_identifiers(
        workspace_id: UUID,
        artifact: VerifiedLearningTrainingArtifact,
    ) -> None:
        if type(workspace_id) is not UUID or type(artifact) is not VerifiedLearningTrainingArtifact:
            raise ValueError("Cần workspace UUID và VerifiedLearningTrainingArtifact.")
        if artifact.workspace_id != workspace_id:
            raise LearningTrainingArtifactConflict("Artifact thuộc workspace khác.")

    @classmethod
    def _validate_against_attempt(
        cls,
        artifact: VerifiedLearningTrainingArtifact,
        record: LearningTrainingAttemptRecord,
    ) -> None:
        try:
            attempt = record.to_domain()
            payload = json.loads(artifact.canonical_artifact)
        except TypeError, ValueError, json.JSONDecodeError:
            raise LearningTrainingArtifactConflict("Attempt hoặc artifact không hợp lệ.") from None
        if (
            hashlib.sha256(artifact.canonical_artifact.encode("utf-8")).hexdigest()
            != artifact.artifact_hash
            or not isinstance(payload, dict)
            or artifact.attempt_id != attempt.id
            or artifact.workspace_id != attempt.workspace_id
            or artifact.normalized_dataset_id != record.normalized_dataset_id
            or artifact.attempt_content_hash != attempt.content_hash
            or artifact.manifest_hash != attempt.manifest_hash
            or artifact.fold_plan_hash != attempt.fold_plan_hash
            or artifact.fold_id != attempt.fold_id
            or artifact.approval_hash != attempt.approval_hash
            or artifact.trainer_version != attempt.trainer_version
            or artifact.actual_operation_units
            != (attempt.resource_plan.estimated_operation_units if attempt.resource_plan else None)
            or payload.get("schema_version") != _ARTIFACT_SCHEMA_VERSION
            or payload.get("dependency_fingerprint") != attempt.dependency_fingerprint
            or payload.get("seed") != attempt.seed
            or payload.get("resource_request") != _request_payload(attempt)
            or payload.get("resource_plan") != _plan_payload(attempt)
        ):
            raise LearningTrainingArtifactConflict("Artifact metadata không khớp attempt.")

    @staticmethod
    def _same_artifact(
        record: LearningTrainingArtifactRecord,
        artifact: VerifiedLearningTrainingArtifact,
    ) -> bool:
        return (
            record.artifact_hash == artifact.artifact_hash
            and record.canonical_artifact == artifact.canonical_artifact
            and record.elapsed_milliseconds == artifact.elapsed_milliseconds
            and record.cpu_time_milliseconds == artifact.cpu_time_milliseconds
            and record.peak_memory_bytes == artifact.peak_memory_bytes
            and record.actual_operation_units == artifact.actual_operation_units
            and record.process_id == artifact.process_id
        )

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )


def _request_payload(attempt: LearningTrainingAttempt) -> dict[str, int]:
    request = attempt.resource_request
    return {
        "feature_count": request.feature_count,
        "max_runtime_seconds": request.max_runtime_seconds,
        "max_steps": request.max_steps,
        "memory_limit_bytes": request.memory_limit_bytes,
        "training_samples": request.training_samples,
        "validation_samples": request.validation_samples,
    }


def _plan_payload(attempt: LearningTrainingAttempt) -> dict[str, int] | None:
    plan = attempt.resource_plan
    if plan is None:
        return None
    return {
        "estimated_memory_bytes": plan.estimated_memory_bytes,
        "estimated_operation_units": plan.estimated_operation_units,
        "feature_count": plan.feature_count,
        "max_runtime_seconds": plan.max_runtime_seconds,
        "max_steps": plan.max_steps,
        "memory_limit_bytes": plan.memory_limit_bytes,
        "sample_count": plan.sample_count,
    }


__all__ = [
    "LearningTrainingArtifactConflict",
    "LearningTrainingArtifactRepository",
    "LearningTrainingArtifactWriteResult",
]

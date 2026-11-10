"""Append-only repository cho scorer artifact v2."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    LearningAttemptEventRecord,
    LearningScorerArtifactRecord,
    LearningTrainingArtifactRecord,
    LearningTrainingAttemptRecord,
)
from trading_bot.adapters.database.repositories.learning_training_artifacts import (
    LearningTrainingArtifactRepository,
)
from trading_bot.application.learning_scorer_artifact import VerifiedLearningScorerArtifact
from trading_bot.domain.learning.accounting import LearningAttemptState

_SCHEMA_VERSION = "crypidian.learning.scorer-temperature.v2"


class LearningScorerArtifactConflict(RuntimeError):
    """Scorer artifact retry hoặc lineage không khớp base v1/attempt."""


@dataclass(frozen=True, slots=True)
class LearningScorerArtifactWriteResult:
    record: LearningScorerArtifactRecord
    is_duplicate: bool


class LearningScorerArtifactRepository:
    """Lưu duy nhất một scorer v2 cho attempt, giữ nguyên artifact v1."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_success(
        self,
        *,
        workspace_id: UUID,
        artifact: VerifiedLearningScorerArtifact,
    ) -> LearningScorerArtifactWriteResult:
        if type(workspace_id) is not UUID or type(artifact) is not VerifiedLearningScorerArtifact:
            raise ValueError("Cần workspace UUID và VerifiedLearningScorerArtifact.")
        if artifact.workspace_id != workspace_id:
            raise LearningScorerArtifactConflict("Scorer artifact thuộc workspace khác.")
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"wp06-scorer-artifact:{workspace_id}:{artifact.attempt_id}"},
        )
        attempt = await self._session.scalar(
            select(LearningTrainingAttemptRecord).where(
                LearningTrainingAttemptRecord.workspace_id == workspace_id,
                LearningTrainingAttemptRecord.id == artifact.attempt_id,
            )
        )
        base = await self._session.scalar(
            select(LearningTrainingArtifactRecord).where(
                LearningTrainingArtifactRecord.workspace_id == workspace_id,
                LearningTrainingArtifactRecord.attempt_id == artifact.attempt_id,
            )
        )
        if attempt is None or base is None:
            raise LearningScorerArtifactConflict("Thiếu attempt hoặc base artifact v1.")
        LearningTrainingArtifactRepository.validate_record(base)
        if (
            base.artifact_hash != artifact.base_artifact_hash
            or base.id is None
            or base.normalized_dataset_id != artifact.normalized_dataset_id
            or base.manifest_hash != artifact.artifact.manifest_hash
            or base.fold_plan_hash != artifact.artifact.fold_plan_hash
            or base.fold_id != artifact.artifact.fold_id
            or base.approval_hash != artifact.artifact.approval_hash
        ):
            raise LearningScorerArtifactConflict("Scorer v2 không khớp artifact v1 authoritative.")

        existing = await self._session.scalar(
            select(LearningScorerArtifactRecord).where(
                LearningScorerArtifactRecord.workspace_id == workspace_id,
                LearningScorerArtifactRecord.attempt_id == artifact.attempt_id,
            )
        )
        if existing is not None:
            self.validate_record(existing)
            if (
                existing.artifact_hash != artifact.artifact_hash
                or existing.canonical_artifact != artifact.canonical_artifact
                or existing.base_artifact_id != base.id
            ):
                raise LearningScorerArtifactConflict(
                    "Attempt đã có scorer artifact v2 khác; không thể thay thế."
                )
            return LearningScorerArtifactWriteResult(existing, True)

        latest_event = await self._session.scalar(
            select(LearningAttemptEventRecord)
            .where(
                LearningAttemptEventRecord.workspace_id == workspace_id,
                LearningAttemptEventRecord.attempt_id == artifact.attempt_id,
            )
            .order_by(LearningAttemptEventRecord.sequence.desc())
            .limit(1)
        )
        if latest_event is None or latest_event.state != LearningAttemptState.SUCCEEDED.value:
            raise LearningScorerArtifactConflict(
                "Scorer v2 chỉ theo attempt huấn luyện thành công."
            )
        row = LearningScorerArtifactRecord.from_verified(artifact, base_artifact_id=base.id)
        self.validate_record(row)
        self._session.add(row)
        await self._session.flush()
        return LearningScorerArtifactWriteResult(row, False)

    @staticmethod
    def validate_record(record: LearningScorerArtifactRecord) -> None:
        if type(record) is not LearningScorerArtifactRecord:
            raise LearningScorerArtifactConflict("Record scorer artifact không hợp lệ.")
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
            raise LearningScorerArtifactConflict(
                "Canonical scorer artifact không đọc được."
            ) from None
        if (
            canonical != record.canonical_artifact
            or hashlib.sha256(record.canonical_artifact.encode("utf-8")).hexdigest()
            != record.artifact_hash
            or not isinstance(payload, dict)
            or record.artifact != payload
            or payload.get("schema_version") != _SCHEMA_VERSION
            or payload.get("base_artifact_hash") != record.base_artifact_hash
            or payload.get("state") != record.state
            or payload.get("reason_code") != record.reason_code
            or payload.get("workspace_id") != str(record.workspace_id)
            or payload.get("attempt_id") != str(record.attempt_id)
            or payload.get("normalized_dataset_id") != str(record.normalized_dataset_id)
            or payload.get("manifest_hash") != record.manifest_hash
            or payload.get("fold_plan_hash") != record.fold_plan_hash
            or payload.get("fold_id") != record.fold_id
            or payload.get("approval_hash") != record.approval_hash
            or payload.get("strategy_release_id") != record.strategy_release_id
            or payload.get("strategy_release_content_hash") != record.strategy_release_content_hash
            or payload.get("vector_schema_version") != record.vector_schema_version
            or payload.get("vector_schema_hash") != record.vector_schema_hash
            or payload.get("training_membership_digest") != record.training_membership_digest
            or payload.get("validation_membership_digest") != record.validation_membership_digest
            or payload.get("calibration_policy_hash") != record.calibration_policy_hash
            or payload.get("artifact_hash") is not None
            or any(key in payload for key in ("samples", "labels", "features", "test_payload"))
            or record.state == "ready_for_evaluation"
            and (record.temperature is None or record.temperature <= 0.0)
            or record.state == "abstain"
            and record.temperature is not None
            or record.schema_version != _SCHEMA_VERSION
        ):
            raise LearningScorerArtifactConflict(
                "Scorer artifact JSON/hash/metadata không nhất quán."
            )


__all__ = ["LearningScorerArtifactConflict", "LearningScorerArtifactRepository"]

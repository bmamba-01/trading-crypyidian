"""Xác minh artifact trainer và số đo process trước khi persist."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

from trading_bot.application.learning_training_batch import AuthorizedTrainerBatch
from trading_bot.application.learning_training_process import LearningCpuTrainingProcessResult

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ARTIFACT_SCHEMA_VERSION = "crypidian.learning.cpu-logistic.v1"


class LearningTrainingArtifactConflict(RuntimeError):
    """Artifact, checksum, lineage hoặc process measurement không khớp attempt."""


@dataclass(frozen=True, slots=True)
class VerifiedLearningTrainingArtifact:
    """Artifact canonical đã được buộc vào batch và số đo process con."""

    workspace_id: UUID
    attempt_id: UUID
    normalized_dataset_id: UUID
    artifact_hash: str
    artifact_schema_version: str
    trainer_version: str
    manifest_hash: str
    fold_plan_hash: str
    fold_id: str
    approval_hash: str
    batch_hash: str
    feature_schema_hash: str
    attempt_content_hash: str
    canonical_artifact: str
    elapsed_milliseconds: int
    cpu_time_milliseconds: int
    peak_memory_bytes: int
    actual_operation_units: int
    process_id: int

    @classmethod
    def from_process_result(
        cls,
        batch: AuthorizedTrainerBatch,
        result: LearningCpuTrainingProcessResult,
    ) -> VerifiedLearningTrainingArtifact:
        """Kiểm checksum/canonical/schema/lineage trước khi cho phép lưu artifact."""
        if type(batch) is not AuthorizedTrainerBatch:
            raise LearningTrainingArtifactConflict("Artifact cần batch trainer đã cấp quyền.")
        if type(result) is not LearningCpuTrainingProcessResult:
            raise LearningTrainingArtifactConflict("Artifact thiếu process result đã kiểm.")
        if (
            not isinstance(result.artifact_hash, str)
            or _SHA256.fullmatch(result.artifact_hash) is None
            or not isinstance(result.artifact_json, str)
            or hashlib.sha256(result.artifact_json.encode("utf-8")).hexdigest()
            != result.artifact_hash
        ):
            raise LearningTrainingArtifactConflict("Artifact checksum không khớp canonical bytes.")
        try:
            payload = json.loads(result.artifact_json)
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except TypeError, ValueError, json.JSONDecodeError:
            raise LearningTrainingArtifactConflict(
                "Artifact canonical JSON không hợp lệ."
            ) from None
        if not isinstance(payload, dict) or canonical != result.artifact_json:
            raise LearningTrainingArtifactConflict("Artifact không ở dạng JSON canonical.")
        if payload.get("schema_version") != _ARTIFACT_SCHEMA_VERSION:
            raise LearningTrainingArtifactConflict("Artifact schema version không được hỗ trợ.")
        if "samples" in payload:
            raise LearningTrainingArtifactConflict("Artifact không được chứa sample payload.")
        if payload.get("calibrated") is not False or payload.get("scorer_state") != "abstain":
            raise LearningTrainingArtifactConflict("Artifact baseline phải giữ scorer ở abstain.")

        expected_metadata: dict[str, object] = {
            "approval_hash": batch.approval_hash,
            "attempt_content_hash": batch.attempt_content_hash,
            "attempt_id": str(batch.attempt_id),
            "batch_hash": batch.batch_hash,
            "dependency_fingerprint": batch.dependency_fingerprint,
            "feature_schema_hash": batch.feature_schema_hash,
            "fold_id": batch.fold_id,
            "fold_plan_hash": batch.fold_plan_hash,
            "manifest_hash": batch.manifest_hash,
            "normalized_dataset_id": str(batch.normalized_dataset_id),
            "seed": batch.seed,
            "split_policy_version": batch.split_policy_version,
            "trainer_version": batch.trainer_version,
            "workspace_id": str(batch.workspace_id),
        }
        if any(payload.get(name) != value for name, value in expected_metadata.items()):
            raise LearningTrainingArtifactConflict("Artifact lineage không khớp batch trainer.")

        expected_request = {
            "feature_count": batch.resource_request.feature_count,
            "max_runtime_seconds": batch.resource_request.max_runtime_seconds,
            "max_steps": batch.resource_request.max_steps,
            "memory_limit_bytes": batch.resource_request.memory_limit_bytes,
            "training_samples": batch.resource_request.training_samples,
            "validation_samples": batch.resource_request.validation_samples,
        }
        expected_plan = {
            "estimated_memory_bytes": batch.resource_plan.estimated_memory_bytes,
            "estimated_operation_units": batch.resource_plan.estimated_operation_units,
            "feature_count": batch.resource_plan.feature_count,
            "max_runtime_seconds": batch.resource_plan.max_runtime_seconds,
            "max_steps": batch.resource_plan.max_steps,
            "memory_limit_bytes": batch.resource_plan.memory_limit_bytes,
            "sample_count": batch.resource_plan.sample_count,
        }
        if (
            payload.get("resource_request") != expected_request
            or payload.get("resource_plan") != expected_plan
            or payload.get("actual_operation_units")
            != batch.resource_plan.estimated_operation_units
            or payload.get("feature_names") != [name for name, _ in batch.samples[0].features]
        ):
            raise LearningTrainingArtifactConflict("Artifact feature/resource profile không khớp.")

        measurements = (
            ("elapsed_milliseconds", result.elapsed_milliseconds, 0),
            ("cpu_time_milliseconds", result.cpu_time_milliseconds, 0),
            ("peak_memory_bytes", result.peak_memory_bytes, 1),
            ("process_id", result.process_id, 1),
        )
        for name, value, minimum in measurements:
            if type(value) is not int or value < minimum:
                raise LearningTrainingArtifactConflict(f"Process measurement {name} không hợp lệ.")

        return cls(
            workspace_id=batch.workspace_id,
            attempt_id=batch.attempt_id,
            normalized_dataset_id=batch.normalized_dataset_id,
            artifact_hash=result.artifact_hash,
            artifact_schema_version=str(payload.get("schema_version", "")),
            trainer_version=batch.trainer_version,
            manifest_hash=batch.manifest_hash,
            fold_plan_hash=batch.fold_plan_hash,
            fold_id=batch.fold_id,
            approval_hash=batch.approval_hash,
            batch_hash=batch.batch_hash,
            feature_schema_hash=batch.feature_schema_hash,
            attempt_content_hash=batch.attempt_content_hash,
            canonical_artifact=result.artifact_json,
            elapsed_milliseconds=result.elapsed_milliseconds,
            cpu_time_milliseconds=result.cpu_time_milliseconds,
            peak_memory_bytes=result.peak_memory_bytes,
            actual_operation_units=int(payload["actual_operation_units"]),
            process_id=result.process_id,
        )


__all__ = ["LearningTrainingArtifactConflict", "VerifiedLearningTrainingArtifact"]

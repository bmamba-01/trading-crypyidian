from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_bot.adapters.database.models.learning_fold_sample_entities import (
    NormalizedLearningSampleRecord,
)
from trading_bot.adapters.database.models.learning_training_artifact_entities import (
    LearningTrainingArtifactRecord,
)
from trading_bot.adapters.database.repositories.learning_training_artifacts import (
    LearningTrainingArtifactConflict as LearningTrainingArtifactRepositoryConflict,
)
from trading_bot.adapters.database.repositories.learning_training_artifacts import (
    LearningTrainingArtifactRepository,
)
from trading_bot.application.learning_cpu_trainer import train_authorized_batch
from trading_bot.application.learning_fold_samples import (
    NormalizedLearningDataset,
    NormalizedLearningSample,
)
from trading_bot.application.learning_scorer_artifact import (
    VerifiedLearningScorerArtifact,
    build_calibrated_scorer_artifact,
)
from trading_bot.application.learning_training_artifact import (
    LearningTrainingArtifactConflict,
    VerifiedLearningTrainingArtifact,
)
from trading_bot.application.learning_training_batch import (
    authorized_training_batch_from_rows,
)
from trading_bot.application.learning_training_process import (
    LearningCpuTrainingProcessResult,
    LearningCpuTrainingProcessRunner,
    LearningTrainingProcessFailure,
    LearningTrainingProcessFailureCode,
)
from trading_bot.domain.learning.accounting import LearningTrainingAttempt
from trading_bot.domain.learning.calibration import TemperatureCalibrationPolicy
from trading_bot.domain.learning.resources import (
    LearningResourceRequest,
    assess_learning_resource_request,
)


def _batch(
    *,
    validation_feature_override: tuple[tuple[str, float], ...] | None = None,
    training_label_override: tuple[int, int, int] | None = None,
):
    workspace_id = uuid4()
    dataset_id = uuid4()
    sample_specs = (
        ("train-a", "train", -1, (("momentum", -2.0), ("volatility", 0.2))),
        ("train-b", "train", 0, (("momentum", 0.0), ("volatility", 1.0))),
        ("train-c", "train", 1, (("momentum", 2.0), ("volatility", 1.8))),
        ("validation-a", "validation", -1, (("momentum", -1.5), ("volatility", 0.3))),
        ("validation-b", "validation", 0, (("momentum", 0.1), ("volatility", 0.9))),
        ("validation-c", "validation", 1, (("momentum", 1.7), ("volatility", 1.6))),
    )
    request = LearningResourceRequest(
        training_samples=3,
        validation_samples=3,
        feature_count=2,
        max_steps=30,
        max_runtime_seconds=30,
        memory_limit_bytes=512 * 1024 * 1024,
    )
    attempt = LearningTrainingAttempt(
        id=uuid4(),
        workspace_id=workspace_id,
        actor_membership_id=uuid4(),
        idempotency_key=uuid4(),
        manifest_hash="a" * 64,
        split_policy_version="split-v2",
        fold_plan_hash="b" * 64,
        approval_hash="c" * 64,
        parameter_family_hash="d" * 64,
        trial_index=0,
        trial_count=1,
        fold_id="e" * 64,
        seed=23,
        trainer_version="cpu-logistic-v1",
        dependency_fingerprint="f" * 64,
        resource_policy_version="resource-v1",
        resource_request=request,
        resource_plan=assess_learning_resource_request(request),
    )
    rows: list[SimpleNamespace] = []
    training_label_index = 0
    for sample_id, role, label, features in sample_specs:
        if role == "train" and training_label_override is not None:
            label = training_label_override[training_label_index]
            training_label_index += 1
        if role == "validation" and sample_id == "validation-a" and validation_feature_override:
            features = validation_feature_override
        sample = NormalizedLearningSample(
            sample_id=sample_id,
            outcome_id=f"outcome-{sample_id}",
            outcome_hash="1" * 64,
            feature_snapshot_hash="2" * 64,
            feature_schema_hash="3" * 64,
            label_hash="4" * 64,
            label_value=label,
            features=features,
        )
        dataset = NormalizedLearningDataset(
            workspace_id=workspace_id,
            manifest_hash=attempt.manifest_hash,
            split_policy_version=attempt.split_policy_version,
            fold_plan_hash=attempt.fold_plan_hash,
            samples=(sample,),
            assignments=(),
            content_hash="5" * 64,
        )
        persisted = NormalizedLearningSampleRecord.from_domain(dataset, sample)
        rows.append(
            SimpleNamespace(
                attempt_id=attempt.id,
                workspace_id=workspace_id,
                normalized_dataset_id=dataset_id,
                manifest_hash=attempt.manifest_hash,
                split_policy_version=attempt.split_policy_version,
                fold_plan_hash=attempt.fold_plan_hash,
                approval_hash=attempt.approval_hash,
                fold_id=attempt.fold_id,
                seed=attempt.seed,
                trainer_version=attempt.trainer_version,
                dependency_fingerprint=attempt.dependency_fingerprint,
                sample_id=persisted.sample_id,
                role=role,
                outcome_id=persisted.outcome_id,
                outcome_hash=persisted.outcome_hash,
                feature_snapshot_hash=persisted.feature_snapshot_hash,
                feature_schema_hash=persisted.feature_schema_hash,
                label_hash=persisted.label_hash,
                label_value=persisted.label_value,
                features=persisted.features,
                sample_hash=persisted.sample_hash,
            )
        )
    return authorized_training_batch_from_rows(
        attempt=attempt,
        normalized_dataset_id=dataset_id,
        rows=rows,
        lineage_rows=[
            SimpleNamespace(
                attempt_id=attempt.id,
                workspace_id=workspace_id,
                normalized_dataset_id=dataset_id,
                manifest_hash=attempt.manifest_hash,
                split_policy_version=attempt.split_policy_version,
                fold_plan_hash=attempt.fold_plan_hash,
                approval_hash=attempt.approval_hash,
                fold_id=attempt.fold_id,
                sample_id=row.sample_id,
                role=row.role,
                strategy_release_id="h03-v1",
                strategy_release_content_hash="6" * 64,
                vector_schema_version="crypidian-learning-feature-vector-v1",
                vector_schema_hash=row.feature_schema_hash,
                vector_hash=hashlib.sha256(row.sample_id.encode()).hexdigest(),
            )
            for row in rows
        ],
    )


def test_cpu_trainer_emits_reproducible_diagnostic_artifact_without_scoring_authority() -> None:
    batch = _batch()

    first = train_authorized_batch(batch)
    second = train_authorized_batch(batch)

    assert first.artifact_hash == second.artifact_hash
    assert first.canonical_json == second.canonical_json
    assert [model.label for model in first.models] == [-1, 0, 1]
    assert first.validation_report.sample_count == 3
    assert sum(sum(row) for row in first.validation_report.confusion_matrix) == 3
    assert first.calibrated is False
    assert first.scorer_state == "abstain"
    assert first.artifact_hash == hashlib.sha256(first.canonical_json.encode("utf-8")).hexdigest()


def test_scorer_artifact_v2_binds_authorized_training_lineage_without_sample_payload() -> None:
    batch = _batch()
    base_artifact = train_authorized_batch(batch)

    scorer_artifact = build_calibrated_scorer_artifact(batch, base_artifact)
    payload = json.loads(scorer_artifact.canonical_json)

    assert scorer_artifact.base_artifact_hash == base_artifact.artifact_hash
    assert scorer_artifact.strategy_release_id == "h03-v1"
    assert scorer_artifact.strategy_release_content_hash == "6" * 64
    assert scorer_artifact.vector_schema_version == "crypidian-learning-feature-vector-v1"
    assert scorer_artifact.vector_schema_hash == batch.feature_schema_hash
    assert (
        scorer_artifact.training_membership_digest != scorer_artifact.validation_membership_digest
    )
    assert (
        scorer_artifact.artifact_hash
        == hashlib.sha256(scorer_artifact.canonical_json.encode("utf-8")).hexdigest()
    )
    assert payload["schema_version"] == "crypidian.learning.scorer-temperature.v2"
    assert "samples" not in payload
    assert "labels" not in payload
    assert scorer_artifact.state == "abstain"
    assert scorer_artifact.reason_code == "insufficient_support"
    verified = VerifiedLearningScorerArtifact.from_process_result(
        batch,
        LearningCpuTrainingProcessResult(
            artifact_json=base_artifact.canonical_json,
            artifact_hash=base_artifact.artifact_hash,
            elapsed_milliseconds=123,
            cpu_time_milliseconds=98,
            peak_memory_bytes=4096,
            process_id=456,
            scorer_artifact_json=scorer_artifact.canonical_json,
            scorer_artifact_hash=scorer_artifact.artifact_hash,
        ),
    )
    assert verified.artifact_hash == scorer_artifact.artifact_hash


def test_scorer_artifact_v2_calibrates_only_validation_logits_and_missing_policy_abstains() -> None:
    batch = _batch()
    base_artifact = train_authorized_batch(batch)
    policy = TemperatureCalibrationPolicy(
        policy_version="unit-temperature-v1",
        minimum_temperature=0.05,
        maximum_temperature=20.0,
        minimum_samples=3,
        minimum_class_support=1,
        relative_tolerance=1e-10,
        maximum_iterations=256,
    )

    calibrated = build_calibrated_scorer_artifact(batch, base_artifact, policy=policy)
    missing_policy = build_calibrated_scorer_artifact(batch, base_artifact, policy=None)

    assert calibrated.state == "ready_for_evaluation"
    assert calibrated.reason_code is None
    assert 0.05 < calibrated.temperature < 20.0
    assert calibrated.validation_class_support == ((-1, 1), (0, 1), (1, 1))
    assert calibrated.validation_nll_after < calibrated.validation_nll_before
    assert calibrated.training_membership_digest != calibrated.validation_membership_digest
    assert missing_policy.state == "abstain"
    assert missing_policy.reason_code == "calibration_policy_missing"
    assert missing_policy.temperature is None


def test_scorer_artifact_verifier_rejects_rebound_strategy_lineage() -> None:
    batch = _batch()
    base_artifact = train_authorized_batch(batch)
    scorer_artifact = build_calibrated_scorer_artifact(batch, base_artifact)
    payload = json.loads(scorer_artifact.canonical_json)
    payload["strategy_release_id"] = "forged-release"
    forged_json = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(ValueError, match="lineage khác batch"):
        VerifiedLearningScorerArtifact.from_process_result(
            batch,
            LearningCpuTrainingProcessResult(
                artifact_json=base_artifact.canonical_json,
                artifact_hash=base_artifact.artifact_hash,
                elapsed_milliseconds=123,
                cpu_time_milliseconds=98,
                peak_memory_bytes=4096,
                process_id=456,
                scorer_artifact_json=forged_json,
                scorer_artifact_hash=hashlib.sha256(forged_json.encode("utf-8")).hexdigest(),
            ),
        )


def test_scorer_artifact_v2_cannot_be_built_from_a_batch_containing_test_rows() -> None:
    batch = _batch()
    base_artifact = train_authorized_batch(batch)
    test_row = SimpleNamespace(
        attempt_id=batch.attempt_id,
        workspace_id=batch.workspace_id,
        normalized_dataset_id=batch.normalized_dataset_id,
        manifest_hash=batch.manifest_hash,
        split_policy_version=batch.split_policy_version,
        fold_plan_hash=batch.fold_plan_hash,
        approval_hash=batch.approval_hash,
        fold_id=batch.fold_id,
        seed=batch.seed,
        trainer_version=batch.trainer_version,
        dependency_fingerprint=batch.dependency_fingerprint,
        sample_id="test-only",
        role="test",
        outcome_id="test-outcome",
        outcome_hash="1" * 64,
        feature_snapshot_hash="2" * 64,
        feature_schema_hash=batch.feature_schema_hash,
        label_hash="3" * 64,
        label_value=1,
        features={"momentum": 1.0, "volatility": 1.0},
        sample_hash="4" * 64,
    )

    with pytest.raises(ValueError, match="role test"):
        authorized_training_batch_from_rows(
            attempt=LearningTrainingAttempt(
                id=batch.attempt_id,
                workspace_id=batch.workspace_id,
                actor_membership_id=uuid4(),
                idempotency_key=uuid4(),
                manifest_hash=batch.manifest_hash,
                split_policy_version=batch.split_policy_version,
                fold_plan_hash=batch.fold_plan_hash,
                approval_hash=batch.approval_hash,
                parameter_family_hash="d" * 64,
                trial_index=0,
                trial_count=1,
                fold_id=batch.fold_id,
                seed=batch.seed,
                trainer_version=batch.trainer_version,
                dependency_fingerprint=batch.dependency_fingerprint,
                resource_policy_version="resource-v1",
                resource_request=batch.resource_request,
                resource_plan=batch.resource_plan,
            ),
            normalized_dataset_id=batch.normalized_dataset_id,
            rows=[test_row],
            lineage_rows=[
                SimpleNamespace(
                    attempt_id=batch.attempt_id,
                    workspace_id=batch.workspace_id,
                    normalized_dataset_id=batch.normalized_dataset_id,
                    manifest_hash=batch.manifest_hash,
                    split_policy_version=batch.split_policy_version,
                    fold_plan_hash=batch.fold_plan_hash,
                    approval_hash=batch.approval_hash,
                    fold_id=batch.fold_id,
                    sample_id="test-only",
                    role="test",
                    strategy_release_id="h03-v1",
                    strategy_release_content_hash="6" * 64,
                    vector_schema_version="crypidian-learning-feature-vector-v1",
                    vector_schema_hash=batch.feature_schema_hash,
                    vector_hash="5" * 64,
                )
            ],
        )

    assert build_calibrated_scorer_artifact(batch, base_artifact).base_artifact_hash == (
        base_artifact.artifact_hash
    )


def test_missing_authoritative_opportunity_lineage_keeps_scorer_abstaining() -> None:
    source_batch = _batch()
    unbound_batch = replace(
        source_batch,
        samples=tuple(
            replace(
                sample,
                strategy_release_id=None,
                strategy_release_content_hash=None,
                vector_schema_version=None,
                vector_schema_hash=None,
                vector_hash=None,
            )
            for sample in source_batch.samples
        ),
    )
    base_artifact = train_authorized_batch(unbound_batch)

    scorer_artifact = build_calibrated_scorer_artifact(unbound_batch, base_artifact)

    assert scorer_artifact.state == "abstain"
    assert scorer_artifact.reason_code == "authoritative_lineage_missing"
    assert scorer_artifact.strategy_release_id is None
    assert scorer_artifact.vector_schema_version is None


def test_cpu_trainer_fits_normalization_on_train_only() -> None:
    baseline = train_authorized_batch(_batch())
    changed_validation = train_authorized_batch(
        _batch(validation_feature_override=(("momentum", 50_000.0), ("volatility", -80_000.0)))
    )

    assert baseline.feature_means == changed_validation.feature_means
    assert baseline.feature_scales == changed_validation.feature_scales
    assert baseline.models == changed_validation.models
    assert baseline.validation_report != changed_validation.validation_report


def test_majority_baseline_class_is_selected_from_train_not_validation() -> None:
    artifact = train_authorized_batch(_batch(training_label_override=(1, 1, -1)))

    assert artifact.training_class_counts == ((-1, 1), (0, 0), (1, 2))
    assert artifact.validation_report.majority_class == 1
    assert artifact.validation_report.majority_baseline_accuracy == 1 / 3


def test_cpu_artifact_counts_actual_training_and_validation_feature_visits() -> None:
    artifact = train_authorized_batch(_batch())

    assert artifact.actual_operation_units == 3 * (2 * 3 * 2 * 30 + 3 * 2)
    assert json.loads(artifact.canonical_json)["actual_operation_units"] == (
        artifact.actual_operation_units
    )


def test_verified_training_artifact_binds_process_result_to_authorized_batch() -> None:
    batch = _batch()
    artifact = train_authorized_batch(batch)
    process_result = LearningCpuTrainingProcessResult(
        artifact_json=artifact.canonical_json,
        artifact_hash=artifact.artifact_hash,
        elapsed_milliseconds=123,
        cpu_time_milliseconds=98,
        peak_memory_bytes=4096,
        process_id=456,
    )

    verified = VerifiedLearningTrainingArtifact.from_process_result(batch, process_result)

    assert verified.attempt_id == batch.attempt_id
    assert verified.batch_hash == batch.batch_hash
    assert verified.artifact_hash == artifact.artifact_hash
    assert verified.elapsed_milliseconds == 123
    assert verified.actual_operation_units == batch.resource_plan.estimated_operation_units
    record = LearningTrainingArtifactRecord.from_verified(verified)

    LearningTrainingArtifactRepository.validate_record(record)


def test_training_artifact_record_rejects_payload_or_metadata_tampering() -> None:
    batch = _batch()
    artifact = train_authorized_batch(batch)
    verified = VerifiedLearningTrainingArtifact.from_process_result(
        batch,
        LearningCpuTrainingProcessResult(
            artifact_json=artifact.canonical_json,
            artifact_hash=artifact.artifact_hash,
            elapsed_milliseconds=123,
            cpu_time_milliseconds=98,
            peak_memory_bytes=4096,
            process_id=456,
        ),
    )
    record = LearningTrainingArtifactRecord.from_verified(verified)
    record.artifact_hash = "0" * 64

    with pytest.raises(LearningTrainingArtifactRepositoryConflict, match="checksum"):
        LearningTrainingArtifactRepository.validate_record(record)


def test_verified_training_artifact_rejects_sample_payload_and_checksum_conflict() -> None:
    batch = _batch()
    artifact = train_authorized_batch(batch)
    payload = json.loads(artifact.canonical_json)
    payload["samples"] = []
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    process_result = LearningCpuTrainingProcessResult(
        artifact_json=canonical,
        artifact_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        elapsed_milliseconds=123,
        cpu_time_milliseconds=98,
        peak_memory_bytes=4096,
        process_id=456,
    )

    with pytest.raises(LearningTrainingArtifactConflict, match="sample payload"):
        VerifiedLearningTrainingArtifact.from_process_result(batch, process_result)

    with pytest.raises(LearningTrainingArtifactConflict, match="checksum"):
        VerifiedLearningTrainingArtifact.from_process_result(
            batch,
            LearningCpuTrainingProcessResult(
                artifact_json=artifact.canonical_json,
                artifact_hash="0" * 64,
                elapsed_milliseconds=123,
                cpu_time_milliseconds=98,
                peak_memory_bytes=4096,
                process_id=456,
            ),
        )


def test_verified_training_artifact_rejects_unknown_schema_version() -> None:
    batch = _batch()
    artifact = train_authorized_batch(batch)
    payload = json.loads(artifact.canonical_json)
    payload["schema_version"] = "future.unknown.schema"
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(LearningTrainingArtifactConflict, match="schema version"):
        VerifiedLearningTrainingArtifact.from_process_result(
            batch,
            LearningCpuTrainingProcessResult(
                artifact_json=canonical,
                artifact_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                elapsed_milliseconds=123,
                cpu_time_milliseconds=98,
                peak_memory_bytes=4096,
                process_id=456,
            ),
        )


def test_cpu_trainer_rejects_non_authorized_batch_type() -> None:
    with pytest.raises(ValueError, match="AuthorizedTrainerBatch"):
        train_authorized_batch(object())


def test_cpu_process_runner_returns_canonical_artifact_and_measured_child_usage() -> None:
    runner = LearningCpuTrainingProcessRunner()
    if not sys.platform.startswith("linux"):
        with pytest.raises(LearningTrainingProcessFailure) as error:
            runner.train(_batch())
        assert error.value.code is LearningTrainingProcessFailureCode.RESOURCE_LIMIT_UNAVAILABLE
        return

    result = runner.train(_batch())

    assert result.process_id != os.getpid()
    assert result.elapsed_milliseconds >= 0
    assert result.cpu_time_milliseconds >= 0
    assert result.peak_memory_bytes > 0
    assert result.artifact_hash == hashlib.sha256(result.artifact_json.encode("utf-8")).hexdigest()
    payload = json.loads(result.artifact_json)
    assert payload["scorer_state"] == "abstain"
    assert payload["calibrated"] is False
    assert "samples" not in payload


def test_pre_cancelled_training_does_not_spawn_a_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_bot.application import learning_training_process

    cancel_event = Event()
    cancel_event.set()

    def fail_if_spawned(_method: str):
        pytest.fail("Attempt đã bị hủy không được tạo child process.")

    monkeypatch.setattr(learning_training_process.multiprocessing, "get_context", fail_if_spawned)

    with pytest.raises(LearningTrainingProcessFailure) as error:
        LearningCpuTrainingProcessRunner().train(_batch(), cancel_event=cancel_event)

    assert error.value.code is LearningTrainingProcessFailureCode.CANCELLED

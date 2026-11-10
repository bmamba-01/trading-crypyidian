from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_bot.adapters.database.models.learning_fold_sample_entities import (
    NormalizedLearningSampleRecord,
)
from trading_bot.application.learning_fold_samples import (
    NormalizedLearningDataset,
    NormalizedLearningSample,
)
from trading_bot.application.learning_training_batch import (
    authorized_training_batch_from_rows,
)
from trading_bot.domain.learning.accounting import LearningTrainingAttempt
from trading_bot.domain.learning.resources import (
    LearningResourceRequest,
    assess_learning_resource_request,
)


def _attempt() -> LearningTrainingAttempt:
    request = LearningResourceRequest(1, 1, 2, 10, 30, 256 * 1024)
    return LearningTrainingAttempt(
        id=uuid4(),
        workspace_id=uuid4(),
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
        seed=7,
        trainer_version="cpu-logistic-v1",
        dependency_fingerprint="f" * 64,
        resource_policy_version="resource-v1",
        resource_request=request,
        resource_plan=assess_learning_resource_request(request),
    )


def _row(attempt: LearningTrainingAttempt, sample_id: str, role: str, label: int):
    sample = NormalizedLearningSample(
        sample_id=sample_id,
        outcome_id=f"outcome-{sample_id}",
        outcome_hash="1" * 64,
        feature_snapshot_hash="2" * 64,
        feature_schema_hash="3" * 64,
        label_hash="4" * 64,
        label_value=label,
        features=(("momentum", 0.25), ("volatility", 1.5)),
    )
    dataset = NormalizedLearningDataset(
        workspace_id=attempt.workspace_id,
        manifest_hash=attempt.manifest_hash,
        split_policy_version=attempt.split_policy_version,
        fold_plan_hash=attempt.fold_plan_hash,
        samples=(sample,),
        assignments=(),
        content_hash="5" * 64,
    )
    record = NormalizedLearningSampleRecord.from_domain(dataset, sample)
    return SimpleNamespace(
        attempt_id=attempt.id,
        workspace_id=attempt.workspace_id,
        normalized_dataset_id=uuid4(),
        manifest_hash=attempt.manifest_hash,
        split_policy_version=attempt.split_policy_version,
        fold_plan_hash=attempt.fold_plan_hash,
        approval_hash=attempt.approval_hash,
        fold_id=attempt.fold_id,
        seed=attempt.seed,
        trainer_version=attempt.trainer_version,
        dependency_fingerprint=attempt.dependency_fingerprint,
        sample_id=record.sample_id,
        role=role,
        outcome_id=record.outcome_id,
        outcome_hash=record.outcome_hash,
        feature_snapshot_hash=record.feature_snapshot_hash,
        feature_schema_hash=record.feature_schema_hash,
        label_hash=record.label_hash,
        label_value=record.label_value,
        features=record.features,
        sample_hash=record.sample_hash,
    )


def test_authorized_training_batch_contains_exact_train_validation_only() -> None:
    attempt = _attempt()
    dataset_id = uuid4()
    rows = [
        _row(attempt, "train-1", "train", -1),
        _row(attempt, "validation-1", "validation", 1),
    ]
    for row in rows:
        row.normalized_dataset_id = dataset_id
    lineage_rows = [
        SimpleNamespace(
            attempt_id=attempt.id,
            workspace_id=attempt.workspace_id,
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
            vector_hash="7" * 64,
        )
        for row in rows
    ]

    batch = authorized_training_batch_from_rows(
        attempt=attempt,
        normalized_dataset_id=dataset_id,
        rows=rows,
        lineage_rows=lineage_rows,
    )

    assert batch.attempt_id == attempt.id
    assert batch.normalized_dataset_id == dataset_id
    assert [sample.role for sample in batch.samples] == ["train", "validation"]
    assert all(sample.label_value in {-1, 0, 1} for sample in batch.samples)
    assert len(batch.batch_hash) == 64


def test_authorized_training_batch_rejects_mixed_strategy_release_lineage() -> None:
    attempt = _attempt()
    dataset_id = uuid4()
    rows = [
        _row(attempt, "train-1", "train", -1),
        _row(attempt, "validation-1", "validation", 1),
    ]
    for row in rows:
        row.normalized_dataset_id = dataset_id
    lineage_rows = [
        SimpleNamespace(
            attempt_id=attempt.id,
            workspace_id=attempt.workspace_id,
            normalized_dataset_id=dataset_id,
            manifest_hash=attempt.manifest_hash,
            split_policy_version=attempt.split_policy_version,
            fold_plan_hash=attempt.fold_plan_hash,
            approval_hash=attempt.approval_hash,
            fold_id=attempt.fold_id,
            sample_id=row.sample_id,
            role=row.role,
            strategy_release_id="h03-v1" if row.role == "train" else "h04-v1",
            strategy_release_content_hash="6" * 64,
            vector_schema_version="crypidian-learning-feature-vector-v1",
            vector_schema_hash=row.feature_schema_hash,
            vector_hash="7" * 64,
        )
        for row in rows
    ]

    with pytest.raises(ValueError, match="cùng strategy release"):
        authorized_training_batch_from_rows(
            attempt=attempt,
            normalized_dataset_id=dataset_id,
            rows=rows,
            lineage_rows=lineage_rows,
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("test_role", "role test"),
        ("foreign_workspace", "attempt"),
        ("tampered_hash", "sample_hash"),
        ("duplicate_sample", "sample_id"),
    ],
)
def test_authorized_training_batch_rejects_untrusted_gateway_rows(
    change: str,
    message: str,
) -> None:
    attempt = _attempt()
    dataset_id = uuid4()
    rows = [
        _row(attempt, "train-1", "train", -1),
        _row(attempt, "validation-1", "validation", 1),
    ]
    for row in rows:
        row.normalized_dataset_id = dataset_id
    lineage_rows = [
        SimpleNamespace(
            attempt_id=attempt.id,
            workspace_id=attempt.workspace_id,
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
            vector_hash="7" * 64,
        )
        for row in rows
    ]
    if change == "test_role":
        rows[0].role = "test"
    elif change == "foreign_workspace":
        rows[0].workspace_id = uuid4()
    elif change == "tampered_hash":
        rows[0].sample_hash = "0" * 64
    else:
        rows[1].sample_id = rows[0].sample_id

    with pytest.raises(ValueError, match=message):
        authorized_training_batch_from_rows(
            attempt=attempt,
            normalized_dataset_id=dataset_id,
            rows=rows,
            lineage_rows=lineage_rows,
        )

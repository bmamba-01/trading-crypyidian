from datetime import UTC, datetime
from uuid import uuid4

import pytest

from trading_bot.application.learning_fold_plan import build_manifest_fold_plan
from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSample, DatasetSplit
from trading_bot.domain.learning.evaluation import TemporalWindow, WalkForwardSplit

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def at(hour: int) -> datetime:
    from datetime import timedelta

    return BASE + timedelta(hours=hour)


def sample(sample_id: str, decision: int, outcome_end: int, split: DatasetSplit) -> DatasetSample:
    digest = ("a" if sample_id != "validation-overlap" else "b") * 64
    return DatasetSample(
        sample_id=sample_id,
        decision_at=at(decision),
        feature_available_at=at(decision - 1),
        outcome_start=at(decision),
        outcome_end=at(outcome_end),
        feature_snapshot_hash=digest,
        outcome_id=f"outcome-{sample_id}",
        outcome_hash=("c" if sample_id != "validation-overlap" else "d") * 64,
        label_hash=("e" if sample_id != "validation-overlap" else "f") * 64,
        label_value=1,
        label_version="labels-v1",
        split=split,
        source_dependent=False,
        source_rights_hashes=(),
        source_revision_refs=(),
    )


def manifest() -> DatasetManifest:
    return DatasetManifest(
        workspace_id=uuid4(),
        query_hash="1" * 64,
        symbols=("BTCUSDT",),
        data_start=at(0),
        data_end=at(10),
        as_of=at(10),
        feature_schema_version="features-v1",
        feature_version="feature-recipe-v1",
        label_version="labels-v1",
        sample_schema_version="samples-v1",
        split_policy_version="policy-v1",
        samples=(
            sample("train-safe", 1, 2, DatasetSplit.TRAIN),
            sample("train-overlap", 2, 4, DatasetSplit.TRAIN),
            sample("validation-safe", 4, 5, DatasetSplit.VALIDATION),
            sample("validation-overlap", 4, 6, DatasetSplit.VALIDATION),
            sample("test-safe", 6, 7, DatasetSplit.TEST),
        ),
        exclusions=(),
        holdout_sealed_at=None,
    )


def split(
    train_start: int = 0,
    train_end: int = 3,
    validation_end: int = 5,
    test_end: int = 8,
) -> WalkForwardSplit:
    return WalkForwardSplit(
        train=TemporalWindow(at(train_start), at(train_end)),
        validation=TemporalWindow(at(train_end), at(validation_end)),
        test=TemporalWindow(at(validation_end), at(test_end)),
    )


def test_manifest_plan_binds_authoritative_hashes_and_purges_overlapping_outcomes() -> None:
    dataset = manifest()
    plan = build_manifest_fold_plan(
        dataset,
        folds=(split(),),
        expected_split_policy_version="policy-v1",
        evaluation_cutoff=at(8),
    )

    assert plan.manifest_hash == dataset.content_hash
    assert plan.split_policy_version == dataset.split_policy_version
    assert plan.evaluation_cutoff == at(8)
    assert plan.folds[0].train_sample_ids == ("train-safe",)
    assert plan.folds[0].validation_sample_ids == ("validation-safe",)
    assert plan.folds[0].test_sample_ids == ("test-safe",)
    assert plan.folds[0].excluded_sample_ids == (
        "train-overlap",
        "validation-overlap",
    )
    assert len(plan.content_hash) == 64


def test_manifest_plan_is_deterministic_across_candidate_order() -> None:
    original = manifest()
    reordered = DatasetManifest(
        workspace_id=original.workspace_id,
        query_hash=original.query_hash,
        symbols=original.symbols,
        data_start=original.data_start,
        data_end=original.data_end,
        as_of=original.as_of,
        feature_schema_version=original.feature_schema_version,
        feature_version=original.feature_version,
        label_version=original.label_version,
        sample_schema_version=original.sample_schema_version,
        split_policy_version=original.split_policy_version,
        samples=tuple(reversed(original.samples)),
        exclusions=(),
        holdout_sealed_at=None,
    )
    first = build_manifest_fold_plan(
        original,
        folds=(split(),),
        expected_split_policy_version="policy-v1",
        evaluation_cutoff=at(8),
    )
    second = build_manifest_fold_plan(
        reordered,
        folds=(split(),),
        expected_split_policy_version="policy-v1",
        evaluation_cutoff=at(8),
    )
    assert first.manifest_hash == second.manifest_hash
    assert first.content_hash == second.content_hash


@pytest.mark.parametrize(
    ("folds", "expected_policy", "cutoff", "message"),
    [
        ((split(),), "different-policy", at(8), "split policy"),
        (
            (split(train_start=-2, train_end=-1, validation_end=1, test_end=2),),
            "policy-v1",
            at(8),
            "phạm vi manifest",
        ),
        ((split(test_end=11),), "policy-v1", at(10), "phạm vi manifest"),
        ((split(), split()), "policy-v1", at(8), "Fold bị lặp"),
        ((split(),), "policy-v1", at(7), "evaluation_cutoff"),
    ],
)
def test_manifest_plan_rejects_incompatible_policy_range_duplicates_and_cutoff(
    folds: tuple[WalkForwardSplit, ...],
    expected_policy: str,
    cutoff: datetime,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_manifest_fold_plan(
            manifest(),
            folds=folds,
            expected_split_policy_version=expected_policy,
            evaluation_cutoff=cutoff,
        )


def test_manifest_plan_rejects_sample_split_that_disagrees_with_fold_role() -> None:
    with pytest.raises(ValueError, match="split assignment"):
        build_manifest_fold_plan(
            manifest(),
            folds=(split(train_end=5, validation_end=7),),
            expected_split_policy_version="policy-v1",
            evaluation_cutoff=at(8),
        )


def test_manifest_plan_rejects_reusing_prior_test_window_as_next_validation() -> None:
    first = split()
    second = split(train_end=4, validation_end=7, test_end=9)
    with pytest.raises(ValueError, match="không tái dùng cửa sổ test"):
        build_manifest_fold_plan(
            manifest(),
            folds=(first, second),
            expected_split_policy_version="policy-v1",
            evaluation_cutoff=at(10),
        )

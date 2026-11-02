from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

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


def development_manifest() -> DatasetManifest:
    return replace(
        manifest(),
        manifest_version="dataset-manifest-v2",
        data_end=at(16),
        as_of=at(16),
        samples=tuple(replace(item, split=DatasetSplit.DEVELOPMENT) for item in manifest().samples)
        + (
            sample("validation-next", 9, 10, DatasetSplit.DEVELOPMENT),
            sample("test-next", 11, 12, DatasetSplit.DEVELOPMENT),
        ),
    )


@pytest.mark.parametrize(
    "fixed_split", [DatasetSplit.TRAIN, DatasetSplit.VALIDATION, DatasetSplit.TEST]
)
def test_v2_rejects_manifests_containing_only_fixed_role(fixed_split: DatasetSplit) -> None:
    with pytest.raises(ValueError, match="v2"):
        replace(
            manifest(),
            manifest_version="dataset-manifest-v2",
            samples=(sample("fixed", 1, 2, fixed_split),),
        )


def plan_for(dataset: DatasetManifest, *, train_start: int = 0):
    return build_manifest_fold_plan(
        dataset,
        folds=(split(), split(train_start, 8, 10, 13)),
        expected_split_policy_version=dataset.split_policy_version,
        evaluation_cutoff=at(13),
    )


@pytest.mark.parametrize("train_start", [0, 3])
def test_v2_expanding_and_rolling_folds_reassign_old_validation_and_test_to_train(
    train_start: int,
) -> None:
    dataset = development_manifest()
    plan = plan_for(dataset, train_start=train_start)
    first, second = plan.folds
    assert first.train_sample_ids == ("train-safe",)
    assert first.validation_sample_ids == ("validation-safe",)
    assert first.test_sample_ids == ("test-safe",)
    assert [(item.role, item.sample_id, item.reasons) for item in first.exclusions] == [
        ("train", "train-overlap", ("label_incomplete_at_cutoff",)),
        ("validation", "validation-overlap", ("label_incomplete_at_cutoff",)),
    ]
    expected_train = ("test-safe", "validation-overlap", "validation-safe")
    if train_start == 0:
        expected_train = (
            "test-safe",
            "train-overlap",
            "train-safe",
            "validation-overlap",
            "validation-safe",
        )
    assert second.train_sample_ids == expected_train
    assert second.validation_sample_ids == ("validation-next",)
    assert second.test_sample_ids == ("test-next",)
    assert second.exclusions == ()
    for fold in plan.folds:
        assert len(
            {*fold.train_sample_ids, *fold.validation_sample_ids, *fold.test_sample_ids}
        ) == sum(
            len(ids)
            for ids in (fold.train_sample_ids, fold.validation_sample_ids, fold.test_sample_ids)
        )
    assert (
        plan.content_hash
        == plan_for(
            replace(dataset, samples=tuple(reversed(dataset.samples))), train_start=train_start
        ).content_hash
    )


@pytest.mark.parametrize("decision", [1, 4, 6, 9, 11])
def test_v2_rejects_holdout_inside_any_fold_role(decision: int) -> None:
    dataset = development_manifest()
    dataset = replace(
        dataset,
        samples=dataset.samples + (sample("sealed", decision, decision + 1, DatasetSplit.HOLDOUT),),
        holdout_sealed_at=at(16),
    )
    with pytest.raises(ValueError, match="[Hh]oldout"):
        plan_for(dataset)


def test_v2_keeps_holdout_outside_fold_windows_separate() -> None:
    dataset = development_manifest()
    sealed = replace(
        dataset,
        samples=dataset.samples + (sample("sealed", 14, 15, DatasetSplit.HOLDOUT),),
        holdout_sealed_at=at(16),
    )
    plan = plan_for(sealed)
    assert all(
        "sealed"
        not in (
            *fold.train_sample_ids,
            *fold.validation_sample_ids,
            *fold.test_sample_ids,
            *fold.excluded_sample_ids,
        )
        for fold in plan.folds
    )


@pytest.mark.parametrize(("test_outcome_end", "should_reject"), [(8, False), (9, True)])
def test_v2_protects_holdout_outcome_interval_at_half_open_test_boundary(
    test_outcome_end: int,
    should_reject: bool,
) -> None:
    dataset = development_manifest()
    samples = tuple(
        replace(item, outcome_end=at(test_outcome_end)) if item.sample_id == "test-safe" else item
        for item in dataset.samples
    )
    dataset = replace(
        dataset,
        samples=samples + (sample("sealed-edge", 8, 10, DatasetSplit.HOLDOUT),),
        holdout_sealed_at=at(16),
    )

    def build():
        return build_manifest_fold_plan(
            dataset,
            folds=(split(),),
            expected_split_policy_version="policy-v1",
            evaluation_cutoff=at(13),
        )

    if should_reject:
        with pytest.raises(ValueError, match="Holdout.*(interval|khoảng outcome)"):
            build()
    else:
        plan = build()
        assert plan.folds[0].test_sample_ids == ("test-safe",)


def test_v2_interval_lookup_handles_nested_holdouts() -> None:
    dataset = development_manifest()
    dataset = replace(
        dataset,
        data_end=at(22),
        as_of=at(21),
        samples=(
            sample("train-window", 6, 7, DatasetSplit.DEVELOPMENT),
            sample("validation-window", 7, 8, DatasetSplit.DEVELOPMENT),
            sample("test-window", 8, 13, DatasetSplit.DEVELOPMENT),
            sample("holdout-long", 1, 20, DatasetSplit.HOLDOUT),
            sample("holdout-short", 2, 3, DatasetSplit.HOLDOUT),
        ),
        holdout_sealed_at=at(21),
    )
    fold = WalkForwardSplit(
        train=TemporalWindow(at(6), at(7)),
        validation=TemporalWindow(at(7), at(8)),
        test=TemporalWindow(at(8), at(9)),
    )
    with pytest.raises(ValueError, match="Holdout outcome interval holdout-long"):
        build_manifest_fold_plan(
            dataset,
            folds=(fold,),
            expected_split_policy_version="policy-v1",
            evaluation_cutoff=at(21),
        )


@pytest.mark.parametrize(
    "fixed_split", [DatasetSplit.TRAIN, DatasetSplit.VALIDATION, DatasetSplit.TEST]
)
def test_v2_rejects_each_fixed_role_even_when_mixed_with_development(
    fixed_split: DatasetSplit,
) -> None:
    dataset = development_manifest()
    with pytest.raises(ValueError, match="v2"):
        replace(dataset, samples=dataset.samples + (sample("fixed", 14, 15, fixed_split),))


@pytest.mark.parametrize("version", ["dataset-manifest-v1", "legacy-custom", "dataset-manifest-v3"])
def test_development_split_requires_exact_v2_version(version: str) -> None:
    with pytest.raises(ValueError, match="v2"):
        replace(development_manifest(), manifest_version=version)


def test_legacy_bytes_hash_loader_and_fixed_roles_remain_unchanged() -> None:
    dataset = replace(manifest(), workspace_id=UUID("00000000-0000-0000-0000-000000000001"))
    assert (
        dataset.content_hash == "dbb32408b2b9792d656e86c1f2d3aa8041eab7efecca2652435b791114aef196"
    )
    for legacy in (dataset, replace(dataset, manifest_version="legacy-custom")):
        restored = DatasetManifest.from_canonical_json(
            legacy.canonical_json, expected_content_hash=legacy.content_hash
        )
        assert restored.canonical_json == legacy.canonical_json
        assert restored.content_hash == legacy.content_hash
        with pytest.raises(ValueError, match="split assignment"):
            build_manifest_fold_plan(
                restored,
                folds=(split(train_end=5, validation_end=7),),
                expected_split_policy_version="policy-v1",
                evaluation_cutoff=at(8),
            )


def test_v2_hash_binds_membership_windows_policy_cutoff_and_exclusion_reasons() -> None:
    dataset = development_manifest()
    baseline = plan_for(dataset)
    added = replace(
        dataset, samples=dataset.samples + (sample("train-extra", 1, 2, DatasetSplit.DEVELOPMENT),)
    )
    policy = replace(dataset, split_policy_version="policy-v2")
    overlap = replace(
        dataset,
        samples=tuple(
            replace(item, outcome_end=at(5)) if item.sample_id == "train-overlap" else item
            for item in dataset.samples
        ),
    )
    assert baseline.content_hash != plan_for(added).content_hash
    assert baseline.content_hash != plan_for(policy).content_hash
    assert baseline.content_hash != plan_for(dataset, train_start=1).content_hash
    changed = plan_for(overlap)
    assert changed.folds[0].exclusions[0].reasons == (
        "label_incomplete_at_cutoff",
        "protected_label_overlap",
    )
    assert changed.content_hash != baseline.content_hash
    later = build_manifest_fold_plan(
        dataset,
        folds=tuple(fold.split for fold in baseline.folds),
        expected_split_policy_version="policy-v1",
        evaluation_cutoff=at(14),
    )
    assert later.content_hash != baseline.content_hash


def test_v2_old_test_must_still_complete_before_later_training_cutoff() -> None:
    dataset = development_manifest()
    dataset = replace(
        dataset, samples=dataset.samples + (sample("test-late", 7, 11, DatasetSplit.DEVELOPMENT),)
    )
    first, second = plan_for(dataset).folds
    assert "test-late" in first.test_sample_ids
    assert "test-late" not in second.train_sample_ids
    assert ("label_incomplete_at_cutoff", "protected_label_overlap") == next(
        item.reasons for item in second.exclusions if item.sample_id == "test-late"
    )


def test_v2_test_outcome_after_evaluation_cutoff_is_excluded() -> None:
    dataset = development_manifest()
    dataset = replace(
        dataset,
        samples=dataset.samples + (sample("test-incomplete", 7, 15, DatasetSplit.DEVELOPMENT),),
    )
    first = plan_for(dataset).folds[0]
    assert first.test_sample_ids == ("test-safe",)
    assert next(
        item.reasons for item in first.exclusions if item.sample_id == "test-incomplete"
    ) == ("label_incomplete_at_cutoff",)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("duplicate", "duplicate"),
        ("feature", "Feature phải khả dụng"),
        ("as_of", "evaluation_cutoff không được sau manifest.as_of"),
        ("range", "Fold không tương thích với thứ tự hoặc phạm vi manifest"),
        ("cutoff", "evaluation_cutoff phải không trước cuối cửa sổ test"),
        ("reuse", "không tái dùng cửa sổ test"),
        ("empty", "Mỗi fold phải còn sample train, validation và test"),
    ],
)
def test_v2_retains_temporal_and_membership_guards(failure: str, message: str) -> None:
    dataset = development_manifest()

    def duplicate() -> object:
        return replace(dataset, samples=dataset.samples + (dataset.samples[0],))

    def feature() -> object:
        return replace(
            dataset.samples[0],
            feature_available_at=dataset.samples[0].decision_at.replace(day=2),
        )

    def as_of() -> object:
        return build_manifest_fold_plan(
            dataset,
            folds=(split(),),
            expected_split_policy_version="policy-v1",
            evaluation_cutoff=at(17),
        )

    def range_error() -> object:
        return build_manifest_fold_plan(
            dataset,
            folds=(split(train_start=-1),),
            expected_split_policy_version="policy-v1",
            evaluation_cutoff=at(13),
        )

    def cutoff() -> object:
        return build_manifest_fold_plan(
            dataset,
            folds=(split(),),
            expected_split_policy_version="policy-v1",
            evaluation_cutoff=at(7),
        )

    def reuse() -> object:
        return build_manifest_fold_plan(
            dataset,
            folds=(split(), split(train_end=4, validation_end=7, test_end=9)),
            expected_split_policy_version="policy-v1",
            evaluation_cutoff=at(13),
        )

    def empty() -> object:
        return plan_for(
            replace(
                dataset,
                samples=tuple(item for item in dataset.samples if item.sample_id != "test-next"),
            )
        )

    actions = {
        "duplicate": duplicate,
        "feature": feature,
        "as_of": as_of,
        "range": range_error,
        "cutoff": cutoff,
        "reuse": reuse,
        "empty": empty,
    }

    with pytest.raises(ValueError, match=message):
        actions[failure]()

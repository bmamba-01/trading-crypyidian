from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.domain.learning.dataset import (
    DatasetExclusion,
    DatasetManifest,
    DatasetSample,
    DatasetSplit,
)
from trading_bot.domain.learning.labels import (
    OutcomeLabelInput,
    build_outcome_label,
)

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
WORKSPACE_ID = uuid4()


def label_input(**overrides: object) -> OutcomeLabelInput:
    values: dict[str, object] = {
        "sample_id": "sample-1",
        "outcome_id": "trade-1",
        "outcome_hash": "a" * 64,
        "recipe_hash": "b" * 64,
        "expected_recipe_hash": "b" * 64,
        "decision_at": NOW,
        "feature_available_at": NOW - timedelta(minutes=1),
        "opened_at": NOW,
        "closed_at": NOW + timedelta(minutes=5),
        "net_pnl": "1.25",
        "outcome_complete": True,
        "fills_complete": True,
        "fees_complete": True,
        "funding_complete": True,
        "source_dependent": True,
        "source_rights_eligible": True,
        "source_rights_hash": "c" * 64,
        "label_version": "pnl-sign-v1",
    }
    values.update(overrides)
    return OutcomeLabelInput(**values)  # type: ignore[arg-type]


def sample(sample_id: str = "sample-1", split: DatasetSplit = DatasetSplit.TRAIN) -> DatasetSample:
    return DatasetSample(
        sample_id=sample_id,
        decision_at=NOW,
        feature_available_at=NOW - timedelta(minutes=1),
        outcome_start=NOW,
        outcome_end=NOW + timedelta(minutes=5),
        feature_snapshot_hash="d" * 64,
        outcome_id=f"trade-{sample_id}",
        outcome_hash="a" * 64,
        label_hash="e" * 64,
        label_value=1,
        label_version="pnl-sign-v1",
        split=split,
        source_dependent=True,
        source_rights_hashes=("c" * 64,),
        source_revision_refs=("source-a:revision-1",),
    )


def manifest(
    samples: tuple[DatasetSample, ...] | None = None,
    exclusions: tuple[DatasetExclusion, ...] = (),
) -> DatasetManifest:
    return DatasetManifest(
        workspace_id=WORKSPACE_ID,
        query_hash="f" * 64,
        symbols=("BTCUSDT",),
        data_start=NOW - timedelta(days=10),
        data_end=NOW + timedelta(days=1),
        as_of=NOW + timedelta(minutes=10),
        feature_schema_version="features-v1",
        feature_version="features-build-4",
        label_version="pnl-sign-v1",
        sample_schema_version="sample-v1",
        samples=(sample(),) if samples is None else samples,
        exclusions=exclusions,
        holdout_sealed_at=NOW
        if any(
            item.split is DatasetSplit.HOLDOUT
            for item in ((sample(),) if samples is None else samples)
        )
        else None,
    )


def test_label_requires_completed_outcome_and_all_cost_components() -> None:
    result = build_outcome_label(label_input())

    assert result.label.value == 1
    assert result.label.outcome_hash == "a" * 64
    assert result.label.label_version == "pnl-sign-v1"

    for field in ("outcome_complete", "fills_complete", "fees_complete", "funding_complete"):
        rejected = build_outcome_label(label_input(**{field: False}))
        assert rejected.label is None
        assert "incomplete_outcome" in rejected.exclusion_reasons


def test_label_excludes_recipe_mismatch_future_feature_and_unapproved_rights() -> None:
    mismatch = build_outcome_label(label_input(recipe_hash="9" * 64))
    future_feature = build_outcome_label(
        label_input(feature_available_at=NOW + timedelta(seconds=1))
    )
    preexisting_outcome = build_outcome_label(label_input(opened_at=NOW - timedelta(seconds=1)))
    rights = build_outcome_label(label_input(source_rights_eligible=False))

    assert mismatch.label is None
    assert "recipe_mismatch" in mismatch.exclusion_reasons
    assert future_feature.label is None
    assert "feature_not_available_at_decision" in future_feature.exclusion_reasons
    assert preexisting_outcome.label is None
    assert "outcome_started_before_decision" in preexisting_outcome.exclusion_reasons
    assert rights.label is None
    assert "source_rights_not_eligible" in rights.exclusion_reasons


def test_wp02_only_label_does_not_need_research_rights() -> None:
    result = build_outcome_label(
        label_input(
            source_dependent=False,
            source_rights_eligible=None,
            source_rights_hash=None,
        )
    )

    assert result.label is not None
    assert result.label.source_rights_hash is None


def test_non_dependent_label_rejects_research_rights_assertions() -> None:
    with pytest.raises(ValueError, match="không áp dụng"):
        label_input(source_dependent=False)


def test_label_rejects_naive_timestamps_and_invalid_hashes() -> None:
    with pytest.raises(ValueError, match="decision_at"):
        label_input(decision_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="outcome_hash"):
        label_input(outcome_hash="bad")


def test_manifest_hash_is_canonical_and_independent_of_sample_input_order() -> None:
    first = manifest(samples=(sample("b"), sample("a")))
    second = manifest(samples=(sample("a"), sample("b")))

    assert first.content_hash == second.content_hash
    assert first.canonical_json == second.canonical_json


def test_manifest_rejects_duplicate_or_out_of_range_samples() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        manifest(samples=(sample("a"), sample("a")))
    with pytest.raises(ValueError, match="range"):
        manifest(
            samples=(
                replace(
                    sample(),
                    decision_at=NOW + timedelta(days=2),
                    outcome_start=NOW + timedelta(days=2),
                    outcome_end=NOW + timedelta(days=2, minutes=5),
                ),
            )
        )
    with pytest.raises(ValueError, match="label_value"):
        manifest(samples=(replace(sample(), label_value=2),))
    with pytest.raises(ValueError, match="rights hash"):
        manifest(samples=(replace(sample(), source_rights_hashes=()),))
    with pytest.raises(ValueError, match="source revision"):
        manifest(samples=(replace(sample(), source_revision_refs=()),))


def test_manifest_pins_complete_outcome_interval_and_source_revision_lineage() -> None:
    completed_after_as_of = replace(sample(), outcome_end=NOW + timedelta(minutes=11))
    with pytest.raises(ValueError, match="outcome_end.*as_of"):
        manifest(samples=(completed_after_as_of,))

    baseline = manifest()
    changed_interval = manifest(
        samples=(replace(sample(), outcome_start=NOW + timedelta(minutes=1)),)
    )
    assert baseline.content_hash != changed_interval.content_hash
    assert "source_revision_refs" in baseline.canonical_json


def test_manifest_requires_explicit_holdout_seal_when_holdout_samples_exist() -> None:
    holdout = sample("holdout", DatasetSplit.HOLDOUT)
    with pytest.raises(ValueError, match="sealed"):
        DatasetManifest(
            workspace_id=WORKSPACE_ID,
            query_hash="f" * 64,
            symbols=("BTCUSDT",),
            data_start=NOW - timedelta(days=10),
            data_end=NOW + timedelta(days=1),
            as_of=NOW + timedelta(minutes=10),
            feature_schema_version="features-v1",
            feature_version="features-build-4",
            label_version="pnl-sign-v1",
            sample_schema_version="sample-v1",
            samples=(holdout,),
            exclusions=(),
            holdout_sealed_at=None,
        )


def test_manifest_hash_changes_with_version_exclusion_or_holdout_state() -> None:
    baseline = manifest()
    new_feature_version = replace(baseline, feature_version="features-build-5")
    excluded = manifest(exclusions=(DatasetExclusion("sample-2", ("missing_feature",)),))

    assert baseline.content_hash != new_feature_version.content_hash
    assert baseline.content_hash != excluded.content_hash

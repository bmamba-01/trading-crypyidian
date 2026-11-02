from datetime import UTC, datetime, timedelta

import pytest

from trading_bot.application.learning_temporal_evaluation import select_walk_forward_samples
from trading_bot.domain.learning.evaluation import (
    SampleLineage,
    TemporalWindow,
    WalkForwardSplit,
)

ORIGIN = datetime(2026, 1, 1, tzinfo=UTC)


def at(hours: int) -> datetime:
    return ORIGIN + timedelta(hours=hours)


def lineage(
    sample_id: str,
    decision_hour: int,
    label_start_hour: int,
    label_end_hour: int,
    feature_available_hour: int,
) -> SampleLineage:
    return SampleLineage(
        sample_id=sample_id,
        decision_at=at(decision_hour),
        feature_available_at=at(feature_available_hour),
        label_interval=TemporalWindow(at(label_start_hour), at(label_end_hour)),
    )


def test_walk_forward_selection_purges_label_overlap_and_checks_point_in_time() -> None:
    split = WalkForwardSplit(
        train=TemporalWindow(at(0), at(10)),
        validation=TemporalWindow(at(10), at(15)),
        test=TemporalWindow(at(15), at(20)),
    )
    samples = (
        lineage("train-safe", 5, 5, 7, 4),
        lineage("train-overlap-validation", 8, 8, 11, 3),
        lineage("train-overlap-test", 7, 7, 16, 2),
        lineage("train-future-feature", 6, 6, 7, 7),
        lineage("validation-safe", 12, 12, 14, 11),
        lineage("validation-overlap-test", 14, 14, 16, 11),
        lineage("test-safe", 17, 17, 19, 16),
    )

    selected = select_walk_forward_samples(
        samples,
        split=split,
        evaluation_cutoff=at(20),
    )

    assert selected.train_sample_ids == ("train-safe",)
    assert selected.validation_sample_ids == ("validation-safe",)
    assert selected.test_sample_ids == ("test-safe",)
    assert selected.excluded_sample_ids == (
        "train-future-feature",
        "train-overlap-test",
        "train-overlap-validation",
        "validation-overlap-test",
    )


def test_shared_historical_lookback_is_allowed_when_available_at_decision() -> None:
    split = WalkForwardSplit(
        train=TemporalWindow(at(0), at(10)),
        validation=TemporalWindow(at(10), at(15)),
        test=TemporalWindow(at(15), at(20)),
    )
    selected = select_walk_forward_samples(
        (
            lineage("train", 8, 8, 9, 2),
            lineage("validation", 12, 12, 14, 3),
            lineage("test", 17, 17, 19, 4),
        ),
        split=split,
        evaluation_cutoff=at(20),
    )

    assert selected.train_sample_ids == ("train",)
    assert selected.validation_sample_ids == ("validation",)
    assert selected.test_sample_ids == ("test",)
    assert selected.excluded_sample_ids == ()


def test_selection_does_not_assign_samples_outside_fold_windows() -> None:
    split = WalkForwardSplit(
        train=TemporalWindow(at(0), at(10)),
        validation=TemporalWindow(at(10), at(15)),
        test=TemporalWindow(at(15), at(20)),
    )
    selected = select_walk_forward_samples(
        (
            lineage("left-boundary", 0, 0, 1, 0),
            lineage("train-last", 9, 9, 10, 8),
            lineage("validation-first-boundary", 10, 10, 11, 9),
            lineage("test-end-boundary", 20, 20, 21, 19),
        ),
        split=split,
        evaluation_cutoff=at(22),
    )

    assert selected.train_sample_ids == ("left-boundary", "train-last")
    assert selected.validation_sample_ids == ("validation-first-boundary",)
    assert selected.test_sample_ids == ()
    assert selected.excluded_sample_ids == ()


@pytest.mark.parametrize(
    ("train", "validation", "test"),
    [
        (
            TemporalWindow(at(0), at(11)),
            TemporalWindow(at(10), at(15)),
            TemporalWindow(at(15), at(20)),
        ),
        (
            TemporalWindow(at(10), at(12)),
            TemporalWindow(at(5), at(15)),
            TemporalWindow(at(15), at(20)),
        ),
        (
            TemporalWindow(at(0), at(10)),
            TemporalWindow(at(10), at(15)),
            TemporalWindow(at(14), at(20)),
        ),
    ],
)
def test_selection_rejects_overlapping_or_out_of_order_fold_windows(
    train: TemporalWindow,
    validation: TemporalWindow,
    test: TemporalWindow,
) -> None:
    with pytest.raises(ValueError, match="theo thứ tự"):
        select_walk_forward_samples(
            (),
            split=WalkForwardSplit(train=train, validation=validation, test=test),
            evaluation_cutoff=at(20),
        )

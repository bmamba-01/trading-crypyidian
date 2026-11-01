"""Kiểm tra loại mẫu sai thời điểm mà không cấm dùng chung dữ liệu quá khứ."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from trading_bot.domain.learning import evaluation

ORIGIN = datetime(2026, 1, 1, tzinfo=UTC)


def at(hours: int) -> datetime:
    return ORIGIN + timedelta(hours=hours)


def sample(sample_id: str = "sample", **changes: Any) -> Any:
    values = {
        "sample_id": sample_id,
        "decision_at": at(3),
        "feature_available_at": at(2),
        "label_interval": evaluation.TemporalWindow(at(3), at(5)),
    }
    values.update(changes)
    return evaluation.SampleLineage(**values)


def test_selection_excludes_future_features_incomplete_labels_and_overlap() -> None:
    samples = (
        sample("z-eligible"),
        sample("a-future", feature_available_at=at(4)),
        sample("b-unfinished", label_interval=evaluation.TemporalWindow(at(3), at(11))),
        sample("c-overlap", label_interval=evaluation.TemporalWindow(at(3), at(7))),
        sample(
            "d-multiple",
            feature_available_at=at(4),
            label_interval=evaluation.TemporalWindow(at(3), at(11)),
        ),
    )
    protected = (
        sample(
            "holdout", decision_at=at(6), label_interval=evaluation.TemporalWindow(at(6), at(8))
        ),
    )

    result = evaluation.select_temporal_samples(
        samples, evaluation_cutoff=at(10), protected_samples=protected
    )

    assert result.eligible_sample_ids == ("z-eligible",)
    assert [
        (item.sample_id, tuple(reason.value for reason in item.reasons))
        for item in result.exclusions
    ] == [
        ("a-future", ("feature_unavailable_at_decision",)),
        ("b-unfinished", ("label_incomplete_at_cutoff", "protected_label_overlap")),
        ("c-overlap", ("protected_label_overlap",)),
        (
            "d-multiple",
            (
                "feature_unavailable_at_decision",
                "label_incomplete_at_cutoff",
                "protected_label_overlap",
            ),
        ),
    ]
    assert (
        evaluation.select_temporal_samples(
            tuple(reversed(samples)), evaluation_cutoff=at(10), protected_samples=protected
        )
        == result
    )


def test_half_open_label_boundaries_and_shared_available_history_remain_eligible() -> None:
    protected = sample(
        "validation", decision_at=at(5), label_interval=evaluation.TemporalWindow(at(5), at(7))
    )
    result = evaluation.select_temporal_samples(
        (
            sample("before", feature_available_at=at(3)),
            sample(
                "after", decision_at=at(7), label_interval=evaluation.TemporalWindow(at(7), at(9))
            ),
        ),
        evaluation_cutoff=at(9),
        protected_samples=(protected,),
    )

    assert result.eligible_sample_ids == ("after", "before")
    assert result.exclusions == ()


def test_lineage_is_immutable_and_empty_selection_is_explicit() -> None:
    lineage = sample()
    with pytest.raises(FrozenInstanceError):
        lineage.sample_id = "changed"
    result = evaluation.select_temporal_samples((), evaluation_cutoff=at(10))
    assert result.eligible_sample_ids == ()
    assert result.exclusions == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"sample_id": ""},
        {"sample_id": " "},
        {"sample_id": None},
        {"decision_at": datetime(2026, 1, 1)},  # noqa: DTZ001
        {"feature_available_at": datetime(2026, 1, 1)},  # noqa: DTZ001
        {"decision_at": None},
        {"feature_available_at": None},
        {"label_interval": None},
        {"label_interval": {"start": at(3), "end": at(5)}},
        {"label_interval": evaluation.TemporalWindow(at(2), at(4))},
    ],
)
def test_lineage_rejects_missing_naive_unknown_or_reversed_information(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="phải|không|múi giờ"):
        sample(**changes)


@pytest.mark.parametrize("offsets", [(3, 3), (5, 3)])
def test_label_interval_rejects_empty_or_reversed_time(offsets: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="start < end"):
        sample(label_interval=evaluation.TemporalWindow(at(offsets[0]), at(offsets[1])))


def test_label_interval_rejects_naive_time() -> None:
    with pytest.raises(ValueError, match="múi giờ"):
        sample(label_interval=evaluation.TemporalWindow(datetime(2026, 1, 1), at(5)))  # noqa: DTZ001


@pytest.mark.parametrize(
    "location",
    ["candidates", "protected", "both"],
)
def test_selection_rejects_duplicate_ids_in_or_across_roles(location: str) -> None:
    first = sample()
    different = replace(first, feature_available_at=at(1))
    candidates = (first, different) if location == "candidates" else (first,)
    protected = (different, different) if location == "protected" else ()
    if location == "both":
        protected = (different,)
    with pytest.raises(ValueError, match="sample_id"):
        evaluation.select_temporal_samples(
            candidates, evaluation_cutoff=at(10), protected_samples=protected
        )


@pytest.mark.parametrize("invalid", [None, {}, object(), evaluation.TemporalWindow(at(3), at(5))])
@pytest.mark.parametrize("protected", [False, True])
def test_selection_rejects_unknown_lineage_for_candidates_and_protected(
    invalid: Any, protected: bool
) -> None:
    with pytest.raises(ValueError, match="SampleLineage"):
        evaluation.select_temporal_samples(
            () if protected else (invalid,),
            evaluation_cutoff=at(10),
            protected_samples=(invalid,) if protected else (),
        )


def test_selection_rejects_naive_evaluation_cutoff() -> None:
    with pytest.raises(ValueError, match="múi giờ"):
        evaluation.select_temporal_samples((sample(),), evaluation_cutoff=datetime(2026, 1, 1))  # noqa: DTZ001


def test_future_feature_in_repeated_dst_hour_is_excluded_by_absolute_time() -> None:
    zone = ZoneInfo("America/New_York")
    decision_at = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    feature_available_at = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
    lineage = sample(
        "dst-feature",
        decision_at=decision_at,
        feature_available_at=feature_available_at,
        label_interval=evaluation.TemporalWindow(
            decision_at, datetime(2026, 11, 1, 2, 0, tzinfo=zone, fold=1)
        ),
    )

    result = evaluation.select_temporal_samples(
        (lineage,), evaluation_cutoff=datetime(2026, 11, 1, 3, 0, tzinfo=zone)
    )

    assert result.eligible_sample_ids == ()
    assert result.exclusions[0].sample_id == "dst-feature"
    assert result.exclusions[0].reasons == (
        evaluation.SampleExclusionReason.FEATURE_UNAVAILABLE_AT_DECISION,
    )

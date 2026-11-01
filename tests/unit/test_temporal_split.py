from datetime import UTC, datetime, timedelta

import pytest

from trading_bot.domain.learning.evaluation import (
    TemporalWindow,
    purge_overlapping_samples,
    walk_forward_splits,
)


def test_walk_forward_splits_are_chronological_with_purge_and_embargo_gaps() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    splits = walk_forward_splits(
        TemporalWindow(start, start + timedelta(days=30)),
        train=timedelta(days=10),
        validation=timedelta(days=5),
        test=timedelta(days=5),
        step=timedelta(days=5),
        purge=timedelta(days=1),
        embargo=timedelta(days=2),
    )

    assert len(splits) == 2
    for split in splits:
        assert split.train.end + timedelta(days=1) == split.validation.start
        assert split.validation.end + timedelta(days=2) == split.test.start
        assert split.train.start < split.train.end
        assert split.validation.start < split.validation.end
        assert split.test.start < split.test.end
    assert splits[1].train.start > splits[0].train.start


@pytest.mark.parametrize(
    "kwargs",
    [
        {"train": timedelta(0)},
        {"validation": timedelta(0)},
        {"test": timedelta(0)},
        {"step": timedelta(0)},
        {"purge": timedelta(days=-1)},
        {"embargo": timedelta(days=-1)},
    ],
)
def test_walk_forward_rejects_invalid_durations(kwargs: dict[str, timedelta]) -> None:
    values = {
        "train": timedelta(days=10),
        "validation": timedelta(days=5),
        "test": timedelta(days=5),
        "step": timedelta(days=5),
        "purge": timedelta(0),
        "embargo": timedelta(0),
    }
    values.update(kwargs)

    with pytest.raises(ValueError, match="phải|không"):
        walk_forward_splits(
            TemporalWindow(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)),
            **values,
        )


def test_purge_overlapping_samples_removes_any_interval_touching_protected_data() -> None:
    origin = datetime(2026, 1, 1, tzinfo=UTC)
    samples = (
        TemporalWindow(origin, origin + timedelta(hours=1)),
        TemporalWindow(origin + timedelta(hours=1), origin + timedelta(hours=2)),
        TemporalWindow(origin + timedelta(hours=2), origin + timedelta(hours=3)),
        TemporalWindow(origin + timedelta(hours=3), origin + timedelta(hours=4)),
    )
    protected = (
        TemporalWindow(origin + timedelta(minutes=30), origin + timedelta(hours=1, minutes=30)),
    )

    kept = purge_overlapping_samples(samples, protected)

    assert kept == (samples[2], samples[3])


def test_purge_overlapping_samples_accepts_half_open_boundary_and_empty_protected_set() -> None:
    origin = datetime(2026, 1, 1, tzinfo=UTC)
    sample = TemporalWindow(origin, origin + timedelta(hours=1))

    assert purge_overlapping_samples(
        (sample,), (TemporalWindow(sample.end, sample.end + timedelta(hours=1)),)
    ) == (sample,)
    assert purge_overlapping_samples((sample,), ()) == (sample,)

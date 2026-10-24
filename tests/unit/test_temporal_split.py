from datetime import UTC, datetime, timedelta

import pytest

from trading_bot.domain.learning.evaluation import TemporalWindow, walk_forward_splits


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

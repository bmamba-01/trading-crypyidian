from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from trading_bot.domain.research.source_item_history import (
    SourceItemHistory,
    SourceItemHistoryConflict,
)
from trading_bot.domain.research.source_items import SourceItemRevision

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


def revision(
    revision_id: str,
    *,
    observed_at: datetime = NOW,
    available_at: datetime | None = None,
    tombstoned: bool = False,
) -> SourceItemRevision:
    return SourceItemRevision(
        source_id="source-1",
        item_id="item-1",
        revision_id=revision_id,
        canonical_url="https://example.test/items/1",
        observed_at=observed_at,
        available_at=available_at if available_at is not None else observed_at,
        tombstoned=tombstoned,
    )


def test_append_builds_a_verifiable_hash_chain_and_retries_are_idempotent() -> None:
    history = SourceItemHistory.empty(source_id="source-1", item_id="item-1")
    first = revision("revision-1")
    history = history.append(first, predecessor_revision_id=None)
    entry = history.entries[0]

    assert entry.predecessor_revision_id is None
    assert entry.predecessor_hash is None
    assert len(entry.revision_hash) == 64
    assert history.append(first, predecessor_revision_id=None) is history

    second = revision("revision-2", observed_at=NOW + timedelta(minutes=1))
    next_history = history.append(second, predecessor_revision_id="revision-1")
    assert next_history.entries[-1].predecessor_revision_id == "revision-1"
    assert next_history.entries[-1].predecessor_hash == entry.revision_hash


def test_duplicate_revision_id_with_changed_payload_conflicts() -> None:
    history = SourceItemHistory.empty(source_id="source-1", item_id="item-1").append(
        revision("revision-1"), predecessor_revision_id=None
    )

    with pytest.raises(SourceItemHistoryConflict, match="payload khác"):
        history.append(
            replace(revision("revision-1"), canonical_url="https://example.test/items/2"),
            predecessor_revision_id=None,
        )


def test_append_requires_exact_latest_predecessor_and_rejects_tampered_chain() -> None:
    history = SourceItemHistory.empty(source_id="source-1", item_id="item-1").append(
        revision("revision-1"), predecessor_revision_id=None
    )

    with pytest.raises(SourceItemHistoryConflict, match="predecessor"):
        history.append(
            revision("revision-2", observed_at=NOW + timedelta(minutes=1)),
            predecessor_revision_id=None,
        )

    entry = history.entries[0]
    with pytest.raises(ValueError, match="revision_hash"):
        replace(entry, revision_hash="0" * 64)


def test_as_of_selects_latest_available_revision_without_future_leakage() -> None:
    history = SourceItemHistory.empty(source_id="source-1", item_id="item-1").append(
        revision("revision-1", observed_at=NOW), predecessor_revision_id=None
    )
    history = history.append(
        revision(
            "revision-2",
            observed_at=NOW + timedelta(hours=1),
            available_at=NOW + timedelta(hours=2),
        ),
        predecessor_revision_id="revision-1",
    )

    before_revision = history.select_as_of(
        NOW + timedelta(minutes=30), evaluated_at=NOW + timedelta(hours=3)
    )
    after_revision = history.select_as_of(
        NOW + timedelta(hours=2), evaluated_at=NOW + timedelta(hours=3)
    )
    assert before_revision is not None
    assert before_revision.revision_id == "revision-1"
    assert after_revision is not None
    assert after_revision.revision_id == "revision-2"


def test_late_availability_of_older_entry_does_not_regress_current_revision() -> None:
    history = SourceItemHistory.empty(source_id="source-1", item_id="item-1").append(
        revision(
            "revision-1",
            observed_at=NOW,
            available_at=NOW + timedelta(hours=3),
        ),
        predecessor_revision_id=None,
    )
    history = history.append(
        revision(
            "revision-2",
            observed_at=NOW + timedelta(minutes=1),
            available_at=NOW + timedelta(hours=2),
        ),
        predecessor_revision_id="revision-1",
    )

    selected = history.select_as_of(NOW + timedelta(hours=4), evaluated_at=NOW + timedelta(hours=5))

    assert selected is not None
    assert selected.revision_id == "revision-2"


def test_current_tombstone_and_takedown_hide_prior_revision_from_history() -> None:
    history = SourceItemHistory.empty(source_id="source-1", item_id="item-1").append(
        revision("revision-1", observed_at=NOW), predecessor_revision_id=None
    )
    history = history.append(
        revision(
            "revision-2",
            observed_at=NOW + timedelta(hours=1),
            tombstoned=True,
        ),
        predecessor_revision_id="revision-1",
    )

    assert (
        history.select_as_of(NOW + timedelta(minutes=30), evaluated_at=NOW + timedelta(hours=2))
        is None
    )
    assert (
        history.select_as_of(
            NOW + timedelta(minutes=30),
            evaluated_at=NOW + timedelta(hours=2),
            current_takedown_at=NOW + timedelta(hours=1),
        )
        is None
    )


def test_tombstoned_history_cannot_be_revived_under_same_item_id() -> None:
    history = SourceItemHistory.empty(source_id="source-1", item_id="item-1").append(
        revision("revision-1", observed_at=NOW), predecessor_revision_id=None
    )
    history = history.append(
        revision("revision-2", observed_at=NOW + timedelta(minutes=1), tombstoned=True),
        predecessor_revision_id="revision-1",
    )

    with pytest.raises(SourceItemHistoryConflict, match="tombstone"):
        history.append(
            revision("revision-3", observed_at=NOW + timedelta(minutes=2)),
            predecessor_revision_id="revision-2",
        )


def test_as_of_and_evaluation_times_must_be_aware_and_ordered() -> None:
    history = SourceItemHistory.empty(source_id="source-1", item_id="item-1").append(
        revision("revision-1"), predecessor_revision_id=None
    )

    with pytest.raises(ValueError, match="as_of"):
        history.select_as_of(NOW.replace(tzinfo=None), evaluated_at=NOW)
    with pytest.raises(ValueError, match="evaluated_at"):
        history.select_as_of(NOW, evaluated_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="as_of"):
        history.select_as_of(NOW + timedelta(seconds=1), evaluated_at=NOW)


def test_empty_history_returns_no_revision() -> None:
    history = SourceItemHistory.empty(source_id="source-1", item_id="item-1")
    assert history.select_as_of(NOW, evaluated_at=NOW) is None

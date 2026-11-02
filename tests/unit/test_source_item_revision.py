from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from trading_bot.domain.research.source_items import SourceItemRevision
from trading_bot.domain.research.source_policy import (
    SourcePolicyState,
    SourceRightsPolicy,
    SourceUsePurpose,
)

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


def revision() -> SourceItemRevision:
    return SourceItemRevision(
        source_id="source-1",
        item_id="item-1",
        revision_id="revision-1",
        canonical_url="https://example.test/news/1",
        observed_at=NOW,
        available_at=NOW + timedelta(minutes=5),
        published_at=NOW - timedelta(hours=1),
    )


def policy() -> SourceRightsPolicy:
    return SourceRightsPolicy(
        policy_id="policy-1",
        source_id="source-1",
        state=SourcePolicyState.VERIFIED,
        allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
        reviewed_at=NOW - timedelta(days=1),
        evidence_ref="https://example.test/terms",
    )


def test_late_revision_is_hidden_until_its_actual_availability() -> None:
    item = revision()
    assert not item.is_eligible(as_of=NOW, purpose=SourceUsePurpose.METADATA, policy=policy())
    assert item.is_eligible(
        as_of=NOW + timedelta(minutes=5), purpose=SourceUsePurpose.METADATA, policy=policy()
    )


def test_revision_does_not_mutate_an_earlier_version() -> None:
    original = revision()
    correction = replace(
        original,
        revision_id="revision-2",
        observed_at=NOW + timedelta(hours=1),
        available_at=NOW + timedelta(hours=1),
        edited_at=NOW + timedelta(hours=1),
    )
    assert original.is_eligible(
        as_of=NOW + timedelta(minutes=10), purpose=SourceUsePurpose.METADATA, policy=policy()
    )
    assert not correction.is_eligible(
        as_of=NOW + timedelta(minutes=10), purpose=SourceUsePurpose.METADATA, policy=policy()
    )
    with pytest.raises(FrozenInstanceError):
        original.revision_id = "changed"  # type: ignore[misc]


def test_missing_availability_and_tombstone_are_ineligible() -> None:
    for item in (replace(revision(), available_at=None), replace(revision(), tombstoned=True)):
        assert not item.is_eligible(
            as_of=NOW + timedelta(days=1), purpose=SourceUsePurpose.METADATA, policy=policy()
        )


@pytest.mark.parametrize("state", list(SourcePolicyState))
def test_eligibility_requires_verified_rights(state: SourcePolicyState) -> None:
    assert revision().is_eligible(
        as_of=NOW + timedelta(minutes=5),
        purpose=SourceUsePurpose.METADATA,
        policy=replace(policy(), state=state),
    ) is (state is SourcePolicyState.VERIFIED)


def test_policy_must_match_source_purpose_and_as_of() -> None:
    item = revision()
    as_of = NOW + timedelta(minutes=5)
    assert not item.is_eligible(
        as_of=as_of,
        purpose=SourceUsePurpose.METADATA,
        policy=replace(policy(), source_id="other-source"),
    )
    assert not item.is_eligible(as_of=as_of, purpose=SourceUsePurpose.TRAINING, policy=policy())
    for purpose in ("metadata", "unknown", None):
        assert not item.is_eligible(
            as_of=as_of,
            purpose=purpose,
            policy=policy(),  # type: ignore[arg-type]
        )
    assert not item.is_eligible(
        as_of=as_of,
        purpose=SourceUsePurpose.METADATA,
        policy=replace(policy(), reviewed_at=as_of + timedelta(seconds=1)),
    )
    assert not item.is_eligible(
        as_of=as_of,
        purpose=SourceUsePurpose.METADATA,
        policy=replace(policy(), valid_until=as_of),
    )


@pytest.mark.parametrize("field", ["source_id", "item_id", "revision_id"])
@pytest.mark.parametrize("value", ["", " ", " padded", "a/b", "a\nb", 12, None])
def test_malformed_identity_is_rejected(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        replace(revision(), **{field: value})


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/a",
        "https:///a",
        "https://",
        "https://-invalid.example/a",
        "https://user:pass@example.test/a",
        "https://example.test/a#fragment",
        "https://Example.test/a",
        "https://example.test/ a",
        "https://example.test:bad/a",
        "https://example.test:99999/a",
        "https://example.test:/a",
        "https://example.test\\evil/a",
        "https://example..test/a",
        "https://example.test/%zz",
        "https://example.test/\na",
        "https://[broken/a",
        "https://example.test/<bad>",
        "https://example.test/\u007f",
        "",
        None,
    ],
)
def test_malformed_or_noncanonical_https_url_is_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="canonical_url"):
        replace(revision(), canonical_url=url)


@pytest.mark.parametrize(
    "field",
    [
        "observed_at",
        "available_at",
        "published_at",
        "edited_at",
        "event_at",
    ],
)
def test_all_timestamps_require_timezone(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        replace(revision(), **{field: NOW.replace(tzinfo=None)})


@pytest.mark.parametrize(
    "changes",
    [
        {"available_at": NOW - timedelta(seconds=1)},
        {"published_at": NOW + timedelta(seconds=1)},
        {"edited_at": NOW + timedelta(seconds=1)},
        {"edited_at": NOW - timedelta(hours=2)},
    ],
)
def test_invalid_causal_order_is_rejected(changes: dict[str, datetime]) -> None:
    with pytest.raises(ValueError, match="available_at|published_at|edited_at"):
        replace(revision(), **changes)


def test_scheduled_event_time_does_not_replace_availability() -> None:
    item = replace(revision(), event_at=NOW + timedelta(days=2))
    assert item.is_eligible(
        as_of=NOW + timedelta(minutes=5), purpose=SourceUsePurpose.METADATA, policy=policy()
    )


def test_equivalent_timezone_instants_use_same_availability_boundary() -> None:
    as_of = (NOW + timedelta(minutes=5)).astimezone(timezone(timedelta(hours=7)))
    assert revision().is_eligible(as_of=as_of, purpose=SourceUsePurpose.METADATA, policy=policy())


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(ValueError, match="as_of"):
        revision().is_eligible(
            as_of=NOW.replace(tzinfo=None), purpose=SourceUsePurpose.METADATA, policy=policy()
        )


def test_availability_uses_absolute_time_across_repeated_dst_hour() -> None:
    zone = ZoneInfo("America/New_York")
    available_at = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    earlier_as_of = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0)
    item = SourceItemRevision(
        source_id="source-1",
        item_id="item-1",
        revision_id="revision-dst",
        canonical_url="https://example.test/news/dst",
        observed_at=available_at,
        available_at=available_at,
    )

    assert not item.is_eligible(
        as_of=earlier_as_of, purpose=SourceUsePurpose.METADATA, policy=policy()
    )


def test_causal_order_uses_absolute_time_across_repeated_dst_hour() -> None:
    zone = ZoneInfo("America/New_York")
    observed_at = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0)
    available_at = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)

    item = SourceItemRevision(
        source_id="source-1",
        item_id="item-1",
        revision_id="revision-dst",
        canonical_url="https://example.test/news/dst",
        observed_at=observed_at,
        available_at=available_at,
    )

    assert item.available_at == available_at.astimezone(UTC)


def test_causal_order_rejects_available_time_after_observation_across_repeated_dst_hour() -> None:
    zone = ZoneInfo("America/New_York")
    observed_at = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    available_at = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0)

    with pytest.raises(ValueError, match="available_at"):
        SourceItemRevision(
            source_id="source-1",
            item_id="item-1",
            revision_id="revision-dst-future",
            canonical_url="https://example.test/news/dst-future",
            observed_at=observed_at,
            available_at=available_at,
        )


def test_tombstone_requires_a_boolean() -> None:
    with pytest.raises(ValueError, match="tombstoned"):
        replace(revision(), tombstoned="false")  # type: ignore[arg-type]

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trading_bot.domain.research.source_policy import (
    SourcePolicyState,
    SourceRightsPolicy,
    SourceUsePurpose,
)


def test_rights_pending_policy_does_not_permit_any_use() -> None:
    reviewed_at = datetime(2026, 11, 1, tzinfo=UTC)
    policy = SourceRightsPolicy(
        policy_id="policy-1",
        source_id="source-1",
        state=SourcePolicyState.RIGHTS_PENDING,
        allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
        evidence_ref="https://example.test/terms",
        reviewed_at=reviewed_at,
    )

    assert not policy.permits(SourceUsePurpose.METADATA, at=reviewed_at)


def test_verified_policy_permits_only_explicit_purpose_after_review() -> None:
    reviewed_at = datetime(2026, 11, 1, tzinfo=UTC)
    policy = SourceRightsPolicy(
        policy_id="policy-1",
        source_id="source-1",
        state=SourcePolicyState.VERIFIED,
        allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
        evidence_ref="https://example.test/terms",
        reviewed_at=reviewed_at,
    )

    assert not policy.permits(SourceUsePurpose.METADATA, at=reviewed_at - timedelta(seconds=1))
    assert policy.permits(SourceUsePurpose.METADATA, at=reviewed_at)
    assert not policy.permits(SourceUsePurpose.TRAINING, at=reviewed_at)


def test_unrecognized_purpose_fails_closed() -> None:
    reviewed_at = datetime(2026, 11, 1, tzinfo=UTC)
    policy = SourceRightsPolicy(
        policy_id="policy-1",
        source_id="source-1",
        state=SourcePolicyState.VERIFIED,
        allowed_purposes=frozenset({SourceUsePurpose.TRAINING}),
        evidence_ref="https://example.test/terms",
        reviewed_at=reviewed_at,
    )

    assert not policy.permits("training", at=reviewed_at)  # type: ignore[arg-type]


def test_unknown_policy_state_is_rejected() -> None:
    with pytest.raises(ValueError, match="state"):
        SourceRightsPolicy(
            policy_id="policy-1",
            source_id="source-1",
            state="verified",  # type: ignore[arg-type]
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
            evidence_ref="https://example.test/terms",
            reviewed_at=datetime(2026, 11, 1, tzinfo=UTC),
        )


def test_verified_policy_stops_permitting_use_at_expiry() -> None:
    reviewed_at = datetime(2026, 11, 1, tzinfo=UTC)
    expires_at = reviewed_at + timedelta(days=1)
    policy = SourceRightsPolicy(
        policy_id="policy-1",
        source_id="source-1",
        state=SourcePolicyState.VERIFIED,
        allowed_purposes=frozenset({SourceUsePurpose.DISPLAY}),
        evidence_ref="https://example.test/terms",
        reviewed_at=reviewed_at,
        valid_until=expires_at,
    )

    assert policy.permits(SourceUsePurpose.DISPLAY, at=expires_at - timedelta(seconds=1))
    assert not policy.permits(SourceUsePurpose.DISPLAY, at=expires_at)


def test_rights_expiry_uses_absolute_time_across_repeated_dst_hour() -> None:
    zone = ZoneInfo("America/New_York")
    reviewed_at = datetime(2026, 10, 31, 12, tzinfo=UTC)
    expires_at = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0)
    after_expiry = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    policy = SourceRightsPolicy(
        policy_id="policy-1",
        source_id="source-1",
        state=SourcePolicyState.VERIFIED,
        allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
        evidence_ref="https://example.test/terms",
        reviewed_at=reviewed_at,
        valid_until=expires_at,
    )

    assert not policy.permits(SourceUsePurpose.METADATA, at=after_expiry)


@pytest.mark.parametrize(
    "state",
    [SourcePolicyState.BLOCKED, SourcePolicyState.REVOKED, SourcePolicyState.EXPIRED],
)
def test_non_verified_policy_states_never_permit_use(state: SourcePolicyState) -> None:
    reviewed_at = datetime(2026, 11, 1, tzinfo=UTC)
    policy = SourceRightsPolicy(
        policy_id="policy-1",
        source_id="source-1",
        state=state,
        allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
        evidence_ref="https://example.test/terms",
        reviewed_at=reviewed_at,
    )

    assert not policy.permits(SourceUsePurpose.METADATA, at=reviewed_at)


@pytest.mark.parametrize("name", ["policy_id", "source_id", "evidence_ref"])
def test_rights_policy_rejects_blank_identifiers(name: str) -> None:
    values: dict[str, object] = {
        "policy_id": "policy-1",
        "source_id": "source-1",
        "state": SourcePolicyState.VERIFIED,
        "allowed_purposes": frozenset({SourceUsePurpose.METADATA}),
        "evidence_ref": "https://example.test/terms",
        "reviewed_at": datetime(2026, 11, 1, tzinfo=UTC),
    }
    values[name] = "  "

    with pytest.raises(ValueError, match="không được để trống"):
        SourceRightsPolicy(**values)  # type: ignore[arg-type]


def test_verified_policy_requires_review_evidence_and_timezone_aware_dates() -> None:
    with pytest.raises(ValueError, match="evidence"):
        SourceRightsPolicy(
            policy_id="policy-1",
            source_id="source-1",
            state=SourcePolicyState.VERIFIED,
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
            evidence_ref="",
            reviewed_at=datetime(2026, 11, 1, tzinfo=UTC),
        )

    with pytest.raises(ValueError, match="múi giờ"):
        SourceRightsPolicy(
            policy_id="policy-1",
            source_id="source-1",
            state=SourcePolicyState.VERIFIED,
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
            evidence_ref="https://example.test/terms",
            reviewed_at=datetime(2026, 11, 1, tzinfo=UTC).replace(tzinfo=None),
        )

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.research.source_policy import (
    SourcePolicyState,
    SourceRightsPolicy,
    SourceUsePurpose,
)
from trading_bot.domain.research.source_registry import (
    SourceBudget,
    SourcePolicyRevision,
    SourcePolicyTransitionError,
    SourceQuotaExceeded,
    SourceRegistryEntry,
    SourceUsage,
)

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


def entry() -> SourceRegistryEntry:
    return SourceRegistryEntry.discover(
        source_id="source-1",
        publisher="Example Publisher",
        endpoint_url="https://example.test/api/events",
        access_method="HTTPS_API",
        auth_scope="public",
        budget=SourceBudget(
            window=timedelta(hours=1),
            max_requests=2,
            max_bytes=1024,
            max_cost=Decimal("1.00"),
            cost_unit="credits",
        ),
        actor_id="operator-1",
        at=NOW,
        reason="Candidate discovered for review",
    )


def rights_pending(source: SourceRegistryEntry) -> SourceRegistryEntry:
    return source.transition(
        policy_id="source-1-policy-2",
        state=SourcePolicyState.RIGHTS_PENDING,
        allowed_purposes=frozenset(),
        actor_id="operator-1",
        at=NOW + timedelta(minutes=1),
        reason="Need source terms review",
    )


def verified(source: SourceRegistryEntry) -> SourceRegistryEntry:
    return source.transition(
        policy_id="source-1-policy-3",
        state=SourcePolicyState.VERIFIED,
        allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
        evidence_ref="https://example.test/terms",
        reviewed_at=NOW + timedelta(minutes=2),
        valid_until=NOW + timedelta(days=30),
        approved_by="reviewer-1",
        actor_id="operator-1",
        at=NOW + timedelta(minutes=2),
        reason="Metadata-only access approved for this endpoint",
    )


def test_new_source_is_discovered_disabled_and_history_is_immutable() -> None:
    source = entry()

    assert source.current.state is SourcePolicyState.DISCOVERED
    assert not source.permits(SourceUsePurpose.METADATA, at=NOW)
    assert len(source.revisions) == 1

    pending = rights_pending(source)
    assert len(source.revisions) == 1
    assert len(pending.revisions) == 2


def test_verified_source_requires_policy_path_review_and_explicit_purpose() -> None:
    source = verified(rights_pending(entry()))
    at = NOW + timedelta(minutes=3)

    assert source.permits(SourceUsePurpose.METADATA, at=at)
    assert not source.permits(SourceUsePurpose.TRAINING, at=at)
    assert not source.permits("metadata", at=at)  # type: ignore[arg-type]


def test_cannot_skip_rights_pending_or_verify_without_reviewer() -> None:
    with pytest.raises(SourcePolicyTransitionError, match="transition"):
        entry().transition(
            policy_id="policy-direct-verified",
            state=SourcePolicyState.VERIFIED,
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
            evidence_ref="https://example.test/terms",
            reviewed_at=NOW + timedelta(minutes=1),
            approved_by="reviewer-1",
            actor_id="operator-1",
            at=NOW + timedelta(minutes=1),
            reason="Skipped required review stage",
        )


def test_policy_actor_cannot_approve_their_own_rights_review() -> None:
    with pytest.raises(ValueError, match="khác actor_id"):
        rights_pending(entry()).transition(
            policy_id="source-1-policy-self-approved",
            state=SourcePolicyState.VERIFIED,
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
            evidence_ref="https://example.test/terms",
            reviewed_at=NOW + timedelta(minutes=2),
            approved_by="operator-1",
            actor_id="operator-1",
            at=NOW + timedelta(minutes=2),
            reason="Actor cannot approve their own source policy",
        )

    with pytest.raises(ValueError, match="approved_by"):
        rights_pending(entry()).transition(
            policy_id="source-1-policy-no-reviewer",
            state=SourcePolicyState.VERIFIED,
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
            evidence_ref="https://example.test/terms",
            reviewed_at=NOW + timedelta(minutes=2),
            actor_id="operator-1",
            at=NOW + timedelta(minutes=2),
            reason="No reviewer identity",
        )


def test_revocation_disables_source_and_reenable_requires_new_review_version() -> None:
    active = verified(rights_pending(entry()))
    revoked = active.transition(
        policy_id="source-1-policy-4",
        state=SourcePolicyState.REVOKED,
        actor_id="operator-1",
        at=NOW + timedelta(minutes=5),
        reason="Rights were withdrawn",
    )
    assert not revoked.permits(SourceUsePurpose.METADATA, at=NOW + timedelta(minutes=6))

    pending_again = revoked.transition(
        policy_id="source-1-policy-5",
        state=SourcePolicyState.RIGHTS_PENDING,
        actor_id="operator-1",
        at=NOW + timedelta(minutes=7),
        reason="A new rights review is required",
    )
    reapproved = pending_again.transition(
        policy_id="source-1-policy-6",
        state=SourcePolicyState.VERIFIED,
        allowed_purposes=frozenset({SourceUsePurpose.DISPLAY}),
        evidence_ref="https://example.test/new-terms",
        reviewed_at=NOW + timedelta(minutes=8),
        valid_until=NOW + timedelta(days=60),
        approved_by="reviewer-2",
        actor_id="operator-1",
        at=NOW + timedelta(minutes=8),
        reason="New review with narrower purpose",
    )
    assert not reapproved.permits(SourceUsePurpose.METADATA, at=NOW + timedelta(minutes=9))
    assert reapproved.permits(SourceUsePurpose.DISPLAY, at=NOW + timedelta(minutes=9))
    assert len(reapproved.revisions) == 6


def test_expired_rights_and_blocked_state_fail_closed() -> None:
    source = verified(rights_pending(entry()))
    expiry = source.current.policy.valid_until
    assert expiry is not None
    assert not source.permits(SourceUsePurpose.METADATA, at=expiry)

    blocked = source.transition(
        policy_id="source-1-policy-blocked",
        state=SourcePolicyState.BLOCKED,
        actor_id="operator-1",
        at=NOW + timedelta(minutes=4),
        reason="Endpoint failed compliance review",
    )
    assert not blocked.permits(SourceUsePurpose.METADATA, at=NOW + timedelta(minutes=4))


def test_policy_revision_requires_new_id_and_matching_source_state() -> None:
    with pytest.raises(ValueError, match="policy_id"):
        entry().transition(
            policy_id="source-1:policy:1",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="operator-1",
            at=NOW + timedelta(minutes=1),
            reason="Duplicate policy id",
        )


def test_entry_invariants_reject_illegal_manually_built_history() -> None:
    initial = entry()
    skipped_review = SourcePolicyRevision(
        sequence=2,
        policy=SourceRightsPolicy(
            policy_id="source-1-policy-skip",
            source_id="source-1",
            state=SourcePolicyState.VERIFIED,
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
            evidence_ref="https://example.test/terms",
            reviewed_at=NOW + timedelta(minutes=1),
        ),
        publisher=initial.current.publisher,
        endpoint_url=initial.current.endpoint_url,
        access_method=initial.current.access_method,
        auth_scope=initial.current.auth_scope,
        attribution=initial.current.attribution,
        retention_ref=initial.current.retention_ref,
        budget=initial.current.budget,
        created_at=NOW + timedelta(minutes=1),
        actor_id="operator-1",
        reason="Direct verification must be impossible",
        approved_by="reviewer-1",
    )

    with pytest.raises(ValueError, match="transition"):
        SourceRegistryEntry("source-1", (*initial.revisions, skipped_review))


def test_https_endpoint_rejects_credentials_and_blank_updates() -> None:
    with pytest.raises(ValueError, match="endpoint_url"):
        SourceRegistryEntry.discover(
            source_id="source-1",
            publisher="Example",
            endpoint_url="https://user:secret@example.test/api",
            access_method="HTTPS_API",
            auth_scope="public",
            budget=entry().current.budget,
            actor_id="operator-1",
            at=NOW,
            reason="Unsafe endpoint registration",
        )

    with pytest.raises(ValueError, match="publisher"):
        verified(rights_pending(entry())).transition(
            policy_id="source-1-policy-blank-publisher",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="operator-1",
            at=NOW + timedelta(minutes=3),
            reason="Blank update must not be silently ignored",
            publisher="",
        )

    with pytest.raises(ValueError, match="source_id"):
        entry().transition(
            policy_id="source-1-policy-other",
            state=SourcePolicyState.RIGHTS_PENDING,
            source_id="other-source",
            actor_id="operator-1",
            at=NOW + timedelta(minutes=1),
            reason="Identity must be stable",
        )


def test_budget_consumption_enforces_request_byte_and_cost_limits() -> None:
    budget = entry().current.budget
    usage = budget.consume(
        None,
        at=NOW,
        byte_count=512,
        cost=Decimal("0.50"),
    )
    second = budget.consume(
        usage,
        at=NOW + timedelta(minutes=5),
        byte_count=512,
        cost=Decimal("0.50"),
    )
    assert second.requests_used == 2

    with pytest.raises(SourceQuotaExceeded, match="request"):
        budget.consume(second, at=NOW + timedelta(minutes=5), byte_count=0, cost=Decimal(0))
    with pytest.raises(SourceQuotaExceeded, match="byte"):
        budget.consume(None, at=NOW, byte_count=1025, cost=Decimal(0))
    with pytest.raises(SourceQuotaExceeded, match="cost"):
        budget.consume(None, at=NOW, byte_count=0, cost=Decimal("1.01"))


def test_budget_resets_only_after_window_and_rejects_mismatched_usage() -> None:
    budget = entry().current.budget
    usage = SourceUsage(
        window_started_at=NOW,
        requests_used=2,
        bytes_used=100,
        cost_used=Decimal("0.5"),
        cost_unit="credits",
    )
    with pytest.raises(SourceQuotaExceeded):
        budget.consume(usage, at=NOW + timedelta(minutes=59), byte_count=1, cost=Decimal(0))

    reset = budget.consume(usage, at=NOW + timedelta(hours=1), byte_count=1, cost=Decimal("0.1"))
    assert reset.window_started_at == NOW + timedelta(hours=1)
    assert reset.requests_used == 1

    bad_usage = SourceUsage(
        window_started_at=NOW,
        requests_used=0,
        bytes_used=0,
        cost_used=Decimal(0),
        cost_unit="usd",
    )
    with pytest.raises(ValueError, match="cost_unit"):
        budget.consume(bad_usage, at=NOW, byte_count=0, cost=Decimal(0))


def test_cost_accumulation_does_not_round_to_global_decimal_context() -> None:
    cost = Decimal("0.12345678901234567890123456789")
    budget = SourceBudget(
        window=timedelta(hours=1),
        max_requests=2,
        max_bytes=10,
        max_cost=Decimal("0.24691357802469135780246913578"),
        cost_unit="credits",
    )
    first = budget.consume(None, at=NOW, byte_count=1, cost=cost)
    second = budget.consume(first, at=NOW + timedelta(seconds=1), byte_count=1, cost=cost)

    assert second.cost_used == Decimal("0.24691357802469135780246913578")


@pytest.mark.parametrize("cost", [Decimal("1e-100000000"), Decimal("1e100000000")])
def test_cost_rejects_extreme_decimal_exponents_before_quota_arithmetic(
    cost: Decimal,
) -> None:
    with pytest.raises(ValueError, match="cost"):
        SourceBudget(
            window=timedelta(hours=1),
            max_requests=2,
            max_bytes=10,
            max_cost=cost,
            cost_unit="credits",
        )


def test_cost_rejects_precision_beyond_configured_bound() -> None:
    cost = Decimal("0." + "1" * 300)

    with pytest.raises(ValueError, match="cost"):
        SourceBudget(
            window=timedelta(hours=1),
            max_requests=2,
            max_bytes=10,
            max_cost=cost,
            cost_unit="credits",
        )


def test_cost_accepts_database_scale_padding_without_rounding() -> None:
    padded = Decimal("1." + "0" * 128)
    budget = SourceBudget(
        window=timedelta(hours=1),
        max_requests=1,
        max_bytes=1,
        max_cost=padded,
        cost_unit="credits",
    )

    assert budget.max_cost == Decimal("1")


def test_verified_policy_must_expire_after_revision_creation() -> None:
    pending = rights_pending(entry())

    with pytest.raises(ValueError, match="valid_until"):
        pending.transition(
            policy_id="source-1-policy-expired-on-create",
            state=SourcePolicyState.VERIFIED,
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
            evidence_ref="https://example.test/terms",
            reviewed_at=NOW + timedelta(minutes=1),
            valid_until=NOW + timedelta(minutes=2),
            approved_by="reviewer-1",
            actor_id="operator-1",
            at=NOW + timedelta(minutes=2),
            reason="Expiry must follow the policy creation instant",
        )

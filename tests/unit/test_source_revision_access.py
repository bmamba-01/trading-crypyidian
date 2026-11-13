from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from trading_bot.domain.research.source_items import SourceItemRevision
from trading_bot.domain.research.source_policy import (
    SourcePolicyState,
    SourceUsePurpose,
)
from trading_bot.domain.research.source_registry import (
    SourceBudget,
    SourceRegistryEntry,
    source_rights_hash,
)
from trading_bot.domain.research.source_revision_access import (
    SourceAccessMode,
    SourceAccessReason,
    evaluate_source_revision_access,
)

NOW = datetime(2026, 11, 2, 10, tzinfo=UTC)


def source_registry() -> SourceRegistryEntry:
    budget = SourceBudget(
        window=timedelta(hours=1),
        max_requests=10,
        max_bytes=10_000,
        max_cost="10",
        cost_unit="credits",
    )
    source = SourceRegistryEntry.discover(
        source_id="source-1",
        publisher="Publisher",
        endpoint_url="https://example.test/api",
        access_method="HTTPS_API",
        auth_scope="public",
        budget=budget,
        actor_id="owner",
        at=NOW - timedelta(days=2),
        reason="Discovered source",
    )
    source = source.transition(
        policy_id="policy-pending",
        state=SourcePolicyState.RIGHTS_PENDING,
        actor_id="owner",
        at=NOW - timedelta(days=1, hours=1),
        reason="Review rights",
    )
    return source.transition(
        policy_id="policy-verified",
        state=SourcePolicyState.VERIFIED,
        allowed_purposes=frozenset({SourceUsePurpose.METADATA, SourceUsePurpose.TRAINING}),
        evidence_ref="https://example.test/terms",
        reviewed_at=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=10),
        approved_by="reviewer",
        actor_id="owner",
        at=NOW - timedelta(days=1),
        reason="Rights approved",
    )


def item() -> SourceItemRevision:
    return SourceItemRevision(
        source_id="source-1",
        item_id="item-1",
        revision_id="revision-1",
        canonical_url="https://example.test/items/1",
        observed_at=NOW - timedelta(hours=2),
        available_at=NOW - timedelta(hours=1),
    )


def access(
    *,
    mode: SourceAccessMode = SourceAccessMode.CURRENT,
    as_of: datetime = NOW,
    evaluated_at: datetime = NOW,
    current_tombstoned: bool = False,
    retention_expires_at: datetime | None = None,
    takedown_at: datetime | None = None,
    revision: SourceItemRevision | None = None,
    purpose: SourceUsePurpose = SourceUsePurpose.METADATA,
):
    return evaluate_source_revision_access(
        revision=revision or item(),
        policy_history=source_registry(),
        purpose=purpose,
        mode=mode,
        as_of=as_of,
        evaluated_at=evaluated_at,
        current_tombstoned=current_tombstoned,
        retention_expires_at=retention_expires_at,
        takedown_at=takedown_at,
    )


def test_current_access_uses_current_verified_policy_and_explicit_purpose() -> None:
    assert access().permitted
    assert access(purpose=SourceUsePurpose.TRAINING).permitted
    decision = access(purpose=SourceUsePurpose.EMBEDDING)
    assert not decision.permitted
    assert decision.reason is SourceAccessReason.PURPOSE_NOT_APPROVED


def test_rights_hash_is_stable_and_changes_with_authoritative_rights_terms() -> None:
    revision = source_registry().current

    stable = source_rights_hash(revision)
    changed_purposes = replace(
        revision,
        policy=replace(
            revision.policy,
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
        ),
    )
    changed_reason = replace(revision, reason="Audit note changed")

    assert len(stable) == 64
    assert source_rights_hash(revision) == stable
    assert source_rights_hash(changed_purposes) != stable
    assert source_rights_hash(changed_reason) == stable
    with pytest.raises(ValueError, match="SourcePolicyRevision"):
        source_rights_hash(object())  # type: ignore[arg-type]


def test_revoked_current_rights_deny_current_use_and_historical_replay() -> None:
    history = source_registry().transition(
        policy_id="policy-revoked",
        state=SourcePolicyState.REVOKED,
        actor_id="owner",
        at=NOW,
        reason="Rights withdrawn",
    )

    current = evaluate_source_revision_access(
        revision=item(),
        policy_history=history,
        purpose=SourceUsePurpose.METADATA,
        mode=SourceAccessMode.CURRENT,
        as_of=NOW + timedelta(minutes=1),
        evaluated_at=NOW + timedelta(minutes=1),
        current_tombstoned=False,
    )
    assert not current.permitted
    assert current.reason is SourceAccessReason.CURRENT_POLICY_NOT_VERIFIED

    historical_at = NOW - timedelta(hours=1)
    historical = evaluate_source_revision_access(
        revision=item(),
        policy_history=history,
        purpose=SourceUsePurpose.METADATA,
        mode=SourceAccessMode.HISTORICAL_REPLAY,
        as_of=historical_at,
        evaluated_at=NOW + timedelta(minutes=1),
        current_tombstoned=False,
    )
    assert not historical.permitted
    assert historical.mode is SourceAccessMode.HISTORICAL_REPLAY
    assert historical.reason is SourceAccessReason.CURRENT_POLICY_NOT_VERIFIED
    assert historical.policy_id == "policy-revoked"


def test_explicit_historical_replay_is_allowed_while_current_rights_remain_valid() -> None:
    historical = evaluate_source_revision_access(
        revision=item(),
        policy_history=source_registry(),
        purpose=SourceUsePurpose.METADATA,
        mode=SourceAccessMode.HISTORICAL_REPLAY,
        as_of=NOW - timedelta(hours=1),
        evaluated_at=NOW,
        current_tombstoned=False,
    )
    assert historical.permitted
    assert historical.mode is SourceAccessMode.HISTORICAL_REPLAY
    assert historical.policy_id == "policy-verified"


def test_current_takedown_blocks_historical_replay_even_if_rights_were_valid() -> None:
    decision = access(
        mode=SourceAccessMode.HISTORICAL_REPLAY,
        as_of=NOW - timedelta(hours=1),
        current_tombstoned=True,
    )
    assert not decision.permitted
    assert decision.reason is SourceAccessReason.CURRENT_TAKEDOWN


def test_tombstoned_revision_and_retention_expiry_are_hard_denials() -> None:
    tombstoned = access(revision=replace(item(), tombstoned=True))
    assert not tombstoned.permitted
    assert tombstoned.reason is SourceAccessReason.REVISION_TOMBSTONED

    expired = access(
        mode=SourceAccessMode.HISTORICAL_REPLAY,
        as_of=NOW - timedelta(hours=1),
        retention_expires_at=NOW - timedelta(seconds=1),
    )
    assert not expired.permitted
    assert expired.reason is SourceAccessReason.RETENTION_EXPIRED


def test_availability_and_current_takedown_time_are_checked_against_absolute_time() -> None:
    future_revision = replace(item(), available_at=NOW + timedelta(minutes=1))
    decision = access(revision=future_revision)
    assert not decision.permitted
    assert decision.reason is SourceAccessReason.NOT_AVAILABLE_AS_OF

    takedown_at = NOW - timedelta(minutes=1)
    decision = access(
        mode=SourceAccessMode.HISTORICAL_REPLAY,
        as_of=NOW - timedelta(hours=1),
        evaluated_at=NOW,
        takedown_at=takedown_at,
    )
    assert not decision.permitted
    assert decision.reason is SourceAccessReason.CURRENT_TAKEDOWN


def test_historical_replay_requires_past_as_of_and_current_requires_current_as_of() -> None:
    with pytest.raises(ValueError, match="as_of"):
        access(mode=SourceAccessMode.HISTORICAL_REPLAY, as_of=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="as_of"):
        access(as_of=NOW - timedelta(seconds=1))


def test_naive_retention_takedown_and_evaluation_times_are_rejected() -> None:
    with pytest.raises(ValueError, match="retention_expires_at"):
        access(retention_expires_at=NOW.replace(tzinfo=None))


def licensed_revision():
    from uuid import UUID

    from trading_bot.domain.research.license_scope import SourceLicenseScope, scope_sha256

    revision = source_registry().current
    scope = SourceLicenseScope(
        source_id=revision.policy.source_id,
        provider="PROVIDER",
        product="OHLCV",
        venue="BINANCE",
        contract="USD_M",
        symbols=("BTCUSDT",),
        intervals=("1h",),
        data_start=NOW - timedelta(days=30),
        data_end=NOW + timedelta(days=30),
        allowed_purposes=(SourceUsePurpose.METADATA, SourceUsePurpose.TRAINING),
        allowed_outputs=("derived_features", "trained_model"),
        derived_use=("feature_extraction", "model_training"),
        attribution=revision.attribution,
        max_retention_days=None,
    )
    return replace(
        revision,
        provider=scope.provider,
        product=scope.product,
        venue=scope.venue,
        contract=scope.contract,
        license_scope=scope,
        license_evidence_id=UUID("12345678-1234-5678-1234-567812345678"),
        license_digest="a" * 64,
        license_scope_hash=scope_sha256(scope),
    )


def test_legacy_rights_fingerprint_stays_byte_identical_and_write_requires_evidence() -> None:
    revision = source_registry().current
    assert source_rights_hash(revision) == (
        "8481ab8f1ff2ad59a93dabe457329c4ee1eb39e8579fbde7b0f165512967d852"
    )
    assert not revision.has_license_evidence
    with pytest.raises(ValueError, match="evidence"):
        revision.require_license_evidence()


def test_rights_fingerprint_binds_each_evidence_component() -> None:
    from uuid import uuid4

    revision = licensed_revision()
    revision.require_license_evidence()
    assert revision.has_license_evidence
    fingerprint = source_rights_hash(revision)
    assert source_rights_hash(replace(revision, license_digest="b" * 64)) != fingerprint
    assert source_rights_hash(replace(revision, license_evidence_id=uuid4())) != fingerprint
    assert fingerprint != source_rights_hash(source_registry().current)


@pytest.mark.parametrize(
    "changes",
    [
        {"license_scope_hash": "f" * 64},
        {"license_digest": "INVALID"},
        {"provider": "OTHER"},
        {"attribution": "Không khớp điều khoản"},
    ],
)
def test_license_revision_rejects_inconsistent_metadata(changes) -> None:
    with pytest.raises(ValueError, match="license|attribution"):
        replace(licensed_revision(), **changes)


def test_license_revision_rejects_purpose_outside_scope() -> None:
    revision = licensed_revision()
    with pytest.raises(ValueError, match="purpose"):
        replace(
            revision,
            policy=replace(
                revision.policy,
                allowed_purposes=frozenset(
                    {
                        SourceUsePurpose.REDISTRIBUTION,
                    }
                ),
            ),
        )


def test_source_identity_cannot_change_or_disappear_once_set() -> None:
    history = source_registry()
    revision = licensed_revision()
    history = replace(history, revisions=(*history.revisions[:-1], revision))
    revoked = history.transition(
        policy_id="revoke-with-identity",
        state=SourcePolicyState.REVOKED,
        actor_id="owner",
        at=NOW,
        reason="Thu hồi",
    )
    assert revoked.current.provider == "PROVIDER"
    with pytest.raises(ValueError, match="identity"):
        replace(
            revoked,
            revisions=(
                *revoked.revisions[:-1],
                replace(
                    revoked.current,
                    provider="OTHER",
                    license_scope=None,
                    license_evidence_id=None,
                    license_digest=None,
                    license_scope_hash=None,
                ),
            ),
        )

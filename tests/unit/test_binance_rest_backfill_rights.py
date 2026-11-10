from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.application.binance_rest_backfill import (
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.application.binance_rest_backfill_rights import (
    SourceRegistryBackfillRightsGate,
)
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import (
    ObservedSourcePolicy,
    SourceBudget,
    SourceRegistryEntry,
    source_rights_hash,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
SOURCE_ID = "BINANCE_USDM_REST"
WORKSPACE_ID = uuid4()


def request(*, purpose: BackfillRightsPurpose = BackfillRightsPurpose.BACKTEST_RESEARCH):
    return RestBackfillRequest(
        symbol="BTCUSDT",
        interval="1m",
        start=NOW - timedelta(minutes=2),
        end=NOW,
        purpose=purpose,
    )


def entry(
    *,
    purposes: frozenset[SourceUsePurpose] = frozenset({SourceUsePurpose.BACKTEST_RESEARCH}),
    valid_until: datetime | None = NOW + timedelta(days=1),
    evidence_ref: str = "https://www.binance.com/en/terms",
) -> SourceRegistryEntry:
    source = SourceRegistryEntry.discover(
        source_id=SOURCE_ID,
        publisher="Binance",
        endpoint_url="https://fapi.binance.com/fapi/v1/klines",
        access_method="GET",
        auth_scope="public",
        budget=SourceBudget(
            window=timedelta(hours=1),
            max_requests=100,
            max_bytes=1024 * 1024,
            max_cost=Decimal("0"),
            cost_unit="USD",
        ),
        actor_id="operator",
        at=NOW - timedelta(days=3),
        reason="Nguồn được phát hiện để xét quyền.",
    )
    pending = source.transition(
        policy_id=f"{SOURCE_ID}:pending",
        state=SourcePolicyState.RIGHTS_PENDING,
        actor_id="operator",
        at=NOW - timedelta(days=2),
        reason="Chờ duyệt quyền.",
    )
    return pending.transition(
        policy_id=f"{SOURCE_ID}:verified",
        state=SourcePolicyState.VERIFIED,
        actor_id="operator",
        at=NOW - timedelta(days=1),
        reason="Đã duyệt purpose cụ thể.",
        allowed_purposes=purposes,
        evidence_ref=evidence_ref,
        reviewed_at=NOW - timedelta(days=1),
        valid_until=valid_until,
        approved_by="reviewer",
    )


class FakeRightsRepository:
    def __init__(
        self,
        observed: ObservedSourcePolicy | None,
        *,
        database_now: datetime = NOW,
    ) -> None:
        self.observed = observed
        self.database_now = database_now
        self.calls: list[tuple[str, object]] = []

    async def get_observed_entry(self, *, workspace_id, source_id):
        self.calls.append(("observed", (workspace_id, source_id)))
        return self.observed

    async def database_clock(self, *, workspace_id):
        self.calls.append(("clock", workspace_id))
        return self.database_now


def observed(source: SourceRegistryEntry) -> ObservedSourcePolicy:
    return ObservedSourcePolicy(
        source,
        {
            revision.policy.policy_id: max(
                NOW - timedelta(minutes=2),
                revision.created_at,
                revision.policy.reviewed_at or revision.created_at,
            )
            for revision in source.revisions
        },
    )


@pytest.mark.asyncio
async def test_registry_rights_gate_uses_current_observed_policy_and_database_clock() -> None:
    source = entry()
    repository = FakeRightsRepository(observed(source))
    gate = SourceRegistryBackfillRightsGate(
        workspace_id=WORKSPACE_ID,
        repository=repository,
    )

    evidence = await gate.authorize(request=request(), now=NOW - timedelta(hours=2))

    assert evidence.provider == "BINANCE_USDM_REST"
    assert evidence.workspace_id == WORKSPACE_ID
    assert evidence.purpose is BackfillRightsPurpose.BACKTEST_RESEARCH
    assert evidence.policy_fingerprint == source_rights_hash(source.current)
    assert evidence.terms_url == "https://www.binance.com/en/terms"
    assert evidence.verified_at == source.current.policy.reviewed_at
    assert evidence.checked_at == NOW
    assert evidence.valid_until == NOW + timedelta(minutes=5)
    assert [kind for kind, _ in repository.calls] == ["observed", "clock"]


@pytest.mark.asyncio
async def test_registry_rights_gate_caps_lease_at_policy_expiry() -> None:
    source = entry(valid_until=NOW + timedelta(minutes=2))
    gate = SourceRegistryBackfillRightsGate(
        workspace_id=WORKSPACE_ID,
        repository=FakeRightsRepository(observed(source)),
    )

    evidence = await gate.authorize(request=request(), now=NOW)

    assert evidence.valid_until == NOW + timedelta(minutes=2)


@pytest.mark.asyncio
async def test_registry_rights_gate_denies_missing_observation_or_wrong_purpose() -> None:
    missing = SourceRegistryBackfillRightsGate(
        workspace_id=WORKSPACE_ID,
        repository=FakeRightsRepository(None),
    )
    with pytest.raises(PermissionError, match="receipt"):
        await missing.authorize(request=request(), now=NOW)

    metadata_only = entry(purposes=frozenset({SourceUsePurpose.METADATA}))
    denied = SourceRegistryBackfillRightsGate(
        workspace_id=WORKSPACE_ID,
        repository=FakeRightsRepository(observed(metadata_only)),
    )
    with pytest.raises(PermissionError, match="mục đích"):
        await denied.authorize(request=request(), now=NOW)


@pytest.mark.asyncio
async def test_registry_rights_gate_denies_revoked_policy_and_future_receipt() -> None:
    verified = entry()
    revoked = verified.transition(
        policy_id=f"{SOURCE_ID}:revoked",
        state=SourcePolicyState.REVOKED,
        actor_id="operator",
        at=NOW - timedelta(minutes=1),
        reason="Quyền nguồn bị thu hồi.",
    )
    revoked_gate = SourceRegistryBackfillRightsGate(
        workspace_id=WORKSPACE_ID,
        repository=FakeRightsRepository(observed(revoked)),
    )
    with pytest.raises(PermissionError, match="verified"):
        await revoked_gate.authorize(request=request(), now=NOW)

    future_receipt = ObservedSourcePolicy(
        verified,
        {revision.policy.policy_id: NOW + timedelta(seconds=1) for revision in verified.revisions},
    )
    receipt_gate = SourceRegistryBackfillRightsGate(
        workspace_id=WORKSPACE_ID,
        repository=FakeRightsRepository(future_receipt),
    )
    with pytest.raises(PermissionError, match="observer receipt"):
        await receipt_gate.authorize(request=request(), now=NOW)


@pytest.mark.asyncio
async def test_registry_rights_gate_checks_expiry_using_database_clock_not_caller_time() -> None:
    source = entry(valid_until=NOW - timedelta(seconds=1))
    gate = SourceRegistryBackfillRightsGate(
        workspace_id=WORKSPACE_ID,
        repository=FakeRightsRepository(observed(source), database_now=NOW),
    )

    with pytest.raises(PermissionError, match="hết hạn"):
        await gate.authorize(request=request(), now=NOW - timedelta(days=10))


@pytest.mark.asyncio
async def test_registry_rights_gate_requires_https_terms_evidence() -> None:
    source = entry(evidence_ref="fixture:approved-policy")
    gate = SourceRegistryBackfillRightsGate(
        workspace_id=WORKSPACE_ID,
        repository=FakeRightsRepository(observed(source)),
    )

    with pytest.raises(PermissionError, match="HTTPS"):
        await gate.authorize(request=request(), now=NOW)


@pytest.mark.asyncio
async def test_paper_demo_requires_a_separate_registry_purpose() -> None:
    source = entry(purposes=frozenset({SourceUsePurpose.PAPER_DEMO}))
    gate = SourceRegistryBackfillRightsGate(
        workspace_id=WORKSPACE_ID,
        repository=FakeRightsRepository(observed(source)),
    )

    evidence = await gate.authorize(
        request=request(purpose=BackfillRightsPurpose.PAPER_DEMO),
        now=NOW,
    )

    assert evidence.purpose is BackfillRightsPurpose.PAPER_DEMO
    assert evidence.policy_fingerprint == source_rights_hash(source.current)

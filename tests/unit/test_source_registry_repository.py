from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.models import (
    SourcePolicyObservationRecord,
    SourcePolicyRevisionRecord,
    SourceQuotaEventRecord,
)
from trading_bot.adapters.database.repositories.source_registry import (
    SourceQuotaExceededError,
    SourceRegistryConflict,
    SourceRegistryRepository,
)
from trading_bot.domain.research.source_policy import (
    SourcePolicyState,
    SourceUsePurpose,
)
from trading_bot.domain.research.source_registry import (
    SourceBudget,
    SourcePolicyRevision,
    SourceRegistryEntry,
)

NOW = datetime(2026, 11, 1, 8, tzinfo=UTC)
WORKSPACE_ID = uuid4()


def discovered(*, source_id: str = "official-calendar") -> SourceRegistryEntry:
    return SourceRegistryEntry.discover(
        source_id=source_id,
        publisher="Official Publisher",
        endpoint_url="https://example.gov/calendar.json",
        access_method="GET",
        auth_scope="public",
        budget=SourceBudget(
            window=timedelta(hours=1),
            max_requests=3,
            max_bytes=1000,
            max_cost=Decimal("1.00"),
            cost_unit="USD",
        ),
        actor_id="operator-1",
        at=NOW,
        reason="Candidate source",
    )


def approved(source: SourceRegistryEntry) -> SourceRegistryEntry:
    pending = source.transition(
        policy_id=f"{source.source_id}:policy:pending",
        state=SourcePolicyState.RIGHTS_PENDING,
        actor_id="operator-1",
        at=NOW + timedelta(minutes=1),
        reason="Rights evidence submitted",
    )
    return pending.transition(
        policy_id=f"{source.source_id}:policy:2",
        state=SourcePolicyState.VERIFIED,
        actor_id="operator-1",
        at=NOW + timedelta(minutes=2),
        reason="Rights reviewed",
        allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
        evidence_ref="https://example.gov/terms",
        reviewed_at=NOW,
        valid_until=NOW + timedelta(days=30),
        approved_by="reviewer-1",
    )


def policy_receipts(rows):
    return [
        SourcePolicyObservationRecord(
            workspace_id=row.workspace_id,
            source_id=row.source_id,
            policy_id=row.policy_id,
            sequence=row.sequence,
            known_at=row.created_at,
        )
        for row in rows
    ]


class FakeResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class FakeSession:
    def __init__(
        self,
        *,
        scalar_results: list[object | None] | None = None,
        rows_results: list[list[object]] | None = None,
        database_now: datetime = NOW,
    ) -> None:
        self.scalar_results = iter(scalar_results or [])
        self.rows_results = iter(rows_results or [])
        self.added: list[object] = []
        self.statements: list[str] = []
        self.database_now = database_now

    async def execute(self, statement: object, params: object | None = None) -> object:
        del params
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return SimpleNamespace()

    async def scalar(self, statement: object) -> object | None:
        compiled = str(statement.compile(dialect=postgresql.dialect()))
        self.statements.append(compiled)
        if "clock_timestamp()" in compiled:
            return self.database_now
        return next(self.scalar_results, None)

    async def scalars(self, statement: object) -> FakeResult:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return FakeResult(next(self.rows_results, []))

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        return None

    @asynccontextmanager
    async def begin_nested(self):
        yield


@pytest.mark.asyncio
async def test_append_revision_uses_database_clock_for_creation_timestamp() -> None:
    entry = discovered()
    database_now = NOW + timedelta(hours=4)
    session = FakeSession(scalar_results=[None], rows_results=[[]], database_now=database_now)

    result = await SourceRegistryRepository(session).append_revision(
        workspace_id=WORKSPACE_ID,
        revision=entry.current,
    )

    row = session.added[0]
    assert result.is_duplicate is False
    assert isinstance(row, SourcePolicyRevisionRecord)
    assert row.workspace_id == WORKSPACE_ID
    assert row.source_id == entry.source_id
    assert row.sequence == 1
    assert row.state == "discovered"
    assert row.created_at == database_now
    assert result.revision.created_at == database_now
    assert row.allowed_purposes == []
    assert "source_policy_revision.workspace_id" in session.statements[4]
    assert "pg_advisory_xact_lock" in session.statements[1]
    assert any("clock_timestamp()" in statement for statement in session.statements)


@pytest.mark.asyncio
async def test_verified_revision_review_time_comes_from_database_clock_not_candidate() -> None:
    source = approved(discovered())
    candidate = replace(
        source.current,
        created_at=NOW - timedelta(days=20),
        policy=replace(source.current.policy, reviewed_at=NOW - timedelta(days=20)),
    )
    database_now = NOW + timedelta(hours=3)
    existing = [
        SourcePolicyRevisionRecord.from_domain(WORKSPACE_ID, revision)
        for revision in source.revisions[:-1]
    ]
    session = FakeSession(scalar_results=[None], rows_results=[existing], database_now=database_now)
    result = (
        await SourceRegistryRepository(session).append_revision(
            workspace_id=WORKSPACE_ID,
            revision=candidate,
        )
    ).revision

    assert result.created_at == database_now
    assert result.policy.reviewed_at == database_now
    assert result.actor_id == candidate.actor_id
    assert result.approved_by == candidate.approved_by
    row = session.added[0]
    assert isinstance(row, SourcePolicyRevisionRecord)
    assert row.created_at == database_now
    assert row.reviewed_at == database_now


@pytest.mark.asyncio
async def test_verified_revision_expired_by_database_clock_is_rejected() -> None:
    source = approved(discovered())
    expired_at_db = NOW + timedelta(days=31)
    existing = [
        SourcePolicyRevisionRecord.from_domain(WORKSPACE_ID, revision)
        for revision in source.revisions[:-1]
    ]
    session = FakeSession(
        scalar_results=[None], rows_results=[existing], database_now=expired_at_db
    )

    with pytest.raises(SourceRegistryConflict, match="database clock"):
        await SourceRegistryRepository(session).append_revision(
            workspace_id=WORKSPACE_ID, revision=source.current
        )

    assert session.added == []


@pytest.mark.asyncio
async def test_append_revision_retry_is_idempotent_but_changed_payload_conflicts() -> None:
    entry = discovered()
    existing = SourcePolicyRevisionRecord(
        workspace_id=WORKSPACE_ID,
        source_id=entry.source_id,
        sequence=1,
        policy_id=entry.current.policy.policy_id,
        state="discovered",
        allowed_purposes=[],
        evidence_ref=None,
        reviewed_at=None,
        valid_until=None,
        publisher=entry.current.publisher,
        endpoint_url=entry.current.endpoint_url,
        access_method=entry.current.access_method,
        auth_scope=entry.current.auth_scope,
        attribution=entry.current.attribution,
        retention_ref=entry.current.retention_ref,
        quota_window_seconds=3600,
        quota_max_requests=3,
        quota_max_bytes=1000,
        quota_max_cost=Decimal("1.00"),
        quota_cost_unit="USD",
        created_at=entry.current.created_at,
        actor_id=entry.current.actor_id,
        reason=entry.current.reason,
        approved_by=None,
    )
    retry = await SourceRegistryRepository(FakeSession(scalar_results=[existing])).append_revision(
        workspace_id=WORKSPACE_ID, revision=entry.current
    )

    assert retry.is_duplicate is True
    assert retry.revision == entry.current

    changed = replace(entry.current, reason="Different request using same policy ID")
    with pytest.raises(SourceRegistryConflict, match="idempotency"):
        await SourceRegistryRepository(FakeSession(scalar_results=[existing])).append_revision(
            workspace_id=WORKSPACE_ID, revision=changed
        )


@pytest.mark.asyncio
async def test_append_revision_requires_next_sequence_and_never_accepts_verified_as_first() -> None:
    entry = approved(discovered())
    with pytest.raises(SourceRegistryConflict, match="sequence"):
        await SourceRegistryRepository(FakeSession()).append_revision(
            workspace_id=WORKSPACE_ID,
            revision=entry.current,
        )

    source = discovered()
    with pytest.raises(SourceRegistryConflict, match="sequence"):
        await SourceRegistryRepository(
            FakeSession(scalar_results=[None], rows_results=[[]])
        ).append_revision(
            workspace_id=WORKSPACE_ID,
            revision=SourcePolicyRevision(
                sequence=2,
                policy=source.current.policy,
                publisher=source.current.publisher,
                endpoint_url=source.current.endpoint_url,
                access_method=source.current.access_method,
                auth_scope=source.current.auth_scope,
                attribution=source.current.attribution,
                retention_ref=source.current.retention_ref,
                budget=source.current.budget,
                created_at=source.current.created_at,
                actor_id=source.current.actor_id,
                reason=source.current.reason,
            ),
        )


@pytest.mark.asyncio
async def test_list_entry_reconstructs_ordered_history_for_exact_workspace() -> None:
    source = approved(discovered())
    rows = [
        SourcePolicyRevisionRecord.from_domain(WORKSPACE_ID, revision)
        for revision in source.revisions
    ]
    session = FakeSession(rows_results=[rows])

    restored = await SourceRegistryRepository(session).get_entry(
        workspace_id=WORKSPACE_ID,
        source_id=source.source_id,
    )

    assert restored == source
    assert "source_policy_revision.workspace_id" in session.statements[1]
    assert "source_policy_revision.source_id" in session.statements[1]


@pytest.mark.asyncio
async def test_quota_reservation_requires_current_policy_and_is_idempotent() -> None:
    source = approved(discovered())
    policy_rows = [
        SourcePolicyRevisionRecord.from_domain(WORKSPACE_ID, revision)
        for revision in source.revisions
    ]
    session = FakeSession(
        scalar_results=[None],
        rows_results=[policy_rows, policy_receipts(policy_rows), []],
        database_now=NOW + timedelta(minutes=3),
    )

    result = await SourceRegistryRepository(session).reserve_quota(
        workspace_id=WORKSPACE_ID,
        source_id=source.source_id,
        idempotency_key="poll:2026-11-01T08:00Z",
        purpose=SourceUsePurpose.METADATA,
        reserved_bytes=250,
        reserved_cost=Decimal("0.10"),
    )

    event = session.added[0]
    assert result.is_duplicate is False
    assert isinstance(event, SourceQuotaEventRecord)
    assert event.workspace_id == WORKSPACE_ID
    assert event.policy_sequence == source.current.sequence
    assert event.reserved_requests == 1
    assert event.reserved_bytes == 250
    assert event.reserved_cost == Decimal("0.10")
    assert event.cost_unit == "USD"
    assert "pg_advisory_xact_lock" in session.statements[1]
    assert any("clock_timestamp()" in statement for statement in session.statements)
    assert "source_quota_event.workspace_id" in session.statements[-1]


@pytest.mark.asyncio
async def test_quota_reservation_retry_returns_original_without_charging_again() -> None:
    source = approved(discovered())
    policy_rows = [
        SourcePolicyRevisionRecord.from_domain(WORKSPACE_ID, revision)
        for revision in source.revisions
    ]
    stored = SourceQuotaEventRecord(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        source_id=source.source_id,
        policy_sequence=source.current.sequence,
        idempotency_key="poll-idempotent",
        purpose="metadata",
        window_started_at=NOW + timedelta(minutes=3),
        occurred_at=NOW + timedelta(minutes=3),
        reserved_requests=1,
        reserved_bytes=250,
        reserved_cost=Decimal("0.10"),
        cost_unit="USD",
    )
    session = FakeSession(
        scalar_results=[stored], rows_results=[policy_rows, policy_receipts(policy_rows)]
    )

    result = await SourceRegistryRepository(session).reserve_quota(
        workspace_id=WORKSPACE_ID,
        source_id=source.source_id,
        idempotency_key="poll-idempotent",
        purpose=SourceUsePurpose.METADATA,
        reserved_bytes=250,
        reserved_cost=Decimal("0.10"),
    )

    assert result.is_duplicate is True
    assert result.event_id == stored.id
    assert session.added == []


@pytest.mark.asyncio
async def test_quota_idempotent_retry_returns_duplicate_after_policy_revocation() -> None:
    source = approved(discovered()).transition(
        policy_id="official-calendar:policy:revoked",
        state=SourcePolicyState.REVOKED,
        actor_id="reviewer-1",
        at=NOW + timedelta(days=1),
        reason="Source permission revoked",
    )
    stored = SourceQuotaEventRecord(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        source_id=source.source_id,
        policy_sequence=3,
        idempotency_key="poll-before-revocation",
        purpose="metadata",
        window_started_at=NOW + timedelta(minutes=3),
        occurred_at=NOW + timedelta(minutes=3),
        reserved_requests=1,
        reserved_bytes=250,
        reserved_cost=Decimal("0.10"),
        cost_unit="USD",
    )

    result = await SourceRegistryRepository(
        FakeSession(
            scalar_results=[stored],
            rows_results=[
                [
                    SourcePolicyRevisionRecord.from_domain(WORKSPACE_ID, item)
                    for item in source.revisions
                ],
                policy_receipts(
                    [
                        SourcePolicyRevisionRecord.from_domain(WORKSPACE_ID, item)
                        for item in source.revisions
                    ]
                ),
            ],
        )
    ).reserve_quota(
        workspace_id=WORKSPACE_ID,
        source_id=source.source_id,
        idempotency_key="poll-before-revocation",
        purpose=SourceUsePurpose.METADATA,
        reserved_bytes=250,
        reserved_cost=Decimal("0.10"),
    )

    assert result.is_duplicate is True
    assert result.event_id == stored.id


@pytest.mark.parametrize(
    ("purpose", "reserved_bytes", "reserved_cost", "error_match"),
    [
        (SourceUsePurpose.METADATA, True, Decimal("0.10"), "reserved_bytes"),
        (SourceUsePurpose.METADATA, 1, Decimal("NaN"), "reserved_cost"),
        ("metadata", 1, Decimal("0.10"), "purpose"),
    ],
)
@pytest.mark.asyncio
async def test_duplicate_quota_key_does_not_bypass_request_validation(
    purpose: object,
    reserved_bytes: object,
    reserved_cost: object,
    error_match: str,
) -> None:
    stored = SourceQuotaEventRecord(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        source_id="official-calendar",
        policy_sequence=2,
        idempotency_key="already-reserved",
        purpose="metadata",
        window_started_at=NOW,
        occurred_at=NOW,
        reserved_requests=1,
        reserved_bytes=1,
        reserved_cost=Decimal("0.10"),
        cost_unit="USD",
    )
    session = FakeSession(scalar_results=[stored])

    with pytest.raises(ValueError, match=error_match):
        await SourceRegistryRepository(session).reserve_quota(
            workspace_id=WORKSPACE_ID,
            source_id="official-calendar",
            idempotency_key="already-reserved",
            purpose=purpose,  # type: ignore[arg-type]
            reserved_bytes=reserved_bytes,  # type: ignore[arg-type]
            reserved_cost=reserved_cost,  # type: ignore[arg-type]
        )

    assert session.statements == []


@pytest.mark.asyncio
async def test_quota_reservation_is_denied_without_verified_purpose_or_after_expiry() -> None:
    pending = discovered()
    pending_rows = [
        SourcePolicyRevisionRecord.from_domain(WORKSPACE_ID, revision)
        for revision in pending.revisions
    ]
    with pytest.raises(PermissionError, match="policy"):
        await SourceRegistryRepository(
            FakeSession(
                scalar_results=[None],
                rows_results=[pending_rows, policy_receipts(pending_rows)],
                database_now=NOW + timedelta(minutes=2),
            )
        ).reserve_quota(
            workspace_id=WORKSPACE_ID,
            source_id=pending.source_id,
            idempotency_key="poll-pending",
            purpose=SourceUsePurpose.METADATA,
            reserved_bytes=100,
            reserved_cost=Decimal("0.01"),
        )

    source = approved(discovered())
    rows = [
        SourcePolicyRevisionRecord.from_domain(WORKSPACE_ID, revision)
        for revision in source.revisions
    ]
    rows[-1].valid_until = NOW + timedelta(minutes=5)
    session = FakeSession(
        scalar_results=[None],
        rows_results=[rows, policy_receipts(rows)],
        database_now=NOW + timedelta(minutes=10),
    )
    with pytest.raises(PermissionError, match="policy"):
        await SourceRegistryRepository(session).reserve_quota(
            workspace_id=WORKSPACE_ID,
            source_id=source.source_id,
            idempotency_key="poll-expired",
            purpose=SourceUsePurpose.METADATA,
            reserved_bytes=100,
            reserved_cost=Decimal("0.01"),
        )
    assert any("clock_timestamp()" in statement for statement in session.statements)


@pytest.mark.asyncio
async def test_cost_unit_change_within_active_window_fails_closed_with_stable_message() -> None:
    source = approved(discovered())
    policy_rows = [
        SourcePolicyRevisionRecord.from_domain(WORKSPACE_ID, revision)
        for revision in source.revisions
    ]
    policy_rows[-1].quota_cost_unit = "EUR"
    prior_event = SourceQuotaEventRecord(
        workspace_id=WORKSPACE_ID,
        source_id=source.source_id,
        policy_sequence=source.current.sequence,
        idempotency_key="prior-unit-reservation",
        purpose="metadata",
        window_started_at=NOW + timedelta(minutes=2),
        occurred_at=NOW + timedelta(minutes=2),
        reserved_requests=1,
        reserved_bytes=10,
        reserved_cost=Decimal("0.01"),
        cost_unit="USD",
    )
    session = FakeSession(
        scalar_results=[None],
        rows_results=[policy_rows, policy_receipts(policy_rows), [prior_event]],
        database_now=NOW + timedelta(minutes=3),
    )

    with pytest.raises(ValueError, match="cost_unit changed within the active quota window"):
        await SourceRegistryRepository(session).reserve_quota(
            workspace_id=WORKSPACE_ID,
            source_id=source.source_id,
            idempotency_key="different-unit",
            purpose=SourceUsePurpose.METADATA,
            reserved_bytes=1,
            reserved_cost=Decimal("0.01"),
        )
    assert session.added == []


@pytest.mark.asyncio
async def test_quota_reservation_serializes_usage_and_rejects_budget_overflow() -> None:
    source = approved(discovered())
    policy_rows = [
        SourcePolicyRevisionRecord.from_domain(WORKSPACE_ID, revision)
        for revision in source.revisions
    ]
    prior_event = SourceQuotaEventRecord(
        workspace_id=WORKSPACE_ID,
        source_id=source.source_id,
        policy_sequence=source.current.sequence,
        idempotency_key="prior-reservation",
        purpose="metadata",
        window_started_at=NOW + timedelta(minutes=2),
        occurred_at=NOW + timedelta(minutes=2),
        reserved_requests=3,
        reserved_bytes=900,
        reserved_cost=Decimal("0.90"),
        cost_unit="USD",
    )
    session = FakeSession(
        scalar_results=[None],
        rows_results=[policy_rows, policy_receipts(policy_rows), [prior_event]],
        database_now=NOW + timedelta(minutes=3),
    )

    with pytest.raises(SourceQuotaExceededError):
        await SourceRegistryRepository(session).reserve_quota(
            workspace_id=WORKSPACE_ID,
            source_id=source.source_id,
            idempotency_key="over-budget",
            purpose=SourceUsePurpose.METADATA,
            reserved_bytes=1,
            reserved_cost=Decimal("0.01"),
        )

    assert session.added == []
    assert "pg_advisory_xact_lock" in session.statements[1]

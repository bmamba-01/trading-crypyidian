from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.adapters.database.models.source_item_entities import (
    SourceItemRevisionRecord,
    SourceTakedownRecord,
)
from trading_bot.adapters.database.repositories.source_item_repository import (
    SourceItemRepository,
)
from trading_bot.domain.research.source_item_history import (
    SourceItemHistory,
    SourceItemHistoryConflict,
    SourceItemRevisionEntry,
)
from trading_bot.domain.research.source_items import SourceItemRevision

WORKSPACE_ID = uuid4()
SOURCE_ID = "official-feed"
ITEM_ID = "macro-briefing-20261102"
POLICY_ID = "policy-v1"
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


def make_revision(
    revision_id: str = "rev-1",
    *,
    source_id: str = SOURCE_ID,
    item_id: str = ITEM_ID,
    canonical_url: str = "https://example.gov/briefing/20261102",
    observed_at: datetime = NOW,
    available_at: datetime | None = None,
    tombstoned: bool = False,
) -> SourceItemRevision:
    eff_available_at = (
        available_at if available_at is not None else observed_at + timedelta(minutes=5)
    )
    return SourceItemRevision(
        source_id=source_id,
        item_id=item_id,
        revision_id=revision_id,
        canonical_url=canonical_url,
        observed_at=observed_at,
        available_at=eff_available_at,
        tombstoned=tombstoned,
    )


def test_source_item_revision_record_round_trips_domain_entry() -> None:
    rev = make_revision()
    entry = SourceItemRevisionEntry.create(
        rev,
        predecessor_revision_id=None,
        predecessor_hash=None,
    )

    record = SourceItemRevisionRecord.from_domain(
        workspace_id=WORKSPACE_ID,
        entry=entry,
        policy_id=POLICY_ID,
        sequence=1,
    )

    assert record.workspace_id == WORKSPACE_ID
    assert record.source_id == SOURCE_ID
    assert record.item_id == ITEM_ID
    assert record.revision_id == "rev-1"
    assert record.sequence == 1
    assert record.policy_id == POLICY_ID
    assert record.predecessor_revision_id is None
    assert record.predecessor_hash is None
    assert record.revision_hash == entry.revision_hash
    assert record.is_tombstone is False
    assert record.tombstoned is False

    restored = record.to_domain()
    assert restored == entry
    assert restored.revision == rev


def test_source_item_revision_record_detects_tampered_payload_or_index_fields() -> None:
    rev = make_revision()
    entry = SourceItemRevisionEntry.create(
        rev,
        predecessor_revision_id=None,
        predecessor_hash=None,
    )
    record = SourceItemRevisionRecord.from_domain(
        workspace_id=WORKSPACE_ID,
        entry=entry,
        policy_id=POLICY_ID,
        sequence=1,
    )

    record.canonical_payload = {"source_id": "tampered"}
    with pytest.raises(ValueError, match="canonical_payload"):
        record.to_domain()


class FakeSession:
    def __init__(
        self,
        *,
        scalar_results: list[object | None] | None = None,
        scalars_results: list[list[object]] | None = None,
        database_now: datetime = NOW,
    ) -> None:
        self.scalar_results = iter(scalar_results or [])
        self.scalars_results = iter(scalars_results or [])
        self.added: list[object] = []
        self.statements: list[str] = []
        self.database_now = database_now

    async def execute(self, statement: object, params: object | None = None) -> None:
        del params
        self.statements.append(str(statement))

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(str(statement))
        return next(self.scalar_results, None)

    async def scalars(self, statement: object) -> FakeResult:
        self.statements.append(str(statement))
        return FakeResult(next(self.scalars_results, []))

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        return None


class FakeResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


@pytest.mark.asyncio
async def test_append_first_revision_persists_record_and_binds_workspace() -> None:
    rev = make_revision("rev-1")
    entry = SourceItemRevisionEntry.create(
        rev,
        predecessor_revision_id=None,
        predecessor_hash=None,
    )

    # scalar_results: duplicate check returns None
    # scalars_results: existing_records query returns []
    session = FakeSession(
        scalar_results=[None],
        scalars_results=[[]],
    )
    repo = SourceItemRepository(session)  # type: ignore[arg-type]

    record = await repo.append_revision(
        workspace_id=WORKSPACE_ID,
        entry=entry,
        policy_id=POLICY_ID,
    )

    assert len(session.added) == 1
    assert session.added[0] is record
    assert record.sequence == 1
    assert record.revision_id == "rev-1"
    assert record.predecessor_revision_id is None
    assert record.predecessor_hash is None
    assert any("set_config" in stmt for stmt in session.statements)
    assert any("pg_advisory_xact_lock" in stmt for stmt in session.statements)


@pytest.mark.asyncio
async def test_append_revision_idempotent_retry_returns_existing() -> None:
    rev = make_revision("rev-1")
    entry = SourceItemRevisionEntry.create(
        rev,
        predecessor_revision_id=None,
        predecessor_hash=None,
    )
    existing_record = SourceItemRevisionRecord.from_domain(
        workspace_id=WORKSPACE_ID,
        entry=entry,
        policy_id=POLICY_ID,
        sequence=1,
    )

    # duplicate check returns existing_record with same hash & policy
    session = FakeSession(scalar_results=[existing_record])
    repo = SourceItemRepository(session)  # type: ignore[arg-type]

    result = await repo.append_revision(
        workspace_id=WORKSPACE_ID,
        entry=entry,
        policy_id=POLICY_ID,
    )

    assert result is existing_record
    assert len(session.added) == 0


@pytest.mark.asyncio
async def test_append_revision_rejects_duplicate_id_with_different_hash() -> None:
    rev = make_revision("rev-1")
    entry = SourceItemRevisionEntry.create(
        rev,
        predecessor_revision_id=None,
        predecessor_hash=None,
    )
    # Stored record with different hash
    existing_record = SourceItemRevisionRecord.from_domain(
        workspace_id=WORKSPACE_ID,
        entry=entry,
        policy_id=POLICY_ID,
        sequence=1,
    )
    existing_record.revision_hash = "a" * 64

    session = FakeSession(scalar_results=[existing_record])
    repo = SourceItemRepository(session)  # type: ignore[arg-type]

    with pytest.raises(SourceItemHistoryConflict, match="payload hoặc hash khác"):
        await repo.append_revision(
            workspace_id=WORKSPACE_ID,
            entry=entry,
            policy_id=POLICY_ID,
        )


@pytest.mark.asyncio
async def test_append_second_revision_checks_predecessor_and_increments_sequence() -> None:
    rev1 = make_revision("rev-1", observed_at=NOW)
    entry1 = SourceItemRevisionEntry.create(
        rev1,
        predecessor_revision_id=None,
        predecessor_hash=None,
    )
    record1 = SourceItemRevisionRecord.from_domain(
        workspace_id=WORKSPACE_ID,
        entry=entry1,
        policy_id=POLICY_ID,
        sequence=1,
    )

    rev2 = make_revision("rev-2", observed_at=NOW + timedelta(minutes=10))
    entry2 = SourceItemRevisionEntry.create(
        rev2,
        predecessor_revision_id="rev-1",
        predecessor_hash=entry1.revision_hash,
    )

    session = FakeSession(
        scalar_results=[None],  # duplicate check
        scalars_results=[[record1]],  # existing records query
    )
    repo = SourceItemRepository(session)  # type: ignore[arg-type]

    record2 = await repo.append_revision(
        workspace_id=WORKSPACE_ID,
        entry=entry2,
        policy_id=POLICY_ID,
    )

    assert record2.sequence == 2
    assert record2.revision_id == "rev-2"
    assert record2.predecessor_revision_id == "rev-1"
    assert record2.predecessor_hash == entry1.revision_hash


@pytest.mark.asyncio
async def test_append_rejects_broken_predecessor_chain() -> None:
    rev1 = make_revision("rev-1", observed_at=NOW)
    entry1 = SourceItemRevisionEntry.create(
        rev1,
        predecessor_revision_id=None,
        predecessor_hash=None,
    )
    record1 = SourceItemRevisionRecord.from_domain(
        workspace_id=WORKSPACE_ID,
        entry=entry1,
        policy_id=POLICY_ID,
        sequence=1,
    )

    rev2 = make_revision("rev-2", observed_at=NOW + timedelta(minutes=10))
    # Predecessor specifies non-existent rev-0
    entry2 = SourceItemRevisionEntry.create(
        rev2,
        predecessor_revision_id=None,
        predecessor_hash=None,
    )

    session = FakeSession(
        scalar_results=[None],
        scalars_results=[[record1]],
    )
    repo = SourceItemRepository(session)  # type: ignore[arg-type]

    with pytest.raises(SourceItemHistoryConflict, match="predecessor"):
        await repo.append_revision(
            workspace_id=WORKSPACE_ID,
            entry=entry2,
            policy_id=POLICY_ID,
        )


@pytest.mark.asyncio
async def test_append_rejects_revision_after_tombstone() -> None:
    rev1 = make_revision("rev-1", observed_at=NOW, tombstoned=True)
    entry1 = SourceItemRevisionEntry.create(
        rev1,
        predecessor_revision_id=None,
        predecessor_hash=None,
    )
    record1 = SourceItemRevisionRecord.from_domain(
        workspace_id=WORKSPACE_ID,
        entry=entry1,
        policy_id=POLICY_ID,
        sequence=1,
    )

    rev2 = make_revision("rev-2", observed_at=NOW + timedelta(minutes=10))
    entry2 = SourceItemRevisionEntry.create(
        rev2,
        predecessor_revision_id="rev-1",
        predecessor_hash=entry1.revision_hash,
    )

    session = FakeSession(
        scalar_results=[None],
        scalars_results=[[record1]],
    )
    repo = SourceItemRepository(session)  # type: ignore[arg-type]

    with pytest.raises(SourceItemHistoryConflict, match="tombstone"):
        await repo.append_revision(
            workspace_id=WORKSPACE_ID,
            entry=entry2,
            policy_id=POLICY_ID,
        )


@pytest.mark.asyncio
async def test_load_history_reconstructs_full_hash_chain() -> None:
    rev1 = make_revision("rev-1", observed_at=NOW)
    entry1 = SourceItemRevisionEntry.create(
        rev1, predecessor_revision_id=None, predecessor_hash=None
    )
    record1 = SourceItemRevisionRecord.from_domain(
        workspace_id=WORKSPACE_ID, entry=entry1, policy_id=POLICY_ID, sequence=1
    )

    rev2 = make_revision("rev-2", observed_at=NOW + timedelta(minutes=5))
    entry2 = SourceItemRevisionEntry.create(
        rev2, predecessor_revision_id="rev-1", predecessor_hash=entry1.revision_hash
    )
    record2 = SourceItemRevisionRecord.from_domain(
        workspace_id=WORKSPACE_ID, entry=entry2, policy_id=POLICY_ID, sequence=2
    )

    session = FakeSession(scalars_results=[[record1, record2]])
    repo = SourceItemRepository(session)  # type: ignore[arg-type]

    history = await repo.load_history(WORKSPACE_ID, SOURCE_ID, ITEM_ID)

    assert isinstance(history, SourceItemHistory)
    assert history.source_id == SOURCE_ID
    assert history.item_id == ITEM_ID
    assert len(history.entries) == 2
    assert history.entries[0] == entry1
    assert history.entries[1] == entry2


@pytest.mark.asyncio
async def test_record_takedown_and_check_is_active() -> None:
    requested_at = NOW
    effective_at = NOW + timedelta(minutes=1)

    # 1. Record takedown
    session = FakeSession(
        scalar_results=[None],  # active takedown check: None
    )
    repo = SourceItemRepository(session)  # type: ignore[arg-type]

    takedown = await repo.record_takedown(
        workspace_id=WORKSPACE_ID,
        source_id=SOURCE_ID,
        item_id=ITEM_ID,
        reason="DMCA Notice",
        requested_by="legal@example.gov",
        requested_at=requested_at,
        effective_at=effective_at,
    )

    assert isinstance(takedown, SourceTakedownRecord)
    assert takedown.reason == "DMCA Notice"
    assert takedown.is_active is True
    assert len(session.added) == 1

    # 2. Check active before effective_at -> False
    session_before = FakeSession(scalar_results=[None])
    repo_before = SourceItemRepository(session_before)  # type: ignore[arg-type]
    is_active_before = await repo_before.is_takedown_active(
        WORKSPACE_ID, SOURCE_ID, ITEM_ID, as_of=NOW
    )
    assert is_active_before is False

    # 3. Check active at or after effective_at -> True
    session_after = FakeSession(scalar_results=[takedown])
    repo_after = SourceItemRepository(session_after)  # type: ignore[arg-type]
    is_active_after = await repo_after.is_takedown_active(
        WORKSPACE_ID, SOURCE_ID, ITEM_ID, as_of=NOW + timedelta(minutes=5)
    )
    assert is_active_after is True


@pytest.mark.asyncio
async def test_get_latest_usable_revision_respects_point_in_time_and_takedown() -> None:
    rev1 = make_revision(
        "rev-1",
        observed_at=NOW,
        available_at=NOW + timedelta(minutes=5),
    )
    entry1 = SourceItemRevisionEntry.create(
        rev1, predecessor_revision_id=None, predecessor_hash=None
    )
    record1 = SourceItemRevisionRecord.from_domain(
        workspace_id=WORKSPACE_ID, entry=entry1, policy_id=POLICY_ID, sequence=1
    )

    rev2 = make_revision(
        "rev-2",
        observed_at=NOW + timedelta(minutes=10),
        available_at=NOW + timedelta(minutes=15),
    )
    entry2 = SourceItemRevisionEntry.create(
        rev2, predecessor_revision_id="rev-1", predecessor_hash=entry1.revision_hash
    )
    record2 = SourceItemRevisionRecord.from_domain(
        workspace_id=WORKSPACE_ID, entry=entry2, policy_id=POLICY_ID, sequence=2
    )

    # Case A: as_of before rev1 is available (NOW + 2m) -> None
    session_a = FakeSession(
        scalar_results=[None],  # no active takedown
        scalars_results=[[record1, record2]],  # history records
    )
    repo_a = SourceItemRepository(session_a)  # type: ignore[arg-type]
    res_a = await repo_a.get_latest_usable_revision(
        WORKSPACE_ID, SOURCE_ID, ITEM_ID, as_of=NOW + timedelta(minutes=2)
    )
    assert res_a is None

    # Case B: as_of between rev1 and rev2 (NOW + 10m) -> rev1
    session_b = FakeSession(
        scalar_results=[None],
        scalars_results=[[record1, record2]],
    )
    repo_b = SourceItemRepository(session_b)  # type: ignore[arg-type]
    res_b = await repo_b.get_latest_usable_revision(
        WORKSPACE_ID, SOURCE_ID, ITEM_ID, as_of=NOW + timedelta(minutes=10)
    )
    assert res_b == rev1

    # Case C: as_of after rev2 is available (NOW + 20m) -> rev2
    session_c = FakeSession(
        scalar_results=[None],
        scalars_results=[[record1, record2]],
    )
    repo_c = SourceItemRepository(session_c)  # type: ignore[arg-type]
    res_c = await repo_c.get_latest_usable_revision(
        WORKSPACE_ID, SOURCE_ID, ITEM_ID, as_of=NOW + timedelta(minutes=20)
    )
    assert res_c == rev2

    # Case D: Active takedown blocks retrieval
    takedown = SourceTakedownRecord(
        workspace_id=WORKSPACE_ID,
        source_id=SOURCE_ID,
        item_id=ITEM_ID,
        reason="Infringement",
        requested_by="author@example.com",
        requested_at=NOW,
        effective_at=NOW,
        is_active=True,
    )
    session_d = FakeSession(
        scalar_results=[takedown],  # active takedown exists
    )
    repo_d = SourceItemRepository(session_d)  # type: ignore[arg-type]
    res_d = await repo_d.get_latest_usable_revision(
        WORKSPACE_ID, SOURCE_ID, ITEM_ID, as_of=NOW + timedelta(minutes=20)
    )
    assert res_d is None


@pytest.mark.asyncio
async def test_append_revision_rejects_backward_observed_at() -> None:
    rev1 = make_revision("rev-1", observed_at=NOW + timedelta(minutes=10))
    entry1 = SourceItemRevisionEntry.create(
        rev1, predecessor_revision_id=None, predecessor_hash=None
    )
    record1 = SourceItemRevisionRecord.from_domain(
        workspace_id=WORKSPACE_ID, entry=entry1, policy_id=POLICY_ID, sequence=1
    )

    rev2 = make_revision("rev-2", observed_at=NOW + timedelta(minutes=5))
    entry2 = SourceItemRevisionEntry.create(
        rev2, predecessor_revision_id="rev-1", predecessor_hash=entry1.revision_hash
    )

    session = FakeSession(
        scalar_results=[None],
        scalars_results=[[record1]],
    )
    repo = SourceItemRepository(session)  # type: ignore[arg-type]

    with pytest.raises(SourceItemHistoryConflict, match="observed_at không được lùi"):
        await repo.append_revision(
            workspace_id=WORKSPACE_ID,
            entry=entry2,
            policy_id=POLICY_ID,
        )


@pytest.mark.asyncio
async def test_append_first_revision_rejects_predecessor_specified() -> None:
    rev1 = make_revision("rev-1", observed_at=NOW)
    entry1 = SourceItemRevisionEntry.create(
        rev1, predecessor_revision_id="rev-0", predecessor_hash="f" * 64
    )

    session = FakeSession(
        scalar_results=[None],
        scalars_results=[[]],
    )
    repo = SourceItemRepository(session)  # type: ignore[arg-type]

    with pytest.raises(SourceItemHistoryConflict, match="Revision đầu tiên"):
        await repo.append_revision(
            workspace_id=WORKSPACE_ID,
            entry=entry1,
            policy_id=POLICY_ID,
        )


@pytest.mark.asyncio
async def test_record_takedown_idempotent_and_conflict() -> None:
    takedown = SourceTakedownRecord(
        workspace_id=WORKSPACE_ID,
        source_id=SOURCE_ID,
        item_id=ITEM_ID,
        reason="DMCA Notice",
        requested_by="legal@example.gov",
        requested_at=NOW,
        effective_at=NOW,
        is_active=True,
    )

    # Idempotent retry: cùng reason, requested_by, effective_at
    session_idempotent = FakeSession(scalar_results=[takedown])
    repo_idempotent = SourceItemRepository(session_idempotent)  # type: ignore[arg-type]
    retried = await repo_idempotent.record_takedown(
        workspace_id=WORKSPACE_ID,
        source_id=SOURCE_ID,
        item_id=ITEM_ID,
        reason="DMCA Notice",
        requested_by="legal@example.gov",
        requested_at=NOW,
        effective_at=NOW,
    )
    assert retried is takedown
    assert len(session_idempotent.added) == 0

    # Conflict: khác reason
    session_conflict = FakeSession(scalar_results=[takedown])
    repo_conflict = SourceItemRepository(session_conflict)  # type: ignore[arg-type]
    with pytest.raises(SourceItemHistoryConflict, match="takedown đang hoạt động"):
        await repo_conflict.record_takedown(
            workspace_id=WORKSPACE_ID,
            source_id=SOURCE_ID,
            item_id=ITEM_ID,
            reason="Different reason",
            requested_by="legal@example.gov",
            requested_at=NOW,
            effective_at=NOW,
        )


@pytest.mark.asyncio
async def test_record_takedown_rejects_effective_at_before_requested_at() -> None:
    session = FakeSession()
    repo = SourceItemRepository(session)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="effective_at không được trước requested_at"):
        await repo.record_takedown(
            workspace_id=WORKSPACE_ID,
            source_id=SOURCE_ID,
            item_id=ITEM_ID,
            reason="Notice",
            requested_by="legal@example.gov",
            requested_at=NOW,
            effective_at=NOW - timedelta(minutes=1),
        )


@pytest.mark.asyncio
async def test_load_history_empty_returns_empty_history() -> None:
    session = FakeSession(scalars_results=[[]])
    repo = SourceItemRepository(session)  # type: ignore[arg-type]

    history = await repo.load_history(WORKSPACE_ID, SOURCE_ID, ITEM_ID)
    assert history.source_id == SOURCE_ID
    assert history.item_id == ITEM_ID
    assert history.entries == ()
    assert history.latest is None


@pytest.mark.asyncio
async def test_get_latest_usable_revision_rejects_as_of_after_evaluated_at() -> None:
    session = FakeSession()
    repo = SourceItemRepository(session)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="as_of không được sau evaluated_at"):
        await repo.get_latest_usable_revision(
            WORKSPACE_ID,
            SOURCE_ID,
            ITEM_ID,
            as_of=NOW + timedelta(minutes=5),
            evaluated_at=NOW,
        )

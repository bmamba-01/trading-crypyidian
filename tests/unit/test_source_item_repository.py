"""Kiểm repository metadata nguồn; PostgreSQL/RLS được kiểm riêng bằng Compose."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from tests.unit.test_source_registry_repository import NOW, FakeSession, approved, discovered

from trading_bot.adapters.database.models import SourceItemRevisionRecord, SourceItemTakedownRecord
from trading_bot.adapters.database.repositories.source_items import SourceItemRepository
from trading_bot.domain.research.source_item_history import (
    SourceItemHistory,
    SourceItemHistoryConflict,
)
from trading_bot.domain.research.source_items import SourceItemRevision
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose

WORKSPACE = uuid4()


def revision(revision_id="r1", **changes):
    return replace(
        SourceItemRevision(
            source_id="official-calendar",
            item_id="item-1",
            revision_id=revision_id,
            canonical_url="https://example.gov/synthetic/1",
            observed_at=NOW + timedelta(minutes=3),
            available_at=NOW + timedelta(minutes=5),
        ),
        **changes,
    )


def rows(*revisions, retention_expires_at=None):
    history = SourceItemHistory.empty(
        source_id=revisions[0].source_id, item_id=revisions[0].item_id
    )
    result = []
    for number, candidate in enumerate(revisions, 1):
        history = history.append(
            candidate,
            predecessor_revision_id=history.latest.revision.revision_id if history.latest else None,
        )
        result.append(
            SourceItemRevisionRecord.from_entry(
                WORKSPACE, number, history.latest, retention_expires_at=retention_expires_at
            )
        )
    return result


@pytest.mark.asyncio
async def test_append_persists_chain_and_identical_retry_is_idempotent():
    first = revision()
    second = revision("r2", observed_at=NOW + timedelta(minutes=4))
    stored = rows(first)
    session = FakeSession(rows_results=[stored])
    result = await SourceItemRepository(session).append_revision(
        workspace_id=WORKSPACE, revision=second, predecessor_revision_id="r1"
    )
    record = session.added[0]
    assert result.is_duplicate is False
    assert record.sequence == 2
    assert record.predecessor_hash == stored[0].revision_hash
    assert result.entry.predecessor_revision_id == "r1"
    assert result.entry.revision == second
    assert record.workspace_id == WORKSPACE
    duplicate_session = FakeSession(rows_results=[rows(first, second)])
    duplicate = await SourceItemRepository(duplicate_session).append_revision(
        workspace_id=WORKSPACE, revision=second, predecessor_revision_id="r1"
    )
    assert duplicate.is_duplicate is True
    assert duplicate.entry == result.entry
    assert duplicate_session.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["payload", "predecessor", "retention"])
async def test_retry_rejects_conflicting_payload_predecessor_or_retention(change):
    session = FakeSession(rows_results=[rows(revision())])
    with pytest.raises(SourceItemHistoryConflict):
        await SourceItemRepository(session).append_revision(
            workspace_id=WORKSPACE,
            revision=revision(canonical_url="https://example.gov/changed")
            if change == "payload"
            else revision(),
            predecessor_revision_id="wrong" if change == "predecessor" else None,
            retention_expires_at=NOW + timedelta(days=1) if change == "retention" else None,
        )
    assert session.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["predecessor", "observed", "tombstone"])
async def test_append_rejects_broken_lineage_and_terminal_tombstone(change):
    session = FakeSession(rows_results=[rows(revision(tombstoned=change == "tombstone"))])
    with pytest.raises(SourceItemHistoryConflict):
        await SourceItemRepository(session).append_revision(
            workspace_id=WORKSPACE,
            revision=revision(
                "r2", observed_at=NOW if change == "observed" else NOW + timedelta(minutes=4)
            ),
            predecessor_revision_id=None if change == "predecessor" else "r1",
        )
    assert session.added == []


@pytest.mark.asyncio
async def test_read_revalidates_hash_chain_and_scopes_all_identity_columns():
    stored = rows(revision())
    stored[0].revision_hash = "0" * 64
    session = FakeSession(rows_results=[stored])
    with pytest.raises(ValueError, match="revision_hash"):
        await SourceItemRepository(session).get_history(
            workspace_id=WORKSPACE, source_id="official-calendar", item_id="item-1"
        )
    query = session.statements[-1]
    assert "source_item_revision.workspace_id =" in query
    assert "source_item_revision.source_id =" in query
    assert "source_item_revision.item_id =" in query


@pytest.mark.asyncio
async def test_group_uses_latest_exact_url_within_workspace_and_source():
    stored = rows(revision(), revision("r2", canonical_url="https://example.gov/changed"))
    stored += rows(revision(item_id="repost"))
    session = FakeSession(rows_results=[stored])
    grouped = await SourceItemRepository(session).find_group(
        workspace_id=WORKSPACE,
        source_id="official-calendar",
        canonical_url="https://example.gov/synthetic/1",
    )
    assert grouped == ("repost",)
    query = session.statements[-1]
    assert "source_item_revision.workspace_id =" in query
    assert "source_item_revision.source_id =" in query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["http://example.gov/a", "https://EXAMPLE.gov/a", "https://example.gov/a#x"]
)
async def test_group_rejects_unvalidated_url_without_rewriting(invalid):
    with pytest.raises(ValueError, match="canonical_url"):
        await SourceItemRepository(FakeSession()).find_group(
            workspace_id=WORKSPACE, source_id="official-calendar", canonical_url=invalid
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [None, "00000000-0000-0000-0000-000000000000"])
async def test_history_requires_explicit_uuid_workspace(workspace):
    with pytest.raises(ValueError, match="workspace_id"):
        await SourceItemRepository(FakeSession()).get_history(
            workspace_id=workspace, source_id="official-calendar", item_id="item-1"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "allowed",
        "late",
        "unavailable",
        "tombstone",
        "takedown",
        "retention",
        "revoked",
        "purpose",
        "missing_policy",
    ],
)
async def test_access_loads_authoritative_current_state_before_pure_policy_gate(case):
    first = revision()
    second = revision("r2", available_at=NOW + timedelta(days=2))
    candidates = [first, second]
    if case == "tombstone":
        candidates.append(revision("r3", tombstoned=True, available_at=NOW + timedelta(hours=1)))
    if case == "unavailable":
        candidates = [revision(available_at=None)]
    deadline = NOW + timedelta(hours=2) if case == "retention" else None
    stored = rows(*candidates, retention_expires_at=deadline)
    policy = approved(discovered())
    if case == "revoked":
        policy = policy.transition(
            policy_id="revoked",
            state=SourcePolicyState.REVOKED,
            actor_id="operator",
            at=NOW + timedelta(hours=1),
            reason="Thu hồi fixture",
        )
    from trading_bot.adapters.database.models import SourcePolicyRevisionRecord

    policy_rows = [
        SourcePolicyRevisionRecord.from_domain(WORKSPACE, item) for item in policy.revisions
    ]
    takedown = (
        SourceItemTakedownRecord(
            workspace_id=WORKSPACE,
            source_id=first.source_id,
            item_id=first.item_id,
            taken_down_at=NOW + timedelta(hours=1),
            reason_code="rights_withdrawn",
        )
        if case == "takedown"
        else None
    )
    session = FakeSession(
        scalar_results=[takedown],
        rows_results=[stored, [] if case == "missing_policy" else policy_rows],
        database_now=NOW + timedelta(days=1),
    )
    selected = await SourceItemRepository(session).select_accessible(
        workspace_id=WORKSPACE,
        source_id=first.source_id,
        item_id=first.item_id,
        as_of=NOW + timedelta(minutes=5),
        purpose=SourceUsePurpose.TRAINING if case == "purpose" else SourceUsePurpose.METADATA,
    )
    if case in {"allowed", "late"}:
        assert selected.revision.revision_id == "r1"
    else:
        assert selected is None


@pytest.mark.asyncio
async def test_takedown_is_db_clock_stamped_and_immutable_retry():
    now = NOW + timedelta(days=1)
    session = FakeSession(scalar_results=[None], rows_results=[rows(revision())], database_now=now)
    record = await SourceItemRepository(session).take_down(
        workspace_id=WORKSPACE,
        source_id="official-calendar",
        item_id="item-1",
        reason_code="rights_withdrawn",
    )
    assert record.taken_down_at == now
    assert session.added == [record]
    retry = FakeSession(scalar_results=[record])
    assert (
        await SourceItemRepository(retry).take_down(
            workspace_id=WORKSPACE,
            source_id="official-calendar",
            item_id="item-1",
            reason_code="rights_withdrawn",
        )
        is record
    )
    with pytest.raises(SourceItemHistoryConflict):
        await SourceItemRepository(FakeSession(scalar_results=[record])).take_down(
            workspace_id=WORKSPACE,
            source_id="official-calendar",
            item_id="item-1",
            reason_code="provider_deleted",
        )

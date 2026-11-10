"""Gate tri thức dùng receipt quan sát sau commit, không timestamp audit."""

from datetime import timedelta

import pytest
from tests.unit.test_source_item_repository import revision
from tests.unit.test_source_registry_repository import NOW, approved, discovered

from trading_bot.adapters.database import models
from trading_bot.adapters.database.repositories.source_items import SourceItemRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.domain.research.source_policy import SourceUsePurpose
from trading_bot.domain.research.source_revision_access import (
    SourceAccessMode,
    evaluate_source_revision_access,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["complete", "latest_missing", "gap", "mismatch", "regressed"])
async def test_observed_policy_loader_requires_complete_immutable_chain(case):
    from tests.unit.test_source_registry_repository import (
        WORKSPACE_ID,
        FakeSession,
        policy_receipts,
    )

    history = approved(discovered())
    rows = [
        models.SourcePolicyRevisionRecord.from_domain(WORKSPACE_ID, entry)
        for entry in history.revisions
    ]
    receipts = policy_receipts(rows)
    if case == "latest_missing":
        receipts.pop()
    elif case == "gap":
        receipts.pop(0)
    elif case == "mismatch":
        receipts[-1].policy_id = "wrong"
    elif case == "regressed":
        receipts[-1].known_at = NOW
    result = await SourceRegistryRepository(
        FakeSession(rows_results=[rows, receipts])
    ).get_observed_entry(
        workspace_id=WORKSPACE_ID,
        source_id=history.source_id,
    )
    if case == "complete":
        assert result.history == history
        assert result.known_at[history.current.policy.policy_id] == history.current.created_at
        with pytest.raises(TypeError):
            result.known_at["changed"] = NOW
    else:
        assert result is None


@pytest.mark.asyncio
async def test_quota_duplicate_cannot_bypass_latest_unfinalized_policy():
    from tests.unit.test_source_registry_repository import WORKSPACE_ID, FakeSession

    history = approved(discovered())
    rows = [
        models.SourcePolicyRevisionRecord.from_domain(WORKSPACE_ID, entry)
        for entry in history.revisions
    ]
    with pytest.raises(PermissionError, match="receipt"):
        await SourceRegistryRepository(FakeSession(rows_results=[rows, []])).reserve_quota(
            workspace_id=WORKSPACE_ID,
            source_id=history.source_id,
            idempotency_key="existing",
            purpose=SourceUsePurpose.METADATA,
            reserved_bytes=1,
            reserved_cost="0",
        )


@pytest.mark.parametrize("kind", ["policy", "item"])
@pytest.mark.asyncio
async def test_append_observation_drains_101_revisions_without_resetting_cursor(monkeypatch, kind):
    from tests.unit.test_source_registry_repository import WORKSPACE_ID

    from trading_bot.adapters.database.source_observer import (
        ObservationProgress,
        PostCommitSourceObserver,
    )

    observer = PostCommitSourceObserver(None)
    cursors = []

    async def page(**kwargs):
        cursor = kwargs["after_sequence"]
        cursors.append(cursor)
        return ObservationProgress(100, False) if cursor == 0 else ObservationProgress(101, True)

    monkeypatch.setattr(observer, f"recover_{kind}", page)
    arguments = {"workspace_id": WORKSPACE_ID, "source_id": "source"}
    if kind == "item":
        arguments["item_id"] = "item"
    result = await getattr(observer, f"_finish_{kind}")(**arguments)
    assert cursors == [0, 100]
    assert (result.last_sequence, result.complete) == (101, True)


def test_application_observer_requires_explicit_quota_factory():
    from trading_bot.adapters.database.source_observer import PostCommitSourceObserver
    from trading_bot.application.source_registry import SourceRegistryService

    with pytest.raises(ValueError, match="quota_sessions"):
        SourceRegistryService(PostCommitSourceObserver(None))


def test_observation_schema_and_explicit_finalizers_exist():
    assert getattr(models, "SourceItemObservationRecord", None) is not None
    assert getattr(models, "SourcePolicyObservationRecord", None) is not None
    assert callable(getattr(SourceItemRepository, "finalize_revision", None))
    assert callable(getattr(SourceRegistryRepository, "finalize_policy", None))


@pytest.mark.parametrize(
    ("cutoff", "permitted"),
    [
        (timedelta(minutes=15), False),
        (timedelta(minutes=30), False),
        (timedelta(minutes=30, microseconds=1), True),
    ],
)
def test_policy_knowledge_uses_strict_observation_boundary(cutoff, permitted):
    history = approved(discovered())
    known = {
        entry.policy.policy_id: NOW + timedelta(minutes=10 * number)
        for number, entry in enumerate(history.revisions, 1)
    }
    decision = evaluate_source_revision_access(
        revision=revision(),
        policy_history=history,
        purpose=SourceUsePurpose.METADATA,
        mode=SourceAccessMode.HISTORICAL_REPLAY,
        as_of=NOW + cutoff,
        evaluated_at=NOW + timedelta(hours=1),
        current_tombstoned=False,
        policy_known_at=known,
    )
    assert decision.permitted is permitted
    assert history.current.created_at == NOW + timedelta(minutes=2)


@pytest.mark.parametrize("case", ["latest_missing", "gap", "regressed"])
def test_policy_missing_observation_or_clock_regression_fails_closed(case):
    history = approved(discovered())
    known = {
        entry.policy.policy_id: NOW + timedelta(minutes=10 * number)
        for number, entry in enumerate(history.revisions, 1)
    }
    if case == "regressed":
        known[history.current.policy.policy_id] = NOW + timedelta(minutes=1)
    else:
        known.pop(history.revisions[-1 if case == "latest_missing" else 0].policy.policy_id)
    decision = evaluate_source_revision_access(
        revision=revision(),
        policy_history=history,
        purpose=SourceUsePurpose.METADATA,
        mode=SourceAccessMode.HISTORICAL_REPLAY,
        as_of=NOW + timedelta(minutes=45),
        evaluated_at=NOW + timedelta(hours=1),
        current_tombstoned=False,
        policy_known_at=known,
    )
    assert decision.permitted is False

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from trading_bot.adapters.database.models.learning_scoring_deployment_entities import (
    LearningScoringDeploymentEventRecord,
)
from trading_bot.adapters.database.repositories.learning_scoring_deployments import (
    LearningScoringDeploymentRepository,
    ScorerDeploymentConflict,
)
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentEvent,
    ScorerDeploymentMode,
)


def _event(*, reason: str, now: datetime) -> ScorerDeploymentEvent:
    return ScorerDeploymentEvent(
        event_id=uuid4(),
        workspace_id=uuid4(),
        revision=1,
        mode=ScorerDeploymentMode.DISABLED,
        target=None,
        actor_membership_id=uuid4(),
        mfa_verified_at=now - timedelta(minutes=1),
        mfa_method="TOTP",
        expected_pointer_version=0,
        expected_deployment_revision=0,
        reason=reason,
        idempotency_key=uuid4(),
        previous_event_id=None,
        previous_event_hash=None,
        created_at=now,
    )


def _session(*scalar_results: object) -> AsyncMock:
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=list(scalar_results))
    session.execute = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


def test_repository_same_idempotency_intent_returns_stored_event() -> None:
    now = datetime.now(UTC)
    stored_event = _event(reason="Tắt scorer sau khi rà soát.", now=now)
    stored = LearningScoringDeploymentEventRecord.from_domain(stored_event)
    session = _session(now + timedelta(seconds=3), stored)
    repository = LearningScoringDeploymentRepository(session)

    result = asyncio.run(
        repository.append_disabled_event(
            workspace_id=stored_event.workspace_id,
            actor_membership_id=stored_event.actor_membership_id,
            mfa_verified_at=now + timedelta(seconds=1),
            expected_pointer_version=0,
            expected_deployment_revision=0,
            idempotency_key=stored_event.idempotency_key,
            reason=stored_event.reason,
        )
    )

    assert result.event_id == stored_event.event_id
    assert result.event_hash == stored_event.event_hash
    assert session.scalar.await_count == 2
    session.add.assert_not_called()


def test_repository_changed_intent_under_same_key_conflicts() -> None:
    now = datetime.now(UTC)
    stored_event = _event(reason="Tắt scorer sau khi rà soát.", now=now)
    stored = LearningScoringDeploymentEventRecord.from_domain(stored_event)
    session = _session(now + timedelta(seconds=3), stored)
    repository = LearningScoringDeploymentRepository(session)

    with pytest.raises(ScorerDeploymentConflict, match="ý định deployment khác"):
        asyncio.run(
            repository.append_disabled_event(
                workspace_id=stored_event.workspace_id,
                actor_membership_id=stored_event.actor_membership_id,
                mfa_verified_at=now + timedelta(seconds=1),
                expected_pointer_version=0,
                expected_deployment_revision=0,
                idempotency_key=stored_event.idempotency_key,
                reason="Tắt scorer vì phát hiện thay đổi quyền nguồn.",
            )
        )
    assert session.scalar.await_count == 2
    session.add.assert_not_called()


def test_repository_deployment_revision_cas_rejects_stale_request() -> None:
    now = datetime.now(UTC)
    workspace_id = uuid4()
    actor_id = uuid4()
    session = _session(
        now,
        None,
        SimpleNamespace(revision=2, active_pointer_version=4, event_id=uuid4()),
    )
    repository = LearningScoringDeploymentRepository(session)

    with pytest.raises(ScorerDeploymentConflict, match="CAS đã cũ"):
        asyncio.run(
            repository.append_disabled_event(
                workspace_id=workspace_id,
                actor_membership_id=actor_id,
                mfa_verified_at=now - timedelta(minutes=1),
                expected_pointer_version=4,
                expected_deployment_revision=1,
                idempotency_key=uuid4(),
                reason="Tắt scorer sau khi rà soát.",
            )
        )
    session.add.assert_not_called()


def test_repository_uses_database_clock_for_audit_event() -> None:
    db_clock = datetime.now(UTC)
    current_pointer = SimpleNamespace(revision=0, active_pointer_version=0, event_id=None)
    registry_pointer = SimpleNamespace(version=0)
    session = _session(db_clock, None, current_pointer, registry_pointer)
    repository = LearningScoringDeploymentRepository(session)

    event = asyncio.run(
        repository.append_disabled_event(
            workspace_id=uuid4(),
            actor_membership_id=uuid4(),
            mfa_verified_at=db_clock - timedelta(minutes=1),
            expected_pointer_version=0,
            expected_deployment_revision=0,
            idempotency_key=uuid4(),
            reason="Tắt scorer sau khi rà soát.",
        )
    )

    assert event.created_at == db_clock
    persisted = session.add.call_args.args[0]
    assert persisted.event_hash == event.event_hash
    assert persisted.intent_hash == event.intent_hash


def test_runtime_deployment_projection_selects_only_workspace_metadata() -> None:
    workspace_id = uuid4()

    class Result:
        def __init__(self, row):
            self._row = row

        def one_or_none(self):
            return self._row

    pointer_result = Result(None)
    registry_result = Result(SimpleNamespace(workspace_id=workspace_id, version=7))
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            pointer_result,
            registry_result,
        ]
    )
    repository = LearningScoringDeploymentRepository(session)

    snapshot = asyncio.run(repository.load_admin_scoring_deployment(workspace_id))

    assert snapshot.mode is ScorerDeploymentMode.DISABLED
    assert snapshot.active_pointer_version == 7
    scorer_lock = session.execute.await_args_list[1].args[0]
    model_lock = session.execute.await_args_list[2].args[0]
    assert session.execute.await_args_list[1].args[1] == {
        "key": f"learning-scoring-deployment:{workspace_id}:current-pointer"
    }
    assert session.execute.await_args_list[2].args[1] == {
        "key": f"learning-model-registry:{workspace_id}:active-pointer"
    }
    assert "pg_advisory_xact_lock" in str(scorer_lock)
    assert "pg_advisory_xact_lock" in str(model_lock)
    pointer_query, registry_query = session.execute.await_args_list[3:]
    assert {item["name"] for item in pointer_query.args[0].column_descriptions} == {
        "workspace_id",
        "event_id",
        "revision",
        "active_pointer_version",
        "mode",
    }
    assert {item["name"] for item in registry_query.args[0].column_descriptions} == {
        "workspace_id",
        "version",
    }

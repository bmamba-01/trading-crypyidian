from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from trading_bot.adapters.database.repositories.learning_observations_admin import (
    LearningObservationAdminItem,
    LearningObservationsAdminRepository,
)
from trading_bot.application.learning_observations_admin import (
    LearningObservationsAdminService,
)
from trading_bot.auth.policy import Permission, Principal, Role

WORKSPACE_ID = uuid4()
MEMBERSHIP_ID = uuid4()
USER_ID = uuid4()
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def _principal(*, roles: frozenset[Role] | None = None) -> Principal:
    return Principal(
        user_id=USER_ID,
        membership_id=MEMBERSHIP_ID,
        workspace_id=WORKSPACE_ID,
        roles=roles or frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=NOW,
    )


class _FakeUnitOfWork:
    instances: list[_FakeUnitOfWork] = []

    def __init__(
        self,
        sessions: object,
        *,
        workspace_id: object,
        actor_membership_id: object,
        snapshot_read_only: bool = False,
    ) -> None:
        self.workspace_id = workspace_id
        self.actor_membership_id = actor_membership_id
        self.snapshot_read_only = snapshot_read_only
        self.session = object()
        self.instances.append(self)

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _FakeRepository:
    calls: list[tuple[str, object, int]] = []

    def __init__(self, session: object) -> None:
        self.session = session

    async def list_recent(self, *, workspace_id: object, limit: int) -> tuple[object, ...]:
        self.calls.append(("list", workspace_id, limit))
        return ()


def _service() -> LearningObservationsAdminService:
    _FakeUnitOfWork.instances.clear()
    _FakeRepository.calls.clear()
    return LearningObservationsAdminService(
        object(),
        unit_of_work_factory=_FakeUnitOfWork,
        repository_factory=_FakeRepository,
    )


@pytest.mark.asyncio
async def test_recent_observations_use_principal_workspace_and_read_committed_uow() -> None:
    facade = _service().for_principal(_principal())

    result = await facade.list_recent()

    assert result == ()
    assert _FakeRepository.calls == [("list", WORKSPACE_ID, 50)]
    unit = _FakeUnitOfWork.instances[-1]
    assert unit.workspace_id == WORKSPACE_ID
    assert unit.actor_membership_id == MEMBERSHIP_ID
    assert unit.snapshot_read_only is False


@pytest.mark.asyncio
async def test_recent_observations_require_learning_read_and_cap_at_fifty() -> None:
    service = _service()

    with pytest.raises(PermissionError):
        service.for_principal(_principal(roles=frozenset({Role.OPERATOR})))
    facade = service.for_principal(_principal())
    with pytest.raises(ValueError, match="limit"):
        await facade.list_recent(limit=51)

    assert not _FakeUnitOfWork.instances
    assert not _FakeRepository.calls


def test_recent_projection_uses_admin_gate_without_direct_ledger_reads() -> None:
    statement = LearningObservationsAdminRepository.recent_statement(
        workspace_id=WORKSPACE_ID,
        limit=50,
    )
    sql = str(statement).lower()

    assert "wp06_read_learning_observations_admin_feed" in sql
    assert "workspace_id" in statement.compile().params
    assert "limit" in statement.compile().params
    assert all(
        forbidden not in sql
        for forbidden in (
            "learning_shadow_observation",
            "learning_drift_assessment",
            "learning_retrain_proposal",
            "features",
            "label",
            "source_refs",
            "payload",
            "outcome",
        )
    )
    assert Permission.LEARNING_MODEL_READ.value == "learning:model_read"


def test_admin_item_rejects_feature_or_label_payloads() -> None:
    with pytest.raises(ValueError, match="metadata chứa trường"):
        LearningObservationAdminItem(
            kind="observation",
            record_id=uuid4(),
            occurred_at=NOW,
            metadata={"feature_values": {"rsi": 72.0}},
        )

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.application import learning_model_registry as registry_application
from trading_bot.auth.policy import Permission, Principal, Role, authorize


class _FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def list_events(self, **kwargs: object) -> tuple[()]:
        self.calls.append(("list_events", kwargs))
        return ()

    async def approve(self, **kwargs: object) -> str:
        self.calls.append(("approve", kwargs))
        return "approved"


class _FakeUnitOfWork:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.session = object()

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def _principal(*, role: Role, mfa_verified_at: datetime) -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({role}),
        mfa_verified_at=mfa_verified_at,
    )


@pytest.mark.asyncio
async def test_recent_mfa_reviewer_is_the_only_actor_passed_to_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    principal = _principal(role=Role.ANALYST_REVIEWER, mfa_verified_at=now)
    repository = _FakeRepository()
    monkeypatch.setattr(registry_application, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(
        registry_application,
        "LearningModelRegistryRepository",
        lambda _session: repository,
    )

    result = (
        await registry_application.LearningModelRegistryService(object())
        .for_principal(principal)
        .approve(
            registry_id=uuid4(),
            expected_revision=2,
            idempotency_key=uuid4(),
            reason="Đã rà soát lineage và xác nhận artifact.",
        )
    )

    assert result == "approved"
    action, command = repository.calls[0]
    assert action == "approve"
    assert command["workspace_id"] == principal.workspace_id
    assert command["actor_membership_id"] == principal.membership_id
    assert command["mfa_verified_at"] == principal.mfa_verified_at
    assert command["expected_revision"] == 2


@pytest.mark.asyncio
async def test_model_approval_rejects_stale_mfa_before_repository_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = _principal(
        role=Role.ANALYST_REVIEWER,
        mfa_verified_at=datetime.now(UTC) - timedelta(minutes=16),
    )
    repository = _FakeRepository()
    monkeypatch.setattr(registry_application, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(
        registry_application,
        "LearningModelRegistryRepository",
        lambda _session: repository,
    )

    with pytest.raises(PermissionError, match="MFA"):
        await (
            registry_application.LearningModelRegistryService(object())
            .for_principal(principal)
            .approve(
                registry_id=uuid4(),
                expected_revision=2,
                idempotency_key=uuid4(),
                reason="Đã rà soát lineage và xác nhận artifact.",
            )
        )

    assert repository.calls == []


def test_only_owner_and_analyst_reviewer_can_manage_model_registry() -> None:
    reviewer = _principal(role=Role.ANALYST_REVIEWER, mfa_verified_at=datetime.now(UTC))
    operator = _principal(role=Role.OPERATOR, mfa_verified_at=datetime.now(UTC))

    assert authorize(reviewer, Permission.LEARNING_MODEL_READ)
    assert authorize(reviewer, Permission.LEARNING_MODEL_WRITE)
    assert authorize(reviewer, Permission.LEARNING_MODEL_APPROVE)
    assert not authorize(operator, Permission.LEARNING_MODEL_READ)
    assert not authorize(operator, Permission.LEARNING_MODEL_APPROVE)


@pytest.mark.asyncio
async def test_registry_history_uses_authenticated_workspace_and_read_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = _principal(role=Role.ANALYST_REVIEWER, mfa_verified_at=datetime.now(UTC))
    repository = _FakeRepository()
    monkeypatch.setattr(registry_application, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(
        registry_application, "LearningModelRegistryRepository", lambda _: repository
    )
    registry_id = uuid4()
    service = registry_application.LearningModelRegistryService(object())

    assert await service.for_principal(principal).list_events(registry_id) == ()
    assert repository.calls == [
        ("list_events", {"workspace_id": principal.workspace_id, "registry_id": registry_id})
    ]
    operator = _principal(role=Role.OPERATOR, mfa_verified_at=datetime.now(UTC))
    with pytest.raises(PermissionError):
        await service.for_principal(operator).list_events(registry_id)
    assert len(repository.calls) == 1

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_bot.admin.learning_model_registry_router import router
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity
from trading_bot.domain.learning.model_registry import (
    LearningModelActivePointer,
    LearningModelRegistryEntry,
    LearningModelRegistryState,
)


def _identity(role: Role = Role.OWNER) -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            user_id=uuid4(),
            membership_id=uuid4(),
            workspace_id=uuid4(),
            roles=frozenset({role}),
            mfa_verified_at=datetime.now(UTC),
        ),
        email="owner@example.test",
    )


def _entry() -> LearningModelRegistryEntry:
    return LearningModelRegistryEntry(
        id=uuid4(),
        workspace_id=uuid4(),
        artifact_id=uuid4(),
        artifact_hash="a" * 64,
        manifest_hash="b" * 64,
        fold_plan_hash="c" * 64,
        approval_hash="d" * 64,
        state=LearningModelRegistryState.DRAFT,
        revision=1,
        evaluation_report_id=None,
        evaluation_report_hash=None,
        created_by_membership_id=uuid4(),
        created_at=datetime.now(UTC),
    )


class _Facade:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def list_entries(self):
        self.calls.append(("list_entries", {}))
        return (_entry(),)

    async def get_active_pointer(self):
        self.calls.append(("get_active_pointer", {}))
        return LearningModelActivePointer(uuid4(), None, 0, None, datetime.now(UTC))

    async def approve(self, **kwargs: object):
        self.calls.append(("approve", kwargs))
        return _entry()


class _Service:
    def __init__(self) -> None:
        self.facade = _Facade()
        self.principals: list[Principal] = []

    def for_principal(self, principal: Principal) -> _Facade:
        self.principals.append(principal)
        return self.facade


def _client(*, role: Role = Role.OWNER) -> tuple[TestClient, _Service, AuthIdentity]:
    app = FastAPI()
    app.include_router(router)
    service = _Service()
    actor = _identity(role)
    app.state.learning_model_registry_service = service
    app.dependency_overrides[current_identity] = lambda: actor
    app.dependency_overrides[require_csrf] = lambda: actor
    return TestClient(app), service, actor


def test_registry_reads_are_no_store_and_principal_scoped() -> None:
    http, service, actor = _client()
    entries = http.get("/api/v1/learning-models")
    pointer = http.get("/api/v1/learning-models/active-pointer")

    assert entries.status_code == pointer.status_code == 200
    assert entries.headers["Cache-Control"] == pointer.headers["Cache-Control"] == "no-store"
    assert entries.json()[0]["state"] == "draft"
    assert pointer.json()["version"] == 0
    assert service.principals == [actor.principal, actor.principal]


def test_approval_maps_idempotency_and_never_accepts_actor_injection() -> None:
    http, service, actor = _client()
    registry_id = uuid4()
    key = str(uuid4())
    path = f"/api/v1/learning-models/{registry_id}/approve"
    payload = {"expected_revision": 2, "reason": "Đã rà soát lineage và kiểm tra báo cáo."}

    assert http.post(path, json=payload).status_code == 422
    injected_actor = payload | {"actor_id": str(uuid4())}
    assert http.post(path, headers={"Idempotency-Key": key}, json=injected_actor).status_code == 422
    response = http.post(path, headers={"Idempotency-Key": key}, json=payload)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert service.facade.calls[-1] == (
        "approve",
        {
            "registry_id": registry_id,
            "expected_revision": 2,
            "idempotency_key": type(uuid4())(key),
            "reason": payload["reason"],
        },
    )
    assert service.principals[-1] is actor.principal


def test_non_reviewer_cannot_read_registry() -> None:
    http, service, _actor = _client(role=Role.OPERATOR)
    response = http.get("/api/v1/learning-models")
    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store"
    assert service.facade.calls == []

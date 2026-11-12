"""Kiểm HTTP boundary cho API incident entry hold."""

from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.unit.test_learning_incident_entry_hold import _hold

from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.router import current_identity, get_auth_service
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity

WORKSPACE_ID = UUID(int=1)
ACTOR_ID = UUID(int=2)


def _identity(role: Role = Role.OWNER) -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            user_id=uuid4(),
            membership_id=ACTOR_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({role}),
            mfa_verified_at=datetime.now(UTC),
            mfa_method=MfaMethod.TOTP,
        ),
        email="owner@example.test",
    )


def _router_module():
    name = "trading_bot.admin.learning_incident_entry_hold_router"
    assert importlib.util.find_spec(name) is not None, "Chưa có router incident entry hold"
    return importlib.import_module(name)


class _Facade:
    def __init__(self, event) -> None:
        self.event = event
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def list_history(self, *, limit: int):
        self.calls.append(("history", {"limit": limit}))
        return (self.event,)

    async def hold(self, **kwargs):
        self.calls.append(("hold", kwargs))
        return self.event

    async def release(self, **kwargs):
        self.calls.append(("release", kwargs))
        return self.event


class _Service:
    def __init__(self, event) -> None:
        self.event = event
        self.principals = []
        self.facade = None
        self.hold_behavior = None

    def for_principal(self, principal: Principal):
        self.principals.append(principal)
        self.facade = _Facade(self.event)
        if self.hold_behavior is not None:
            self.facade.hold = self.hold_behavior
        return self.facade


class _CsrfService:
    async def verify_csrf(self, cookie_value: str | None, csrf_token: str | None) -> None:
        if cookie_value != "session-value" or csrf_token != "csrf-value":
            raise AuthenticationFailure(403, "Mã CSRF không hợp lệ")


def _client(*, role: Role = Role.OWNER):
    app = FastAPI()
    app.include_router(_router_module().router)
    event = _hold()
    service = _Service(event)
    identity = _identity(role)
    app.state.learning_incident_entry_hold_service = service
    app.state.settings = SimpleNamespace(app_env="test")
    app.dependency_overrides[current_identity] = lambda: identity
    app.dependency_overrides[get_auth_service] = lambda: _CsrfService()
    return TestClient(app), service, identity, event


def test_history_api_is_no_store_workspace_scoped_and_available_to_owner_and_risk_admin() -> None:
    for role in (Role.OWNER, Role.RISK_ADMIN):
        http, service, identity, event = _client(role=role)
        response = http.get("/api/v1/learning/incident-entry-holds")

        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["events"][0]["proposal_id"] == str(event.proposal_id)
        assert response.json()["events"][0]["action"] == "HOLD"
        assert service.principals == [identity.principal]
        assert service.facade.calls == [("history", {"limit": 200})]


def test_hold_api_requires_csrf_idempotency_and_strict_principal_free_body() -> None:
    http, service, identity, event = _client()
    http.cookies.set("crypidian_session", "session-value")
    path = f"/api/v1/learning/incident-entry-holds/{event.proposal_id}/hold"
    body = {
        "assessment_id": str(event.assessment_id),
        "assessment_hash": event.assessment_hash,
        "expected_revision": 0,
        "expected_deployment_revision": event.deployment_revision,
        "expected_active_pointer_version": event.active_pointer_version,
        "reason": "Xác nhận thiếu coverage",
    }

    assert http.post(path, headers={"Idempotency-Key": str(uuid4())}, json=body).status_code == 403
    headers = {"Idempotency-Key": str(uuid4()), "X-CSRF-Token": "csrf-value"}
    injected = body | {"workspace_id": str(uuid4()), "actor_membership_id": str(uuid4())}
    assert http.post(path, headers=headers, json=injected).status_code == 422
    assert http.post(path, headers={"X-CSRF-Token": "csrf-value"}, json=body).status_code == 422

    response = http.post(path, headers=headers, json=body)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["event_id"] == str(event.event_id)
    assert service.principals == [identity.principal]
    assert service.facade.calls == [
        (
            "hold",
            {
                "proposal_id": event.proposal_id,
                "assessment_id": event.assessment_id,
                "assessment_hash": event.assessment_hash,
                "expected_revision": 0,
                "expected_deployment_revision": event.deployment_revision,
                "expected_active_pointer_version": event.active_pointer_version,
                "idempotency_key": UUID(headers["Idempotency-Key"]),
                "reason": body["reason"],
            },
        )
    ]


def test_release_api_uses_authenticated_identity_and_maps_conflict_no_store() -> None:
    http, service, identity, event = _client(role=Role.RISK_ADMIN)
    http.cookies.set("crypidian_session", "session-value")
    headers = {"Idempotency-Key": str(uuid4()), "X-CSRF-Token": "csrf-value"}
    body = {"expected_revision": 1, "reason": "Đã xử lý incident"}

    response = http.post(
        f"/api/v1/learning/incident-entry-holds/{event.proposal_id}/release",
        headers=headers,
        json=body,
    )

    assert response.status_code == 200
    assert response.json()["proposal_id"] == str(event.proposal_id)
    assert service.principals == [identity.principal]
    assert service.facade.calls[0] == (
        "release",
        {
            "proposal_id": event.proposal_id,
            "expected_revision": 1,
            "idempotency_key": UUID(headers["Idempotency-Key"]),
            "reason": body["reason"],
        },
    )


def test_analyst_and_operator_cannot_read_or_mutate_incident_history() -> None:
    for role in (Role.ANALYST_REVIEWER, Role.OPERATOR, Role.STRATEGY_ADMIN):
        http, service, _identity, event = _client(role=role)
        http.cookies.set("crypidian_session", "session-value")
        read = http.get("/api/v1/learning/incident-entry-holds")
        write = http.post(
            f"/api/v1/learning/incident-entry-holds/{event.proposal_id}/release",
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": "csrf-value"},
            json={"expected_revision": 1, "reason": "Đã xử lý incident"},
        )
        assert read.status_code == write.status_code == 403
        assert service.principals == []


def test_hold_api_maps_conflict_and_permission_errors_without_cache() -> None:
    http, service, _identity, event = _client()
    http.cookies.set("crypidian_session", "session-value")
    service.hold_behavior = _raise_conflict
    headers = {"Idempotency-Key": str(uuid4()), "X-CSRF-Token": "csrf-value"}
    response = http.post(
        f"/api/v1/learning/incident-entry-holds/{event.proposal_id}/hold",
        headers=headers,
        json={
            "assessment_id": str(event.assessment_id),
            "assessment_hash": event.assessment_hash,
            "expected_revision": 0,
            "expected_deployment_revision": event.deployment_revision,
            "expected_active_pointer_version": event.active_pointer_version,
            "reason": "Xác nhận thiếu coverage",
        },
    )
    assert response.status_code == 409
    assert response.headers["Cache-Control"] == "no-store"


async def _raise_conflict(**_kwargs):
    module = importlib.import_module(
        "trading_bot.adapters.database.repositories.learning_incident_entry_holds"
    )
    raise module.LearningIncidentHoldConflict("CAS cũ")

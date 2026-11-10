from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_bot.admin.learning_scoring_deployment_router import router
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.router import current_identity, get_auth_service
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentAdminSnapshot,
    ScorerDeploymentEvent,
    ScorerDeploymentHealth,
    ScorerDeploymentMode,
)


def _identity(role: Role = Role.OWNER) -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            user_id=uuid4(),
            membership_id=uuid4(),
            workspace_id=uuid4(),
            roles=frozenset({role}),
            mfa_verified_at=datetime.now(UTC),
            mfa_method=MfaMethod.TOTP,
        ),
        email="reviewer@example.test",
    )


class _Facade:
    def __init__(
        self,
        principal: Principal,
        *,
        mode: ScorerDeploymentMode = ScorerDeploymentMode.DISABLED,
        revision: int = 0,
        pointer_version: int = 0,
    ) -> None:
        self.principal = principal
        self.mode = mode
        self.revision = revision
        self.pointer_version = pointer_version
        self.calls: list[dict[str, object]] = []

    async def load_admin_scoring_deployment(self) -> ScorerDeploymentAdminSnapshot:
        actor = self.principal
        return ScorerDeploymentAdminSnapshot(
            workspace_id=actor.workspace_id,
            mode=self.mode,
            deployment_event_id=uuid4() if self.revision > 0 else None,
            deployment_revision=self.revision,
            active_pointer_version=self.pointer_version,
            health=(
                ScorerDeploymentHealth.DISABLED
                if self.mode is ScorerDeploymentMode.DISABLED
                else ScorerDeploymentHealth.RIGHTS_RECHECK_REQUIRED
            ),
        )

    async def set_mode(self, **kwargs: object) -> ScorerDeploymentEvent:
        self.calls.append(kwargs)
        actor = self.principal
        now = datetime.now(UTC)
        return ScorerDeploymentEvent(
            event_id=uuid4(),
            workspace_id=actor.workspace_id,
            revision=int(kwargs["expected_deployment_revision"]) + 1,
            mode=kwargs["mode"],  # type: ignore[arg-type]
            target=None,
            actor_membership_id=actor.membership_id,
            mfa_verified_at=actor.mfa_verified_at,
            mfa_method="TOTP",
            expected_pointer_version=int(kwargs["expected_pointer_version"]),
            expected_deployment_revision=int(kwargs["expected_deployment_revision"]),
            reason=str(kwargs["reason"]),
            idempotency_key=kwargs["idempotency_key"],  # type: ignore[arg-type]
            previous_event_id=None,
            previous_event_hash=None,
            created_at=now,
        )


class _Service:
    def __init__(self) -> None:
        self.facade: _Facade | None = None
        self.principals: list[Principal] = []
        self.mode = ScorerDeploymentMode.DISABLED
        self.revision = 0
        self.pointer_version = 0

    def for_principal(self, principal: Principal) -> _Facade:
        self.principals.append(principal)
        self.facade = _Facade(
            principal,
            mode=self.mode,
            revision=self.revision,
            pointer_version=self.pointer_version,
        )
        return self.facade


class _CsrfService:
    async def verify_csrf(self, cookie_value: str | None, csrf_token: str | None) -> None:
        if cookie_value != "session-value" or csrf_token != "csrf-value":
            raise AuthenticationFailure(403, "Mã CSRF không hợp lệ")


def _client(*, role: Role = Role.OWNER) -> tuple[TestClient, _Service, AuthIdentity]:
    app = FastAPI()
    app.include_router(router)
    service = _Service()
    actor = _identity(role)
    app.state.learning_scoring_deployment_service = service
    app.state.settings = SimpleNamespace(app_env="test")
    app.dependency_overrides[current_identity] = lambda: actor
    app.dependency_overrides[get_auth_service] = lambda: _CsrfService()
    return TestClient(app), service, actor


def test_deployment_api_reads_no_store_and_uses_authenticated_workspace() -> None:
    http, service, actor = _client()

    response = http.get("/api/v1/learning-scorer/deployment")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["workspace_id"] == str(actor.principal.workspace_id)
    assert response.json()["runtime_usable"] is False
    assert response.json()["health"] == "DISABLED"
    assert service.principals == [actor.principal]


def test_deployment_post_requires_csrf_idempotency_and_rejects_actor_injection() -> None:
    http, service, actor = _client()
    http.cookies.set("crypidian_session", "session-value")
    path = "/api/v1/learning-scorer/deployment"
    body = {
        "mode": "DISABLED",
        "expected_pointer_version": 0,
        "expected_deployment_revision": 0,
        "reason": "Tắt scorer sau khi rà soát trạng thái.",
    }

    missing_csrf = http.post(path, headers={"Idempotency-Key": str(uuid4())}, json=body)
    assert missing_csrf.status_code == 403

    headers = {"Idempotency-Key": str(uuid4()), "X-CSRF-Token": "csrf-value"}
    injected_actor = body | {"actor_membership_id": str(uuid4())}
    assert http.post(path, headers=headers, json=injected_actor).status_code == 422
    assert http.post(path, headers={"X-CSRF-Token": "csrf-value"}, json=body).status_code == 422

    response = http.post(path, headers=headers, json=body)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["workspace_id"] == str(actor.principal.workspace_id)
    assert service.facade is not None
    assert service.facade.calls == [
        {
            "mode": ScorerDeploymentMode.DISABLED,
            "expected_pointer_version": 0,
            "expected_deployment_revision": 0,
            "idempotency_key": UUID(headers["Idempotency-Key"]),
            "reason": body["reason"],
        }
    ]
    assert service.principals == [actor.principal]


def test_non_reviewer_cannot_read_or_mutate_deployment() -> None:
    http, service, _actor = _client(role=Role.OPERATOR)
    http.cookies.set("crypidian_session", "session-value")

    read = http.get("/api/v1/learning-scorer/deployment")
    write = http.post(
        "/api/v1/learning-scorer/deployment",
        headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": "csrf-value"},
        json={
            "mode": "DISABLED",
            "expected_pointer_version": 0,
            "expected_deployment_revision": 0,
            "reason": "Tắt scorer sau khi rà soát trạng thái.",
        },
    )

    assert read.status_code == write.status_code == 403
    assert service.principals == []


def test_admin_can_read_blocked_mode_cas_and_disable_without_a_runtime_target() -> None:
    http, service, actor = _client()
    http.cookies.set("crypidian_session", "session-value")
    service.mode = ScorerDeploymentMode.SHADOW
    service.revision = 4
    service.pointer_version = 11

    current = http.get("/api/v1/learning-scorer/deployment")
    assert current.status_code == 200
    assert current.json()["deployment_revision"] == 4
    assert current.json()["active_pointer_version"] == 11
    assert current.json()["health"] == "RIGHTS_RECHECK_REQUIRED"
    assert current.json()["runtime_usable"] is False
    assert current.json().get("scorer_artifact_id") is None

    body = {
        "mode": "DISABLED",
        "expected_pointer_version": 11,
        "expected_deployment_revision": 4,
        "reason": "Tắt scorer vì cần rà soát quyền hiện hành.",
    }
    response = http.post(
        "/api/v1/learning-scorer/deployment",
        headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": "csrf-value"},
        json=body,
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "DISABLED"
    assert response.json()["deployment_revision"] == 5
    assert service.facade is not None
    assert service.facade.calls[-1]["expected_deployment_revision"] == 4
    assert service.facade.calls[-1]["expected_pointer_version"] == 11
    assert service.principals == [actor.principal, actor.principal]

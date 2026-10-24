from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings


def identity(role: Role) -> AuthIdentity:
    principal = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({role}),
        mfa_verified_at=datetime.now(UTC),
    )
    return AuthIdentity(principal=principal, email="admin@example.test")


@pytest.mark.parametrize("role", [Role.OPERATOR, Role.ANALYST_REVIEWER])
def test_non_admin_roles_cannot_read_settings(role: Role) -> None:
    app = create_app(AppSettings(app_env="test"))
    app.dependency_overrides[current_identity] = lambda: identity(role)

    response = TestClient(app).get("/api/v1/settings/current")

    assert response.status_code == 403


def test_owner_can_read_current_settings_from_workspace_scope() -> None:
    owner = identity(Role.OWNER)

    class Service:
        async def get_current(self, principal: Principal) -> None:
            assert principal.workspace_id == owner.principal.workspace_id
            return None

    app = create_app(AppSettings(app_env="test"))
    app.state.settings_admin_service = cast(object, Service())
    app.dependency_overrides[current_identity] = lambda: owner

    response = TestClient(app).get("/api/v1/settings/current")

    assert response.status_code == 200
    assert response.json() == {"current": None}


def test_strategy_admin_can_read_settings() -> None:
    actor = identity(Role.STRATEGY_ADMIN)

    class Service:
        async def get_current(self, principal: Principal) -> None:
            assert principal.workspace_id == actor.principal.workspace_id
            return None

    app = create_app(AppSettings(app_env="test"))
    app.state.settings_admin_service = cast(object, Service())
    app.dependency_overrides[current_identity] = lambda: actor

    response = TestClient(app).get("/api/v1/settings/current")

    assert response.status_code == 200


def test_operator_cannot_create_settings_draft() -> None:
    operator = identity(Role.OPERATOR)
    app = create_app(AppSettings(app_env="test"))
    app.dependency_overrides[current_identity] = lambda: operator
    app.dependency_overrides[require_csrf] = lambda: operator

    response = TestClient(app).post("/api/v1/settings/drafts", json={"settings": None})

    assert response.status_code == 403


def test_mutation_requires_csrf_dependency() -> None:
    app = create_app(AppSettings(app_env="test"))
    owner = identity(Role.OWNER)
    app.dependency_overrides[current_identity] = lambda: owner

    class AuthServiceStub:
        async def verify_csrf(self, cookie: str | None, token: str | None) -> None:
            del cookie, token
            raise AuthenticationFailure(403, "Thiếu mã CSRF")

    from trading_bot.auth.router import get_auth_service

    app.dependency_overrides[get_auth_service] = lambda: cast(object, AuthServiceStub())

    response = TestClient(app).post("/api/v1/settings/drafts", json={"settings": None})

    assert response.status_code == 403


def test_settings_validation_error_does_not_echo_secret_input() -> None:
    owner = identity(Role.OWNER)
    app = create_app(AppSettings(app_env="test"))
    app.dependency_overrides[current_identity] = lambda: owner
    app.dependency_overrides[require_csrf] = lambda: owner

    response = TestClient(app).post(
        "/api/v1/settings/drafts",
        json={"settings": {"api_secret": "DO_NOT_ECHO_THIS_VALUE"}},
    )

    assert response.status_code == 422
    assert "DO_NOT_ECHO_THIS_VALUE" not in response.text

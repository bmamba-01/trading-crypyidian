"""SEC-13: thuộc tính cookie phiên ở dev (HTTP) và production (HTTPS) (SECT-12)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from typing import Literal
from uuid import uuid4

from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.cookies import (
    SESSION_COOKIE_BASE_NAME,
    session_cookie_name,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import get_auth_service
from trading_bot.auth.service import AuthIdentity, LoginResult
from trading_bot.config.settings import AppSettings

COOKIE_VALUE = f"{uuid4()}:{'t' * 43}"
PRINCIPAL = Principal(
    user_id=uuid4(),
    membership_id=uuid4(),
    workspace_id=uuid4(),
    roles=frozenset({Role.OWNER}),
    mfa_verified_at=datetime.now(UTC),
)
LOGIN_BODY = {
    "workspace_slug": "paper-workspace",
    "email": "owner@example.test",
    "password": "a long owner password for test",
    "code": "123456",
}


@dataclass
class FakeAuthService:
    seen_cookies: list[str | None] = field(default_factory=list)

    async def login(self, **_: object) -> LoginResult:
        return LoginResult(
            cookie_value=COOKIE_VALUE,
            csrf_token="csrf-token",
            principal=PRINCIPAL,
            email="owner@example.test",
        )

    async def authenticate(self, cookie_value: str | None) -> AuthIdentity:
        self.seen_cookies.append(cookie_value)
        if cookie_value != COOKIE_VALUE:
            from trading_bot.auth.service import AuthenticationFailure

            raise AuthenticationFailure(401, "Cần đăng nhập")
        return AuthIdentity(principal=PRINCIPAL, email="owner@example.test")

    async def verify_csrf(self, cookie_value: str | None, csrf_token: str | None) -> None:
        self.seen_cookies.append(cookie_value)

    async def logout(self, cookie_value: str | None) -> None:
        self.seen_cookies.append(cookie_value)


def _client(app_env: Literal["local", "test", "production"]) -> tuple[TestClient, FakeAuthService]:
    service = FakeAuthService()
    app = create_app(AppSettings(app_env=app_env))
    app.dependency_overrides[get_auth_service] = lambda: service
    return TestClient(app), service


def _session_set_cookie(header_values: list[str], name: str) -> str:
    matches = [value for value in header_values if value.startswith(f"{name}=")]
    assert len(matches) == 1, header_values
    return matches[0]


def _attributes(raw: str) -> dict[str, str]:
    parsed = SimpleCookie()
    parsed.load(raw)
    morsel = next(iter(parsed.values()))
    return {key: str(value) for key, value in morsel.items() if value}


def test_cookie_name_derives_from_secure_setting() -> None:
    assert session_cookie_name(AppSettings(app_env="local")) == SESSION_COOKIE_BASE_NAME
    assert session_cookie_name(AppSettings(app_env="test")) == SESSION_COOKIE_BASE_NAME
    assert (
        session_cookie_name(AppSettings(app_env="production"))
        == f"__Host-{SESSION_COOKIE_BASE_NAME}"
    )


def test_local_login_cookie_is_httponly_strict_root_path_without_secure_prefix() -> None:
    client, _ = _client("local")

    response = client.post("/api/v1/auth/login", json=LOGIN_BODY)

    assert response.status_code == 200
    raw = _session_set_cookie(response.headers.get_list("set-cookie"), "crypidian_session")
    attributes = _attributes(raw)
    assert attributes["path"] == "/"
    assert attributes["httponly"] == "True"
    assert attributes["samesite"].lower() == "strict"
    assert attributes["max-age"] == str(12 * 3600)
    assert "secure" not in attributes
    assert "domain" not in attributes


def test_production_login_cookie_uses_host_prefix_and_secure() -> None:
    client, _ = _client("production")

    response = client.post("/api/v1/auth/login", json=LOGIN_BODY)

    assert response.status_code == 200
    raw = _session_set_cookie(response.headers.get_list("set-cookie"), "__Host-crypidian_session")
    attributes = _attributes(raw)
    assert attributes["secure"] == "True"
    assert attributes["path"] == "/"
    assert attributes["httponly"] == "True"
    assert attributes["samesite"].lower() == "strict"
    assert "domain" not in attributes
    assert not any(
        value.startswith("crypidian_session=") for value in response.headers.get_list("set-cookie")
    )


def test_production_readers_accept_only_the_host_prefixed_cookie() -> None:
    client, service = _client("production")

    accepted = client.get(
        "/api/v1/auth/me", headers={"Cookie": f"__Host-crypidian_session={COOKIE_VALUE}"}
    )
    ignored = client.get("/api/v1/auth/me", headers={"Cookie": f"crypidian_session={COOKIE_VALUE}"})

    assert accepted.status_code == 200
    assert ignored.status_code == 401
    assert service.seen_cookies == [COOKIE_VALUE, None]


def test_logout_clears_cookie_with_same_name_and_attributes() -> None:
    for app_env, name, secure in (
        ("local", "crypidian_session", False),
        ("production", "__Host-crypidian_session", True),
    ):
        client, service = _client(app_env)  # type: ignore[arg-type]

        response = client.post(
            "/api/v1/auth/logout",
            headers={"Cookie": f"{name}={COOKIE_VALUE}", "X-CSRF-Token": "csrf-token"},
        )

        assert response.status_code == 204
        cleared = _cleared_by_path(response.headers.get_list("set-cookie"), name)
        attributes = cleared["/"]
        assert attributes["max-age"] == "0"
        assert attributes["httponly"] == "True"
        assert ("secure" in attributes) is secure
        assert service.seen_cookies[-1] == COOKIE_VALUE


def _cleared_by_path(header_values: list[str], name: str) -> dict[str, dict[str, str]]:
    cleared: dict[str, dict[str, str]] = {}
    for value in header_values:
        if value.startswith(f"{name}="):
            attributes = _attributes(value)
            cleared[attributes["path"]] = attributes
    return cleared


def test_local_logout_also_clears_legacy_api_path_cookie() -> None:
    """Cookie cũ `Path=/api/v1` (trước SEC-13) không được dùng lại sau logout."""
    client, _ = _client("local")

    response = client.post(
        "/api/v1/auth/logout",
        headers={"Cookie": f"crypidian_session={COOKIE_VALUE}", "X-CSRF-Token": "csrf-token"},
    )

    assert response.status_code == 204
    cleared = _cleared_by_path(response.headers.get_list("set-cookie"), "crypidian_session")
    assert set(cleared) == {"/", "/api/v1"}
    for attributes in cleared.values():
        assert attributes["max-age"] == "0"
        assert attributes["httponly"] == "True"
        assert attributes["samesite"].lower() == "strict"

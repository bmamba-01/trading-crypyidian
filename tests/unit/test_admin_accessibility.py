"""Kiểm tra cấu trúc nền trợ năng của Admin Web."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings


def test_admin_layout_exposes_skip_link_semantic_main_and_keyboard_focus_styles() -> None:
    app = create_app(AppSettings(app_env="test"))
    principal = Principal(
        workspace_id=UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510"),
        membership_id=UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc"),
        user_id=UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57"),
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=principal, email="owner@example.test"
    )
    app.state.bot_admin_service = _BotAdminStub()
    app.state.bot_operations_service = _BotOperationsStub()
    app.state.auth_service = _AuthServiceStub()

    client = TestClient(app)
    response = client.get("/admin")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert 'href="#main-content"' in response.text
    assert '<main id="main-content">' in response.text
    assert 'rel="stylesheet" href="/static/admin.css"' in response.text

    stylesheet = client.get("/static/admin.css")
    assert stylesheet.status_code == 200
    assert ":focus-visible" in stylesheet.text
    assert "prefers-reduced-motion" in stylesheet.text


class _BotAdminStub:
    async def list_bots(self, _principal: Principal) -> list[dict[str, object]]:
        return []


class _BotOperationsStub:
    def for_principal(self, _principal: Principal) -> _BotOperationsStub:
        return self

    async def list_bots(self) -> list[dict[str, object]]:
        return []

    async def workspace_status(self) -> dict[str, object]:
        return {"kill_state": "ACTIVE", "revision": 1, "bots": []}


class _AuthServiceStub:
    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return "csrf-token-test-123"

"""API MB-07b Telegram phải bảo vệ CSRF và chỉ trả mã khi service tạo mới."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings

WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
MEMBERSHIP = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")


class AuthStub:
    async def verify_csrf(self, _cookie: str | None, token: str | None) -> None:
        if token != "csrf-good":
            raise AuthenticationFailure(403, "CSRF invalid")


class BindingServiceStub:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def create_link(self, principal: Principal, *, reason: str) -> object:
        self.calls.append(("link", principal.membership_id, reason))
        return SimpleNamespace(
            url="https://t.me/crypidian_test_bot?start=" + "a" * 43,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )

    async def revoke(self, principal: Principal, membership_id: UUID, *, reason: str) -> int:
        self.calls.append(("revoke", principal.membership_id, membership_id, reason))
        return 1


def _client() -> tuple[TestClient, BindingServiceStub]:
    app = create_app(AppSettings(app_env="test"))
    principal = Principal(
        user_id=UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57"),
        membership_id=MEMBERSHIP,
        workspace_id=WORKSPACE,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=principal, email="owner@example.test"
    )
    service = BindingServiceStub()
    app.state.telegram_binding_service = service
    app.state.auth_service = AuthStub()
    return TestClient(app), service


def test_link_api_rejects_missing_csrf_and_does_not_call_service() -> None:
    client, service = _client()

    response = client.post("/api/v1/telegram-binding/link", json={"reason": "Liên kết"})

    assert response.status_code == 403
    assert service.calls == []


def test_link_api_returns_one_time_url_without_cache_headers() -> None:
    client, service = _client()

    response = client.post(
        "/api/v1/telegram-binding/link",
        json={"reason": "Liên kết thiết bị"},
        headers={"X-CSRF-Token": "csrf-good"},
    )

    assert response.status_code == 200
    assert response.json()["url"] == "https://t.me/crypidian_test_bot?start=" + "a" * 43
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert service.calls == [("link", MEMBERSHIP, "Liên kết thiết bị")]


def test_revoke_api_checks_csrf_before_service_call() -> None:
    client, service = _client()

    response = client.post(
        f"/api/v1/telegram-binding/{MEMBERSHIP}/revoke",
        json={"reason": "Thu hồi"},
        headers={"X-CSRF-Token": "csrf-good"},
    )

    assert response.status_code == 200
    assert response.json() == {"revoked": 1}
    assert service.calls == [("revoke", MEMBERSHIP, MEMBERSHIP, "Thu hồi")]


class TopicServiceStub(BindingServiceStub):
    """TG-4: service tạo/bỏ topic; ``error`` mô phỏng Telegram từ chối."""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.error = error

    async def create_topics(self, principal: Principal, membership_id: UUID) -> object:
        self.calls.append(("topics", principal.membership_id, membership_id))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(signal_thread_id=11, report_thread_id=12)

    async def clear_topics(self, principal: Principal, membership_id: UUID) -> bool:
        self.calls.append(("clear", principal.membership_id, membership_id))
        return True


def _topic_client(error: Exception | None = None) -> tuple[TestClient, TopicServiceStub]:
    client, _ = _client()
    service = TopicServiceStub(error)
    client.app.state.telegram_binding_service = service  # type: ignore[attr-defined]
    return client, service


def test_topics_api_requires_csrf_and_hides_thread_ids() -> None:
    client, service = _topic_client()
    target = UUID("b93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")

    assert client.post(f"/api/v1/telegram-binding/{target}/topics").status_code == 403
    assert service.calls == []
    created = client.post(
        f"/api/v1/telegram-binding/{target}/topics", headers={"X-CSRF-Token": "csrf-good"}
    )
    cleared = client.delete(
        f"/api/v1/telegram-binding/{target}/topics", headers={"X-CSRF-Token": "csrf-good"}
    )

    assert created.status_code == 200
    assert created.json() == {"topics": True}
    assert "11" not in created.text
    assert "12" not in created.text
    assert cleared.json() == {"cleared": True}
    assert service.calls == [("topics", MEMBERSHIP, target), ("clear", MEMBERSHIP, target)]


def test_topics_api_maps_telegram_rejection_to_502_without_details() -> None:
    from trading_bot.application.telegram_binding import TelegramTopicSetupError

    client, _ = _topic_client(TelegramTopicSetupError("TELEGRAM_HTTP_400"))
    target = UUID("b93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")

    response = client.post(
        f"/api/v1/telegram-binding/{target}/topics", headers={"X-CSRF-Token": "csrf-good"}
    )

    assert response.status_code == 502
    assert "TELEGRAM_HTTP_400" in response.text

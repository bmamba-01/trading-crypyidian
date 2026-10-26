"""SEC-08: throttle ở route xác thực, gồm reauthenticate (SECT-05) và phản hồi đồng nhất."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import get_auth_service, require_csrf
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings

PRINCIPAL = Principal(
    user_id=uuid4(),
    membership_id=uuid4(),
    workspace_id=uuid4(),
    roles=frozenset({Role.OWNER}),
    mfa_verified_at=datetime.now(UTC),
)
THROTTLED_DETAIL = "Thử lại sau khi hết thời gian giới hạn"


@dataclass
class RejectingAuthService:
    """Luôn trả cùng lỗi 401 như service thật cho mọi sai mật khẩu/TOTP/tài khoản."""

    reauth_calls: int = 0
    login_calls: int = 0

    async def reauthenticate(self, cookie_value: str | None, **_: object) -> None:
        del cookie_value
        self.reauth_calls += 1
        raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")

    async def login(self, **_: object) -> object:
        self.login_calls += 1
        raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")


def _app(service: RejectingAuthService) -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    app.dependency_overrides[get_auth_service] = lambda: service
    app.dependency_overrides[require_csrf] = lambda: AuthIdentity(
        principal=PRINCIPAL, email="owner@example.test"
    )
    return TestClient(app)


def test_reauthenticate_is_throttled_after_repeated_failures() -> None:
    service = RejectingAuthService()
    client = _app(service)
    body = {"password": "wrong password value", "code": "000000"}

    statuses = [client.post("/api/v1/auth/reauthenticate", json=body) for _ in range(6)]

    assert [response.status_code for response in statuses] == [401] * 5 + [429]
    assert statuses[-1].json()["detail"] == THROTTLED_DETAIL
    # Lần bị chặn không chạy Argon2/TOTP.
    assert service.reauth_calls == 5


def test_reauthenticate_failures_count_toward_the_login_limit() -> None:
    service = RejectingAuthService()
    client = _app(service)
    body = {"password": "wrong password value", "code": "000000"}

    for _ in range(5):
        assert client.post("/api/v1/auth/reauthenticate", json=body).status_code == 401
    login = client.post(
        "/api/v1/auth/login",
        json={
            "workspace_slug": "paper-workspace",
            "email": "OWNER@example.test",
            "password": "wrong password value",
            "code": "000000",
        },
    )

    assert login.status_code == 429
    assert service.login_calls == 0


def test_login_throttle_response_is_identical_for_known_and_unknown_accounts() -> None:
    responses = {}
    for email in ("owner@example.test", "nobody@example.test"):
        client = _app(RejectingAuthService())
        payload = {
            "workspace_slug": "paper-workspace",
            "email": email,
            "password": "wrong password value",
            "code": "000000",
        }
        attempts = [client.post("/api/v1/auth/login", json=payload) for _ in range(6)]
        responses[email] = [(item.status_code, item.json()) for item in attempts]

    assert responses["owner@example.test"] == responses["nobody@example.test"]
    assert responses["owner@example.test"][-1] == (429, {"detail": THROTTLED_DETAIL})

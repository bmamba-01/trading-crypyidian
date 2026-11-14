"""Kiểm thử luồng đăng nhập HTML của Admin Web."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.service import LoginResult
from trading_bot.config.settings import AppSettings


def test_login_page_renders_form_and_issues_short_lived_csrf_cookie() -> None:
    client = TestClient(create_app(AppSettings(app_env="test")))

    response = client.get("/admin/login")

    assert response.status_code == 200
    assert 'name="workspace_slug"' in response.text
    assert 'name="email"' in response.text
    assert 'name="password"' in response.text
    assert 'name="code"' in response.text
    assert 'name="csrf_token"' in response.text
    assert response.headers["Cache-Control"] == "no-store"
    csrf_cookie = response.cookies.get("crypidian_login_csrf")
    assert csrf_cookie is not None
    assert len(csrf_cookie) >= 32


def test_login_form_requires_matching_pre_auth_csrf_before_authenticating() -> None:
    app = create_app(AppSettings(app_env="test"))
    service = _AuthStub()
    app.state.auth_service = service
    client = TestClient(app)
    page = client.get("/admin/login")
    csrf_token = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]

    response = client.post(
        "/admin/login",
        data={
            "workspace_slug": "paper-workspace",
            "email": "owner@example.test",
            "password": "a long owner password for test",
            "code": "123456",
            "csrf_token": f"{csrf_token}tampered",
        },
    )

    assert response.status_code == 403
    assert service.calls == 0


def test_valid_login_form_sets_session_cookie_and_redirects_to_admin() -> None:
    app = create_app(AppSettings(app_env="test"))
    service = _AuthStub()
    app.state.auth_service = service
    client = TestClient(app)
    page = client.get("/admin/login")
    csrf_token = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]

    response = client.post(
        "/admin/login",
        data={
            "workspace_slug": "paper-workspace",
            "email": " OWNER@example.test ",
            "password": "a long owner password for test",
            "code": "123456",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["Location"] == "/admin"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.cookies.get("crypidian_session") == "session-cookie-value"
    assert service.calls == 1
    assert service.arguments["email"] == "owner@example.test"


def test_login_form_preserves_auth_service_unavailable_status() -> None:
    client = TestClient(create_app(AppSettings(app_env="test")))
    page = client.get("/admin/login")
    csrf_token = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]

    response = client.post(
        "/admin/login",
        data={
            "workspace_slug": "paper-workspace",
            "email": "owner@example.test",
            "password": "a long owner password for test",
            "code": "123456",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 503


class _AuthStub:
    def __init__(self) -> None:
        self.calls = 0
        self.arguments: dict[str, str] = {}

    async def login(self, **arguments: str) -> LoginResult:
        self.calls += 1
        self.arguments = arguments
        return LoginResult(
            cookie_value="session-cookie-value",
            csrf_token="csrf-token-value",
            principal=Principal(
                workspace_id=UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510"),
                membership_id=UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc"),
                user_id=UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57"),
                roles=frozenset({Role.OWNER}),
                mfa_verified_at=datetime.now(UTC),
            ),
            email="owner@example.test",
        )


def test_login_form_supports_password_managers_and_remembers_only_non_secret_fields() -> None:
    client = TestClient(create_app(AppSettings(app_env="test")))

    page = client.get("/admin/login").text

    # Trình quản lý mật khẩu (Google Password Manager, iCloud Keychain…) nhận diện cặp
    # username/current-password; mã một lần dùng one-time-code để tự điền từ SMS/ứng dụng.
    assert 'autocomplete="username"' in page
    assert 'autocomplete="current-password"' in page
    assert 'autocomplete="one-time-code"' in page
    assert 'autocomplete="off"' not in page
    assert "/static/login.js" in page
    assert 'id="remember-login"' in page
    script = client.get("/static/login.js").text
    # Chỉ lưu mã workspace và email; mật khẩu và mã xác thực không bao giờ vào localStorage.
    assert "localStorage" in script
    assert "password" not in script.split("REMEMBERED_FIELDS", 1)[1].split("]", 1)[0]
    assert "workspace-slug" in script
    assert "email" in script


def test_expired_login_form_is_shown_again_with_a_fresh_token_without_authenticating() -> None:
    app = create_app(AppSettings(app_env="test"))
    service = _AuthStub()
    app.state.auth_service = service
    client = TestClient(app)
    page = client.get("/admin/login")
    old_token = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    # Cookie hết hạn (ví dụ để trang mở quá lâu): trình duyệt không còn gửi cookie.
    client.cookies.clear()

    response = client.post(
        "/admin/login",
        data={
            "workspace_slug": "paper-workspace",
            "email": "owner@example.test",
            "password": "a long owner password for test",
            "code": "123456",
            "csrf_token": old_token,
        },
    )

    assert response.status_code == 403
    assert service.calls == 0
    assert "Biểu mẫu đăng nhập đã hết hạn" in response.text
    assert 'role="alert"' in response.text
    new_token = response.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    assert new_token != old_token
    assert response.cookies.get("crypidian_login_csrf") == new_token

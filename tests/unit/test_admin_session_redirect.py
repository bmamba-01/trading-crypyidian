"""Phiên hết hạn trên trang HTML Admin phải đưa về trang đăng nhập rồi quay lại đúng trang.

API JSON (`/api/v1/...`, `/admin/api/...`, fetch/XHR) vẫn giữ 401 JSON như cũ.
"""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from trading_bot.admin.session_redirect import safe_next_path
from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.service import AuthenticationFailure, LoginResult
from trading_bot.config.settings import AppSettings

BOT_ID = "77117cc4-6c7f-407e-8f23-948d740a46c2"
# Header trình duyệt gửi khi người dùng mở trang (điều hướng tài liệu).
NAVIGATION = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
}


class _ExpiredAuth:
    """Mọi phiên đều đã hết hạn; đăng nhập lại thì thành công."""

    def __init__(self) -> None:
        self.login_calls = 0

    async def authenticate(self, _cookie: str | None) -> object:
        raise AuthenticationFailure(401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn")

    async def verify_csrf(self, _cookie: str | None, _token: str | None) -> None:
        raise AssertionError("Không được kiểm CSRF khi phiên đã hết hạn")

    async def login(self, **_arguments: str) -> LoginResult:
        self.login_calls += 1
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


@pytest.fixture
def client() -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    app.state.auth_service = _ExpiredAuth()
    return TestClient(app)


def _next_of(location: str) -> str | None:
    parts = urlsplit(location)
    assert parts.path == "/admin/login"
    assert not parts.netloc
    values = parse_qs(parts.query).get("next")
    return values[0] if values else None


@pytest.mark.parametrize(
    "value",
    [
        "/admin",
        "/admin/",
        "/admin/bots",
        f"/admin/bots/{BOT_ID}?tab=orders&page=2",
        "/admin/learning/models",
    ],
)
def test_safe_next_path_accepts_internal_admin_paths(value: str) -> None:
    assert safe_next_path(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "https://evil.example/admin",
        "//evil.example/admin",
        "/\\evil.example",
        "\\\\evil.example/admin",
        "/admin\\..\\evil",
        "javascript:alert(1)",
        "/adminevil",
        "/api/v1/auth/me",
        "/admin/login",
        "/admin/login?next=/admin",
        "/admin/../etc",
        "/admin/bots\r\nSet-Cookie: x=1",
        "/admin/\tbots",
        "admin/bots",
        "/admin/" + "a" * 3000,
        "/admin#fragment",
    ],
)
def test_safe_next_path_rejects_external_or_unsafe_targets(value: str | None) -> None:
    assert safe_next_path(value) is None


@pytest.mark.parametrize(
    ("path", "expected_next"),
    [
        ("/admin", "/admin"),
        (f"/admin/bots/{BOT_ID}?tab=orders", f"/admin/bots/{BOT_ID}?tab=orders"),
        ("/admin/flatten", "/admin/flatten"),
        # Router trang không dùng AdminHTMLRoute (trước đây trả JSON 401).
        ("/admin/learning/models", "/admin/learning/models"),
        ("/admin/learning/training", "/admin/learning/training"),
        ("/admin/research-sources", "/admin/research-sources"),
        ("/admin/backtests", "/admin/backtests"),
    ],
)
def test_expired_session_on_html_page_redirects_to_login_with_next(
    client: TestClient, path: str, expected_next: str
) -> None:
    response = client.get(path, headers=NAVIGATION, follow_redirects=False)

    assert response.status_code == 303
    assert _next_of(response.headers["Location"]) == expected_next
    assert response.headers["Cache-Control"] == "no-store"


def test_browser_without_fetch_metadata_still_redirects(client: TestClient) -> None:
    """Trình duyệt cũ không gửi Sec-Fetch-*: dựa vào `Accept: text/html`."""
    response = client.get("/admin/bots", headers={"Accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 303
    assert _next_of(response.headers["Location"]) == "/admin/bots"


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        ("/api/v1/auth/me", NAVIGATION),
        ("/api/v1/bots", {"Accept": "application/json"}),
        ("/admin/api/dashboard/realtime", NAVIGATION),
        (f"/admin/api/bots/{BOT_ID}/realtime", {"Accept": "application/json"}),
        # fetch() mặc định gửi Accept */* và Sec-Fetch-Mode cors/same-origin.
        (f"/admin/dashboard/bots/{BOT_ID}/snapshot", {"Accept": "*/*"}),
        (
            f"/admin/dashboard/bots/{BOT_ID}/snapshot",
            {"Accept": "*/*", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
        ),
        ("/admin/learning/models", {"Accept": "application/json"}),
    ],
)
def test_api_and_fetch_requests_keep_json_401(
    client: TestClient, path: str, headers: dict[str, str]
) -> None:
    response = client.get(path, headers=headers, follow_redirects=False)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert "location" not in response.headers


def test_fetch_asking_html_from_admin_page_is_not_redirected(client: TestClient) -> None:
    """Script fetch trang HTML (Sec-Fetch-Mode: cors) nhận 401 để tự xử lý, không bị 303."""
    response = client.get(
        "/admin/bots",
        headers={"Accept": "text/html", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
        follow_redirects=False,
    )

    assert response.status_code == 401


def test_expired_session_on_html_form_post_redirects_without_running_action(
    client: TestClient,
) -> None:
    response = client.post(
        "/admin/flatten/preview",
        data={"reason": "kiểm thử", "csrf_token": "bất kỳ"},
        headers={**NAVIGATION, "Referer": "http://testserver/admin/flatten?x=1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _next_of(response.headers["Location"]) == "/admin/flatten?x=1"


@pytest.mark.parametrize(
    "referer",
    [None, "https://evil.example/admin/bots", "http://testserver/api/v1/auth/me"],
)
def test_form_post_next_falls_back_to_admin_home_when_referer_unusable(
    client: TestClient, referer: str | None
) -> None:
    headers = dict(NAVIGATION)
    if referer is not None:
        headers["Referer"] = referer
    response = client.post(
        "/admin/flatten/preview",
        data={"reason": "kiểm thử"},
        headers=headers,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _next_of(response.headers["Location"]) == "/admin"


def _csrf_from(html: str) -> str:
    return html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]


def test_login_page_carries_safe_next_into_form(client: TestClient) -> None:
    response = client.get("/admin/login", params={"next": f"/admin/bots/{BOT_ID}?tab=orders"})

    assert response.status_code == 200
    assert (
        f'name="next" value="/admin/bots/{BOT_ID}?tab=orders"' in response.text
        or f'name="next" value="/admin/bots/{BOT_ID}?tab=orders"'.replace("&", "&amp;")
        in response.text
    )


@pytest.mark.parametrize("evil", ["https://evil.example/", "//evil.example", "/api/v1/auth/me"])
def test_login_page_drops_unsafe_next(client: TestClient, evil: str) -> None:
    response = client.get("/admin/login", params={"next": evil})

    assert response.status_code == 200
    assert 'name="next"' not in response.text
    assert "evil.example" not in response.text


@pytest.mark.parametrize(
    ("next_value", "expected"),
    [
        (f"/admin/bots/{BOT_ID}?tab=orders", f"/admin/bots/{BOT_ID}?tab=orders"),
        ("https://evil.example/admin", "/admin"),
        ("//evil.example", "/admin"),
        ("", "/admin"),
    ],
)
def test_login_returns_to_safe_next_after_success(
    client: TestClient, next_value: str, expected: str
) -> None:
    page = client.get("/admin/login")
    response = client.post(
        "/admin/login",
        data={
            "workspace_slug": "paper-workspace",
            "email": "owner@example.test",
            "password": "a long owner password for test",
            "code": "123456",
            "csrf_token": _csrf_from(page.text),
            "next": next_value,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["Location"] == expected


def test_full_round_trip_expired_page_to_login_and_back(client: TestClient) -> None:
    target = f"/admin/bots/{BOT_ID}?tab=orders"
    expired = client.get(target, headers=NAVIGATION, follow_redirects=False)
    login_url = expired.headers["Location"]

    page = client.get(login_url, headers=NAVIGATION)
    assert page.status_code == 200
    response = client.post(
        "/admin/login",
        data={
            "workspace_slug": "paper-workspace",
            "email": "owner@example.test",
            "password": "a long owner password for test",
            "code": "123456",
            "csrf_token": _csrf_from(page.text),
            "next": _next_of(login_url) or "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["Location"] == target


def test_failed_login_keeps_next_in_rerendered_form() -> None:
    class _Rejecting(_ExpiredAuth):
        async def login(self, **_arguments: str) -> LoginResult:
            raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")

    app = create_app(AppSettings(app_env="test"))
    app.state.auth_service = _Rejecting()
    client = TestClient(app)
    page = client.get("/admin/login", params={"next": "/admin/reports"})
    response = client.post(
        "/admin/login",
        data={
            "workspace_slug": "paper-workspace",
            "email": "owner@example.test",
            "password": "wrong password here",
            "code": "123456",
            "csrf_token": _csrf_from(page.text),
            "next": "/admin/reports",
        },
    )

    assert response.status_code == 401
    assert 'name="next" value="/admin/reports"' in response.text

"""Playwright: phiên hết hạn trên Admin Web đưa về đăng nhập rồi quay lại đúng trang.

Server là `create_app` thật (gồm `SecurityHeadersMiddleware`/CSP) với dịch vụ xác thực giả lập
có thể "làm hết hạn" mọi phiên theo ý test; không cần DB. Kiểm:
- mở trang HTML khi chưa/hết phiên → 303 về `/admin/login?next=...` → đăng nhập → về trang cũ;
- script trên trang nhận 401 từ API JSON → `session-guard.js` chuyển về đăng nhập kèm `next`;
- 401 của API xác thực (ví dụ xác thực lại sai mật khẩu) không làm chuyển trang.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from tests.e2e.csp_harness import (
    browser_violations,  # noqa: F401 - fixture nạp qua usefixtures
    watch_browser_violations,
)
from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity, LoginResult
from trading_bot.config.settings import AppSettings

pytestmark = pytest.mark.usefixtures("browser_violations")

SESSION_VALUE = "e2e-session-cookie-value"
CSRF = "e2e-csrf-token"
TARGET = "/admin/strategy-cards?view=compact"
PRINCIPAL = Principal(
    workspace_id=UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510"),
    membership_id=UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc"),
    user_id=UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57"),
    roles=frozenset({Role.OWNER}),
    mfa_verified_at=datetime.now(UTC),
)


class _SwitchableAuth:
    """Phiên hợp lệ khi cookie đúng và `expired` = False; đăng nhập luôn thành công."""

    def __init__(self) -> None:
        self.expired = False

    async def authenticate(self, cookie: str | None) -> AuthIdentity:
        if self.expired or cookie != SESSION_VALUE:
            raise AuthenticationFailure(401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn")
        return AuthIdentity(principal=PRINCIPAL, email="owner@example.test")

    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return CSRF

    async def verify_csrf(self, _cookie: str | None, token: str | None) -> None:
        if token != CSRF:
            raise AuthenticationFailure(403, "Mã CSRF không hợp lệ")

    async def reauthenticate(self, _cookie: str | None, **_arguments: str) -> None:
        raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")

    async def login(self, **_arguments: str) -> LoginResult:
        self.expired = False
        return LoginResult(
            cookie_value=SESSION_VALUE,
            csrf_token=CSRF,
            principal=PRINCIPAL,
            email="owner@example.test",
        )


@pytest.fixture(scope="module")
def server() -> Iterator[tuple[str, _SwitchableAuth]]:
    app = create_app(AppSettings(app_env="test"))
    auth = _SwitchableAuth()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    uv = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    time.sleep(0.6)
    # Lifespan đặt `auth_service` = None khi không có DB: gán stub sau khi server đã khởi động.
    app.state.auth_service = auth
    yield f"http://127.0.0.1:{port}", auth
    uv.should_exit = True
    thread.join(timeout=3)


def _login(page: Page) -> None:
    page.get_by_label("Mã không gian làm việc").fill("paper-workspace")
    page.get_by_label("Địa chỉ thư điện tử").fill("owner@example.test")
    page.get_by_label("Mật khẩu").fill("not-a-real-password")
    page.get_by_label("Mã xác thực 6 chữ số").fill("123456")
    page.get_by_role("button", name="Đăng nhập").click()


def _next_param(url: str) -> str | None:
    values = parse_qs(urlsplit(url).query).get("next")
    return values[0] if values else None


def test_expired_page_redirects_to_login_then_returns_to_same_page(
    server: tuple[str, _SwitchableAuth],
) -> None:
    base, auth = server
    auth.expired = True
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1280, "height": 860}).new_page()
        watch_browser_violations(page)

        page.goto(f"{base}{TARGET}", wait_until="domcontentloaded")
        assert urlsplit(page.url).path == "/admin/login"
        assert _next_param(page.url) == TARGET
        expect(page.locator('input[name="next"]')).to_have_value(TARGET)
        assert "Cần đăng nhập" not in page.content()

        _login(page)
        page.wait_for_url(f"{base}{TARGET}")
        assert urlsplit(page.url).path + "?" + urlsplit(page.url).query == TARGET
        browser.close()


def test_external_next_is_ignored_and_login_lands_on_admin_home(
    server: tuple[str, _SwitchableAuth],
) -> None:
    base, auth = server
    auth.expired = True
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        watch_browser_violations(page)

        page.goto(f"{base}/admin/login?next=https://evil.example/", wait_until="domcontentloaded")
        expect(page.locator('input[name="next"]')).to_have_count(0)
        _login(page)
        page.wait_for_url(lambda url: urlsplit(url).path != "/admin/login")
        assert urlsplit(page.url).netloc == urlsplit(base).netloc
        assert urlsplit(page.url).path == "/admin"
        browser.close()


def test_api_401_from_page_script_sends_user_to_login_with_next(
    server: tuple[str, _SwitchableAuth],
) -> None:
    base, auth = server
    auth.expired = True
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        watch_browser_violations(page)
        page.goto(f"{base}{TARGET}", wait_until="domcontentloaded")
        _login(page)
        page.wait_for_url(f"{base}{TARGET}")

        # 401 của API xác thực (sai mật khẩu khi xác thực lại) không được chuyển trang.
        status = page.evaluate(
            """async () => (await fetch('/api/v1/auth/reauthenticate', {
                method: 'POST', credentials: 'same-origin',
                headers: {'Content-Type': 'application/json', 'X-CSRF-Token': 'e2e-csrf-token'},
                body: JSON.stringify({password: 'sai', code: '000000'}),
            })).status"""
        )
        assert status == 401
        page.wait_for_timeout(300)
        assert urlsplit(page.url).path == "/admin/strategy-cards"

        # Phiên hết hạn giữa chừng: API JSON vẫn trả 401 JSON, script chuyển về đăng nhập.
        auth.expired = True
        api = page.evaluate(
            """async () => {
                const r = await fetch('/admin/api/dashboard/realtime',
                    {headers: {Accept: 'application/json'}});
                return {status: r.status, type: r.headers.get('content-type')};
            }"""
        )
        assert api["status"] == 401
        assert api["type"].startswith("application/json")
        page.wait_for_url(lambda url: urlsplit(url).path == "/admin/login")
        assert _next_param(page.url) == TARGET

        _login(page)
        page.wait_for_url(f"{base}{TARGET}")
        browser.close()

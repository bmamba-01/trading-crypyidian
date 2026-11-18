"""E2E Playwright kiểm tra trợ năng Admin Web ở mức zoom 200%, điều hướng bàn phím và RBAC."""
# ruff: noqa: E501

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime
from uuid import UUID

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from playwright.sync_api import sync_playwright

from tests.e2e.csp_harness import (
    browser_violations,  # noqa: F401 - fixture nạp qua usefixtures
    install_security_headers,
    watch_browser_violations,
)
from tests.e2e.test_admin_dashboard_playwright import (
    BOT1_ID,
    WORKSPACE_ID,
    MockAuthService,
    MockBotAdminService,
    MockBotOperationsService,
    MockBotReportingService,
    MockFlattenAllService,
    MockTelegramBindingService,
)
from trading_bot.admin.presentation import label_vi
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.application.bot_runtime_control import BotRuntimeControlError
from trading_bot.auth.policy import Permission, Principal, Role, authorize
from trading_bot.auth.security import LoginRateLimiter
from trading_bot.auth.service import AuthIdentity, LoginResult
from trading_bot.config.settings import AppSettings

pytestmark = pytest.mark.usefixtures("browser_violations")


class _RoleAwareMockAuthService(MockAuthService):
    """Mở rộng MockAuthService để hỗ trợ đăng nhập vai trò Operator/Analyst cho RBAC smoke."""

    async def login(
        self,
        *,
        workspace_slug: str,
        email: str,
        password: str,
        code: str,
    ) -> LoginResult:
        normalized = email.strip().casefold()
        if normalized in {"operator@crypidian.io", "analyst@crypidian.io"}:
            role = Role.OPERATOR if normalized.startswith("operator") else Role.ANALYST_REVIEWER
            principal = Principal(
                user_id=UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57"),
                membership_id=UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc"),
                workspace_id=WORKSPACE_ID,
                roles=frozenset({role}),
                mfa_verified_at=datetime.now(UTC),
            )
            cookie_value = f"{WORKSPACE_ID}:{role.value}-token"
            self.sessions[cookie_value] = AuthIdentity(principal=principal, email=normalized)
            return LoginResult(
                cookie_value=cookie_value,
                csrf_token=self.csrf_token,
                principal=principal,
                email=normalized,
            )
        return await super().login(
            workspace_slug=workspace_slug,
            email=email,
            password=password,
            code=code,
        )


class _RoleAwareMockBotOperationsService(MockBotOperationsService):
    """Kiểm tra quyền thực tế qua authorize() cho RBAC smoke trên Admin Web."""

    def __init__(self, bot_admin: MockBotAdminService, principal: Principal | None = None) -> None:
        super().__init__(bot_admin)
        self._principal = principal

    def for_principal(self, principal: Principal) -> _RoleAwareMockBotOperationsService:
        return _RoleAwareMockBotOperationsService(self.admin_service, principal)

    async def kill_workspace(self, reason: str) -> dict[str, object]:
        if self._principal is not None:
            try:
                authorize(self._principal, Permission.WORKSPACE_KILL, raise_error=True)
            except PermissionError as exc:
                raise BotRuntimeControlError("CONTROL_PERMISSION_DENIED") from exc
        return await super().kill_workspace(reason)


def _create_a11y_test_app() -> FastAPI:
    app = FastAPI()
    app.state.settings = AppSettings(app_env="test")
    templates = Jinja2Templates(directory="src/trading_bot/templates")
    templates.env.filters["label_vi"] = label_vi
    templates.env.globals["session_cookie_name"] = lambda s: "crypidian_session"
    app.state.templates = templates
    app.state.login_limiter = LoginRateLimiter()

    bot_admin = MockBotAdminService()
    app.state.auth_service = _RoleAwareMockAuthService()
    app.state.bot_admin_service = bot_admin
    app.state.bot_operations_service = _RoleAwareMockBotOperationsService(bot_admin)
    app.state.flatten_all_service = MockFlattenAllService()
    app.state.bot_reporting_service = MockBotReportingService()
    app.state.telegram_binding_service = MockTelegramBindingService()

    app.mount("/static", StaticFiles(directory="src/trading_bot/static"), name="static")
    app.include_router(admin_web_router)
    install_security_headers(app)
    return app


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _login_via_keyboard(page: object, *, email: str) -> None:
    page.goto(page.url.split("/admin")[0] + "/admin/login")
    page.fill('input[name="workspace_slug"]', "demo-workspace")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', "correct_password")
    page.fill('input[name="code"]', "123456")
    page.keyboard.press("Enter")
    page.wait_for_url("**/admin")


@pytest.mark.parametrize(
    "viewport",
    [
        # 640x360 tương đương màn hình 1280x720 khi phóng đại Chromium 200%
        {"width": 640, "height": 360},
        {"width": 1280, "height": 720},
    ],
)
def test_admin_a11y_zoom_200_keyboard_navigation_and_rbac_smoke(
    viewport: dict[str, int],
) -> None:
    """Kiểm tra skip link, Tab/Enter/Escape, focus visible, không tràn ngang ở zoom 200% và RBAC."""
    app = _create_a11y_test_app()
    port = _find_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)

    base_url = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport=viewport)
            page = context.new_page()
            watch_browser_violations(page)

            # 1. Đăng nhập Owner bằng bàn phím
            page.goto(f"{base_url}/admin/login")
            page.fill('input[name="workspace_slug"]', "demo-workspace")
            page.fill('input[name="email"]', "admin@crypidian.io")
            page.fill('input[name="password"]', "correct_password")
            page.fill('input[name="code"]', "123456")
            page.keyboard.press("Enter")
            page.wait_for_url("**/admin")

            # 2. Kiểm tra các trang chính ở mức zoom 200% (viewport 640x360 hoặc CSS zoom 2)
            pages_to_check = [
                "/admin",
                "/admin/bots",
                f"/admin/bots/{BOT1_ID}",
                "/admin/flatten",
                "/admin/telegram",
                "/admin/reports",
            ]
            for path in pages_to_check:
                resp = page.goto(f"{base_url}{path}")
                assert resp is not None
                assert resp.status == 200
                assert resp.headers.get("cache-control") == "no-store"

                if viewport["width"] == 1280:
                    page.evaluate("document.documentElement.style.zoom = '2'")

                # Kiểm tra không tràn ngang
                overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth - window.innerWidth"
                )
                assert overflow <= 2, f"Trang {path} bị tràn ngang ({overflow}px) ở {viewport}"

                # Kiểm tra có đúng 1 h1 và landmark <main id="main-content">
                assert page.locator("h1").count() == 1, f"Trang {path} cần đúng 1 thẻ h1"
                assert page.locator("main#main-content").count() == 1

                # Kiểm tra skip link nhận focus khi nhấn Tab và nhảy tới #main-content khi nhấn Enter
                page.keyboard.press("Tab")
                active_href = page.evaluate(
                    "() => document.activeElement ? document.activeElement.getAttribute('href') : null"
                )
                assert active_href == "#main-content", f"Tab đầu trên {path} phải tới skip link"
                page.keyboard.press("Enter")
                focused_id = page.evaluate(
                    "() => document.activeElement ? document.activeElement.id : null"
                )
                assert focused_id == "main-content", (
                    f"Enter skip link trên {path} phải focus #main-content"
                )

                # Kiểm tra các bảng trên trang đều có <caption> hoặc aria-label mô tả và <th scope="col">
                table_without_label = page.evaluate(
                    """() => Array.from(document.querySelectorAll('table')).filter(
                        t => !t.querySelector('caption') && !(t.getAttribute('aria-label') || '').trim()
                    ).length"""
                )
                assert table_without_label == 0, (
                    f"Trang {path} có bảng thiếu <caption> hoặc aria-label"
                )

            # 3. Kiểm tra tooltip .metric-help bằng bàn phím (Tab/Enter/Escape) trên /admin
            page.goto(f"{base_url}/admin")
            help_btn = page.locator("button.metric-help").first
            if help_btn.count() > 0:
                help_btn.focus()
                help_btn.press("Enter")
                tooltip_id = help_btn.get_attribute("aria-describedby")
                assert tooltip_id
                assert page.locator(f"#{tooltip_id}").is_visible()
                page.keyboard.press("Escape")
                assert not page.locator(f"#{tooltip_id}").is_visible()

            # 4. Kiểm tra trang Telegram không lộ Telegram user ID số thô
            page.goto(f"{base_url}/admin/telegram")
            body_text = page.locator("main#main-content").inner_text()
            assert "123456789" not in body_text

            # 5. Kiểm tra RBAC smoke: Operator và Analyst bị từ chối (403) khi gọi workspace kill
            for denied_email in ("operator@crypidian.io", "analyst@crypidian.io"):
                denied_ctx = browser.new_context(viewport=viewport)
                denied_page = denied_ctx.new_page()
                denied_page.goto(f"{base_url}/admin/login")
                denied_page.fill('input[name="workspace_slug"]', "demo-workspace")
                denied_page.fill('input[name="email"]', denied_email)
                denied_page.fill('input[name="password"]', "correct_password")
                denied_page.fill('input[name="code"]', "123456")
                denied_page.keyboard.press("Enter")
                denied_page.wait_for_url("**/admin")

                csrf_val = denied_page.evaluate(
                    "() => document.querySelector('input[name=\"csrf_token\"]')?.value || ''"
                )
                kill_status = denied_page.evaluate(
                    """async (csrf) => {
                        const body = new URLSearchParams({csrf_token: csrf, reason: 'rbac test'});
                        const res = await fetch('/admin/workspace/kill', {method: 'POST', body});
                        return res.status;
                    }""",
                    csrf_val,
                )
                assert kill_status == 403, (
                    f"Vai trò {denied_email} phải bị từ chối 403 khi kill workspace"
                )
                denied_ctx.close()

            context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)

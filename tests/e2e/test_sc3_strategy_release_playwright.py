"""E2E Playwright cho CRY-34 (SC-3): Quản lý phát hành chiến lược trong workspace.

Kiểm thử trình duyệt Chromium thực (Desktop 1280x800 và Mobile 390x844) với CSP nghiêm ngặt
(`default-src 'self'`, `watch_browser_violations`):
1. Mở `/admin/strategy-cards`, chuyển sang tab `#tab-releases` ("Quản lý Phát hành").
2. Kiểm tra danh sách release từ `strategy_registry()`, kích thước chạm >= 44px, không tràn ngang.
3. Owner có MFA gần đây công bố `h03-v2` -> redirect 303 giữ `#tab-releases`, badge `PUBLISHED`.
4. Chuyển `/admin/bots/{BOT_ID}` -> `#overlay-strategy-release` chỉ có `h03-v2` (không `h01-v1`).
5. Giả lập sửa hash trên form (`content_hash` lệch catalog) -> banner lỗi `CATALOG_HASH_MISMATCH`.
6. Thu hồi `h03-v2` -> badge `RETIRED`, `/admin/bots/{BOT_ID}` không còn tùy chọn `h03-v2`.
7. Trên Mobile 390x844: kiểm tra từ chối MFA hết hạn (`RECENT_MFA_REQUIRED`), sau đó xác thực MFA
   mới và công bố Strategy Card (`sc-*`) với bố cục gọn không tràn ngang.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from playwright.sync_api import Page, sync_playwright

from tests.e2e.csp_harness import (
    browser_violations,  # noqa: F401 - fixture theo dõi CSP qua usefixtures
    install_security_headers,
    watch_browser_violations,
)
from trading_bot.admin.strategy_card_service import StrategyCardAdminService
from trading_bot.admin.strategy_release_service import StrategyReleaseAdminService
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.auth.cookies import session_cookie_name
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.security import LoginRateLimiter
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings

pytestmark = pytest.mark.usefixtures("browser_violations")

SCREENSHOTS_DIR = Path(__file__).resolve().parent / "evidence" / "admin-playwright"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

WORKSPACE_ID = UUID("4d1e2c3b-0a9f-4e8d-9c7b-6a5f4e3d2c1b")
BOT_ID = UUID("6f0e5d4c-3b2a-4190-8f7e-6d5c4b3a2910")
OWNER_COOKIE = f"{WORKSPACE_ID}:sc3-owner-e2e"
OPERATOR_COOKIE = f"{WORKSPACE_ID}:sc3-operator-e2e"
CSRF_TOKEN = "csrf-sc3-release-e2e"


class _E2EAuthService:
    """Dịch vụ xác thực in-memory hỗ trợ Owner (đổi hạn MFA) và Operator chỉ đọc."""

    def __init__(self) -> None:
        self.owner_mfa_at: datetime | None = datetime.now(UTC) - timedelta(minutes=1)

    def _identity_for_cookie(self, cookie_value: str | None) -> AuthIdentity:
        if cookie_value == OWNER_COOKIE:
            principal = Principal(
                user_id=UUID(int=1),
                membership_id=UUID(int=2),
                workspace_id=WORKSPACE_ID,
                roles=frozenset({Role.OWNER}),
                mfa_verified_at=self.owner_mfa_at,
            )
            return AuthIdentity(principal=principal, email="owner@crypidian.io")
        if cookie_value == OPERATOR_COOKIE:
            principal = Principal(
                user_id=UUID(int=3),
                membership_id=UUID(int=4),
                workspace_id=WORKSPACE_ID,
                roles=frozenset({Role.OPERATOR}),
                mfa_verified_at=datetime.now(UTC),
            )
            return AuthIdentity(principal=principal, email="operator@crypidian.io")
        raise AuthenticationFailure(401, "Phiên đăng nhập không hợp lệ.")

    async def authenticate(self, cookie_value: str | None) -> AuthIdentity:
        return self._identity_for_cookie(cookie_value)

    async def issue_csrf_token(self, cookie_value: str | None) -> str:
        self._identity_for_cookie(cookie_value)
        return CSRF_TOKEN

    async def verify_csrf(self, cookie_value: str | None, token: str | None) -> None:
        self._identity_for_cookie(cookie_value)
        if not token or token != CSRF_TOKEN:
            raise AuthenticationFailure(403, "Token CSRF không hợp lệ.")


class _E2EBotAdminStub:
    async def get_bot(self, principal: Principal, *, bot_id: UUID) -> dict[str, Any]:
        assert principal.workspace_id == WORKSPACE_ID
        assert bot_id == BOT_ID
        return {
            "id": str(BOT_ID),
            "name": "T01 SC-3 Bot",
            "status": "ENABLED",
            "symbol": "BTCUSDT",
            "account": {"mode": "PAPER", "status": "ACTIVE"},
            "versions": [
                {
                    "id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                    "version_number": 1,
                    "status": "PUBLISHED",
                    "overlay": {"schema_version": 1},
                }
            ],
        }

    async def list_proposal_backtests(
        self, principal: Principal, *, bot_id: UUID
    ) -> list[dict[str, Any]]:
        del principal, bot_id
        return []


class _E2EBotOpsStub:
    def for_principal(self, principal: Principal) -> _E2EBotOpsStub:
        del principal
        return self

    async def bot_status(self, bot_id: UUID) -> dict[str, Any]:
        del bot_id
        return {
            "readiness": "READY",
            "entry_state": "IDLE",
            "entry_allowed": True,
            "control_revision": 1,
            "needs_manual_enable": False,
        }


def _build_app(
    auth: _E2EAuthService, release_service: StrategyReleaseAdminService
) -> tuple[FastAPI, str]:
    app = FastAPI()
    settings = AppSettings(app_env="local")
    app.state.settings = settings
    app.state.templates = Jinja2Templates(directory="src/trading_bot/templates")
    app.state.login_limiter = LoginRateLimiter()
    app.state.auth_service = auth
    app.state.strategy_card_service = StrategyCardAdminService()
    app.state.strategy_release_service = release_service
    app.state.bot_admin_service = _E2EBotAdminStub()
    app.state.bot_operations_service = _E2EBotOpsStub()
    app.mount("/static", StaticFiles(directory="src/trading_bot/static"), name="static")
    app.include_router(admin_web_router)
    install_security_headers(app)
    return app, session_cookie_name(settings)


@pytest.fixture
def live_server() -> Iterator[tuple[str, str, _E2EAuthService, StrategyReleaseAdminService]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    auth = _E2EAuthService()
    release_service = StrategyReleaseAdminService(workspace_scoped=True)
    app, cookie_name = _build_app(auth, release_service)
    uv_server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv_server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert uv_server.started, "Máy chủ uvicorn kiểm thử chưa khởi động kịp."
    try:
        yield f"http://127.0.0.1:{port}", cookie_name, auth, release_service
    finally:
        uv_server.should_exit = True
        thread.join(timeout=5)


def _open(page: Page, base: str, path: str, cookie_name: str, cookie_val: str) -> None:
    page.context.add_cookies(
        [
            {
                "name": cookie_name,
                "value": cookie_val,
                "domain": "127.0.0.1",
                "path": "/",
                "httpOnly": True,
                "sameSite": "Lax",
            }
        ]
    )
    watch_browser_violations(page)
    page.goto(f"{base}{path}", wait_until="networkidle")


def _no_horizontal_overflow(page: Page) -> bool:
    return bool(
        page.evaluate(
            "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
        )
    )


def _min_touch_target_height(page: Page, selector: str) -> float:
    return float(
        page.evaluate(
            """(sel) => {
                const els = [...document.querySelectorAll(sel)].filter(
                    (el) => el.offsetParent !== null
                );
                if (!els.length) return 0;
                return Math.min(...els.map((el) => el.getBoundingClientRect().height));
            }""",
            selector,
        )
    )


def test_sc3_strategy_release_publish_retire_and_bot_selector_desktop(
    live_server: tuple[str, str, _E2EAuthService, StrategyReleaseAdminService],
) -> None:
    """Owner trên Desktop: liệt kê catalog, công bố, kiểm tra bot selector, lỗi hash, thu hồi."""
    base, cookie_name, _auth, release_service = live_server
    catalog = strategy_registry()
    sc_release = next(r for r in catalog.list_releases() if r.release_id.startswith("sc-"))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
            _open(page, base, "/admin/strategy-cards", cookie_name, OWNER_COOKIE)

            # Chuyển sang tab Quản lý Phát hành
            tab_btn = page.locator("#tab-releases")
            tab_btn.click()
            assert tab_btn.get_attribute("aria-selected") == "true"
            assert page.locator("#panel-releases").is_visible()
            assert _no_horizontal_overflow(page)

            # Kiểm tra catalog hiển thị cả built-in rule và Strategy Card
            assert page.locator('[data-release-id="h03-v2"]').is_visible()
            assert page.locator(f'[data-release-id="{sc_release.release_id}"]').is_visible()
            assert page.locator(".release-card").count() == len(catalog.list_releases())

            # Kiểm tra chuẩn chạm >= 44px cho input và nút submit trong panel phát hành
            assert _min_touch_target_height(page, "#panel-releases .form-input") >= 44
            assert _min_touch_target_height(page, "#panel-releases button[type='submit']") >= 44

            page.screenshot(path=str(SCREENSHOTS_DIR / "sc3-releases-desktop-initial.png"))

            # 1. Owner công bố h03-v2 với lý do tiếng Việt
            h03_form = page.locator('[data-release-form="h03-v2"]')
            h03_form.locator('input[name="reason"]').fill(
                "Đã kiểm chứng H03 đạt yêu cầu phát hành cho workspace."
            )
            h03_form.locator('button[type="submit"]').click()
            page.wait_for_url("**/admin/strategy-cards?release_status=published**")

            assert page.locator("#tab-releases").get_attribute("aria-selected") == "true"
            assert page.locator("[data-release-notice]").is_visible()
            assert "PUBLISHED" in page.locator('[data-release-status-badge="h03-v2"]').inner_text()
            assert _no_horizontal_overflow(page)
            page.screenshot(path=str(SCREENSHOTS_DIR / "sc3-releases-desktop-published.png"))

            # 2. Mở bot detail -> #overlay-strategy-release chỉ chứa h03-v2 (không chứa h01-v1)
            page.goto(f"{base}/admin/bots/{BOT_ID}", wait_until="networkidle")
            assert page.locator('#overlay-strategy-release option[value="h03-v2"]').count() == 1
            assert page.locator('#overlay-strategy-release option[value="h01-v1"]').count() == 0

            # 3. Quay lại Quản lý Phát hành và gửi form có content_hash lệch catalog -> bị từ chối
            page.goto(f"{base}/admin/strategy-cards#tab-releases", wait_until="networkidle")
            assert page.locator("#panel-releases").is_visible()
            page.evaluate(
                """() => {
                    const input = document.querySelector(
                        '[data-release-form="h18-v1"] input[name="content_hash"]'
                    );
                    if (input) input.value = '0'.repeat(64);
                }"""
            )
            h18_form = page.locator('[data-release-form="h18-v1"]')
            h18_form.locator('input[name="reason"]').fill("Thử gửi mã băm bị chỉnh sửa.")
            h18_form.locator('button[type="submit"]').click()
            page.wait_for_load_state("networkidle")

            error_box = page.locator("[data-release-error]")
            assert error_box.is_visible()
            assert "không khớp với danh mục sản phẩm" in error_box.inner_text()
            assert page.locator("#tab-releases").get_attribute("aria-selected") == "true"

            # 4. Owner thu hồi h03-v2 -> cập nhật badge RETIRED và gỡ khỏi bot selector
            retire_form = page.locator('[data-release-form="h03-v2"]')
            retire_form.locator('input[name="reason"]').fill(
                "Thu hồi H03 để chuyển bot sang chiến lược khác."
            )
            retire_form.locator('button[type="submit"]').click()
            page.wait_for_url("**/admin/strategy-cards?release_status=retired**")

            assert "RETIRED" in page.locator('[data-release-status-badge="h03-v2"]').inner_text()

            page.goto(f"{base}/admin/bots/{BOT_ID}", wait_until="networkidle")
            assert page.locator('#overlay-strategy-release option[value="h03-v2"]').count() == 0

            # Audit trail lưu đủ cả publish và retire
            assert [e.action for e in release_service.audit_events] == [
                "strategy_release.published",
                "strategy_release.retired",
            ]
        finally:
            browser.close()


def test_sc3_mobile_mfa_gate_publish_card_and_readonly_operator(
    live_server: tuple[str, str, _E2EAuthService, StrategyReleaseAdminService],
) -> None:
    """Trên Mobile (390x844): kiểm tra chặn MFA hết hạn, công bố Strategy Card và chế độ chỉ đọc."""
    base, cookie_name, auth, _release_service = live_server
    catalog = strategy_registry()
    sc_release = next(r for r in catalog.list_releases() if r.release_id.startswith("sc-"))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            # 1. MFA đã hết hạn (> 15 phút) bị chặn trên giao diện mobile
            auth.owner_mfa_at = datetime.now(UTC) - timedelta(minutes=20)
            mobile = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
            _open(
                mobile,
                base,
                "/admin/strategy-cards#tab-releases",
                cookie_name,
                OWNER_COOKIE,
            )
            assert mobile.locator("#panel-releases").is_visible()
            assert _no_horizontal_overflow(mobile)

            sc_form = mobile.locator(f'[data-release-form="{sc_release.release_id}"]')
            sc_form.scroll_into_view_if_needed()
            sc_form.locator('input[name="reason"]').fill("Thử công bố khi MFA đã quá hạn.")
            sc_form.locator('button[type="submit"]').click()
            mobile.wait_for_load_state("networkidle")

            assert mobile.locator("[data-release-error]").is_visible()
            assert "MFA gần đây" in mobile.locator("[data-release-error]").inner_text()
            assert _no_horizontal_overflow(mobile)

            # 2. Xác thực MFA mới -> công bố thành công Strategy Card trên Mobile
            auth.owner_mfa_at = datetime.now(UTC) - timedelta(minutes=1)
            mobile.goto(f"{base}/admin/strategy-cards#tab-releases", wait_until="networkidle")
            assert _min_touch_target_height(mobile, "#panel-releases .form-input") >= 44
            assert _min_touch_target_height(mobile, "#panel-releases button[type='submit']") >= 44

            sc_form_ok = mobile.locator(f'[data-release-form="{sc_release.release_id}"]')
            sc_form_ok.scroll_into_view_if_needed()
            sc_form_ok.locator('input[name="reason"]').fill(
                "Công bố Strategy Card trên giao diện di động."
            )
            sc_form_ok.locator('button[type="submit"]').click()
            mobile.wait_for_url("**/admin/strategy-cards?release_status=published**")

            assert (
                "PUBLISHED"
                in mobile.locator(
                    f'[data-release-status-badge="{sc_release.release_id}"]'
                ).inner_text()
            )
            assert _no_horizontal_overflow(mobile)
            mobile.screenshot(path=str(SCREENSHOTS_DIR / "sc3-releases-mobile.png"))

            # 3. Operator mở trang -> chỉ đọc, không có form công bố/thu hồi
            op_page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
            _open(
                op_page,
                base,
                "/admin/strategy-cards#tab-releases",
                cookie_name,
                OPERATOR_COOKIE,
            )
            assert op_page.locator("#panel-releases").is_visible()
            assert "Chế độ chỉ đọc" in op_page.locator("#panel-releases").inner_text()
            assert op_page.locator("#panel-releases form").count() == 0
        finally:
            browser.close()

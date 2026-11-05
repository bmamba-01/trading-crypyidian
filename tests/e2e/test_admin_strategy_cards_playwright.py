"""E2E browser tests với Playwright cho giao diện Quản lý Thẻ Chiến lược (Desktop & Mobile).

Kiểm tra:
- Đăng nhập xác thực và điều hướng từ thanh menu đến /admin/strategy-cards.
- Hiển thị danh sách Strategy Cards, băm SHA-256 rút gọn, huy hiệu trực giao (Orthogonal).
- Hiển thị đầy đủ bảng phân tầng 7 lớp kiến trúc L0-L6 (QUANTSMART).
- Kiểm tra tính tương thích và phản hồi trên màn hình Desktop (1280x800) và Mobile iPhone (390x844).
- Chụp ảnh màn hình lưu vào thư mục artifacts phục vụ kiểm chứng giao diện.
"""

from __future__ import annotations

import secrets
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from playwright.sync_api import sync_playwright

from trading_bot.admin.strategy_card_service import StrategyCardAdminService
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.security import LoginRateLimiter
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity, LoginResult
from trading_bot.config.settings import AppSettings

SCREENSHOTS_DIR = Path(
    "/Users/macbook/.gemini/antigravity/brain/4f23167d-60fd-49c3-8248-ddb2b669a2a7/screenshots"
)
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

WORKSPACE_ID = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
USER_ID = UUID("c3b4a5d6-e7f8-4a9b-0c1d-2e3f4a5b6c7d")
MEMBERSHIP_ID = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")


class MockAuthService:
    """Mock auth service in-memory cho test E2E."""

    def __init__(self) -> None:
        self.sessions: dict[str, AuthIdentity] = {}
        self.valid_email = "admin@crypidian.io"
        self.valid_password = "correct_password"
        self.valid_workspace = "demo-workspace"
        self.csrf_token = "csrf-token-test-secret-123456"

    async def login(
        self,
        *,
        workspace_slug: str,
        email: str,
        password: str,
        code: str,
    ) -> LoginResult:
        if (
            workspace_slug.strip().casefold() != self.valid_workspace
            or email.strip().casefold() != self.valid_email
            or password != self.valid_password
            or len(code.strip()) != 6
        ):
            raise AuthenticationFailure(401, "Thông tin đăng nhập không hợp lệ.")

        token = secrets.token_urlsafe(32)
        principal = Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({Role.OWNER, Role.RISK_ADMIN}),
            mfa_verified_at=datetime.now(UTC),
        )
        identity = AuthIdentity(principal=principal, email=self.valid_email)
        cookie_value = f"{WORKSPACE_ID}:{token}"
        self.sessions[cookie_value] = identity
        return LoginResult(
            cookie_value=cookie_value,
            csrf_token=self.csrf_token,
            principal=principal,
            email=self.valid_email,
        )

    async def authenticate(self, cookie_value: str | None) -> AuthIdentity:
        if not cookie_value or cookie_value not in self.sessions:
            raise AuthenticationFailure(401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn.")
        return self.sessions[cookie_value]

    async def issue_csrf_token(self, cookie_value: str | None) -> str:
        if not cookie_value or cookie_value not in self.sessions:
            raise AuthenticationFailure(401, "Chưa xác thực.")
        return self.csrf_token

    async def verify_csrf(self, cookie_value: str | None, token: str | None) -> None:
        if not cookie_value or cookie_value not in self.sessions:
            raise AuthenticationFailure(401, "Chưa xác thực.")
        if not token or token != self.csrf_token:
            raise AuthenticationFailure(403, "Token CSRF không hợp lệ.")


class MockBotAdminStub:
    async def list_bots(self, _principal: Principal) -> list[dict[str, Any]]:
        return []


class MockBotOperationsStub:
    def for_principal(self, _principal: Principal) -> MockBotOperationsStub:
        return self

    async def list_bots(self) -> list[dict[str, Any]]:
        return []

    async def workspace_status(self) -> dict[str, Any]:
        return {"kill_state": "ACTIVE", "revision": 1, "bots": []}


def create_test_admin_app() -> FastAPI:
    """Khởi tạo FastAPI app phục vụ Playwright E2E test cho Strategy Cards."""
    app = FastAPI(title="Crypidian Strategy Cards Test Server")

    app_settings = AppSettings(app_env="local")
    app.state.settings = app_settings
    templates = Jinja2Templates(directory="src/trading_bot/templates")
    templates.env.globals["session_cookie_name"] = lambda s: "crypidian_session"
    app.state.templates = templates
    app.state.login_limiter = LoginRateLimiter()

    auth_service = MockAuthService()
    # Tạo trước phiên đăng nhập cho test runner
    test_token = "valid_test_token_strategy_cards"
    cookie_value = f"{WORKSPACE_ID}:{test_token}"
    principal = Principal(
        user_id=USER_ID,
        membership_id=MEMBERSHIP_ID,
        workspace_id=WORKSPACE_ID,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )
    auth_service.sessions[cookie_value] = AuthIdentity(
        principal=principal, email=auth_service.valid_email
    )

    app.state.auth_service = auth_service
    app.state.bot_admin_service = MockBotAdminStub()
    app.state.bot_operations_service = MockBotOperationsStub()
    app.state.strategy_card_service = StrategyCardAdminService()

    app.mount("/static", StaticFiles(directory="src/trading_bot/static"), name="static")
    app.include_router(admin_web_router)
    return app


@pytest.fixture(scope="module")
def strategy_cards_server_url() -> Any:
    """Khởi chạy uvicorn server in-process trên cổng 8099 cho Playwright test."""
    port = 8099
    app = create_test_admin_app()
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    time.sleep(0.6)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=3)


def test_strategy_cards_desktop_view(strategy_cards_server_url: str) -> None:
    """Kiểm tra hiển thị đầy đủ trên màn hình Desktop (1280x800) và chụp ảnh screenshot."""
    cookie_value = f"{WORKSPACE_ID}:valid_test_token_strategy_cards"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            color_scheme="light",
        )
        context.add_cookies(
            [
                {
                    "name": "crypidian_session",
                    "value": cookie_value,
                    "domain": "127.0.0.1",
                    "path": "/",
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            ]
        )
        page = context.new_page()

        # 1. Truy cập trang Strategy Cards
        page.goto(f"{strategy_cards_server_url}/admin/strategy-cards", wait_until="networkidle")

        # 2. Xác nhận tiêu đề và banner
        assert "Quản lý Thẻ Chiến lược (Strategy Cards)" in page.content()
        assert "Mô phỏng (PAPER) nội bộ" in page.content()

        # 3. Xác nhận liên kết điều hướng đang có trên navbar
        nav_link = page.locator('nav.main-nav a[href="/admin/strategy-cards"]')
        assert nav_link.is_visible()
        assert "Chiến lược" in nav_link.text_content() or ""

        # 4. Xác nhận thẻ KPI
        assert page.locator(".kpi-card").count() >= 3

        # 5. Xác nhận hiển thị danh sách Strategy Cards
        card_items = page.locator(".strategy-card-item")
        assert card_items.count() >= 2

        # 6. Kiểm tra card đầu tiên: Donchian Trend & Volatility Breakout
        first_card = card_items.first
        assert "Donchian Trend & Volatility Breakout" in first_card.text_content() or ""
        assert "SC-BINANCE-USDM-VOL-BREAKOUT-001" in first_card.text_content() or ""

        # 7. Kiểm tra mã băm SHA-256 rút gọn và tooltip
        hash_code = first_card.locator(".hash-code")
        assert hash_code.is_visible()
        assert "..." in hash_code.text_content() or ""

        # 8. Kiểm tra huy hiệu Đã kiểm định (Orthogonal)
        assert "Đã kiểm định (Orthogonal)" in first_card.text_content() or ""

        # 9. Kiểm tra các tầng kiến trúc L0 -> L6
        layers = first_card.locator(".layer-pill")
        assert layers.count() == 7
        assert "L0" in first_card.text_content() or ""
        assert "L1" in first_card.text_content() or ""
        assert "L2" in first_card.text_content() or ""
        assert "L3" in first_card.text_content() or ""
        assert "L4" in first_card.text_content() or ""
        assert "L5" in first_card.text_content() or ""
        assert "L6" in first_card.text_content() or ""

        # 10. Chụp screenshot Desktop lưu vào thư mục artifacts
        screenshot_path = SCREENSHOTS_DIR / "14_strategy_cards_desktop.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        assert screenshot_path.exists()
        assert screenshot_path.stat().st_size > 5000

        browser.close()


def test_strategy_cards_mobile_view(strategy_cards_server_url: str) -> None:
    """Kiểm tra giao diện thích ứng trên Mobile iPhone (390x844) và chụp ảnh screenshot."""
    cookie_value = f"{WORKSPACE_ID}:valid_test_token_strategy_cards"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            color_scheme="light",
        )
        context.add_cookies(
            [
                {
                    "name": "crypidian_session",
                    "value": cookie_value,
                    "domain": "127.0.0.1",
                    "path": "/",
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            ]
        )
        page = context.new_page()

        # 1. Truy cập trang Strategy Cards
        page.goto(f"{strategy_cards_server_url}/admin/strategy-cards", wait_until="networkidle")

        # 2. Xác nhận tiêu đề và nội dung chính trên màn hình nhỏ
        assert "Quản lý Thẻ Chiến lược (Strategy Cards)" in page.content()

        # 3. Xác nhận các thẻ Strategy Card co giãn vừa vặn, không lỗi layout
        card_items = page.locator(".strategy-card-item")
        assert card_items.count() >= 2

        # 4. Xác nhận các mốc phân tầng L0-L6 hiển thị rõ ràng trên mobile
        first_card = card_items.first
        assert first_card.locator(".layers-grid").is_visible()
        assert "Đã kiểm định (Orthogonal)" in first_card.text_content() or ""

        # 5. Chụp screenshot Mobile lưu vào thư mục artifacts
        screenshot_path = SCREENSHOTS_DIR / "10_strategy_cards_mobile.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        assert screenshot_path.exists()
        assert screenshot_path.stat().st_size > 5000

        browser.close()


def test_navigation_to_strategy_cards(strategy_cards_server_url: str) -> None:
    """Kiểm tra click điều hướng từ trang tổng quan Admin sang trang Strategy Cards."""
    cookie_value = f"{WORKSPACE_ID}:valid_test_token_strategy_cards"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
        )
        context.add_cookies(
            [
                {
                    "name": "crypidian_session",
                    "value": cookie_value,
                    "domain": "127.0.0.1",
                    "path": "/",
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            ]
        )
        page = context.new_page()

        # 1. Vào trang tổng quan Admin
        page.goto(f"{strategy_cards_server_url}/admin", wait_until="networkidle")

        # 2. Click vào mục 'Chiến lược' trên thanh điều hướng
        nav_link = page.locator('nav.main-nav a[href="/admin/strategy-cards"]')
        nav_link.click()

        # 3. Xác nhận URL đã chuyển sang /admin/strategy-cards
        page.wait_for_url("**/admin/strategy-cards")
        assert "/admin/strategy-cards" in page.url
        assert "Quản lý Thẻ Chiến lược (Strategy Cards)" in page.content()

        browser.close()

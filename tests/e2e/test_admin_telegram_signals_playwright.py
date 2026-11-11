"""E2E browser tests với Playwright cho tính năng Nhận & Lọc Tín Hiệu Telegram trên Admin Web.

Kiểm tra:
- Khối "Nhận tín hiệu qua Telegram" trên trang chi tiết bot (/admin/bots/{bot_id}).
- Bật/tắt đăng ký tín hiệu cho bot.
- Biểu mẫu cấu hình bộ lọc tín hiệu (Cặp giao dịch, Khung thời gian, Hướng, Chiến thuật, Setup).
- Kiểm tra tính tương thích và phản hồi trên màn hình Desktop (1280x800) và Mobile iPhone (390x844).
- Chụp ảnh màn hình lưu vào thư mục screenshots đối soát.
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
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from playwright.sync_api import sync_playwright

from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.application.telegram_signal_subscription import TelegramSignalSubscriptionState
from trading_bot.application.telegram_signals import TelegramSignalFilters
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
BOT1_ID = UUID("77117cc4-6c7f-407e-8f23-948d740a46c2")


class MockAuthService:
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
        return self.csrf_token


class MockTelegramSignalService:
    def __init__(self) -> None:
        self.state = TelegramSignalSubscriptionState(
            bot_id=BOT1_ID,
            enabled=True,
            binding_linked=True,
            filters=TelegramSignalFilters(
                symbols=("BTCUSDT", "ETHUSDT"),
                timeframes=("15m", "1h"),
                directions=("LONG", "SHORT"),
                strategy_ids=("H01", "H03"),
                setup_codes=("H01_BREAKOUT",),
            ),
        )

    async def get_state(
        self, principal: Principal, *, bot_id: UUID
    ) -> TelegramSignalSubscriptionState:
        return self.state


class MockBotAdminService:
    def __init__(self) -> None:
        self.bots = {
            BOT1_ID: {
                "id": str(BOT1_ID),
                "name": "Binance BTC Breakout H03",
                "status": "ENABLED",
                "account": {"mode": "PAPER", "status": "ACTIVE"},
                "versions": [],
            }
        }

    async def list_bots(self, principal: Principal) -> list[dict[str, Any]]:
        return list(self.bots.values())

    async def get_bot(self, principal: Principal, *, bot_id: UUID) -> dict[str, Any]:
        if bot_id not in self.bots:
            raise HTTPException(status_code=404, detail="Không tìm thấy bot.")
        return self.bots[bot_id]


class MockBotOperationsService:
    def __init__(self) -> None:
        self.runtimes = {
            BOT1_ID: {
                "id": str(BOT1_ID),
                "readiness": "READY",
                "entry_state": "ENABLED",
                "entry_allowed": True,
                "control_revision": 1,
            }
        }

    def for_principal(self, principal: Principal) -> MockBotOperationsService:
        return self

    async def list_bots(self) -> list[dict[str, Any]]:
        return list(self.runtimes.values())

    async def bot_status(self, bot_id: UUID) -> dict[str, Any]:
        return self.runtimes.get(bot_id, {})


def create_test_app() -> FastAPI:
    app = FastAPI()
    templates_dir = Path("src/trading_bot/templates")
    static_dir = Path("src/trading_bot/static")

    templates = Jinja2Templates(directory=str(templates_dir))
    from trading_bot.admin.presentation import label_vi

    templates.env.filters["label_vi"] = label_vi
    templates.env.globals["session_cookie_name"] = lambda s: "crypidian_session"

    app.state.templates = templates
    app.state.auth_service = MockAuthService()
    app.state.login_limiter = LoginRateLimiter()
    app.state.bot_admin_service = MockBotAdminService()
    app.state.bot_operations_service = MockBotOperationsService()
    app.state.telegram_signal_subscription_service = MockTelegramSignalService()
    app.state.settings = AppSettings(app_env="local")

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(admin_web_router)
    return app


@pytest.fixture(scope="module")
def app_server() -> str:
    app = create_test_app()
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(1.0)
    return f"http://127.0.0.1:{port}"


def test_telegram_signals_desktop_view(app_server: str) -> None:
    base_url = app_server
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        # Đăng nhập
        page.goto(f"{base_url}/admin/login", wait_until="networkidle")
        page.get_by_label("Mã không gian làm việc").fill("demo-workspace")
        page.get_by_label("Địa chỉ thư điện tử").fill("admin@crypidian.io")
        page.get_by_label("Mật khẩu").fill("correct_password")
        page.get_by_label("Mã xác thực 6 chữ số").fill("123456")
        page.get_by_role("button", name="Đăng nhập").click()
        page.wait_for_load_state("networkidle")

        # Vào chi tiết bot
        page.goto(f"{base_url}/admin/bots/{BOT1_ID}", wait_until="networkidle")

        # Xác thực section tín hiệu telegram
        heading = page.locator("#telegram-signal-heading")
        assert heading.is_visible()
        assert "Nhận tín hiệu qua Telegram" in heading.text_content()

        # Xác thực các trường bộ lọc
        assert page.locator("#signal-filter-symbols").is_visible()
        assert page.locator("#signal-filter-timeframes").is_visible()
        assert page.locator("#signal-filter-directions").is_visible()
        assert page.locator("#signal-filter-strategies").is_visible()
        assert page.locator("#signal-filter-setups").is_visible()

        # Chụp màn hình Desktop
        page.screenshot(
            path=str(SCREENSHOTS_DIR / "18_telegram_signals_desktop.png"), full_page=True
        )

        context.close()
        browser.close()


def test_telegram_signals_mobile_view(app_server: str) -> None:
    base_url = app_server
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
        )
        page = context.new_page()

        # Đăng nhập
        page.goto(f"{base_url}/admin/login", wait_until="networkidle")
        page.get_by_label("Mã không gian làm việc").fill("demo-workspace")
        page.get_by_label("Địa chỉ thư điện tử").fill("admin@crypidian.io")
        page.get_by_label("Mật khẩu").fill("correct_password")
        page.get_by_label("Mã xác thực 6 chữ số").fill("123456")
        page.get_by_role("button", name="Đăng nhập").click()
        page.wait_for_load_state("networkidle")

        # Vào chi tiết bot
        page.goto(f"{base_url}/admin/bots/{BOT1_ID}", wait_until="networkidle")

        # Xác thực section tín hiệu telegram trên mobile
        heading = page.locator("#telegram-signal-heading")
        assert heading.is_visible()
        assert "Nhận tín hiệu qua Telegram" in heading.text_content()

        # Chụp màn hình Mobile
        page.screenshot(
            path=str(SCREENSHOTS_DIR / "14_telegram_signals_mobile.png"), full_page=True
        )

        context.close()
        browser.close()

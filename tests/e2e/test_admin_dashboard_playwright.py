"""E2E browser tests với Playwright cho toàn bộ tính năng Admin Dashboard (Desktop & Mobile)."""
# ruff: noqa: E501

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from playwright.sync_api import sync_playwright

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
BOT1_ID = UUID("77117cc4-6c7f-407e-8f23-948d740a46c2")
BOT2_ID = UUID("88228dd5-7d8e-518f-9a34-059e851b57d3")


@dataclass
class ConfirmOperationResult:
    operation_id: UUID


@dataclass
class TelegramLinkResult:
    url: str
    expires_at: datetime


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


class MockBotAdminService:
    """Mock bot admin service quản lý config drafts và bots."""

    def __init__(self) -> None:
        self.bots: dict[UUID, dict[str, Any]] = {
            BOT1_ID: {
                "id": str(BOT1_ID),
                "name": "Binance BTC Breakout H03",
                "status": "ENABLED",
                "account": {"mode": "PAPER", "status": "ACTIVE"},
                "versions": [
                    {
                        "id": str(uuid4()),
                        "status": "PUBLISHED",
                        "revision": 1,
                        "overlay": {"schema_version": 1, "strategy": {"timeframe": "1h"}},
                    }
                ],
            },
            BOT2_ID: {
                "id": str(BOT2_ID),
                "name": "Binance ETH Trend H01",
                "status": "PAUSED",
                "account": {"mode": "PAPER", "status": "ACTIVE"},
                "versions": [
                    {
                        "id": str(uuid4()),
                        "status": "PUBLISHED",
                        "revision": 1,
                        "overlay": {"schema_version": 1, "strategy": {"timeframe": "15m"}},
                    }
                ],
            },
        }

    async def list_bots(self, principal: Principal) -> list[dict[str, Any]]:
        return list(self.bots.values())

    async def get_bot(self, principal: Principal, *, bot_id: UUID) -> dict[str, Any]:
        if bot_id not in self.bots:
            raise HTTPException(status_code=404, detail="Không tìm thấy bot.")
        return self.bots[bot_id]

    async def create_draft(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        overlay: Any,
        expected_parent_version_id: UUID | None,
    ) -> dict[str, Any]:
        bot = self.bots[bot_id]
        draft = {
            "id": str(uuid4()),
            "status": "DRAFT",
            "revision": 2,
            "overlay": overlay.model_dump() if hasattr(overlay, "model_dump") else overlay,
        }
        bot["versions"] = [v for v in bot["versions"] if v["status"] != "DRAFT"] + [draft]
        return draft

    async def validate_draft(
        self, principal: Principal, *, bot_id: UUID, draft_id: UUID
    ) -> dict[str, Any]:
        return {"valid": True, "errors": []}

    async def publish(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        draft_id: UUID,
        expected_revision: int,
    ) -> dict[str, Any]:
        bot = self.bots[bot_id]
        draft = next((v for v in bot["versions"] if v.get("id") == str(draft_id)), None)
        if draft:
            draft["status"] = "PUBLISHED"
            draft["revision"] = expected_revision
        return draft or {}


class MockBotOperationsService:
    """Mock bot operations service xử lý runtime controls."""

    def __init__(self, admin_service: MockBotAdminService) -> None:
        self.admin_service = admin_service
        self.kill_state = "ACTIVE"
        self.workspace_revision = 1
        self.runtimes: dict[UUID, dict[str, Any]] = {
            BOT1_ID: {
                "id": str(BOT1_ID),
                "readiness": "READY",
                "entry_state": "ENABLED",
                "entry_allowed": True,
                "control_revision": 1,
            },
            BOT2_ID: {
                "id": str(BOT2_ID),
                "readiness": "READY",
                "entry_state": "PAUSED",
                "entry_allowed": False,
                "control_revision": 2,
            },
        }

    def for_principal(self, principal: Principal) -> MockBotOperationsService:
        return self

    async def list_bots(self) -> list[dict[str, Any]]:
        return list(self.runtimes.values())

    async def bot_status(self, bot_id: UUID) -> dict[str, Any]:
        return self.runtimes.get(bot_id, {})

    async def workspace_status(self) -> dict[str, Any]:
        return {
            "kill_state": self.kill_state,
            "revision": self.workspace_revision,
            "bots": list(self.runtimes.values()),
        }

    async def kill_workspace(self, reason: str) -> None:
        self.kill_state = "KILLED"
        self.workspace_revision += 1
        for rt in self.runtimes.values():
            rt["entry_allowed"] = False

    async def release_workspace(self, reason: str, expected_revision: int) -> None:
        self.kill_state = "ACTIVE"
        self.workspace_revision += 1
        for rt in self.runtimes.values():
            if rt["entry_state"] == "ENABLED":
                rt["entry_allowed"] = True

    async def pause_bot(self, bot_id: UUID, reason: str) -> None:
        rt = self.runtimes[bot_id]
        rt["entry_state"] = "PAUSED"
        rt["entry_allowed"] = False
        rt["control_revision"] += 1
        self.admin_service.bots[bot_id]["status"] = "PAUSED"

    async def resume_bot(self, bot_id: UUID, reason: str, expected_revision: int) -> None:
        rt = self.runtimes[bot_id]
        rt["entry_state"] = "ENABLED"
        rt["entry_allowed"] = self.kill_state == "ACTIVE"
        rt["control_revision"] += 1
        self.admin_service.bots[bot_id]["status"] = "ENABLED"

    async def disable_bot(self, bot_id: UUID, reason: str) -> None:
        rt = self.runtimes[bot_id]
        rt["entry_state"] = "DISABLED"
        rt["entry_allowed"] = False
        rt["control_revision"] += 1
        self.admin_service.bots[bot_id]["status"] = "DISABLED"

    async def enable_bot(self, bot_id: UUID, reason: str) -> None:
        rt = self.runtimes[bot_id]
        rt["entry_state"] = "ENABLED"
        rt["entry_allowed"] = self.kill_state == "ACTIVE"
        rt["control_revision"] += 1
        self.admin_service.bots[bot_id]["status"] = "ENABLED"


class MockFlattenAllService:
    """Mock flatten all service."""

    def __init__(self) -> None:
        self.operations: dict[UUID, dict[str, Any]] = {}

    def for_principal(self, principal: Principal) -> MockFlattenAllService:
        return self

    async def create_preview(self, reason: str) -> dict[str, Any]:
        return {
            "preview_id": str(uuid4()),
            "preview_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "expires_at": datetime.now(UTC) + timedelta(seconds=90),
            "bot_count": 2,
            "position_count": 2,
            "open_order_count": 0,
            "targets": [
                {
                    "bot_id": str(BOT1_ID),
                    "target_type": "POSITION",
                    "symbol": "BTCUSDT",
                    "target_id": "pos-btc-1",
                    "quantity": "0.500",
                    "mark_price": "69,250.00",
                    "status": "READY",
                    "error_code": None,
                },
                {
                    "bot_id": str(BOT2_ID),
                    "target_type": "POSITION",
                    "symbol": "ETHUSDT",
                    "target_id": "pos-eth-2",
                    "quantity": "2.000",
                    "mark_price": "3,420.00",
                    "status": "READY",
                    "error_code": None,
                },
            ],
        }

    async def confirm(
        self,
        *,
        preview_id: Any = None,
        preview_hash: str = "",
        idempotency_key: Any = None,
        reason: str = "",
        **kwargs: Any,
    ) -> ConfirmOperationResult:
        op_id = uuid4()
        op = {
            "id": str(op_id),
            "status": "COMPLETED",
            "reason": reason,
            "created_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "items": [
                {
                    "symbol": "BTCUSDT",
                    "target_type": "POSITION",
                    "quantity": "0.500",
                    "status": "COMPLETED",
                    "error_code": None,
                },
                {
                    "symbol": "ETHUSDT",
                    "target_type": "POSITION",
                    "quantity": "2.000",
                    "status": "COMPLETED",
                    "error_code": None,
                },
            ],
        }
        self.operations[op_id] = op
        return ConfirmOperationResult(operation_id=op_id)

    async def get_operation(self, operation_id: UUID) -> dict[str, Any]:
        if operation_id in self.operations:
            return self.operations[operation_id]
        return {
            "id": str(operation_id),
            "status": "COMPLETED",
            "reason": "Hoàn tất thao tác flatten mô phỏng",
            "items": [
                {
                    "symbol": "BTCUSDT",
                    "target_type": "POSITION",
                    "quantity": "0.500",
                    "status": "COMPLETED",
                    "error_code": None,
                }
            ],
        }


class MockBotReportingService:
    """Mock reporting service."""

    async def report(self, query: Any) -> dict[str, Any]:
        return {
            "workspace_id": str(WORKSPACE_ID),
            "scope": "WORKSPACE",
            "as_of": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "quote_asset": "USDT",
            "totals": {
                "gross_pnl": "3,450.00",
                "net_pnl": "3,120.50",
                "fees": "210.50",
                "funding": "75.00",
                "slippage": "44.00",
                "trade_count": 36,
                "winning_trades": 25,
                "losing_trades": 11,
                "win_rate": "69.4%",
                "max_drawdown": "-3.15%",
                "quote_asset": "USDT",
            },
            "trades": [
                {
                    "closed_at": "2026-11-02 19:45:00 UTC",
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "net_pnl": "+375.00 USDT",
                    "status": "CLOSED",
                },
                {
                    "closed_at": "2026-11-02 18:20:00 UTC",
                    "symbol": "ETHUSDT",
                    "direction": "SHORT",
                    "net_pnl": "+120.00 USDT",
                    "status": "CLOSED",
                },
            ],
        }


class MockTelegramBindingService:
    """Mock Telegram binding service."""

    def __init__(self) -> None:
        self.bindings = [
            {
                "membership_id": str(MEMBERSHIP_ID),
                "created_at": "2026-11-01 10:30:00 UTC",
            }
        ]

    async def list_bindings(self, principal: Principal) -> list[dict[str, Any]]:
        return list(self.bindings)

    async def create_link(self, principal: Principal, reason: str) -> TelegramLinkResult:
        return TelegramLinkResult(
            url="https://t.me/crypidian_test_bot?start=SECRET_DEEP_LINK_TOKEN_DEMO",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )

    async def revoke(self, principal: Principal, membership_id: UUID, reason: str) -> None:
        self.bindings = [b for b in self.bindings if b["membership_id"] != str(membership_id)]


def create_test_admin_app() -> FastAPI:
    """Khởi tạo FastAPI app phục vụ Playwright E2E test."""
    app = FastAPI(title="Crypidian Admin Test Server")

    app_settings = AppSettings(app_env="local")
    app.state.settings = app_settings
    from trading_bot.admin.presentation import label_vi

    templates = Jinja2Templates(directory="src/trading_bot/templates")
    templates.env.filters["label_vi"] = label_vi
    templates.env.globals["session_cookie_name"] = lambda s: "crypidian_session"
    app.state.templates = templates
    app.state.login_limiter = LoginRateLimiter()

    auth_service = MockAuthService()
    bot_admin = MockBotAdminService()
    bot_ops = MockBotOperationsService(bot_admin)
    flatten_service = MockFlattenAllService()
    reporting_service = MockBotReportingService()
    telegram_service = MockTelegramBindingService()

    app.state.auth_service = auth_service
    app.state.bot_admin_service = bot_admin
    app.state.bot_operations_service = bot_ops
    app.state.flatten_all_service = flatten_service
    app.state.bot_reporting_service = reporting_service
    app.state.telegram_binding_service = telegram_service

    app.mount("/static", StaticFiles(directory="src/trading_bot/static"), name="static")
    app.include_router(admin_web_router)
    return app


@pytest.fixture(scope="module")
def admin_server_url() -> Any:
    """Khởi chạy uvicorn server in-process cho Playwright."""
    port = 8097
    app = create_test_admin_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(1.2)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def test_admin_dashboard_desktop_and_mobile_workflows(admin_server_url: str) -> None:
    """Kiểm thử toàn diện E2E tất cả các chức năng trên Desktop và Mobile."""
    base_url = admin_server_url

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        # ----------------------------------------------------
        # 1. KỊCH BẢN DESKTOP (1280x800)
        # ----------------------------------------------------
        context_desktop = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context_desktop.new_page()

        # 1.1 Login và lỗi đăng nhập
        page.goto(f"{base_url}/admin/login", wait_until="networkidle")
        page.screenshot(path=str(SCREENSHOTS_DIR / "01_login_desktop.png"), full_page=True)

        page.get_by_label("Mã workspace").fill("demo-workspace")
        page.get_by_label("Email").fill("admin@crypidian.io")
        page.get_by_label("Mật khẩu").fill("wrong_password")
        page.get_by_label("Mã xác thực 6 chữ số").fill("000000")
        page.get_by_role("button", name="Đăng nhập").click()
        page.wait_for_load_state("networkidle")
        assert "Thông tin đăng nhập không hợp lệ" in page.content()
        page.screenshot(path=str(SCREENSHOTS_DIR / "02_login_error_desktop.png"), full_page=True)

        # 1.2 Đăng nhập thành công
        page.get_by_label("Mã workspace").fill("demo-workspace")
        page.get_by_label("Email").fill("admin@crypidian.io")
        page.get_by_label("Mật khẩu").fill("correct_password")
        page.get_by_label("Mã xác thực 6 chữ số").fill("123456")
        page.get_by_role("button", name="Đăng nhập").click()
        page.wait_for_load_state("networkidle")
        assert "/admin" in page.url

        # 1.3 Dashboard & Kill switch
        page.screenshot(path=str(SCREENSHOTS_DIR / "03_dashboard_desktop.png"), full_page=True)
        assert "Chế độ: Mô phỏng (PAPER) nội bộ" in page.content()
        assert "Tổng số bot" in page.content()

        # Kiểm tra Live status bar, Pulse dot và Toggle button trên Desktop
        assert page.locator(".live-status-bar").is_visible()
        assert page.locator("#live-pulse-dot").is_visible()
        assert "Tự động cập nhật 5s" in page.content()
        toggle_btn = page.locator("#toggle-realtime-btn")
        assert toggle_btn.is_visible()
        assert toggle_btn.get_attribute("aria-pressed") == "true"
        assert toggle_btn.inner_text() == "Tạm dừng"

        # Thao tác Tạm dừng cập nhật
        toggle_btn.click()
        assert toggle_btn.get_attribute("aria-pressed") == "false"
        assert toggle_btn.inner_text() == "Tiếp tục"
        assert "Đã tạm dừng tự động cập nhật" in page.content()

        # Thao tác Tiếp tục cập nhật và kiểm tra đồng bộ thời gian từ API
        toggle_btn.click()
        assert toggle_btn.get_attribute("aria-pressed") == "true"
        assert toggle_btn.inner_text() == "Tạm dừng"
        page.wait_for_function(
            "document.getElementById('live-updated-at').textContent !== '--:--:--'"
        )
        updated_time = page.locator("#live-updated-at").inner_text()
        assert len(updated_time) >= 5

        page.get_by_label("Lý do dừng toàn workspace").fill("Thử nghiệm dừng khẩn cấp")
        page.get_by_role("button", name="Dừng entry workspace").click()
        page.wait_for_load_state("networkidle")
        page.screenshot(
            path=str(SCREENSHOTS_DIR / "04_dashboard_killed_desktop.png"), full_page=True
        )
        assert "KILLED" in page.content()

        page.get_by_label("Lý do gỡ kill").fill("Khôi phục hoạt động")
        page.get_by_role("button", name="Gỡ kill workspace").click()
        page.wait_for_load_state("networkidle")
        assert "ACTIVE" in page.content()

        # 1.4 Danh sách Bot
        page.get_by_role("link", name="Bot", exact=True).click()
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(SCREENSHOTS_DIR / "05_bots_list_desktop.png"), full_page=True)
        assert "Binance BTC Breakout H03" in page.content()

        # 1.5 Chi tiết Bot & Pause bot & Config draft
        page.get_by_role("link", name="Binance BTC Breakout H03").first.click()
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(SCREENSHOTS_DIR / "06_bot_detail_desktop.png"), full_page=True)

        page.get_by_label("Lý do tạm dừng").fill("Tạm dừng bot")
        page.get_by_role("button", name="Tạm dừng bot").click()
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(SCREENSHOTS_DIR / "07_bot_paused_desktop.png"), full_page=True)

        page.get_by_role("button", name="Tạo bản nháp").click()
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="Kiểm định bản nháp").click()
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="Công bố bản nháp").click()
        page.wait_for_load_state("networkidle")
        page.screenshot(
            path=str(SCREENSHOTS_DIR / "08_bot_config_published_desktop.png"), full_page=True
        )

        # 1.6 Flatten Workspace
        page.get_by_role("link", name="Flatten workspace", exact=True).click()
        page.wait_for_load_state("networkidle")
        page.get_by_label("Lý do", exact=True).fill("Xem preview các vị thế")
        page.get_by_role("button", name="Xem preview").click()
        page.wait_for_load_state("networkidle")
        page.screenshot(
            path=str(SCREENSHOTS_DIR / "09_flatten_preview_desktop.png"), full_page=True
        )

        page.get_by_label("Lý do xác nhận").fill("Xác nhận flatten vị thế")
        page.get_by_role("button", name="Xác nhận flatten").click()
        page.wait_for_load_state("networkidle")
        page.screenshot(
            path=str(SCREENSHOTS_DIR / "10_flatten_operation_desktop.png"), full_page=True
        )
        assert "COMPLETED" in page.content()

        # 1.7 Báo cáo
        page.get_by_role("link", name="Báo cáo", exact=True).click()
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(SCREENSHOTS_DIR / "11_reports_desktop.png"), full_page=True)

        # 1.8 Telegram
        page.get_by_role("link", name="Telegram", exact=True).click()
        page.wait_for_load_state("networkidle")
        page.screenshot(
            path=str(SCREENSHOTS_DIR / "12_telegram_bindings_desktop.png"), full_page=True
        )

        page.get_by_label("Lý do", exact=True).fill("Tạo mã liên kết bot Telegram")
        page.get_by_role("button", name="Tạo mã liên kết").click()
        page.wait_for_load_state("networkidle")
        page.screenshot(
            path=str(SCREENSHOTS_DIR / "13_telegram_link_created_desktop.png"), full_page=True
        )
        assert "https://t.me/crypidian_test_bot?start=" in page.content()

        context_desktop.close()

        # ----------------------------------------------------
        # 2. KỊCH BẢN MOBILE (390x844)
        # ----------------------------------------------------
        context_mobile = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
        )
        page_m = context_mobile.new_page()

        page_m.goto(f"{base_url}/admin/login", wait_until="networkidle")
        page_m.screenshot(path=str(SCREENSHOTS_DIR / "01_login_mobile.png"), full_page=True)

        page_m.get_by_label("Mã workspace").fill("demo-workspace")
        page_m.get_by_label("Email").fill("admin@crypidian.io")
        page_m.get_by_label("Mật khẩu").fill("correct_password")
        page_m.get_by_label("Mã xác thực 6 chữ số").fill("123456")
        page_m.get_by_role("button", name="Đăng nhập").click()
        page_m.wait_for_load_state("networkidle")

        page_m.screenshot(path=str(SCREENSHOTS_DIR / "02_dashboard_mobile.png"), full_page=True)
        assert page_m.locator(".live-status-bar").is_visible()
        assert page_m.locator("#toggle-realtime-btn").is_visible()
        assert "Tự động cập nhật 5s" in page_m.content()

        page_m.get_by_label("Lý do dừng toàn workspace").fill("Dừng trên mobile")
        page_m.get_by_role("button", name="Dừng entry workspace").click()
        page_m.wait_for_load_state("networkidle")
        page_m.screenshot(
            path=str(SCREENSHOTS_DIR / "03_dashboard_killed_mobile.png"), full_page=True
        )

        page_m.get_by_label("Lý do gỡ kill").fill("Gỡ trên mobile")
        page_m.get_by_role("button", name="Gỡ kill workspace").click()
        page_m.wait_for_load_state("networkidle")

        page_m.get_by_role("link", name="Bot", exact=True).click()
        page_m.wait_for_load_state("networkidle")
        page_m.screenshot(path=str(SCREENSHOTS_DIR / "04_bots_list_mobile.png"), full_page=True)

        page_m.get_by_role("link", name="Binance BTC Breakout H03").first.click()
        page_m.wait_for_load_state("networkidle")
        page_m.screenshot(path=str(SCREENSHOTS_DIR / "05_bot_detail_mobile.png"), full_page=True)

        page_m.get_by_role("link", name="Flatten workspace", exact=True).click()
        page_m.wait_for_load_state("networkidle")
        page_m.get_by_label("Lý do", exact=True).fill("Preview mobile")
        page_m.get_by_role("button", name="Xem preview").click()
        page_m.wait_for_load_state("networkidle")
        page_m.screenshot(
            path=str(SCREENSHOTS_DIR / "06_flatten_preview_mobile.png"), full_page=True
        )

        page_m.get_by_role("link", name="Báo cáo", exact=True).click()
        page_m.wait_for_load_state("networkidle")
        page_m.screenshot(path=str(SCREENSHOTS_DIR / "07_reports_mobile.png"), full_page=True)

        page_m.get_by_role("link", name="Telegram", exact=True).click()
        page_m.wait_for_load_state("networkidle")
        page_m.screenshot(path=str(SCREENSHOTS_DIR / "08_telegram_mobile.png"), full_page=True)

        page_m.get_by_label("Lý do", exact=True).fill("Tạo link mobile")
        page_m.get_by_role("button", name="Tạo mã liên kết").click()
        page_m.wait_for_load_state("networkidle")
        page_m.screenshot(
            path=str(SCREENSHOTS_DIR / "09_telegram_link_created_mobile.png"), full_page=True
        )

        context_mobile.close()
        browser.close()

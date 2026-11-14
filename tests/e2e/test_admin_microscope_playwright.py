"""E2E browser tests với Playwright cho giao diện Soi kính hiển vi (Microscope Replay).

Kiểm tra:
- Điều hướng từ danh sách Chiến lược sang màn hình Microscope Replay qua nút bấm.
- Hiển thị đầy đủ thông tin chiến lược, băm SHA-256, 3 mẫu nến presets và form thủ công.
- Tương tác chọn Preset Mẫu 2: Xác nhận DOM cập nhật ngắt mạch (Short-Circuit) tại Tầng L4 BLOCK,
  cô lập an toàn L5 và L6 (SKIPPED).
- Tương tác chọn Preset Mẫu 1: Xác nhận DOM phục hồi thông suốt, L5 LONG, L6 Zones đầy đủ.
- Tương tác chọn Preset Mẫu 3: Xác nhận DOM cập nhật L5 SHORT_ENTRY và vùng giá bán hợp lệ.
- Kiểm tra tính đáp ứng hiển thị trên Desktop (1280x800) và Mobile iPhone (390x844).
- Chụp ảnh màn hình lưu vào thư mục artifacts:
  * 15_microscope_desktop.png
  * 11_microscope_mobile.png
"""

from __future__ import annotations

import secrets
import socket
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

from tests.e2e.csp_harness import (
    browser_violations,  # noqa: F401 - fixture nạp qua usefixtures
    install_security_headers,
    watch_browser_violations,
)
from trading_bot.admin.strategy_card_service import StrategyCardAdminService
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.application.strategy_pipeline.microscope import MicroscopeReplayService
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.security import LoginRateLimiter
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity, LoginResult
from trading_bot.config.settings import AppSettings

# Ảnh chụp nằm trong repo để test chạy được trên mọi máy và không ghi ra ngoài dự án.
SCREENSHOTS_DIR = Path(__file__).resolve().parent / "evidence" / "admin-playwright"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

WORKSPACE_ID = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
USER_ID = UUID("c3b4a5d6-e7f8-4a9b-0c1d-2e3f4a5b6c7d")
MEMBERSHIP_ID = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")

pytestmark = pytest.mark.usefixtures("browser_violations")


class MockAuthService:
    """Mock auth service in-memory cho test E2E."""

    def __init__(self) -> None:
        self.sessions: dict[str, AuthIdentity] = {}
        self.valid_email = "admin@crypidian.io"
        self.valid_password = "correct_password"
        self.valid_workspace = "demo-workspace"
        self.csrf_token = "csrf-token-test-secret-microscope-123456"

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


def create_test_microscope_app() -> FastAPI:
    """Khởi tạo FastAPI app phục vụ Playwright E2E test cho Microscope Replay."""
    app = FastAPI(title="Crypidian Microscope Replay Test Server")

    app_settings = AppSettings(app_env="local")
    app.state.settings = app_settings
    templates = Jinja2Templates(directory="src/trading_bot/templates")
    templates.env.globals["session_cookie_name"] = lambda s: "crypidian_session"
    app.state.templates = templates
    app.state.login_limiter = LoginRateLimiter()

    auth_service = MockAuthService()
    test_token = "valid_test_token_microscope"
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
    app.state.microscope_replay_service = MicroscopeReplayService()

    app.mount("/static", StaticFiles(directory="src/trading_bot/static"), name="static")
    app.include_router(admin_web_router)
    install_security_headers(app)
    return app


@pytest.fixture(scope="module")
def microscope_server_url() -> Any:
    """Khởi chạy uvicorn server in-process trên cổng ngẫu nhiên cho Playwright test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app = create_test_microscope_app()
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


def test_microscope_desktop_view(microscope_server_url: str) -> None:
    """Kiểm tra giao diện Desktop (1280x800), tương tác Preset ngắt mạch L4 và chụp ảnh."""
    cookie_value = f"{WORKSPACE_ID}:valid_test_token_microscope"

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
        watch_browser_violations(page)

        # 1. Truy cập trang Microscope Replay của card Donchian Breakout
        target_url = (
            f"{microscope_server_url}/admin/strategy-cards/"
            "SC-BINANCE-USDM-VOL-BREAKOUT-001/microscope"
        )
        page.goto(target_url, wait_until="networkidle")

        # 2. Xác nhận tiêu đề và nội dung chính
        assert "Kính hiển vi Soi vết Lịch sử (Microscope Replay)" in page.content()
        assert "Donchian Trend" in page.content()
        card_name = page.locator("#selected-card-name").inner_text()
        assert card_name == "Donchian Trend & Volatility Breakout"

        # 3. Mặc định khởi tạo nạp Mẫu 1 (Hammer) -> PIPELINE PASSED
        page.wait_for_selector("#status-banner")
        banner_init = page.locator("#status-banner").inner_text()
        assert "QUY TRÌNH THÔNG SUỐT (PIPELINE PASSED)" in banner_init

        # 4. Xác nhận 7 phân tầng L0→L6 có mặt trên DOM
        layer_ids = (
            "card-layer-l0",
            "card-layer-l1",
            "card-layer-l2",
            "card-layer-l3",
            "card-layer-l4",
            "card-layer-l5",
            "card-layer-l6",
        )
        for layer_id in layer_ids:
            assert page.locator(f"#{layer_id}").is_visible()

        # 5. TƯƠNG TÁC: Click chọn Mẫu 2 (Nến biến động thấp -> Ngắt mạch L4 BLOCK)
        preset2_btn = page.locator('button[data-preset-id="preset-low-vol-block"]')
        assert preset2_btn.is_visible()
        preset2_btn.click()

        # Đợi phản hồi API và DOM cập nhật trạng thái Ngắt mạch
        page.wait_for_selector(".banner-short-circuit", timeout=5000)
        banner_text = page.locator("#status-banner").inner_text()
        assert "NGẮT MẠCH TỨC THÌ (SHORT-CIRCUITED)" in banner_text

        # Xác nhận Tầng L4 hiển thị CHẶN ĐỨNG (BLOCK)
        l4_status = page.locator("#l4-status-badge").inner_text()
        assert "CHẶN ĐỨNG (BLOCK)" in l4_status

        # Xác nhận Tầng L5 và L6 chuyển sang BỎ QUA (SKIPPED) do cơ chế Short-Circuit
        l5_status = page.locator("#l5-status-badge").inner_text()
        l6_status = page.locator("#l6-status-badge").inner_text()
        assert "BỎ QUA (SKIPPED)" in l5_status
        assert "BỎ QUA (SKIPPED)" in l6_status

        # 6. TƯƠNG TÁC TIẾP: Click lại 'Mẫu 1 — Nến Hammer đảo chiều tăng'
        preset1_btn = page.locator('button[data-preset-id="preset-hammer-long"]')
        preset1_btn.click()

        page.wait_for_selector(".banner-success", timeout=5000)
        banner_p1 = page.locator("#status-banner").inner_text()
        assert "QUY TRÌNH THÔNG SUỐT (PIPELINE PASSED)" in banner_p1
        assert "CHO PHÉP (PASS)" in page.locator("#l4-status-badge").inner_text()
        assert "KÍCH HOẠT (LONG_ENTRY)" in page.locator("#l5-status-badge").inner_text()
        assert "ĐÃ THIẾT LẬP (ZONES READY)" in page.locator("#l6-status-badge").inner_text()

        # Kiểm tra sự hiện diện của Candidate ID SHA-256
        candidate_code = page.locator("#candidate-id-val").inner_text().strip()
        assert len(candidate_code) == 64

        # 7. Chụp screenshot Desktop lưu vào artifacts
        screenshot_path = SCREENSHOTS_DIR / "15_microscope_desktop.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        assert screenshot_path.exists()
        assert screenshot_path.stat().st_size > 5000

        browser.close()


def test_microscope_mobile_view(microscope_server_url: str) -> None:
    """Kiểm tra hiển thị responsive trên Mobile iPhone (390x844), chọn Mẫu 3 và chụp ảnh."""
    cookie_value = f"{WORKSPACE_ID}:valid_test_token_microscope"

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
        watch_browser_violations(page)

        # 1. Truy cập trang Microscope Replay trên mobile
        target_url = (
            f"{microscope_server_url}/admin/strategy-cards/"
            "SC-BINANCE-USDM-VOL-BREAKOUT-001/microscope"
        )
        page.goto(target_url, wait_until="networkidle")

        # 2. Xác nhận hiển thị rõ ràng trên màn hình dọc
        assert "Kính hiển vi Soi vết Lịch sử (Microscope Replay)" in page.content()

        # 3. TƯƠNG TÁC: Click chọn 'Mẫu 3 — Nến Shooting Star đảo chiều giảm'
        preset3_btn = page.locator('button[data-preset-id="preset-shooting-star-short"]')
        assert preset3_btn.is_visible()
        preset3_btn.click()

        # Đợi DOM cập nhật tín hiệu SHORT
        page.wait_for_selector("#status-banner", timeout=5000)
        banner_p3 = page.locator("#status-banner").inner_text()
        assert "QUY TRÌNH THÔNG SUỐT (PIPELINE PASSED)" in banner_p3
        assert "KÍCH HOẠT (SHORT_ENTRY)" in page.locator("#l5-status-badge").inner_text()
        assert "ĐÃ THIẾT LẬP (ZONES READY)" in page.locator("#l6-status-badge").inner_text()

        # Kiểm tra hình học giá lệnh SHORT: Stop Loss phải lớn hơn Entry Price
        entry_val = float(page.locator("#zone-entry-val").inner_text())
        sl_val = float(page.locator("#zone-sl-val").inner_text())
        tp_val = float(page.locator("#zone-tp-val").inner_text())
        assert sl_val > entry_val
        assert tp_val < entry_val

        # 4. Chụp screenshot Mobile lưu vào artifacts
        screenshot_path = SCREENSHOTS_DIR / "11_microscope_mobile.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        assert screenshot_path.exists()
        assert screenshot_path.stat().st_size > 5000

        browser.close()


def test_navigation_from_cards_to_microscope(microscope_server_url: str) -> None:
    """Kiểm tra click nút 'Soi kính hiển vi' từ danh sách Strategy Cards sang Microscope Replay."""
    cookie_value = f"{WORKSPACE_ID}:valid_test_token_microscope"

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
        watch_browser_violations(page)

        # 1. Truy cập trang danh sách Strategy Cards
        page.goto(f"{microscope_server_url}/admin/strategy-cards", wait_until="networkidle")

        # 2. Tìm nút 'Soi kính hiển vi (Microscope)' trên card đầu tiên
        sel = 'a.btn-microscope[href*="SC-BINANCE-USDM-VOL-BREAKOUT-001/microscope"]'
        microscope_btn = page.locator(sel)
        assert microscope_btn.is_visible()
        microscope_btn.click()

        # 3. Đợi chuyển trang thành công
        page.wait_for_url("**/microscope")
        assert "/admin/strategy-cards/SC-BINANCE-USDM-VOL-BREAKOUT-001/microscope" in page.url
        assert "Kính hiển vi Soi vết Lịch sử (Microscope Replay)" in page.content()
        assert "Donchian Trend" in page.content()

        browser.close()

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

import math
import secrets
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from trading_bot.admin.strategy_card_service import CardBarsProvider, StrategyCardAdminService
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.application.strategy_pipeline.consensus import PriceBar
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


async def _rich_consensus_bars_provider(
    _principal: Principal, _symbol: str, timeframe: str
) -> list[PriceBar]:
    """Sinh 300 PriceBar mở rộng đa chế độ (đủ volume/open_time/taker) cho kiểm chứng E2E."""
    step = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}.get(timeframe, 15)
    dt = timedelta(minutes=step)
    start = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    bars: list[PriceBar] = []
    close = 100.0
    for idx in range(300):
        if idx < 60:
            drift = math.sin(idx / 5.0) * 1.1 + math.cos(idx / 2.5) * 0.6
            spread = 0.8 + abs(math.sin(idx)) * 0.6
            vol = 100.0 + 30.0 * abs(math.sin(idx * 0.7))
            tbuy_ratio = 0.45 + 0.15 * math.sin(idx * 0.8)
        elif idx < 95:
            drift = 0.05 * math.sin(idx * 1.3)
            spread = 0.12
            vol = 80.0
            tbuy_ratio = 0.50 + 0.05 * math.sin(idx)
        elif idx == 95:
            drift = 5.5
            spread = 0.6
            vol = 420.0
            tbuy_ratio = 0.88
        elif idx < 150:
            if idx % 14 == 0:
                drift = -1.6
            elif idx % 14 == 1:
                drift = 1.9
            else:
                drift = 0.55 + 0.25 * math.sin(idx / 3.0)
            spread = 0.6 + 0.4 * abs(math.cos(idx))
            vol = 130.0 + (180.0 if idx in (118, 132) else 40.0 * abs(math.sin(idx)))
            tbuy_ratio = 0.82 if idx in (95, 118, 132) else (0.55 + 0.15 * math.sin(idx * 0.6))
        elif idx < 230:
            drift = -0.45 + 0.8 * math.sin(idx / 4.0)
            spread = 0.7 + 0.5 * abs(math.sin(idx * 1.1))
            vol = 140.0 + (220.0 if idx == 190 else 50.0 * abs(math.cos(idx)))
            tbuy_ratio = 0.15 if idx == 190 else (0.42 + 0.15 * math.cos(idx * 0.7))
        else:
            drift = 0.4 + 1.4 * math.sin(idx / 4.5)
            spread = 0.9 + 0.6 * abs(math.sin(idx * 0.9))
            vol = 150.0 + 90.0 * abs(math.sin(idx * 0.6))
            tbuy_ratio = 0.52 + 0.28 * math.sin(idx * 0.85)

        open_ = close
        close = max(10.0, open_ + drift)
        if idx == 95:
            high = close + 0.2
            low = open_ - 0.15
        elif 96 <= idx < 150 and idx % 14 == 1:
            high = close + 0.3
            low = min(open_, close) - 2.2
        else:
            high = max(open_, close) + spread * (0.4 + 0.5 * abs(math.sin(idx * 0.9)))
            low = min(open_, close) - spread * (0.4 + 0.5 * abs(math.cos(idx * 1.1)))

        bars.append(
            PriceBar(
                open=Decimal(f"{open_:.4f}"),
                high=Decimal(f"{high:.4f}"),
                low=Decimal(f"{low:.4f}"),
                close=Decimal(f"{close:.4f}"),
                volume=Decimal(f"{vol:.4f}"),
                open_time=start + idx * dt,
                taker_buy_base_volume=Decimal(f"{vol * tbuy_ratio:.4f}"),
            )
        )
    return bars


def create_test_microscope_app(bars_provider: CardBarsProvider | None = None) -> FastAPI:
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
    app.state.strategy_card_service = StrategyCardAdminService(bars_provider=bars_provider)
    app.state.microscope_replay_service = MicroscopeReplayService()

    app.mount("/static", StaticFiles(directory="src/trading_bot/static"), name="static")
    app.include_router(admin_web_router)
    install_security_headers(app)
    return app


def _start_server(
    bars_provider: CardBarsProvider | None = None,
) -> tuple[uvicorn.Server, threading.Thread, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app = create_test_microscope_app(bars_provider=bars_provider)
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
    return server, thread, f"http://127.0.0.1:{port}"


@pytest.fixture(scope="module")
def microscope_server_url() -> Any:
    """Khởi chạy uvicorn server in-process trên cổng ngẫu nhiên cho Playwright test."""
    server, thread, url = _start_server()
    yield url
    server.should_exit = True
    thread.join(timeout=3)


@pytest.fixture(scope="module")
def consensus_microscope_server_url() -> Any:
    """Server có nguồn nến mở rộng 300 nến để kiểm chứng đủ 8 badge độc lập + Microscope."""
    server, thread, url = _start_server(bars_provider=_rich_consensus_bars_provider)
    yield url
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

        # Mã ứng viên SHA-256: hiển thị rút gọn, giá trị đầy đủ nằm ở data-value và nút sao chép
        candidate = page.locator("#candidate-id-val")
        candidate_full = candidate.get_attribute("data-value") or ""
        assert len(candidate_full) == 64
        assert candidate.inner_text().startswith(candidate_full[:8])
        copy_btn = page.locator(f'button.copy-btn[data-copy="{candidate_full}"]')
        assert copy_btn.count() == 1

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

        # Kiểm tra hình học giá lệnh SHORT: Stop Loss phải lớn hơn Entry Price.
        # Chữ hiển thị theo định dạng Việt Nam 2 chữ số; giá trị gốc nằm ở data-value.
        entry_val = float(page.locator("#zone-entry-val").get_attribute("data-value") or "nan")
        sl_val = float(page.locator("#zone-sl-val").get_attribute("data-value") or "nan")
        tp_val = float(page.locator("#zone-tp-val").get_attribute("data-value") or "nan")
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


def test_expanded_cards_orthogonality_badges_and_independence_counts(
    consensus_microscope_server_url: str,
) -> None:
    """Kiểm chứng đủ 8 Strategy Cards, badge Orthogonal và số điều kiện độc lập x/y trên UI."""
    cookie_value = f"{WORKSPACE_ID}:valid_test_token_microscope"
    expected_counts = (
        ("SC-BINANCE-USDM-VOL-BREAKOUT-001", "2/2 điều kiện độc lập"),
        ("SC-BINANCE-USDM-MEAN-REVERT-002", "5/5 điều kiện độc lập"),
        ("SC-BINANCE-USDM-TSMOM-003", "4/4 điều kiện độc lập"),
        ("SC-BINANCE-USDM-BBKC-SQUEEZE-004", "4/4 điều kiện độc lập"),
        ("SC-BINANCE-USDM-TREND-PULLBACK-005", "6/6 điều kiện độc lập"),
        ("SC-BINANCE-USDM-VWAP-REVERT-006", "5/5 điều kiện độc lập"),
        ("SC-BINANCE-USDM-MACD-ADX-007", "5/5 điều kiện độc lập"),
        ("SC-BINANCE-USDM-TAKER-FLOW-008", "5/5 điều kiện độc lập"),
    )

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

        page.goto(
            f"{consensus_microscope_server_url}/admin/strategy-cards",
            wait_until="networkidle",
        )

        # Đủ 8 thẻ và cả 8 thẻ đều đạt huy hiệu Đã kiểm định (Orthogonal), không cảnh báo giả.
        card_items = page.locator(".strategy-card-item")
        assert card_items.count() == 8
        ortho_sel = '[aria-label="Trạng thái kiểm định: Đã kiểm định (Orthogonal)"]'
        ortho_badges = page.locator(ortho_sel)
        assert ortho_badges.count() == 8
        grid_text = page.locator(".strategy-cards-grid").text_content() or ""
        assert "Cảnh báo tương quan cao" not in grid_text
        assert "Chưa đủ dữ liệu tương quan" not in grid_text
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 0

        # Mở dialog chi tiết của từng thẻ để xác nhận số điều kiện độc lập x/y khớp chuẩn xác.
        for idx, (card_id, expected_label) in enumerate(expected_counts, start=1):
            assert card_id in grid_text
            page.locator(f'button[data-dialog-open="card-dialog-{idx}"]').click()
            dialog = page.locator(f"#card-dialog-{idx}")
            dialog.wait_for(state="visible", timeout=3000)
            dialog_text = dialog.text_content() or ""
            assert card_id in dialog_text
            assert expected_label in dialog_text
            assert "Không có cặp điều kiện tương quan cao" in dialog_text
            dialog.locator('button[aria-label="Đóng chi tiết thẻ"]').click()
            dialog.wait_for(state="hidden", timeout=3000)

        shot_path = SCREENSHOTS_DIR / "17_expanded_cards_consensus_badges_desktop.png"
        page.screenshot(path=str(shot_path), full_page=True)
        assert shot_path.exists()
        assert shot_path.stat().st_size > 5000

        browser.close()


def test_expanded_cards_microscope_inspection_and_short_circuit(
    consensus_microscope_server_url: str,
) -> None:
    """Kiểm chứng Microscope Replay trên các thẻ mở rộng mới (Desktop & Mobile)."""
    cookie_value = f"{WORKSPACE_ID}:valid_test_token_microscope"
    expanded_card_ids = (
        "SC-BINANCE-USDM-TSMOM-003",
        "SC-BINANCE-USDM-BBKC-SQUEEZE-004",
        "SC-BINANCE-USDM-TREND-PULLBACK-005",
        "SC-BINANCE-USDM-VWAP-REVERT-006",
        "SC-BINANCE-USDM-MACD-ADX-007",
        "SC-BINANCE-USDM-TAKER-FLOW-008",
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # 1. Desktop (1280x800): điều hướng từ /admin/strategy-cards vào Microscope thẻ VWAP 006
        desktop_ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            color_scheme="light",
        )
        desktop_ctx.add_cookies(
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
        page = desktop_ctx.new_page()
        watch_browser_violations(page)

        page.goto(
            f"{consensus_microscope_server_url}/admin/strategy-cards",
            wait_until="networkidle",
        )
        vwap_btn = page.locator(
            'a.btn-microscope[href*="SC-BINANCE-USDM-VWAP-REVERT-006/microscope"]'
        )
        assert vwap_btn.is_visible()
        vwap_btn.click()
        page.wait_for_url("**/SC-BINANCE-USDM-VWAP-REVERT-006/microscope")
        assert page.locator("#selected-card-id").inner_text() == "SC-BINANCE-USDM-VWAP-REVERT-006"

        for layer in ("l0", "l1", "l2", "l3", "l4", "l5", "l6"):
            assert page.locator(f"#card-layer-{layer}").is_visible()

        page.locator('button[data-preset-id="preset-low-vol-block"]').click()
        page.wait_for_selector(".banner-short-circuit", timeout=5000)
        assert "CHẶN ĐỨNG (BLOCK)" in page.locator("#l4-status-badge").inner_text()
        assert "BỎ QUA (SKIPPED)" in page.locator("#l5-status-badge").inner_text()
        assert "BỎ QUA (SKIPPED)" in page.locator("#l6-status-badge").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 0

        vwap_shot = SCREENSHOTS_DIR / "18_microscope_expanded_vwap_desktop.png"
        page.screenshot(path=str(vwap_shot), full_page=True)
        assert vwap_shot.exists()
        assert vwap_shot.stat().st_size > 5000

        # Kiểm nhanh các thẻ mới còn lại 003/004/005/007/008 đều render 7 tầng & short-circuit sạch.
        for card_id in expanded_card_ids:
            if card_id == "SC-BINANCE-USDM-VWAP-REVERT-006":
                continue
            page.goto(
                f"{consensus_microscope_server_url}/admin/strategy-cards/{card_id}/microscope",
                wait_until="networkidle",
            )
            assert page.locator("#selected-card-id").inner_text() == card_id
            page.locator('button[data-preset-id="preset-low-vol-block"]').click()
            page.wait_for_selector(".banner-short-circuit", timeout=5000)
            assert "CHẶN ĐỨNG (BLOCK)" in page.locator("#l4-status-badge").inner_text()
            assert "BỎ QUA (SKIPPED)" in page.locator("#l5-status-badge").inner_text()
            assert "BỎ QUA (SKIPPED)" in page.locator("#l6-status-badge").inner_text()

        desktop_ctx.close()

        # 2. Mobile iPhone (390x844, dark mode): kiểm tra Taker Flow 008 không tràn ngang
        mobile_ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            color_scheme="dark",
        )
        mobile_ctx.add_cookies(
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
        mobile_page = mobile_ctx.new_page()
        watch_browser_violations(mobile_page)
        mobile_page.goto(
            f"{consensus_microscope_server_url}/admin/strategy-cards/"
            "SC-BINANCE-USDM-TAKER-FLOW-008/microscope",
            wait_until="networkidle",
        )
        assert (
            mobile_page.locator("#selected-card-id").inner_text()
            == "SC-BINANCE-USDM-TAKER-FLOW-008"
        )
        mobile_page.locator('button[data-preset-id="preset-low-vol-block"]').click()
        mobile_page.wait_for_selector(".banner-short-circuit", timeout=5000)
        assert mobile_page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 0

        taker_shot = SCREENSHOTS_DIR / "19_microscope_expanded_taker_mobile.png"
        mobile_page.screenshot(path=str(taker_shot), full_page=True)
        assert taker_shot.exists()
        assert taker_shot.stat().st_size > 5000

        mobile_ctx.close()
        browser.close()

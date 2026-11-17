"""E2E Playwright CRY-428: trang "Khẩu vị rủi ro" trên Chromium (desktop + di động).

Hành trình: điều hướng từ menu → xem giới hạn workspace, giá trị hiệu lực theo bot, bảng SL/TP
theo release có ví dụ % → nhập sai (giữ giá trị, báo lỗi đúng ô) → lưu hợp lệ (CSRF thật của
form) → chuyển hướng và hiển thị giá trị mới. App dựng trong tiến trình với header bảo mật thật
(CSP); service giả giữ trạng thái trong bộ nhớ, đường DB thật được E2E Compose RT-1 phủ.
"""

from __future__ import annotations

import secrets
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
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
    browser_violations,  # noqa: F401 - fixture nạp qua usefixtures
    install_security_headers,
    watch_browser_violations,
)
from trading_bot.admin.risk_appetite_page_router import router as risk_router
from trading_bot.admin.risk_appetite_service import RiskAppetiteService, RiskAppetiteUpdate
from trading_bot.admin.settings_service import SettingsAdminError
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.security import LoginRateLimiter
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings

SCREENSHOTS_DIR = Path(__file__).resolve().parent / "evidence" / "admin-playwright"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

WORKSPACE_ID = UUID("4d1e2c3b-0a9f-4e8d-9c7b-6a5f4e3d2c1b")
COOKIE = f"{WORKSPACE_ID}:risk-appetite-e2e"
CSRF = "csrf-risk-appetite-e2e"
BOT_ID = "6f0e5d4c-3b2a-4190-8f7e-6d5c4b3a2910"

pytestmark = pytest.mark.usefixtures("browser_violations")


class _Auth:
    def __init__(self) -> None:
        principal = Principal(
            user_id=UUID(int=1),
            membership_id=UUID(int=2),
            workspace_id=WORKSPACE_ID,
            roles=frozenset({Role.OWNER}),
            mfa_verified_at=datetime.now(UTC),
        )
        self.identity = AuthIdentity(principal=principal, email="owner@crypidian.io")

    async def authenticate(self, cookie_value: str | None) -> AuthIdentity:
        if cookie_value != COOKIE:
            raise AuthenticationFailure(401, "Phiên đăng nhập không hợp lệ.")
        return self.identity

    async def issue_csrf_token(self, cookie_value: str | None) -> str:
        await self.authenticate(cookie_value)
        return CSRF

    async def verify_csrf(self, cookie_value: str | None, token: str | None) -> None:
        await self.authenticate(cookie_value)
        if token != CSRF:
            raise AuthenticationFailure(403, "Token CSRF không hợp lệ.")


class _FakeRiskService:
    """Giữ giá trị workspace trong bộ nhớ; bảng SL/TP dùng catalog thật của sản phẩm."""

    def __init__(self) -> None:
        self.version = 7
        self.limits = {
            "per_trade_risk_fraction": "0.01",
            "account_max_open_risk_fraction": "0.03",
            "max_positions": "6",
            "max_leverage": "5",
            "max_notional": "50000",
            "max_symbol_notional": "20000",
            "max_aggregate_open_risk": "1000",
            "max_concentration_fraction": "0.5",
            "max_daily_loss": "500",
            "max_rolling_loss": "1000",
        }
        self.workspace_fraction = "0.05"
        self.drawdown: str | None = None
        self.saved: list[RiskAppetiteUpdate] = []

    async def overview(self, principal: Principal, *, symbol: str | None = None) -> dict[str, Any]:
        chosen = symbol if symbol in ("BTCUSDT", "ETHUSDT") else "BTCUSDT"
        atr = {"BTCUSDT": Decimal("100"), "ETHUSDT": Decimal("12")}[chosen]
        price = {"BTCUSDT": Decimal("50000"), "ETHUSDT": Decimal("3000")}[chosen]
        samples = dict.fromkeys(("5m", "15m", "1h", "4h"), (atr, price))
        return {
            "configured": True,
            "settings": {
                "id": "s",
                "version": self.version,
                "revision": 1,
                "effective_at": None,
                "workspace_max_open_risk_fraction": self.workspace_fraction,
                "max_drawdown_fraction": self.drawdown or "0.1",
                "max_drawdown_is_default": self.drawdown is None,
                "default_release": "h03-v3-1h",
                "symbols": ["BTCUSDT", "ETHUSDT"],
            },
            "profile": {"id": "p", "name": "paper-default", "version": 3, "limits": self.limits},
            "profile_history": [
                {
                    "name": "paper-default",
                    "version": 3,
                    "created_at": "2026-11-17T08:00:00+00:00",
                    "active": True,
                }
            ],
            "bots": [
                {
                    "id": BOT_ID,
                    "name": "T01 BTC",
                    "status": "ENABLED",
                    "release_id": "h03-v3-1h",
                    "limits": {
                        "per_trade_risk_fraction": "0.002",
                        "max_leverage": "2",
                        "max_positions": "1",
                        "max_notional": self.limits["max_notional"],
                        "max_daily_loss": self.limits["max_daily_loss"],
                        "max_rolling_loss": self.limits["max_rolling_loss"],
                    },
                    "overridden": ["per_trade_risk_fraction", "max_leverage", "max_positions"],
                    "error": None,
                    "loss_today": "12.5",
                    "loss_rolling": "12.5",
                    "drawdown": {
                        "fraction": "0.031",
                        "peak": "10000",
                        "current": "9690",
                        "tripped": False,
                    },
                }
            ],
            "symbol": chosen,
            "atr": {},
            "exit_policies": RiskAppetiteService._exit_rows(samples),
        }

    async def save(self, principal: Principal, update: RiskAppetiteUpdate) -> dict[str, Any]:
        if update.caps["max_leverage"] > Decimal("20"):
            raise SettingsAdminError(409, "Giới hạn mới thấp hơn overlay đã publish của bot.")
        self.saved.append(update)
        self.version += 1
        self.limits["max_leverage"] = format(update.caps["max_leverage"], "f")
        self.limits["max_daily_loss"] = format(update.caps["max_daily_loss"], "f")
        self.drawdown = format(update.max_drawdown_fraction, "f")
        return {"status": "published", "version": self.version}


class _BotAdminStub:
    async def list_bots(self, _principal: Principal) -> list[dict[str, Any]]:
        return []


def _app(service: _FakeRiskService) -> FastAPI:
    app = FastAPI()
    app.state.settings = AppSettings(app_env="local")
    templates = Jinja2Templates(directory="src/trading_bot/templates")
    app.state.templates = templates
    app.state.login_limiter = LoginRateLimiter()
    app.state.auth_service = _Auth()
    app.state.bot_admin_service = _BotAdminStub()
    app.state.risk_appetite_service = service
    app.mount("/static", StaticFiles(directory="src/trading_bot/static"), name="static")
    app.include_router(risk_router)
    app.include_router(admin_web_router)
    install_security_headers(app)
    return app


@pytest.fixture
def server() -> Iterator[tuple[str, _FakeRiskService]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    service = _FakeRiskService()
    uv_server = uvicorn.Server(
        uvicorn.Config(_app(service), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv_server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert uv_server.started, "uvicorn chưa khởi động"
    try:
        yield f"http://127.0.0.1:{port}", service
    finally:
        uv_server.should_exit = True
        thread.join(timeout=5)


def _open(base: str, page: Page, path: str = "/admin/risk") -> None:
    page.context.add_cookies(
        [
            {
                "name": "crypidian_session",
                "value": COOKIE,
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


def test_risk_page_view_edit_and_publish_desktop(server: tuple[str, _FakeRiskService]) -> None:
    base, service = server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_context(viewport={"width": 1366, "height": 900}).new_page()
            # Mục menu "Khẩu vị rủi ro" đưa về trang mặc định (coin ví dụ đầu tiên).
            _open(base, page, "/admin/risk?symbol=ETHUSDT")
            page.locator('nav.main-nav a[href="/admin/risk"]').click()
            page.wait_for_url("**/admin/risk")
            assert (
                page.locator('nav.main-nav a[href="/admin/risk"]').get_attribute("aria-current")
                == "page"
            )

            # Giá trị workspace và ngắt mạch đang dùng mặc định 10%.
            assert page.locator("#risk-max_leverage").input_value() == "5"
            assert page.locator("#risk-max_drawdown_fraction").input_value() == "0.1"
            assert "Đang dùng mặc định 10%." in page.locator("#risk-breaker").inner_text()

            # Hiệu lực theo bot: overlay siết rủi ro/lệnh xuống 0,2% và đòn bẩy 2x.
            row = page.locator(f'#risk-bot-table tr[data-bot-id="{BOT_ID}"]')
            assert row.locator('td[data-field="per_trade_risk_fraction"]').inner_text() == "0,2%"
            assert row.locator('td[data-field="max_leverage"]').inner_text() == "2x"
            assert "num--strong" in (
                row.locator('td[data-field="max_leverage"]').get_attribute("class") or ""
            )

            # Bảng SL/TP theo release với ví dụ %: ATR BTC = 0,2% giá.
            h01 = page.locator('#risk-exit-table tr[data-release-id="h01-v1"]')
            assert "2 × ATR" in h01.inner_text()
            assert "SL 0,40%" in h01.inner_text()
            h19 = page.locator('#risk-exit-table tr[data-release-id="h19-v1"]')
            assert "TP1 0,20%" in h19.inner_text()
            assert "TP2 0,40%" in h19.inner_text()
            assert (
                "cấu trúc"
                in page.locator('#risk-exit-table tr[data-release-id="h18-v1"]').inner_text()
            )

            # Đổi coin ví dụ: ETH ATR 12 / giá 3000 = 0,4% → SL H01 = 0,80%.
            page.select_option("#risk-symbol", "ETHUSDT")
            page.get_by_role("button", name="Xem").click()
            page.wait_for_url("**symbol=ETHUSDT")
            assert (
                "SL 0,80%"
                in page.locator('#risk-exit-table tr[data-release-id="h01-v1"]').inner_text()
            )

            # Nhập sai: giữ giá trị, báo lỗi đúng ô, không gọi lưu.
            page.fill("#risk-max_leverage", "ba")
            page.fill("#risk-reason", "Thử nhập sai")
            page.get_by_role("button", name="Lưu và publish").click()
            page.wait_for_load_state("networkidle")
            assert (
                "Đòn bẩy tối đa: Phải là số thập phân." in page.locator("#risk-error").inner_text()
            )
            assert page.locator("#risk-max_leverage").input_value() == "ba"
            assert page.locator("#risk-max_leverage").get_attribute("aria-invalid") == "true"
            assert service.saved == []

            # Lưu hợp lệ: dấu phẩy thập phân, CSRF của form, chuyển hướng và hiện giá trị mới.
            page.fill("#risk-max_leverage", "3")
            page.fill("#risk-max_daily_loss", "250")
            page.fill("#risk-max_drawdown_fraction", "0,08")
            page.fill("#risk-reason", "Siết rủi ro sau tuần lỗ")
            page.get_by_role("button", name="Lưu và publish").click()
            page.wait_for_url("**/admin/risk?saved=8")
            assert "Đã publish cấu hình workspace v8." in page.locator("#risk-saved").inner_text()
            assert page.locator("#risk-max_leverage").input_value() == "3"
            assert page.locator("#risk-max_drawdown_fraction").input_value() == "0.08"
            [update] = service.saved
            assert asdict(update)["max_drawdown_fraction"] == Decimal("0.08")
            assert update.reason == "Siết rủi ro sau tuần lỗ"
            assert _no_horizontal_overflow(page)
            page.screenshot(path=str(SCREENSHOTS_DIR / "cry428_risk_desktop.png"), full_page=True)

            # Xung đột với overlay bot: báo lỗi nghiệp vụ, giữ form.
            page.fill("#risk-max_leverage", "50")
            page.fill("#risk-reason", "Nới đòn bẩy")
            page.get_by_role("button", name="Lưu và publish").click()
            page.wait_for_load_state("networkidle")
            assert "overlay đã publish" in page.locator("#risk-error").inner_text()
            assert len(service.saved) == 1
        finally:
            browser.close()


def test_risk_page_mobile_layout_has_no_horizontal_scroll(
    server: tuple[str, _FakeRiskService],
) -> None:
    base, _service = server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
            )
            page = context.new_page()
            _open(base, page)
            assert page.locator("#risk-form-heading").is_visible()
            assert page.locator("#risk-exit-table").is_visible()
            # Bảng thành thẻ trên di động: nhãn ô lấy từ data-label.
            cell = page.locator(
                '#risk-exit-table tr[data-release-id="h06-v2"] td[data-label="Take-profit"]'
            )
            assert "Tối đa 2 × ATR" in cell.inner_text()
            assert _no_horizontal_overflow(page)
            submit = page.get_by_role("button", name="Lưu và publish").bounding_box()
            assert submit is not None
            assert submit["height"] >= 44
            page.screenshot(path=str(SCREENSHOTS_DIR / "cry428_risk_mobile.png"), full_page=True)
        finally:
            browser.close()


def test_risk_page_requires_session(server: tuple[str, _FakeRiskService]) -> None:
    base, _service = server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_context().new_page()
            page.goto(f"{base}/admin/risk?{secrets.token_hex(2)}=1")
            assert "/admin/login" in page.url
        finally:
            browser.close()

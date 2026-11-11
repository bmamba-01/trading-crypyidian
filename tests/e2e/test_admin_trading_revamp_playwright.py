"""Playwright E2E automation test cho Trading UI Revamp: Light/Dark Mode, Tooltips, TradingView Charts & AI Retraining."""
# ruff: noqa: E501, PT022, PT018

from __future__ import annotations

import secrets
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from playwright.sync_api import sync_playwright

from trading_bot.admin.presentation import label_vi, local_time, number_vi, pnl_tone
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.application.learning_post_mortem import LearningPostMortemService
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.security import LoginRateLimiter
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity, LoginResult
from trading_bot.config.settings import AppSettings
from trading_bot.domain.portfolio.reporting import (
    BotLedgerSnapshot,
    DecisionLineage,
    IntentLineage,
    LedgerCost,
    LedgerFill,
    LedgerTrade,
    PositionTransition,
    build_bot_report,
)

SCREENSHOTS_DIR = Path(
    "/Users/macbook/.gemini/antigravity/brain/4f23167d-60fd-49c3-8248-ddb2b669a2a7/screenshots"
)
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

WORKSPACE_ID = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
USER_ID = UUID("c3b4a5d6-e7f8-4a9b-0c1d-2e3f4a5b6c7d")
MEMBERSHIP_ID = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
BOT_ID = UUID("77117cc4-6c7f-407e-8f23-948d740a46c2")
TRADE_ID = UUID("11223344-5566-7788-99aa-bbccddeeff00")


class MockRevampAuthService:
    """Mock auth service cho test revamp."""

    def __init__(self) -> None:
        self.sessions: dict[str, AuthIdentity] = {}
        self.valid_email = "admin@crypidian.io"
        self.valid_password = "correct_password"
        self.valid_workspace = "demo-workspace"
        self.csrf_token = "csrf-token-revamp-test-123456"

    async def login(
        self,
        *,
        workspace_slug: str,
        email: str,
        password: str,
        code: str,
    ) -> LoginResult:
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
        if token != self.csrf_token:
            raise AuthenticationFailure(403, "Mã bảo vệ CSRF không hợp lệ.")


class MockRevampAdminService:
    async def list_bots(self, principal: Principal) -> list[dict[str, Any]]:
        return [
            {
                "id": str(BOT_ID),
                "name": "Binance BTC Trend Pullback H01",
                "symbol": "BTCUSDT",
                "status": "ENABLED",
                "account": {"mode": "PAPER", "status": "ACTIVE"},
            }
        ]

    async def get_bot(self, principal: Principal, *, bot_id: UUID) -> dict[str, Any]:
        return {
            "id": str(bot_id),
            "name": "Binance BTC Trend Pullback H01",
            "symbol": "BTCUSDT",
            "status": "RUNNING",
            "account": {"mode": "PAPER", "status": "ACTIVE"},
            "versions": [
                {
                    "id": str(uuid4()),
                    "status": "PUBLISHED",
                    "revision": 1,
                    "overlay": {"schema_version": 1},
                }
            ],
        }


class MockRevampReportingService:
    """Mock reporting service cung cấp đầy đủ dữ liệu báo cáo và biểu đồ nến."""

    def __init__(self) -> None:
        self.trade_id = TRADE_ID

    async def get_bot_report(self, query: Any, principal: Principal) -> Any:
        now = datetime.now(UTC)
        if (
            hasattr(query, "start")
            and hasattr(query, "end")
            and query.start
            and query.end
            and query.end > query.start
        ):
            window_duration = query.end - query.start
            closed_at = query.start + window_duration * 0.9
            opened_at = query.start + window_duration * 0.5
        else:
            opened_at = now - timedelta(hours=2)
            closed_at = now - timedelta(minutes=5)
        intent_id = uuid4()
        account_id = uuid4()

        trade = LedgerTrade(
            trade_id=self.trade_id,
            intent_id=intent_id,
            workspace_id=WORKSPACE_ID,
            bot_id=BOT_ID,
            account_id=account_id,
            status="CLOSED",
            symbol="BTCUSDT",
            side="LONG",
            quote_asset="USDT",
            opened_at=opened_at,
            closed_at=closed_at,
            gross_pnl=Decimal("380.0"),
            fee_total=Decimal("4.5"),
            funding_total=Decimal("1.2"),
            slippage_total=Decimal("0.8"),
            total_cost=Decimal("6.5"),
            net_pnl=Decimal("373.5"),
            settings_version_id=uuid4(),
            strategy_release_id=uuid4(),
            strategy_release_hash="hash_btc_strat_v1",
            feature_snapshot_id="feat_snap_1",
            candidate_id="cand_btc_1",
            risk_decision_id=uuid4(),
            risk_reservation_id=uuid4(),
        )

        costs = (
            LedgerCost(
                trade_id=self.trade_id,
                workspace_id=WORKSPACE_ID,
                bot_id=BOT_ID,
                cost_type="FEE",
                amount=Decimal("4.5"),
                currency="USDT",
            ),
            LedgerCost(
                trade_id=self.trade_id,
                workspace_id=WORKSPACE_ID,
                bot_id=BOT_ID,
                cost_type="FUNDING",
                amount=Decimal("1.2"),
                currency="USDT",
            ),
            LedgerCost(
                trade_id=self.trade_id,
                workspace_id=WORKSPACE_ID,
                bot_id=BOT_ID,
                cost_type="SLIPPAGE",
                amount=Decimal("0.8"),
                currency="USDT",
            ),
        )

        fills = (
            LedgerFill(
                fill_key="fill_entry_1",
                intent_id=intent_id,
                workspace_id=WORKSPACE_ID,
                bot_id=BOT_ID,
                account_id=account_id,
                role="ENTRY",
                quantity=Decimal("0.5"),
                price=Decimal("94200.0"),
                occurred_at=opened_at,
                fee_amount=Decimal("2.25"),
                slippage_amount=Decimal("0.4"),
                bar_key="btc_1h_bar_1",
            ),
            LedgerFill(
                fill_key="fill_exit_1",
                intent_id=intent_id,
                workspace_id=WORKSPACE_ID,
                bot_id=BOT_ID,
                account_id=account_id,
                role="EXIT",
                quantity=Decimal("0.5"),
                price=Decimal("94960.0"),
                occurred_at=closed_at,
                fee_amount=Decimal("2.25"),
                slippage_amount=Decimal("0.4"),
                bar_key="btc_1h_bar_2",
            ),
        )

        position_events = (
            PositionTransition(
                event_key="pos_evt_closed",
                intent_id=intent_id,
                workspace_id=WORKSPACE_ID,
                bot_id=BOT_ID,
                account_id=account_id,
                event_type="CLOSED",
                occurred_at=closed_at,
                payload={"exit_reason": "TAKE_PROFIT", "fill_key": "fill_exit_1"},
            ),
        )

        snapshot = BotLedgerSnapshot(
            workspace_id=WORKSPACE_ID,
            bot_id=BOT_ID,
            account_id=account_id,
            closed_trades=(trade,),
            open_trades=(),
            costs=costs,
            intents=(
                IntentLineage(
                    intent_id=intent_id,
                    bot_id=BOT_ID,
                    evidence={"settings_hash": "cfg_btc_1"},
                ),
            ),
            decisions=(
                DecisionLineage(
                    decision_id=uuid4(),
                    bot_id=BOT_ID,
                    candidate_id="cand_btc_1",
                    decision_hash="dec_hash_1",
                    bot_config_version_id=uuid4(),
                    bot_config_hash="bot_cfg_hash_1",
                    root_settings_version_id=uuid4(),
                    model_release_id=None,
                    candidate={
                        "strategy_id": "TREND_PULLBACK_V1",
                        "target_leverage": 5,
                        "account_risk_pct": "1.0",
                        "context_regime": "TRENDING",
                        "context_gate_passed": True,
                        "rejection_wick_score": 86.5,
                        "breakout_score": 88.0,
                        "risk_reward_ratio": "2.0",
                        "detail_vi": "Nến rút chân EMA200 xác nhận đà tăng mạnh mẽ.",
                    },
                ),
            ),
            fills=fills,
            position_events=position_events,
        )

        return build_bot_report(
            snapshot=snapshot,
            start=query.start,
            end=query.end,
        )

    async def get_trade_chart(
        self,
        query: Any,
        principal: Principal,
        *,
        trade_id: UUID,
        timeframe: str = "1h",
    ) -> dict[str, Any]:
        report = await self.get_bot_report(query, principal)
        report_dict = report.as_dict()
        trade = report_dict["trades"][0]

        now = int(time.time())
        bars = [
            {
                "time": now - 7200,
                "open": 93800.0,
                "high": 94300.0,
                "low": 93700.0,
                "close": 94200.0,
                "volume": 125.4,
            },
            {
                "time": now - 3600,
                "open": 94200.0,
                "high": 94650.0,
                "low": 94100.0,
                "close": 94500.0,
                "volume": 98.2,
            },
            {
                "time": now - 300,
                "open": 94500.0,
                "high": 95100.0,
                "low": 94450.0,
                "close": 94960.0,
                "volume": 210.5,
            },
        ]
        return {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "timeframe": timeframe,
            "trade": trade,
            "bars": bars,
            "notice": None,
        }


class MockRevampBotOperationsService:
    def __init__(self) -> None:
        self.kill_state = "ACTIVE"
        self.workspace_revision = 1
        self.runtimes = {
            BOT_ID: {
                "id": str(BOT_ID),
                "readiness": "READY",
                "entry_state": "POSITION_OPEN",
                "entry_allowed": True,
                "control_revision": 1,
                "needs_manual_enable": False,
            }
        }

    def for_principal(self, principal: Principal) -> MockRevampBotOperationsService:
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


def create_revamp_test_app() -> FastAPI:
    """Tạo FastAPI app phục vụ test revamp."""
    app = FastAPI(title="Crypidian Trading Revamp Test")
    app.state.settings = AppSettings(app_env="local")

    templates = Jinja2Templates(directory="src/trading_bot/templates")
    templates.env.filters["local_time"] = local_time
    templates.env.filters["number_vi"] = number_vi
    templates.env.filters["label_vi"] = label_vi
    templates.env.filters["pnl_tone"] = pnl_tone
    templates.env.globals["session_cookie_name"] = lambda s: "crypidian_session"

    app.state.templates = templates
    app.state.login_limiter = LoginRateLimiter()

    auth_service = MockRevampAuthService()
    admin_service = MockRevampAdminService()
    reporting_service = MockRevampReportingService()
    post_mortem_service = LearningPostMortemService()
    bot_ops = MockRevampBotOperationsService()

    app.state.auth_service = auth_service
    app.state.bot_admin_service = admin_service
    app.state.bot_operations_service = bot_ops
    app.state.bot_reporting_service = reporting_service
    app.state.learning_post_mortem_service = post_mortem_service

    app.mount("/static", StaticFiles(directory="src/trading_bot/static"), name="static")
    app.include_router(admin_web_router)
    return app


@pytest.fixture(scope="module")
def revamp_server_url() -> Any:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app = create_revamp_test_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(1.2)
    yield f"http://127.0.0.1:{port}"


def test_trading_revamp_playwright_desktop_and_mobile(revamp_server_url: str) -> None:
    """Kiểm thử tự động Playwright trên Desktop và Mobile cho Trading UI Revamp, Theme, Chart và AI."""
    base_url = revamp_server_url

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        # =========================================================================
        # 1. KỊCH BẢN DESKTOP (1280x800)
        # =========================================================================
        desktop_ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = desktop_ctx.new_page()

        # Đăng nhập
        page.goto(f"{base_url}/admin/login", wait_until="networkidle")
        page.get_by_label("Mã không gian làm việc").fill("demo-workspace")
        page.get_by_label("Địa chỉ thư điện tử").fill("admin@crypidian.io")
        page.get_by_label("Mật khẩu").fill("correct_password")
        page.get_by_label("Mã xác thực 6 chữ số").fill("123456")
        page.get_by_role("button", name="Đăng nhập").click()
        page.wait_for_load_state("networkidle")

        # 1.1 Kiểm tra Theme Toggle Switcher (Light / Dark mode)
        theme_btn = page.locator("#theme-toggle")
        assert theme_btn.is_visible()

        # Chuyển sang Dark Mode
        theme_btn.click()
        current_theme = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        assert current_theme in ("dark", "light")

        # Đảm bảo chụp Dark Mode
        page.evaluate("() => document.documentElement.setAttribute('data-theme', 'dark')")
        page.screenshot(path=str(SCREENSHOTS_DIR / "19_dark_theme_desktop.png"), full_page=True)

        # Chuyển sang Light Mode và chụp
        page.evaluate("() => document.documentElement.setAttribute('data-theme', 'light')")
        page.screenshot(path=str(SCREENSHOTS_DIR / "20_light_theme_desktop.png"), full_page=True)

        # 1.2 Truy cập trang Báo cáo /admin/reports
        page.goto(f"{base_url}/admin/reports", wait_until="networkidle")

        # Kiểm tra các nút Quick Filter Presets
        assert page.locator("button:has-text('Hôm Nay')").is_visible()
        assert page.locator("button:has-text('7 Ngày Qua')").is_visible()
        assert page.locator("button:has-text('30 Ngày Qua')").is_visible()

        # Nhấn nút "7 Ngày Qua" để auto-fill ngày
        page.locator("button:has-text('7 Ngày Qua')").click()
        start_val = page.locator("#start").input_value()
        end_val = page.locator("#end").input_value()
        assert len(start_val) >= 19
        assert len(end_val) >= 19

        # Chọn Bot và nhấn Phân Tích
        page.locator("#bot_id").select_option(str(BOT_ID))
        page.locator("button:has-text('📊 Phân Tích')").click()
        page.wait_for_load_state("networkidle")

        # 1.3 Kiểm tra Lưới Trading Metric Cards và Tooltips
        assert page.locator("text=Tài Sản Ký Quỹ / Định Giá:").is_visible()
        assert page.locator("text=Lời / Lỗ Ròng (Net PnL)").is_visible()
        assert page.locator("text=Tỷ Lệ Thắng (Win Rate)").is_visible()
        assert page.locator("text=Profit Factor").is_visible()
        assert page.locator("text=Max Drawdown").is_visible()
        assert page.locator("text=Thời Gian Giữ Lệnh").first.is_visible()

        # Kiểm tra Tooltips có đầy đủ thuộc tính data-tooltip
        tooltips = page.locator(".tooltip-term")
        assert tooltips.count() >= 4
        first_tooltip = tooltips.first.get_attribute("data-tooltip")
        assert first_tooltip is not None and len(first_tooltip) > 5

        # Chụp màn hình trang Báo cáo với Trading Design System
        page.screenshot(
            path=str(SCREENSHOTS_DIR / "21_reports_trading_revamp_desktop.png"),
            full_page=True,
        )

        # 1.4 Kiểm tra Danh sách Lệnh và Bấm "Xem Biểu Đồ"
        view_chart_btn = page.locator("button:has-text('🔍 Xem Biểu Đồ')").first
        assert view_chart_btn.is_visible()
        view_chart_btn.click()
        page.wait_for_load_state("networkidle")

        # Xác nhận container biểu đồ Lightweight Charts xuất hiện
        assert page.locator("#trade-chart").is_visible()
        assert page.locator("#trade-chart-event-detail").is_visible()

        # 1.5 Kiểm tra Thẻ "Mổ Xẻ Quyết Định Định Lượng & Quản Trị Rủi Ro"
        assert page.locator("text=Mổ Xẻ Quyết Định Định Lượng & Quản Trị Rủi Ro").is_visible()
        assert page.locator("text=Vì Sao Vào Vị Thế?").is_visible()
        assert page.locator("text=Vì Sao Thoát Vị Thế?").is_visible()
        assert page.locator("text=L4 Cửa Bối Cảnh (Context Gate)").is_visible()
        assert page.locator("text=L5 Điểm Kích Hoạt (Trigger Score)").is_visible()
        assert page.locator("text=L6 Tỷ Lệ R:R & Kế Hoạch Vùng Giá").is_visible()
        assert page.locator("text=Trượt giá thực tế (Slippage)").is_visible()
        assert page.locator("text=Đòn bẩy đề xuất (Leverage)").is_visible()

        # 1.6 Kiểm tra Thao tác "Đưa Ca Này Vào Dữ Liệu Phạt AI"
        ingest_btn = page.locator("#btn-ingest-ai")
        assert ingest_btn.is_visible()
        ingest_btn.click(force=True)

        # Xác minh phản hồi thành công tức thì
        page.wait_for_selector("#ai-ingest-feedback", state="visible", timeout=5000)
        assert "Đã tiếp nhận thành công!" in page.locator("#ai-ingest-feedback").inner_text()

        # Chụp màn hình biểu đồ và phân tích quyết định
        page.screenshot(
            path=str(SCREENSHOTS_DIR / "22_trade_chart_reasoning_desktop.png"),
            full_page=True,
        )

        desktop_ctx.close()

        # =========================================================================
        # 2. KỊCH BẢN MOBILE (390x844 - iPhone / Mobile Touch)
        # =========================================================================
        mobile_ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
        )
        page_m = mobile_ctx.new_page()

        # Đăng nhập mobile
        page_m.goto(f"{base_url}/admin/login", wait_until="networkidle")
        page_m.get_by_label("Mã không gian làm việc").fill("demo-workspace")
        page_m.get_by_label("Địa chỉ thư điện tử").fill("admin@crypidian.io")
        page_m.get_by_label("Mật khẩu").fill("correct_password")
        page_m.get_by_label("Mã xác thực 6 chữ số").fill("123456")
        page_m.get_by_role("button", name="Đăng nhập").click()
        page_m.wait_for_load_state("networkidle")

        # Kiểm tra theme toggle trên mobile
        m_theme_btn = page_m.locator("#theme-toggle")
        assert m_theme_btn.is_visible()
        page_m.evaluate("() => document.documentElement.setAttribute('data-theme', 'dark')")
        page_m.screenshot(path=str(SCREENSHOTS_DIR / "15_dark_theme_mobile.png"), full_page=True)

        # Vào Báo cáo trên mobile
        page_m.goto(
            f"{base_url}/admin/reports?scope=bot&bot_id={BOT_ID}&start=2026-11-01T00:00:00%2B07:00&end=2026-11-10T23:59:59%2B07:00&chart_trade_id={TRADE_ID}&chart_timeframe=1h",
            wait_until="networkidle",
        )

        # Kiểm tra responsive hiển thị trên Mobile
        assert page_m.locator("text=Tài Sản Ký Quỹ / Định Giá:").is_visible()
        assert page_m.locator("#trade-chart").is_visible()
        assert page_m.locator("text=Mổ Xẻ Quyết Định Định Lượng & Quản Trị Rủi Ro").is_visible()

        # Chụp screenshots mobile
        page_m.screenshot(
            path=str(SCREENSHOTS_DIR / "16_trading_revamp_mobile.png"), full_page=True
        )

        mobile_ctx.close()
        browser.close()


def test_bot_detail_trading_revamp_playwright_desktop_and_mobile(
    revamp_server_url: str,
) -> None:
    """Kiểm thử Playwright E2E cho giao diện Chi Tiết Bot Revamp: Live 5s, Charts, Positions & AI Phạt."""
    base_url = revamp_server_url

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        # =========================================================================
        # 1. KỊCH BẢN DESKTOP (1280x800)
        # =========================================================================
        desktop_ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = desktop_ctx.new_page()

        # Đăng nhập
        page.goto(f"{base_url}/admin/login", wait_until="networkidle")
        page.get_by_label("Mã không gian làm việc").fill("demo-workspace")
        page.get_by_label("Địa chỉ thư điện tử").fill("admin@crypidian.io")
        page.get_by_label("Mật khẩu").fill("correct_password")
        page.get_by_label("Mã xác thực 6 chữ số").fill("123456")
        page.get_by_role("button", name="Đăng nhập").click()
        page.wait_for_load_state("networkidle")

        # Truy cập trang Chi tiết Bot
        page.goto(f"{base_url}/admin/bots/{BOT_ID}", wait_until="networkidle")

        # 1.1 Kiểm tra Header Bot và Badges
        assert page.locator("text=Binance BTC Trend Pullback H01").first.is_visible()
        assert page.locator("text=BTCUSDT").first.is_visible()
        assert page.locator("text=● ĐANG CHẠY").is_visible()
        assert page.locator("text=Chế độ: PAPER").is_visible()

        # 1.2 Kiểm tra Live Status Bar 5s
        assert page.locator(".live-status-bar").is_visible()
        assert page.locator("#live-pulse-dot").is_visible()
        assert page.locator("#live-updated-at").is_visible()
        toggle_btn = page.locator("#toggle-realtime-btn")
        assert toggle_btn.is_visible()

        # Tương tác với nút Tạm dừng / Tiếp tục cập nhật 5s
        toggle_btn.click()
        assert toggle_btn.inner_text() == "Tiếp tục"
        toggle_btn.click()
        assert toggle_btn.inner_text() == "Tạm dừng"

        # 1.3 Kiểm tra Lưới Thẻ Chỉ Số KPI Trading
        assert page.locator("#kpi-open-positions").is_visible()
        assert page.locator("#kpi-win-rate").is_visible()
        assert page.locator("#kpi-profit-factor").is_visible()
        assert page.locator("#kpi-net-pnl").is_visible()
        assert page.locator("#kpi-max-drawdown").is_visible()

        # 1.4 Kiểm tra Biểu đồ nến Lightweight Charts
        assert page.locator("#trade-chart").is_visible()
        assert page.locator("#trade-chart-event-detail").is_visible()
        assert page.locator("text=Chốt lời (🎯 TP)").is_visible()
        assert page.locator("text=Dừng lỗ (🛑 SL)").is_visible()

        # 1.5 Kiểm tra Bảng Vị Thế Đang Mở và Thẻ Reasoning L4-L6
        assert page.locator("#open-positions-heading").is_visible()
        assert page.locator("text=Khối Lượng").first.is_visible()
        assert page.locator("text=Đòn Bẩy").first.is_visible()
        assert page.locator("text=Giá Vào Lệnh").first.is_visible()
        assert page.locator("text=L4 Cửa Bối Cảnh (Context Gate)").first.is_visible()
        assert page.locator("text=L5 Điểm Kích Hoạt (Trigger Score)").first.is_visible()
        assert page.locator("text=L6 Vùng Giá & Tỷ Lệ R:R").first.is_visible()

        # 1.6 Kiểm tra lịch sử giao dịch và điều kiện tiếp nhận phản hồi
        assert page.locator("#recent-trades-heading").is_visible()
        assert page.locator("text=Vì sao vào:").first.is_visible()
        assert page.locator("text=Vì sao thoát:").first.is_visible()
        ai_btn = page.locator(".btn-ingest-ai").first
        assert ai_btn.count() == 0
        assert page.get_by_text("Thực thi đạt chuẩn").is_visible()

        # Chụp màn hình Desktop Light Theme
        page.screenshot(
            path=str(SCREENSHOTS_DIR / "23_bot_detail_trading_revamp_desktop.png"),
            full_page=True,
        )

        # Chuyển sang Dark Theme và chụp
        page.evaluate("() => document.documentElement.setAttribute('data-theme', 'dark')")
        page.screenshot(
            path=str(SCREENSHOTS_DIR / "24_bot_detail_dark_desktop.png"),
            full_page=True,
        )

        desktop_ctx.close()

        # =========================================================================
        # 2. KỊCH BẢN MOBILE (390x844 - iPhone)
        # =========================================================================
        mobile_ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
        )
        page_m = mobile_ctx.new_page()

        # Đăng nhập mobile
        page_m.goto(f"{base_url}/admin/login", wait_until="networkidle")
        page_m.get_by_label("Mã không gian làm việc").fill("demo-workspace")
        page_m.get_by_label("Địa chỉ thư điện tử").fill("admin@crypidian.io")
        page_m.get_by_label("Mật khẩu").fill("correct_password")
        page_m.get_by_label("Mã xác thực 6 chữ số").fill("123456")
        page_m.get_by_role("button", name="Đăng nhập").click()
        page_m.wait_for_load_state("networkidle")

        # Truy cập trang Chi tiết Bot
        page_m.goto(f"{base_url}/admin/bots/{BOT_ID}", wait_until="networkidle")

        # Xác nhận hiển thị responsive trên mobile
        assert page_m.locator("text=Binance BTC Trend Pullback H01").first.is_visible()
        assert page_m.locator(".live-status-bar").is_visible()
        assert page_m.locator("#trade-chart").is_visible()
        assert page_m.locator("#open-positions-heading").is_visible()

        # Chụp màn hình Mobile
        page_m.screenshot(
            path=str(SCREENSHOTS_DIR / "25_bot_detail_mobile.png"),
            full_page=True,
        )

        mobile_ctx.close()
        browser.close()

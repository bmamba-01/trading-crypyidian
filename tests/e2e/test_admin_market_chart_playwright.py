"""Playwright E2E cho SIG-7: trang biểu đồ thị trường kiểu TradingView.

Đi qua ranh giới thật: router Admin → dịch vụ báo cáo (giả, trả nến) → dựng payload bằng mã
feature SIG-1 → template + CSP thật → Lightweight Charts vẽ trong Chromium → polling API.
"""

from __future__ import annotations

import math
import re
import socket
import threading
import time
from collections.abc import Iterator
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
from playwright.sync_api import Page, expect, sync_playwright

from tests.e2e.csp_harness import (
    browser_violations,  # noqa: F401 - fixture nạp qua usefixtures
    install_security_headers,
    watch_browser_violations,
)
from tests.e2e.test_admin_trading_revamp_playwright import MockRevampAuthService
from trading_bot.admin.presentation import label_vi, local_time, number_vi, pnl_tone
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.application.market_overview import build_market_overview
from trading_bot.auth.policy import Principal
from trading_bot.auth.security import LoginRateLimiter
from trading_bot.config.settings import AppSettings
from trading_bot.domain.strategy.setup_progress import setup_progress

SCREENSHOTS_DIR = Path(__file__).resolve().parent / "evidence" / "admin-playwright"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
BOT_ID = UUID("77117cc4-6c7f-407e-8f23-948d740a46c2")
START = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
# Thời điểm cố định ngay sau nến 5m cuối cùng của dữ liệu mẫu (576 nến = 2 ngày).
OVERVIEW_NOW = START + timedelta(days=2, minutes=2, seconds=30)

pytestmark = pytest.mark.usefixtures("browser_violations")


def _bars() -> list[dict[str, Any]]:
    """Hai ngày nến 5m tổng hợp (dao động + xu hướng) kèm taker buy để có delta/profile mua-bán."""
    output = []
    for index in range(576):
        base = 100 + 4 * math.sin(index / 9) + index * 0.01
        close = base + 0.3 * math.sin(index / 2)
        volume = 10.0 + (index % 5)
        output.append(
            {
                "time": int((START + timedelta(minutes=5 * index)).timestamp()),
                "open": base,
                "high": max(base, close) + 0.4,
                "low": min(base, close) - 0.4,
                "close": close,
                "volume": volume,
                "taker_buy_volume": volume * (0.62 if close > base else 0.38),
            }
        )
    return output


SIGNAL_VIEW = {
    "latest_decision": {
        "decision_id": "d1",
        "status": "ABSTAIN",
        "reason_code": "H01_ADX_BELOW_20",
        "detail_vi": "ADX14 1h phải đạt tối thiểu 20.",
        "evaluated_at": "2026-11-16T10:00:00+00:00",
        "strategy_release_id": "h01-v1",
        "gate_details": [],
        "checklist": [
            {
                "key": "adx",
                "label_vi": "ADX14 1h đủ mạnh",
                "actual": "12",
                "operator": ">=",
                "threshold": "20",
                "passed": False,
            },
        ],
    },
    "card": {
        "card_version": "sig3-card-v1",
        "symbol": "BTCUSDT",
        "direction": "short",
        "strategy_id": "H06",
        "strategy_release_id": "h06-v1",
        "reason_code": "H06_SHORT_ENTRY",
        "signal_at": "2026-11-16T09:00:00+00:00",
        "expires_at": "2026-11-16T09:15:00+00:00",
        "reference_price": 101.0,
        "entry_zone": {"low": 100.9, "high": 101.0},
        "stop": 104.0,
        "risk_per_unit": 3.1,
        "targets": [
            {
                "label": "Mốc 1R (tham chiếu)",
                "price": 97.8,
                "r_multiple": 1.0,
                "source": "r_ladder",
            },
            {"label": "Mục tiêu recipe", "price": 97.0, "r_multiple": 1.26, "source": "recipe"},
        ],
        "plan_source": "approved_plan",
        "checklist": [
            {
                "key": "extreme",
                "label_vi": "Z20 và RSI14 cùng vượt ngưỡng",
                "actual": "Z20 2.3",
                "operator": "vượt",
                "threshold": "Z > 2",
                "passed": True,
            },
        ],
        "checklist_passed": 1,
        "checklist_total": 1,
        "invalidation": ["Giá chạm SL 104 (dừng lỗ theo recipe/kế hoạch)."],
        "confidence": {"tier": "uncalibrated", "label": "Chưa hiệu chuẩn"},
        "decision_id": "d0",
        "evaluated_at": "2026-11-16T09:00:01+00:00",
        "is_expired": False,
    },
    "card_error": None,
    "unverified_decisions": 0,
}


class _AdminService:
    async def list_bots(self, principal: Principal) -> list[dict[str, Any]]:
        return [
            {
                "id": str(BOT_ID),
                "name": "Bot BTC kiểm thử",
                "status": "ENABLED",
                "account": {"mode": "PAPER", "status": "ACTIVE"},
            }
        ]


class _Reporting:
    def __init__(self) -> None:
        self.bars = _bars()
        self.calls = 0
        self.overview_calls = 0
        self.setup_calls: list[dict[str, Any]] = []

    async def list_market_series(self, principal: Principal, *, bot_id: UUID) -> dict:
        return {"BTCUSDT": ["5m"], "ETHUSDT": ["1h"]}

    async def get_latest_signal(self, principal: Principal, *, bot_id: UUID) -> dict:
        return SIGNAL_VIEW

    async def get_market_bars(self, principal: Principal, **kwargs: Any) -> list[dict]:
        self.calls += 1
        return self.bars if kwargs["symbol"] == "BTCUSDT" else []

    async def get_setup_board(self, principal: Principal, **kwargs: Any) -> list[dict]:
        """Bảng Entry giả: một setup H01 chưa đủ (thiếu ADX) cho bot đầu tiên."""
        self.setup_calls.append(kwargs)
        now = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
        decisions = [
            {
                "evaluated_at": (now - timedelta(hours=hours)).isoformat(),
                "status": "ABSTAIN",
                "reason_code": "H01_ADX_BELOW_20",
                "detail_vi": "ADX14 1h phải đạt tối thiểu 20.",
                "checklist": [
                    {
                        "key": "ema_cross",
                        "label_vi": "EMA20 vừa cắt EMA80 (1h)",
                        "actual": "cắt",
                        "operator": "cắt",
                        "threshold": "đổi phía",
                        "passed": True,
                    },
                    {
                        "key": "htf_trend",
                        "label_vi": "Giá đóng 4h cùng phía EMA200",
                        "actual": "trên",
                        "operator": "cùng phía",
                        "threshold": "EMA200",
                        "passed": True,
                    },
                    {
                        "key": "adx",
                        "label_vi": "ADX14 1h đủ mạnh",
                        "actual": "12",
                        "operator": ">=",
                        "threshold": "20",
                        "passed": False,
                    },
                ],
                "candidate": None,
            }
            for hours in (0, 1)
        ]
        bots = kwargs["bots"]
        return (
            [
                {
                    "bot_id": str(bots[0][0]),
                    "bot_name": bots[0][1],
                    "card": dict(SIGNAL_VIEW["card"], stop=105.5),
                    "unverified_decisions": 0,
                }
                | setup_progress(
                    strategy_release_id="h01-v1",
                    decisions=decisions,
                    bonus=kwargs["bonus"],
                    now=now,
                )
            ]
            if bots
            else []
        )

    async def get_market_overview(self, principal: Principal, **kwargs: Any) -> dict:
        """Giá bid nhích 0,01 mỗi lần gọi để kiểm polling cập nhật dải thông số."""
        self.overview_calls += 1
        last_close = Decimal(str(round(self.bars[-1]["close"], 2)))
        bid = last_close + Decimal("0.01") * self.overview_calls
        has_bars = kwargs["symbol"] == "BTCUSDT"
        return build_market_overview(
            symbol=kwargs["symbol"],
            timeframe=kwargs["timeframe"],
            now=OVERVIEW_NOW,
            bars_by_timeframe={"5m": self.bars} if has_bars else {},
            quote={
                "bid_price": bid,
                "ask_price": bid + Decimal("0.02"),
                "bid_quantity": Decimal("3"),
                "ask_quantity": Decimal("4"),
                "event_at": OVERVIEW_NOW - timedelta(seconds=1),
            }
            if has_bars
            else None,
            mark={
                "mark_price": last_close,
                "index_price": last_close,
                "funding_rate": Decimal("0.0001"),
                "next_funding_time": datetime(2026, 11, 18, 8, 0, tzinfo=UTC),
                "event_at": OVERVIEW_NOW - timedelta(seconds=2),
            }
            if has_bars
            else None,
        )


def _app() -> FastAPI:
    app = FastAPI(title="Crypidian market chart E2E")
    app.state.settings = AppSettings(app_env="local")
    templates = Jinja2Templates(directory="src/trading_bot/templates")
    templates.env.filters["local_time"] = local_time
    templates.env.filters["number_vi"] = number_vi
    templates.env.filters["label_vi"] = label_vi
    templates.env.filters["pnl_tone"] = pnl_tone
    templates.env.globals["session_cookie_name"] = lambda s: "crypidian_session"
    app.state.templates = templates
    app.state.login_limiter = LoginRateLimiter()
    app.state.auth_service = MockRevampAuthService()
    app.state.bot_admin_service = _AdminService()
    app.state.bot_reporting_service = _Reporting()
    app.mount("/static", StaticFiles(directory="src/trading_bot/static"), name="static")
    app.include_router(admin_web_router)
    install_security_headers(app)
    return app


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(
        uvicorn.Config(_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(1.2)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _login(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/admin/login", wait_until="networkidle")
    page.get_by_label("Mã không gian làm việc").fill("demo-workspace")
    page.get_by_label("Địa chỉ thư điện tử").fill("admin@crypidian.io")
    page.get_by_label("Mật khẩu").fill("correct_password")
    page.get_by_label("Mã xác thực 6 chữ số").fill("123456")
    page.get_by_role("button", name="Đăng nhập").click()
    page.wait_for_load_state("networkidle")


def _assert_no_horizontal_overflow(page: Page, label: str) -> None:
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"{label}: tràn ngang {overflow}px"
    box = page.locator("#market-chart").bounding_box()
    assert box is not None
    assert box["x"] + box["width"] <= page.viewport_size["width"] + 1, label


def test_market_chart_renders_overlays_polls_and_is_responsive(server_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        watch_browser_violations(page)
        _login(page, server_url)

        page.goto(f"{server_url}/admin/chart?timeframe=5m", wait_until="networkidle")
        expect(page.locator("#market-chart canvas").first).to_be_visible()
        expect(page.locator("#mc-symbol")).to_have_value("BTCUSDT")
        expect(page.locator(".mc-timeframe.is-active")).to_have_text("5M")
        expect(page.locator('.mc-timeframe[href*="timeframe=1h"]')).to_have_class(
            "mc-timeframe is-empty"
        )
        expect(page.locator("[data-mc-trend]")).not_to_have_text("—")
        expect(page.locator("[data-mc-updated]")).to_have_text(
            re.compile(r"^\d{2}:\d{2} \d{2}/\d{2}$")
        )
        events = page.locator("[data-mc-events] li")
        expect(events.first).to_contain_text(re.compile(r"BOS|CHoCH|Quét"))
        # Có taker buy → không còn ghi chú thiếu dữ liệu áp lực mua/bán.
        expect(page.locator("[data-mc-notices]")).not_to_contain_text("taker buy")
        expect(page.locator(".mc-attribution a").first).to_have_attribute(
            "href", "https://www.tradingview.com/"
        )

        # CK-1: dải thông số trực tiếp — bid/ask, đếm ngược nến chạy, mark/funding, phiên.
        strip = page.locator("#mc-live-strip")
        expect(strip).to_be_visible()
        expect(strip.locator("[data-ov-price-source]")).to_have_text("giữa bid/ask")
        expect(strip.locator("[data-ov-bid]")).to_have_text(re.compile(r"^\d{2,3},\d{1,2}$"))
        expect(strip.locator("[data-ov-funding]")).to_have_text("0,0100%")
        first_clock = strip.locator("[data-ov-candle-left]").inner_text()
        assert re.fullmatch(r"\d{2}:\d{2}", first_clock), first_clock
        page.wait_for_timeout(1300)
        assert strip.locator("[data-ov-candle-left]").inner_text() != first_clock
        expect(strip.locator("[data-ov-notices] li")).to_have_count(0)
        # Polling ~2 giây trả số mới và JS cập nhật bid.
        with page.expect_response(
            lambda r: "/admin/api/chart/overview?" in r.url, timeout=6000
        ) as overview_polled:
            pass
        assert overview_polled.value.status == 200
        assert overview_polled.value.headers["cache-control"] == "no-store"
        latest_bid = overview_polled.value.json()["quote"]["bid"]
        expect(strip.locator("[data-ov-bid]")).to_have_text(
            f"{latest_bid:.2f}".rstrip("0").replace(".", ",")
        )

        # CK-2: ma trận đa khung — 4 vai trò theo thứ tự, khung thiếu dữ liệu ghi rõ.
        matrix = page.locator("#mc-mtf-matrix")
        rows = matrix.locator("tbody tr")
        expect(rows).to_have_count(4)
        expect(rows.nth(0)).to_contain_text("Xu hướng chính")
        expect(rows.nth(0)).to_contain_text("Chưa có nến 4H đã đóng trong kho dữ liệu.")
        expect(rows.nth(3)).to_contain_text("Thời điểm")
        expect(matrix.locator(".is-primary")).to_contain_text("5M")

        # CK-3: lớp ICT — số vùng còn hiệu lực, công tắc killzone (mặc định tắt), chip killzone.
        expect(page.locator("[data-mc-ict-count]")).to_have_text(re.compile(r"^\d+$"))
        expect(page.locator('[data-layer="ict_zones"]')).to_be_checked()
        expect(page.locator('[data-layer="killzones"]')).not_to_be_checked()
        page.locator('[data-layer="killzones"]').check()
        expect(strip.locator("[data-ov-killzones]")).not_to_be_empty()
        page.locator("#market-chart").screenshot(
            path=str(SCREENSHOTS_DIR / "ck3-ict-layer-desktop.png")
        )
        page.locator('[data-layer="killzones"]').uncheck()

        # CK-4: TPO mặc định tắt; nPOC/VAH-VAL mặc định bật.
        expect(page.locator('[data-layer="tpo"]')).not_to_be_checked()
        expect(page.locator('[data-layer="profile_levels"]')).to_be_checked()
        page.locator('[data-layer="tpo"]').check()
        page.locator("#market-chart").screenshot(
            path=str(SCREENSHOTS_DIR / "ck4-tpo-npoc-desktop.png")
        )
        page.locator('[data-layer="tpo"]').uncheck()

        # CK-5: ZigZag mặc định bật; VSA tô volume khi bật (mặc định theo delta).
        expect(page.locator('[data-layer="zigzag"]')).to_be_checked()
        expect(page.locator('[data-layer="vsa"]')).not_to_be_checked()
        page.locator('[data-layer="vsa"]').check()
        page.locator("#market-chart").screenshot(
            path=str(SCREENSHOTS_DIR / "ck5-zigzag-vsa-desktop.png")
        )
        page.locator('[data-layer="vsa"]').uncheck()

        # CK-6: bảng Entry — tiến độ k/n, còn thiếu, ghim kịch bản lên chart.
        board = page.locator("#mc-entry-board")
        expect(board).to_contain_text("Bắt buộc 2/3")
        expect(board).to_contain_text("Còn thiếu: ADX14 1h đủ mạnh")
        expect(board.locator("[data-setup-status]").first).to_have_attribute(
            "data-setup-status", "not_ready"
        )
        pin = page.locator("[data-setup-pin]")
        expect(pin.locator("option")).to_have_count(2)
        pin.select_option("0")
        board.screenshot(path=str(SCREENSHOTS_DIR / "ck6-entry-board-desktop.png"))
        pin.select_option("")

        # Thẻ tín hiệu SIG-3: hướng, SL, checklist và quyết định không vào lệnh gần nhất.
        signal = page.locator("[data-signal-card]")
        expect(signal).to_contain_text("BÁN")
        expect(signal.locator(".sc-level--stop")).to_contain_text("104")
        expect(signal.locator(".sc-check--fail").first).to_contain_text("ADX14")
        expect(signal).to_contain_text("Chưa hiệu chuẩn")

        # Bật RSI/MACD → hai pane phụ hiện; lựa chọn được nhớ sau khi tải lại.
        expect(page.locator("#market-chart-rsi")).to_be_hidden()
        page.locator('[data-layer="rsi"]').check()
        page.locator('[data-layer="macd"]').check()
        expect(page.locator("#market-chart-rsi canvas").first).to_be_visible()
        expect(page.locator("#market-chart-macd canvas").first).to_be_visible()
        page.reload(wait_until="networkidle")
        expect(page.locator('[data-layer="rsi"]')).to_be_checked()
        expect(page.locator("#market-chart-rsi")).to_be_visible()

        # Polling 5 giây gửi fingerprint; dữ liệu không đổi → máy chủ trả "unchanged".
        with page.expect_response(lambda r: "/admin/api/chart?" in r.url, timeout=9000) as polled:
            pass
        response = polled.value
        assert response.status == 200
        assert "fp=" in response.url
        assert "symbol=BTCUSDT" in response.url
        assert response.json()["status"] == "unchanged"

        for theme in ("light", "dark"):
            page.emulate_media(color_scheme=theme)
            page.wait_for_timeout(300)
            page.screenshot(path=str(SCREENSHOTS_DIR / f"sig7-market-chart-desktop-{theme}.png"))

        for width in (360, 390, 768, 1024, 1280):
            page.set_viewport_size({"width": width, "height": 860})
            page.wait_for_timeout(250)
            _assert_no_horizontal_overflow(page, f"{width}px")
            strip_box = page.locator("#mc-live-strip").bounding_box()
            assert strip_box is not None
            assert strip_box["x"] + strip_box["width"] <= width + 1, f"dải thông số @ {width}px"
            for selector in (".mc-timeframe", ".mc-layer", "#mc-bot"):
                height = page.locator(selector).first.bounding_box()["height"]
                assert height >= 43.5, f"{selector} @ {width}px cao {height}"
        page.screenshot(
            path=str(SCREENSHOTS_DIR / "sig7-market-chart-mobile-dark.png"), full_page=True
        )
        page.set_viewport_size({"width": 1280, "height": 900})
        page.locator("#mc-live-strip").screenshot(
            path=str(SCREENSHOTS_DIR / "ck1-live-strip-desktop.png")
        )
        page.locator("#mc-mtf-matrix").screenshot(
            path=str(SCREENSHOTS_DIR / "ck2-mtf-matrix-desktop.png")
        )

        # Mã không có nến ở khung chọn → chart vẫn mở, hiện ghi chú thay vì vẽ số liệu bịa.
        page.goto(f"{server_url}/admin/chart?symbol=ETHUSDT&timeframe=5m", wait_until="networkidle")
        expect(page.locator("[data-mc-notices]")).to_contain_text("Chưa có nến đã đóng")
        context.close()
        browser.close()

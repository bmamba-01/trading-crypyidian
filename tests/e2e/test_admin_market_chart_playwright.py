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
from trading_bot.domain.market.chart_quality import inspect_chart_bars
from trading_bot.domain.risk.protection_view import protection_view
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
        self.history_calls: list[dict[str, Any]] = []

    async def list_market_series(self, principal: Principal, *, bot_id: UUID) -> dict:
        return {"BTCUSDT": ["5m", "4h"], "ETHUSDT": ["1h"]}

    async def get_latest_signal(self, principal: Principal, *, bot_id: UUID) -> dict:
        return SIGNAL_VIEW

    async def get_market_bars(self, principal: Principal, **kwargs: Any) -> list[dict]:
        self.calls += 1
        return self.bars if kwargs["symbol"] == "BTCUSDT" else []

    async def get_decision_at(self, principal: Principal, **kwargs: Any) -> dict | None:
        return SIGNAL_VIEW["latest_decision"]

    async def validate_chart(self, principal: Principal, **kwargs: Any) -> dict:
        """Kiểm dữ liệu thật trên nến mẫu đã bị khoét 3 nến (acceptance CK-9)."""
        holed = [bar for index, bar in enumerate(self.bars) if index not in (100, 101, 102)]
        report = inspect_chart_bars(
            holed, interval_seconds=300, now=datetime.fromtimestamp(holed[-1]["time"] + 300, UTC)
        )
        return report | {
            "source": "BINANCE_USDM_REST_BACKFILL",
            "source_label": "Binance USDⓈ-M · REST công khai",
            "open_ingestion_gaps": [],
        }

    async def get_decision_log(self, principal: Principal, **kwargs: Any) -> dict:
        if kwargs.get("offset", 0) > 0:
            return {
                "decisions": [
                    {
                        "evaluated_at": "2026-11-16T09:00:00+00:00",
                        "status": "CANDIDATE",
                        "reason_code": "H01_LONG_ENTRY",
                        "detail_vi": "Điều kiện vào lệnh đạt.",
                        "direction": "long",
                        "strategy_release_id": "h01-v1",
                    }
                ],
                "unverified_decisions": 0,
                "has_more": False,
                "next_offset": None,
            }
        return {
            "decisions": [
                {
                    "evaluated_at": "2026-11-16T10:00:00+00:00",
                    "status": "ABSTAIN",
                    "reason_code": "H01_ADX_BELOW_20",
                    "detail_vi": "ADX14 1h phải đạt tối thiểu 20.",
                    "direction": None,
                    "strategy_release_id": "h01-v1",
                }
            ],
            "unverified_decisions": 1,
            "has_more": True,
            "next_offset": 20,
        }

    async def get_trade_history(self, principal: Principal, **kwargs: Any) -> list[dict]:
        self.history_calls.append(kwargs)
        return [
            {
                "trade_id": "t1",
                "symbol": kwargs["symbol"],
                "side": "LONG",
                "quote_asset": "USDT",
                "opened_at": "2026-11-16T08:00:00+00:00",
                "closed_at": "2026-11-16T09:00:00+00:00",
                "net_pnl": "-1.25",
                "total_cost": "0.20",
                "exit_reason": "STOP_LOSS",
            }
        ]

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
                    "verdict": "Chờ tín hiệu: 2/3 điều kiện bắt buộc",
                    "protection": protection_view(
                        open_positions=1,
                        max_positions=3,
                        net_today=Decimal("-10"),
                        net_24h=Decimal("-10"),
                        max_daily_loss=Decimal("50"),
                        max_rolling_loss=Decimal("100"),
                        quote_asset="USDT",
                    )
                    | {"note": None},
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
            positions=[
                {
                    "trade_id": "paper-1",
                    "symbol": kwargs["symbol"],
                    "side": "LONG",
                    "quote_asset": "USDT",
                    "entry_fills": [
                        {
                            "price": last_close - Decimal("1.5"),
                            "quantity": Decimal("2"),
                            "fee": Decimal("0.08"),
                            "occurred_at": OVERVIEW_NOW - timedelta(minutes=47),
                        }
                    ],
                    "initial_stop": last_close - Decimal("4"),
                    "current_stop": None,
                    "target": last_close + Decimal("3"),
                }
            ]
            if has_bars
            else [],
        )


class _Scanner:
    """Bộ quét giả dùng hàm quét thật trên nến mẫu 5m giả làm 1h (đủ 21 ngày nếu nhân 12)."""

    async def scan(self, principal: Principal, **kwargs: Any) -> dict:
        from trading_bot.domain.market.scanner import scan_markets

        hourly = [
            dict(bar, time=int(START.timestamp()) + index * 3600)
            for index, bar in enumerate(_bars())
        ]
        result = scan_markets(
            {"BTCUSDT": hourly, "ETHUSDT": hourly[-30:]},
            timeframe="1h",
            now=datetime.fromtimestamp(hourly[-1]["time"] + 3600, UTC),
            criteria=kwargs["criteria"],
        )
        return result | {"generated_at": START.isoformat(), "summary": "đạt 1/2 mã"}

    async def recent_runs(self, principal: Principal, **kwargs: Any) -> list[dict]:
        return []


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
    app.state.market_scanner_service = _Scanner()
    app.state.worker_readiness_provider = lambda: [
        {
            "component": "live_market_ingestion",
            "state": "ready",
            "last_tick_at": "2026-11-16T10:02:00+00:00",
            "metadata": {"bars_appended": 205, "reconnect_count": 2},
        },
        {
            "component": "live_market_events",
            "state": "ready",
            "last_tick_at": "2026-11-16T10:01:00+00:00",
            "metadata": {"events_written": 18, "reconnect_count": 1},
        },
    ]
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
        page.evaluate("() => document.documentElement.setAttribute('data-theme', 'dark')")
        expect(page.locator("#market-chart canvas").first).to_be_visible()
        expect(page.locator("[data-sb-stream]")).to_contain_text("đang chạy")
        expect(page.locator("[data-sb-stream]")).to_contain_text("205 nến")
        expect(page.locator("[data-sb-stream]")).to_contain_text("18 sự kiện")
        expect(page.locator("[data-sb-stream]")).to_contain_text("3 lần nối lại")
        metrics_box = page.locator("#mc-live-strip").bounding_box()
        filters_box = page.locator(".mc-toolbar").bounding_box()
        chart_card_box = page.locator(".mc-chart-card").bounding_box()
        assert metrics_box is not None
        assert filters_box is not None
        assert chart_card_box is not None
        assert metrics_box["y"] < filters_box["y"] < chart_card_box["y"]
        chart_box = page.locator("#market-chart").bounding_box()
        assert chart_box is not None
        assert chart_box["y"] < 800
        page.screenshot(path=str(SCREENSHOTS_DIR / "admin-filter-density-compact.png"))
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
        expect(events.first).to_contain_text(
            re.compile(r"BOS|CHoCH|Quét|FVG|OB"), use_inner_text=False
        )
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
        expect(strip.locator("[data-ov-candle-left]")).not_to_have_text(first_clock, timeout=5000)
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
        settings_button = page.locator("[data-chart-settings-open]")
        settings_dialog = page.locator("[data-chart-settings]")
        expect(settings_dialog).not_to_have_attribute("open")
        expect(settings_button).to_have_attribute("aria-expanded", "false")
        settings_button.click()
        expect(settings_dialog).to_be_visible()
        expect(settings_button).to_have_attribute("aria-expanded", "true")
        expect(page.locator("[data-chart-settings-close]")).to_be_focused()
        panel_background = page.locator("[data-chart-settings]").evaluate(
            "dialog => getComputedStyle(dialog).backgroundColor"
        )
        assert panel_background == "rgb(5, 10, 24)"
        page.screenshot(path=str(SCREENSHOTS_DIR / "admin-filter-density-panel.png"))
        expect(page.locator('[data-layer="ict_zones"]')).to_be_checked()
        expect(page.locator('[data-layer="killzones"]')).not_to_be_checked()
        page.locator('[data-layer="killzones"]').check()
        expect(strip.locator("[data-ov-killzones]")).not_to_be_empty()
        page.locator("#market-chart").screenshot(
            path=str(SCREENSHOTS_DIR / "ck3-ict-layer-desktop.png")
        )
        page.locator('[data-layer="killzones"]').uncheck()
        page.keyboard.press("Escape")
        expect(settings_dialog).not_to_be_visible()
        expect(settings_button).to_be_focused()

        # CK-4: TPO mặc định tắt; nPOC/VAH-VAL mặc định bật.
        settings_button.click()
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
        page.keyboard.press("Escape")

        # CK-6: bảng Entry — tiến độ k/n, còn thiếu, ghim kịch bản lên chart.
        board = page.locator("#mc-entry-board")
        first_setup = board.locator(".mc-setup").first
        detail_button = first_setup.get_by_role("button", name="Xem chi tiết Entry")
        expect(board).to_contain_text("Bắt buộc 2/3")
        expect(first_setup).not_to_contain_text("Còn thiếu: ADX14 1h đủ mạnh")
        # CK-12: giám sát bot — kết luận và chỗ chạy.
        expect(board.locator("[data-setup-verdict]")).to_have_text(
            "Chờ tín hiệu: 2/3 điều kiện bắt buộc"
        )
        expect(first_setup.locator("[data-monitor-slots]")).to_have_count(0)
        expect(board.locator("[data-setup-status]").first).to_have_attribute(
            "data-setup-status", "not_ready"
        )
        detail_button.focus()
        page.keyboard.press("Enter")
        dialog = page.locator('dialog[id^="mc-setup-detail-"]')
        expect(dialog).to_be_visible()
        expect(dialog.locator("[data-monitor-slots]")).to_have_text("1/3")
        expect(dialog.locator(".sc-checklist li")).to_have_count(6)
        expect(dialog).to_contain_text("Còn thiếu: ADX14 1h đủ mạnh")
        dialog.screenshot(path=str(SCREENSHOTS_DIR / "ck6-entry-board-details-desktop.png"))
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(detail_button).to_be_focused()
        pin = page.locator("[data-setup-pin]")
        expect(pin.locator("option")).to_have_count(2)
        pin.select_option("0")
        board.screenshot(path=str(SCREENSHOTS_DIR / "ck6-entry-board-desktop.png"))

        # Bộ phân trang dùng chung hiển thị 20/41 dòng đầu, sau đó dòng 21–40.
        page.evaluate("""() => {
          const table = document.createElement('table');
          const body = table.createTBody();
          body.id = 'pagination-e2e-body';
          for (let index = 1; index <= 41; index += 1) {
            const row = body.insertRow();
            row.insertCell().textContent = `Mục ${index}`;
          }
          document.body.append(table);
          window.crypidianPagination.refresh();
        }""")
        expect(page.get_by_role("row", name="Mục 1", exact=True)).to_be_visible()
        expect(page.get_by_role("row", name="Mục 20", exact=True)).to_be_visible()
        expect(page.get_by_role("row", name="Mục 21", exact=True)).to_be_hidden()
        pager = page.locator("nav.admin-pagination").last
        expect(pager).to_contain_text("1–20 / 41")
        pager.get_by_role("button", name="Trang sau").click()
        expect(page.get_by_role("row", name="Mục 20", exact=True)).to_be_hidden()
        expect(page.get_by_role("row", name="Mục 21", exact=True)).to_be_visible()
        expect(page.get_by_role("row", name="Mục 40", exact=True)).to_be_visible()
        expect(page.get_by_role("row", name="Mục 41", exact=True)).to_be_hidden()
        pin.select_option("")

        # CK-7: vị thế PAPER (UPnL theo mark), điều khiển, phím tắt, cỡ, phân tích tại nến.
        overview_json = page.evaluate(
            "() => JSON.parse(document.getElementById('market-overview-data').textContent)"
        )
        (position,) = overview_json["positions"]
        assert position["mark_source"] == "mark"
        assert position["upnl"] == pytest.approx(1.5 * 2)  # mark = giá đóng cuối, vào thấp 1,5
        assert position["r_multiple"] == pytest.approx(1.5 / 2.5)  # rủi ro/đơn vị = 2,5
        expect(page.locator('[data-layer="paper"]')).to_be_checked()
        chart = page.locator("#market-chart")
        for action in ("zoom-in", "zoom-out", "home", "reset", "live"):
            page.locator(f'[data-chart-action="{action}"]').click()
        chart.focus()
        for key in ("+", "-", "Home", "End", "0"):
            page.keyboard.press(key)
        page.locator("[data-chart-size]").select_option("s")
        expect(chart).to_have_class(re.compile(r"mc-canvas--s"))
        assert chart.bounding_box()["height"] == pytest.approx(320, abs=2)
        page.reload(wait_until="networkidle")
        expect(page.locator("[data-chart-size]")).to_have_value("s")
        page.locator("[data-chart-size]").select_option("m")
        box = chart.bounding_box()
        chart.click(button="right", position={"x": box["width"] * 0.6, "y": box["height"] / 2})
        analysis = page.locator("#mc-bar-analysis")
        expect(analysis).to_be_visible()
        expect(analysis).to_contain_text("Không vào lệnh · H01_ADX_BELOW_20")
        expect(analysis).to_contain_text("ADX14 1h đủ mạnh (không đạt)")
        page.locator("#market-chart").screenshot(
            path=str(SCREENSHOTS_DIR / "ck7-paper-position-desktop.png")
        )
        analysis.screenshot(path=str(SCREENSHOTS_DIR / "ck7-bar-analysis-desktop.png"))
        page.locator("[data-bar-analysis-close]").click()
        expect(analysis).to_be_hidden()

        # CK-8: công cụ vẽ — vẽ, lưu theo mã (time + giá), hoàn tác/làm lại, xóa, khóa, nam châm.
        page.evaluate("() => window.localStorage.removeItem('crypidian.chartDrawings.v1.BTCUSDT')")
        page.reload(wait_until="networkidle")
        box = chart.bounding_box()
        count = page.locator("[data-draw-count]")
        draw_tools = page.locator("[data-draw-tools]")
        expect(draw_tools).not_to_have_attribute("open", "")
        expect(count).to_have_text("0 hình vẽ")
        draw_tools.locator("summary").focus()
        page.keyboard.press("Enter")
        expect(draw_tools).to_have_attribute("open", "")
        page.locator('[data-draw-tool="trendline"]').click()
        chart.click(position={"x": box["width"] * 0.3, "y": box["height"] * 0.3})
        chart.click(position={"x": box["width"] * 0.6, "y": box["height"] * 0.5})
        expect(count).to_have_text("1 hình vẽ")
        stored = page.evaluate(
            "() => JSON.parse(localStorage.getItem('crypidian.chartDrawings.v1.BTCUSDT'))"
        )
        assert stored[0]["type"] == "trendline"
        assert all(p["time"] > 0 and p["price"] > 0 for p in stored[0]["points"])
        page.locator('[data-draw-action="undo"]').click()
        expect(count).to_have_text("0 hình vẽ")
        page.locator('[data-draw-action="redo"]').click()
        expect(count).to_have_text("1 hình vẽ")
        # Nam châm: điểm đường ngang bắt đúng một giá OHLC của nến.
        page.locator('[data-draw-toggle="magnet"]').click()
        page.locator('[data-draw-tool="hline"]').click()
        chart.click(position={"x": box["width"] * 0.5, "y": box["height"] * 0.4})
        expect(count).to_have_text("2 hình vẽ")
        snapped = page.evaluate(
            "() => JSON.parse(localStorage.getItem('crypidian.chartDrawings.v1.BTCUSDT'))[1]"
        )
        bar = next(b for b in _bars() if b["time"] == snapped["points"][0]["time"])
        assert snapped["points"][0]["price"] in {bar["open"], bar["high"], bar["low"], bar["close"]}
        page.locator('[data-draw-toggle="magnet"]').click()
        page.locator('[data-draw-tool="rect"]').click()
        chart.click(position={"x": box["width"] * 0.2, "y": box["height"] * 0.6})
        chart.click(position={"x": box["width"] * 0.35, "y": box["height"] * 0.75})
        page.locator('[data-draw-tool="fib"]').click()
        chart.click(position={"x": box["width"] * 0.4, "y": box["height"] * 0.2})
        chart.click(position={"x": box["width"] * 0.55, "y": box["height"] * 0.7})
        expect(count).to_have_text("4 hình vẽ")
        chart.screenshot(path=str(SCREENSHOTS_DIR / "ck8-drawings-desktop.png"))
        # Chọn hình chữ nhật bằng con trỏ rồi Delete.
        page.locator('[data-draw-tool="cursor"]').click()
        chart.click(position={"x": box["width"] * 0.27, "y": box["height"] * 0.68})
        page.keyboard.press("Delete")
        expect(count).to_have_text("3 hình vẽ")
        # Khóa: nhấp không tạo thêm hình.
        page.locator('[data-draw-toggle="lock"]').click()
        page.locator('[data-draw-tool="hline"]').click()
        chart.click(position={"x": box["width"] * 0.5, "y": box["height"] * 0.2})
        expect(count).to_have_text("3 hình vẽ")
        page.locator('[data-draw-toggle="lock"]').click()
        draw_tools.locator("summary").click()
        expect(draw_tools).not_to_have_attribute("open", "")
        expect(count).to_be_visible()
        page.reload(wait_until="networkidle")
        expect(page.locator("[data-draw-count]")).to_have_text("3 hình vẽ")
        expect(page.locator("[data-draw-tools]")).not_to_have_attribute("open", "")

        # CK-8: preset lớp và đếm "Lớp a/b".
        page.locator("[data-chart-settings-open]").click()
        total = page.locator("[data-layer]").count()
        page.locator("[data-layer-preset]").select_option("minimal")
        expect(page.locator("[data-layer-count]").first).to_have_text(f"4/{total}")
        expect(page.locator("[data-layer-count]").nth(1)).to_have_text(f"Lớp 4/{total}")
        page.locator("[data-layer-preset]").select_option("default")
        expect(page.locator('[data-layer="killzones"]')).not_to_be_checked()
        expect(page.locator('[data-layer="ict_zones"]')).to_be_checked()
        page.keyboard.press("Escape")

        # CK-8: nhiều khung — 2 khung → thêm khung 4H, có nến.
        page.locator("[data-multi-count]").select_option("2")
        cells = page.locator(".mc-multi-cell")
        expect(cells).to_have_count(1)
        expect(cells.first).to_have_attribute("data-timeframe", "4h")
        expect(cells.first).to_have_attribute("data-bars", re.compile(r"^[1-9]\d*$"))
        page.locator("[data-multi-grid]").screenshot(
            path=str(SCREENSHOTS_DIR / "ck8-multi-chart-desktop.png")
        )
        page.locator("[data-multi-count]").select_option("1")
        expect(cells).to_have_count(0)

        # CK-9: thanh trạng thái, kiểm dữ liệu (phát hiện lỗ 3 nến), dock 4 tab.
        statusbar = page.locator("[data-statusbar]")
        expect(statusbar.locator("[data-sb-bars]")).to_have_text("576")
        expect(statusbar.locator("[data-sb-quote]")).to_contain_text("trước")
        expect(statusbar.locator("[data-sb-layers]")).to_have_text(re.compile(r"^\d+$"))
        statusbar.locator("[data-validate]").click()
        result = page.locator("[data-validate-result]")
        expect(result).to_contain_text("thiếu 3 nến")
        expect(result).to_have_class(re.compile(r"is-bad"))
        dock = page.locator("[data-dock]")
        expect(dock.locator('[data-dock-count="positions"]')).to_have_text("1")
        expect(dock.locator("[data-dock-positions] tr")).to_have_count(1)
        expect(dock.locator("[data-dock-positions]")).to_contain_text("MUA")
        dock.locator('[data-dock-tab="history"]').click()
        expect(dock.locator("[data-dock-history]")).to_contain_text("Cắt lỗ")
        expect(dock.locator("[data-history-csv]")).to_have_attribute(
            "href", re.compile(r"^/admin/reports/export\.csv\?start=")
        )
        dock.locator('[data-dock-tab="history"]').press("ArrowRight")
        expect(dock.locator('[data-dock-tab="decisions"]')).to_have_attribute(
            "aria-selected", "true"
        )
        expect(dock.locator("[data-dock-decisions]")).to_contain_text("Không vào lệnh")
        expect(dock.locator("[data-dock-decisions]")).to_contain_text("không khớp hash")
        expect(dock.locator("[data-dock-decisions-more]")).to_be_visible()
        dock.locator("[data-dock-decisions-more]").click()
        expect(dock.locator("[data-dock-decisions]")).to_contain_text("Ra tín hiệu MUA")
        expect(dock.locator("[data-dock-decisions-more]")).to_be_hidden()
        dock.locator('[data-dock-tab="events"]').click()
        expect(dock.locator("[data-mc-events] li").first).to_be_visible()
        statusbar.screenshot(path=str(SCREENSHOTS_DIR / "ck9-statusbar-desktop.png"))
        dock.locator('[data-dock-tab="positions"]').click()
        dock.screenshot(path=str(SCREENSHOTS_DIR / "ck9-dock-desktop.png"))

        # CK-10: cột phân tích — nhận định có nguồn, mức giá cuộn chart, thống kê, AI chỉ đọc.
        insights = page.locator("[data-insights]")
        expect(insights.locator("[data-source-kind]").first).to_contain_text("Cấu trúc 5M")
        insights.locator('[data-insight-tab="narrative"]').press("ArrowRight")
        expect(insights.locator('[data-insight-tab="levels"]')).to_have_attribute(
            "aria-selected", "true"
        )
        level = insights.locator("[data-level-time]").first
        expect(level).to_be_visible()
        chart.focus()
        page.keyboard.press("End")
        level.click()
        expect(chart).to_be_focused()
        insights.locator('[data-insight-tab="calendar"]').click()
        expect(insights).to_contain_text("Không đọc được lịch sự kiện nội bộ.")
        insights.locator('[data-insight-tab="stats"]').click()
        expect(insights).to_contain_text("Số lệnh (90 ngày)")
        insights.locator('[data-insight-tab="ai"]').click()
        expect(insights.locator("[data-ai-status]")).to_contain_text("Chưa đọc được trạng thái")
        insights.locator('[data-insight-tab="narrative"]').click()
        page.wait_for_timeout(300)  # chờ hiệu ứng đổi viền tab (0,15 giây) trước khi chụp
        page.screenshot(path=str(SCREENSHOTS_DIR / "ck10-insights-desktop.png"))
        box_chart = chart.bounding_box()
        box_insights = insights.bounding_box()
        assert box_insights["x"] > box_chart["x"] + box_chart["width"] - 1  # cột phải ở 1280px

        # Thẻ tín hiệu SIG-3: hướng, SL, checklist và quyết định không vào lệnh gần nhất.
        signal = page.locator("[data-signal-card]")
        expect(signal).to_contain_text("BÁN")
        expect(signal.locator(".sc-level--stop")).to_contain_text("104")
        expect(signal.locator(".sc-check--fail").first).to_contain_text("ADX14")
        expect(signal).to_contain_text("Chưa hiệu chuẩn")

        # Bật RSI/MACD → hai pane phụ hiện; lựa chọn được nhớ sau khi tải lại.
        expect(page.locator("#market-chart-rsi")).to_be_hidden()
        page.locator("[data-chart-settings-open]").click()
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
            # CK-8: điện thoại chỉ xem bản vẽ (ẩn thanh công cụ vẽ).
            if width < 768:
                expect(page.locator("[data-draw-tools]")).to_be_hidden()
            else:
                expect(page.locator("[data-draw-tools]")).to_be_visible()
                expect(page.locator("[data-draw-toolbar]")).to_be_hidden()
            strip_box = page.locator("#mc-live-strip").bounding_box()
            assert strip_box is not None
            assert strip_box["x"] + strip_box["width"] <= width + 1, f"dải thông số @ {width}px"
            for selector in (".mc-timeframe", "#mc-bot"):
                box = page.locator(selector).first.bounding_box()
                assert box is not None
                height = box["height"]
                min_height = 43.5 if width <= 767 else 40
                assert height >= min_height, f"{selector} @ {width}px cao {height}"
            page.locator("[data-chart-settings-open]").click()
            _assert_no_horizontal_overflow(page, f"panel {width}px")
            layer_box = page.locator(".mc-layer").first.bounding_box()
            assert layer_box is not None
            assert layer_box["height"] >= 43.5
            page.keyboard.press("Escape")
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


def test_chart_stream_heartbeat_and_decision_log_pagination(server_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        _login(page, server_url)
        page.goto(f"{server_url}/admin/chart?timeframe=5m", wait_until="networkidle")

        expect(page.locator("#market-chart canvas").first).to_be_visible()
        stream = page.locator("[data-sb-stream]")
        expect(stream).to_contain_text("đang chạy")
        expect(stream).to_contain_text("205 nến")
        expect(stream).to_contain_text("18 sự kiện")
        expect(stream).to_contain_text("3 lần nối lại")

        dock = page.locator("[data-dock]")
        dock.locator('[data-dock-tab="decisions"]').click()
        expect(dock.locator("[data-dock-decisions]")).to_contain_text("Không vào lệnh")
        more = dock.locator("[data-dock-decisions-more]")
        expect(more).to_be_visible()
        more.click()
        expect(dock.locator("[data-dock-decisions]")).to_contain_text("Ra tín hiệu MUA")
        expect(more).to_be_hidden()
        browser.close()


def test_scanner_page_watchlist_is_saved_in_browser(server_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        watch_browser_violations(page)
        _login(page, server_url)
        page.goto(f"{server_url}/admin/scanner?timeframe=1h&min_volume=0", wait_until="networkidle")
        expect(page.locator('[data-scan-row="BTCUSDT"]').first).to_be_visible()
        # ETHUSDT thiếu 21 ngày: không vào bảng xếp hạng, có trong danh sách loại kèm lý do.
        expect(page.locator('[data-scan-row="ETHUSDT"]')).to_have_count(0)
        excluded = page.locator("details").filter(has=page.locator("#scan-excluded"))
        expect(excluded.locator("summary")).to_be_visible()
        excluded.locator("summary").click()
        expect(excluded).to_contain_text("ETHUSDT — Thiếu dữ liệu 21 ngày")
        # Watchlist được thu gọn mặc định để ưu tiên bảng kết quả.
        watchlist = page.locator("details").filter(has=page.locator("#scan-watch"))
        expect(watchlist).not_to_have_attribute("open", "")
        watchlist.locator("summary").click()
        # Tom Select: gõ để tìm rồi chọn BTCUSDT.
        page.locator(".ts-control input").first.fill("BTC")
        page.locator(".ts-dropdown .option[data-value='BTCUSDT']").click()
        rows = page.locator("[data-watchlist-rows] li")
        expect(rows).to_have_count(1)
        expect(rows.first).to_contain_text("BTCUSDT")
        expect(rows.first).to_contain_text("7 ngày")
        page.reload(wait_until="networkidle")
        expect(page.locator("[data-watchlist-rows] li").first).to_contain_text("BTCUSDT")
        # Chip của Tom Select cũng hiện mã đã lưu sau khi tải lại.
        expect(page.locator(".ts-control .item[data-value='BTCUSDT']")).to_have_count(1)
        page.locator(".mc-page").screenshot(path=str(SCREENSHOTS_DIR / "ck11-scanner-desktop.png"))
        page.set_viewport_size({"width": 390, "height": 860})
        page.wait_for_timeout(250)
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1, f"390px: tràn ngang {overflow}px"
        context.close()
        browser.close()

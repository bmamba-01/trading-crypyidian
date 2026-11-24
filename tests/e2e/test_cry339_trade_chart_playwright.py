"""Playwright E2E cho WP-07A coverage: data-quality, SL/TP, close_event và chạm mobile (CRY-339)."""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from tests.e2e.csp_harness import (
    browser_violations,  # noqa: F401 - fixture kiểm tra CSP/console
    watch_browser_violations,
)
from tests.e2e.test_admin_dashboard_playwright import BOT1_ID, WORKSPACE_ID, create_test_admin_app
from trading_bot.application.bot_reporting import (
    build_trade_chart_data_quality,
    extract_close_event_provenance,
)

TRADE_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


class _ChartCoverageReportingService:
    """Mock BotReportingService phục vụ báo cáo + biểu đồ trade có data_quality & close_event."""

    def __init__(self) -> None:
        self.opened = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
        self.closed = datetime(2026, 11, 1, 14, 0, tzinfo=UTC)
        self.trade: dict[str, Any] = {
            "trade_id": str(TRADE_ID),
            "bot_id": str(BOT1_ID),
            "symbol": "BTCUSDT",
            "side": "LONG",
            "strategy": "trend_follow",
            "timeframe": "1h",
            "opened_at": self.opened.isoformat(),
            "closed_at": self.closed.isoformat(),
            "exit_reason": "TAKE_PROFIT",
            "gross_pnl": "120.00",
            "net_pnl": "108.50",
            "fees": "9.00",
            "funding": "1.50",
            "slippage": "1.00",
            "quote_asset": "USDT",
            "entry_reason": {
                "strategy_id": "trend_follow",
                "detail_vi": "Đột phá đỉnh 20 nến kèm khối lượng tăng.",
            },
            "fills": [
                {
                    "role": "ENTRY",
                    "fill_id": "fill-entry-01",
                    "occurred_at": self.opened.isoformat(),
                    "price": "68000.00",
                    "quantity": "0.10",
                    "position_event_type": "OPENED",
                },
                {
                    "role": "EXIT",
                    "fill_key": "fill-exit-key-339",
                    "occurred_at": self.closed.isoformat(),
                    "price": "69200.00",
                    "quantity": "0.10",
                    "position_event_type": "CLOSED",
                    "bar_key": "BTCUSDT:1h:1793541600",
                },
            ],
            "lineage": {"provenance": "paper-ledger-v2"},
        }

    async def get_bot_report(self, query: Any, _principal: Any) -> Any:
        payload = {
            "report_version": "v2",
            "scope": "bot",
            "bot_id": str(BOT1_ID),
            "workspace_id": str(WORKSPACE_ID),
            "mode": "PAPER",
            "quote_asset": "USDT",
            "window": {"start": query.start.isoformat(), "end": query.end.isoformat()},
            "report_hash": "d" * 64,
            "totals": {
                "USDT": {
                    "gross_pnl": "120.00",
                    "net_pnl": "108.50",
                    "fees": "9.00",
                    "funding": "1.50",
                    "slippage": "1.00",
                    "trade_count": 1,
                    "winning_trades": 1,
                    "losing_trades": 0,
                    "win_rate": "1.0",
                    "max_drawdown": "0.00",
                    "quote_asset": "USDT",
                }
            },
            "trades": [self.trade],
            "open_trades": [],
        }

        class _R:
            def as_dict(self) -> dict[str, Any]:
                return payload

        return _R()

    async def get_workspace_report(self, query: Any, principal: Any) -> Any:
        return await self.get_bot_report(query, principal)

    async def get_trade_plan_summaries(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            str(TRADE_ID): {
                "entry_price": "68000.00",
                "stop_loss_price": "67200.00",
                "take_profit_price": "69200.00",
                "leverage": "5",
                "risk_budget_quote": "80.00",
            }
        }

    async def get_dashboard_reports(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def get_trade_chart(
        self, _query: Any, _principal: Any, *, trade_id: UUID, timeframe: str = "1h"
    ) -> dict[str, Any]:
        assert trade_id == TRADE_ID
        chart_start = self.opened - timedelta(hours=20)
        chart_end = self.closed + timedelta(hours=20)
        # Cố ý trả 6 nến (ít hơn 44 nến kỳ vọng) để kích hoạt cảnh báo data-quality thật.
        bars = [
            {
                "time": int((self.opened + timedelta(hours=idx - 1)).timestamp()),
                "open": 67900 + idx * 200,
                "high": 68150 + idx * 220,
                "low": 67800 + idx * 180,
                "close": 68050 + idx * 210,
                "volume": 120 + idx * 15,
            }
            for idx in range(6)
        ]
        return {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "timeframe": timeframe,
            "trade": self.trade,
            "bars": bars,
            "notice": None,
            "window": {
                "start": chart_start.isoformat(),
                "end": chart_end.isoformat(),
            },
            "data_quality": build_trade_chart_data_quality(
                bars=bars,
                chart_start=chart_start,
                chart_end=chart_end,
                timeframe=timeframe,
            ),
            "close_event": extract_close_event_provenance(self.trade),
        }


@pytest.fixture(scope="module")
def chart_server_url() -> Any:
    """Khởi chạy server Admin Web cho Playwright test CRY-339."""
    app = create_test_admin_app()
    app.state.bot_reporting_service = _ChartCoverageReportingService()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5.0)


def _login_session(page: Any, base_url: str) -> None:
    page.goto(f"{base_url}/admin/login", wait_until="networkidle")
    page.get_by_label("Mã không gian làm việc").fill("demo-workspace")
    page.get_by_label("Địa chỉ thư điện tử").fill("admin@crypidian.io")
    page.get_by_label("Mật khẩu").fill("correct_password")
    page.get_by_label("Mã xác thực 6 chữ số").fill("123456")
    page.get_by_role("button", name="Đăng nhập").click()
    page.wait_for_url(f"{base_url}/admin", timeout=10000)


def test_cry339_trade_chart_desktop_and_mobile_touch(
    chart_server_url: str,
    browser_violations: list[str],  # noqa: F811 - fixture pytest
) -> None:
    """Kiểm tra hiển thị SL/TP, window, cảnh báo data-quality, close_event và chạm mobile."""
    report_url = (
        f"{chart_server_url}/admin/reports"
        f"?scope=bot&bot_id={BOT1_ID}"
        f"&start=2026-11-01T00:00:00Z&end=2026-11-02T00:00:00Z"
        f"&chart_trade_id={TRADE_ID}&chart_timeframe=1h"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # 1. Desktop 1440x900
        desktop = browser.new_context(viewport={"width": 1440, "height": 900})
        page = desktop.new_page()
        watch_browser_violations(page)
        _login_session(page, chart_server_url)
        page.goto(report_url, wait_until="networkidle")

        expect(page.locator("[data-chart-window]")).to_be_visible()
        expect(page.locator("[data-chart-data-quality-warning]")).to_contain_text(
            "Thiếu dữ liệu nến 1h"
        )
        expect(page.locator("[data-close-exit-fill-key]")).to_have_text("fill-exit-key-339")
        expect(page.locator("[data-close-bar-key]")).to_have_text("BTCUSDT:1h:1793541600")
        expect(page.locator("[data-close-provenance]")).to_have_text("paper-ledger-v2")
        expect(page.locator("#trade-chart")).to_have_attribute("data-chart-ready", "true")

        overflow_desktop = page.evaluate(
            "document.documentElement.scrollWidth > window.innerWidth"
        )
        assert not overflow_desktop
        desktop.close()

        # 2. Mobile 390x844 có has_touch=True để kiểm tra thao tác chạm thực trên biểu đồ
        mobile = browser.new_context(
            viewport={"width": 390, "height": 844},
            has_touch=True,
            is_mobile=True,
        )
        m_page = mobile.new_page()
        watch_browser_violations(m_page)
        _login_session(m_page, chart_server_url)
        m_page.goto(report_url, wait_until="networkidle")

        chart_box = m_page.locator("#trade-chart")
        expect(chart_box).to_have_attribute("data-chart-ready", "true")
        chart_box.scroll_into_view_if_needed()
        box = chart_box.bounding_box()
        assert box is not None
        # Chạm thực vào vùng biểu đồ trên mobile viewport.
        m_page.touchscreen.tap(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
        expect(m_page.locator("#trade-chart-legend")).to_have_attribute(
            "data-legend-source", "crosshair"
        )

        overflow_mobile = m_page.evaluate(
            "document.documentElement.scrollWidth > window.innerWidth"
        )
        assert not overflow_mobile
        mobile.close()
        browser.close()

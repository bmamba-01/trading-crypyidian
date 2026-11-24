"""Playwright E2E cho trang /admin/reports: filter, phân rã, so sánh khoảng & mobile (CRY-371)."""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from tests.e2e.csp_harness import (
    browser_violations,  # noqa: F401 - fixture kiểm tra CSP/console
    watch_browser_violations,
)
from tests.e2e.test_admin_dashboard_playwright import BOT1_ID, WORKSPACE_ID, create_test_admin_app


class _RichReportingService:
    """Mock BotReportingService trả dữ liệu giao dịch đa symbol/side/strategy cho Playwright."""

    async def get_bot_report(self, query: Any, _principal: Any) -> Any:
        from tests.unit.test_cry371_report_filters_comparison import _sample_trades

        trades = _sample_trades()
        is_baseline = query.start.day <= 15
        selected = [trades[1]] if is_baseline else trades
        payload = {
            "report_version": "v2",
            "scope": "bot",
            "bot_id": str(BOT1_ID),
            "workspace_id": str(WORKSPACE_ID),
            "mode": "PAPER",
            "quote_asset": "USDT",
            "window": {"start": query.start.isoformat(), "end": query.end.isoformat()},
            "report_hash": "c" * 64,
            "totals": {
                "USDT": {
                    "gross_pnl": "82.00",
                    "net_pnl": "65.00",
                    "fees": "11.00",
                    "funding": "3.00",
                    "slippage": "3.00",
                    "trade_count": len(selected),
                    "winning_trades": 1,
                    "losing_trades": 1,
                    "win_rate": "0.5",
                    "max_drawdown": "-0.02",
                    "quote_asset": "USDT",
                }
            },
            "trades": selected,
        }

        class _R:
            def as_dict(self) -> dict[str, Any]:
                return payload

        return _R()

    async def get_workspace_report(self, query: Any, principal: Any) -> Any:
        return await self.get_bot_report(query, principal)

    async def get_trade_plan_summaries(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}


@pytest.fixture(scope="module")
def reports_server_url() -> Any:
    """Khởi chạy server Admin Web cho Playwright test WP-07D."""
    app = create_test_admin_app()
    app.state.bot_reporting_service = _RichReportingService()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(1.0)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def _login_admin(page: Any, server_url: str) -> None:
    page.goto(f"{server_url}/admin/login", wait_until="networkidle")
    page.get_by_label("Mã không gian làm việc").fill("demo-workspace")
    page.get_by_label("Địa chỉ thư điện tử").fill("admin@crypidian.io")
    page.get_by_label("Mật khẩu").fill("correct_password")
    page.get_by_label("Mã xác thực 6 chữ số").fill("123456")
    page.get_by_role("button", name="Đăng nhập").click()
    page.wait_for_url(f"{server_url}/admin", timeout=10000)


@pytest.mark.parametrize(
    ("width", "height"),
    [
        (1440, 900),
        (390, 844),
        (360, 780),
    ],
)
def test_reports_filters_breakdown_and_comparison_desktop_mobile(
    reports_server_url: str,
    width: int,
    height: int,
) -> None:
    """Kiểm tra /admin/reports lọc, phân rã, so sánh 2 khoảng và không tràn ngang trên mobile."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height})
        page = context.new_page()
        watch_browser_violations(page)

        _login_admin(page, reports_server_url)
        url = (
            f"{reports_server_url}/admin/reports"
            f"?scope=bot&bot_id={BOT1_ID}"
            "&start=2026-11-20T00:00:00Z&end=2026-11-25T00:00:00Z"
            "&symbol=BTCUSDT&side=LONG"
            "&compare_start=2026-11-01T00:00:00Z&compare_end=2026-11-10T00:00:00Z"
        )
        page.goto(url, wait_until="networkidle")

        expect(page.locator("#report-comparison-heading")).to_be_visible()
        expect(page.locator("#report-breakdown-heading")).to_be_visible()
        expect(page.locator(".report-head__meta")).to_contain_text(
            "Lưu UTC · Hiển thị Asia/Ho_Chi_Minh"
        )
        expect(page.locator(".report-head__meta")).to_contain_text("Mã băm sau lọc")
        csv_href = page.locator('[data-testid="export-reports-csv"]').get_attribute("href") or ""
        assert "symbol=BTCUSDT" in csv_href
        assert "side=LONG" in csv_href

        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, height)
        context.close()
        browser.close()

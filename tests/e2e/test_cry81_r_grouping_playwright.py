"""Playwright E2E cho bảng hậu kiểm R theo nhãn MI-7 trên /admin/reports (CRY-81)."""

from __future__ import annotations

import socket
import threading
import time
from typing import Any
from uuid import uuid4

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from tests.e2e.csp_harness import (
    browser_violations,  # noqa: F401 - fixture kiểm tra CSP/console
    watch_browser_violations,
)
from tests.e2e.test_admin_dashboard_playwright import BOT1_ID, WORKSPACE_ID, create_test_admin_app
from tests.e2e.test_cry371_reports_filters_playwright import _login_admin


class _LabeledReportingService:
    """Trả trade có nhãn AI (lý do vào, chế độ) và 1R để tính R-multiple thật."""

    async def get_bot_report(self, query: Any, _principal: Any) -> Any:
        from tests.unit.test_cry371_report_filters_comparison import _sample_trades

        trades = _sample_trades()[:2]  # 2 lệnh: win rate 0.5, không làm tròn Inexact
        for index, trade in enumerate(trades):
            win = index % 2 == 0
            trade["trade_id"] = str(uuid4())
            trade["strategy_release_id"] = "H03"
            trade["net_pnl"] = "20" if win else "-5"
            trade["exit_reason"] = "TAKE_PROFIT" if win else "STOP_LOSS"
            trade["entry_reason"] = {
                "strategy_id": "H03",
                "reason_code": "DONCHIAN_BREAKOUT",
                "context_regime": "TRENDING" if win else "CHOP",
                "estimated_loss": "10",
            }
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
                    "gross_pnl": "30.00",
                    "net_pnl": "30.00",
                    "fees": "0.00",
                    "funding": "0.00",
                    "slippage": "0.00",
                    "trade_count": len(trades),
                    "winning_trades": 1,
                    "losing_trades": 1,
                    "win_rate": "0.5",
                    "max_drawdown": "-0.02",
                    "quote_asset": "USDT",
                }
            },
            "trades": trades,
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
def r_grouping_server_url() -> Any:
    app = create_test_admin_app(reporting_service=_LabeledReportingService())
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
    thread.join(timeout=5)


@pytest.mark.parametrize(("width", "height"), [(1440, 900), (390, 844), (360, 780)])
def test_r_multiple_by_labels_desktop_mobile(
    r_grouping_server_url: str, width: int, height: int
) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height})
        page = context.new_page()
        watch_browser_violations(page)

        _login_admin(page, r_grouping_server_url)
        page.goto(
            f"{r_grouping_server_url}/admin/reports"
            f"?scope=bot&bot_id={BOT1_ID}"
            "&start=2026-11-20T00:00:00Z&end=2026-11-25T00:00:00Z",
            wait_until="networkidle",
        )
        expect(page.locator("#r-multiple-heading")).to_be_visible()
        rows = page.locator('[data-testid="r-dimension-row"]')
        assert rows.count() >= 2
        expect(page.locator("#r-multiple-heading + .report-card__hint")).to_contain_text(
            "1R đã duyệt"
        )
        body = page.locator(".report-breakdown:has(#r-multiple-heading) tbody")
        expect(body).to_contain_text("DONCHIAN_BREAKOUT")
        expect(body).to_contain_text("TRENDING")
        expect(body).to_contain_text("2")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, height)
        context.close()
        browser.close()

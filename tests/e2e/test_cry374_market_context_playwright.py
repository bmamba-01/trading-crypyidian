"""Playwright E2E cho thẻ bối cảnh thị trường trên /admin/reports (CRY-374, desktop + mobile)."""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from tests.e2e.test_cry371_reports_filters_playwright import _login_admin, _RichReportingService
from trading_bot.admin.bot_reports_router import router as bot_reports_router
from trading_bot.domain.portfolio.market_context import (
    MarketContextKind,
    MarketContextObservation,
    build_market_context_report,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class _FakeMarketContextService:
    """Trả report mẫu theo symbol: 'DENIED' mô phỏng thiếu quyền DISPLAY."""

    async def get_report(self, *, workspace_id: UUID, symbol: str) -> Any:
        assert workspace_id == WORKSPACE_ID
        if symbol == "DENIED":
            return build_market_context_report(
                workspace_id=str(workspace_id),
                symbol=symbol,
                observations=(),
                purpose=SourceUsePurpose.TRAINING,
                now=NOW,
            )
        observation = MarketContextObservation(
            kind=MarketContextKind.FUNDING,
            symbol=symbol,
            source="BINANCE_USDM",
            checksum="f" * 64,
            event_at=NOW - timedelta(minutes=5),
            available_at=NOW - timedelta(minutes=4),
            value=Decimal("0.0001"),
            unit="ratio",
            revision=2,
            raw_reference="BINANCE_USDM:funding:1",
        )
        return build_market_context_report(
            workspace_id=str(workspace_id),
            symbol=symbol,
            observations=(observation,),
            purpose=SourceUsePurpose.DISPLAY,
            now=NOW,
        )


@pytest.fixture(scope="module")
def market_context_server_url() -> Any:
    app = create_test_admin_app(reporting_service=_RichReportingService())
    app.state.market_context_service = _FakeMarketContextService()
    app.include_router(bot_reports_router)

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


@pytest.mark.parametrize(
    ("width", "height"),
    [(1440, 900), (390, 844), (360, 780)],
)
def test_market_context_card_desktop_mobile(
    market_context_server_url: str,
    width: int,
    height: int,
) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height})
        page = context.new_page()
        watch_browser_violations(page)

        _login_admin(page, market_context_server_url)
        page.goto(
            f"{market_context_server_url}/admin/reports"
            f"?scope=bot&bot_id={BOT1_ID}"
            "&start=2026-11-20T00:00:00Z&end=2026-11-25T00:00:00Z&symbol=BTCUSDT",
            wait_until="networkidle",
        )
        expect(page.locator("#market-context-heading")).to_be_visible()
        page.locator("#market-context-symbol").fill("BTCUSDT")
        page.locator("#market-context-form button[type=submit]").click()

        result = page.locator("#market-context-result")
        expect(result).to_have_attribute("data-market-context-loaded", "1")
        expect(result).to_have_attribute("data-display-allowed", "1")
        expect(result).to_contain_text("quyền hiển thị: có")
        expect(result).to_contain_text("Funding")
        expect(result).to_contain_text("ffff")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, height)

        # Từ chối quyền: chỉ hiển thị mã cảnh báo, không lộ checksum/nguồn/giá trị.
        page.locator("#market-context-symbol").fill("DENIED")
        page.locator("#market-context-form button[type=submit]").click()
        expect(result).to_have_attribute("data-display-allowed", "0")
        expect(result).to_contain_text("Nguồn chưa được cấp quyền hiển thị bối cảnh")
        assert "ffff" not in (result.text_content() or "")
        assert "BINANCE_USDM" not in (result.text_content() or "")
        context.close()
        browser.close()

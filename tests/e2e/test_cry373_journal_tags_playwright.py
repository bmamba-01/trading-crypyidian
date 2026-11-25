"""Playwright E2E cho WP-07D Journal, Checklist, Tag Setup & Mistake Attribution (CRY-373)."""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from tests.e2e.csp_harness import (
    browser_violations,  # noqa: F401 - fixture pytest
    watch_browser_violations,
)
from tests.e2e.test_admin_dashboard_playwright import BOT1_ID, WORKSPACE_ID, create_test_admin_app
from trading_bot.admin.trade_journal_router import (
    html_router as trade_journal_html_router,
)
from trading_bot.admin.trade_journal_router import (
    router as trade_journal_router,
)
from trading_bot.domain.portfolio.trade_journal import (
    compute_mistake_attribution,
    compute_setup_tag_breakdown,
)

TRADE_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class _InMemoryJournalService:
    """Dịch vụ nhật ký in-memory phục vụ kiểm thử E2E luồng /admin/reports -> POST -> DB -> UI."""

    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.audits: dict[str, list[dict[str, Any]]] = {}

    async def get_trade_journal(
        self, _principal: Any, *, trade_id: UUID, bot_id: UUID | None = None
    ) -> dict[str, Any] | None:
        _ = bot_id
        entry = self.entries.get(str(trade_id))
        if entry is None:
            return None
        return {
            **entry,
            "attachments": [],
            "audit_events": list(reversed(self.audits.get(str(trade_id), []))),
        }

    async def save_trade_journal(
        self,
        principal: Any,
        *,
        bot_id: UUID,
        trade_id: UUID,
        note_vi: str | None = "",
        checklist_items: Any = None,
        setup_tags: Any = None,
        mistake_tags: Any = None,
        privacy_mode: str = "workspace_team",
        retention_days: int | None = 365,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        tid = str(trade_id)
        action = "UPDATED" if tid in self.entries else "CREATED"
        entry = {
            "id": self.entries.get(tid, {}).get("id", str(uuid4())),
            "workspace_id": str(principal.workspace_id),
            "bot_id": str(bot_id),
            "trade_id": tid,
            "author_user_id": str(principal.user_id),
            "note_vi": (note_vi or "").strip(),
            "checklist_items": list(checklist_items or []),
            "setup_tags": list(setup_tags or []),
            "mistake_tags": list(mistake_tags or []),
            "privacy_mode": privacy_mode,
            "retention_days": int(retention_days or 365),
            "expires_at": (now + timedelta(days=int(retention_days or 365))).isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        self.entries[tid] = entry
        self.audits.setdefault(tid, []).append({"action": action, "created_at": now.isoformat()})
        return entry

    async def build_attribution_summary(
        self, _principal: Any, *, trades: Any, bot_id: UUID | None = None
    ) -> dict[str, Any]:
        _ = bot_id
        return {
            "mistake_attribution": compute_mistake_attribution(trades, self.entries),
            "setup_tag_breakdown": compute_setup_tag_breakdown(trades, self.entries),
            "journaled_trade_count": len(self.entries),
        }


class _JournalReportingService:
    """Mock BotReportingService có 1 lệnh lỗ để kiểm tra Mistake Attribution."""

    def __init__(self) -> None:
        self.opened = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
        self.closed = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
        self.trade: dict[str, Any] = {
            "trade_id": str(TRADE_ID),
            "bot_id": str(BOT1_ID),
            "symbol": "ETHUSDT",
            "side": "LONG",
            "strategy": "trend_follow",
            "timeframe": "1h",
            "opened_at": self.opened.isoformat(),
            "closed_at": self.closed.isoformat(),
            "exit_reason": "STOP_LOSS",
            "gross_pnl": "-45.00",
            "net_pnl": "-50.00",
            "fees": "3.00",
            "funding": "1.00",
            "slippage": "1.00",
            "quote_asset": "USDT",
            "fills": [
                {
                    "role": "ENTRY",
                    "fill_id": "entry-eth-1",
                    "occurred_at": self.opened.isoformat(),
                    "price": "3200.00",
                    "quantity": "0.50",
                    "position_event_type": "OPENED",
                },
                {
                    "role": "EXIT",
                    "fill_key": "exit-eth-1",
                    "occurred_at": self.closed.isoformat(),
                    "price": "3110.00",
                    "quantity": "0.50",
                    "position_event_type": "CLOSED",
                },
            ],
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
            "report_hash": "e" * 64,
            "totals": {
                "USDT": {
                    "gross_pnl": "-45.00",
                    "net_pnl": "-50.00",
                    "fees": "3.00",
                    "funding": "1.00",
                    "slippage": "1.00",
                    "trade_count": 1,
                    "winning_trades": 0,
                    "losing_trades": 1,
                    "win_rate": "0.0",
                    "max_drawdown": "-0.05",
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
        return {}

    async def get_dashboard_reports(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def get_trade_chart(
        self, _query: Any, _principal: Any, *, trade_id: UUID, timeframe: str = "1h"
    ) -> dict[str, Any]:
        assert trade_id == TRADE_ID
        bars = [
            {
                "time": int((self.opened + timedelta(hours=idx)).timestamp()),
                "open": 3200 - idx * 30,
                "high": 3210 - idx * 25,
                "low": 3100,
                "close": 3110 + (1 - idx) * 40,
            }
            for idx in range(3)
        ]
        return {
            "symbol": "ETHUSDT",
            "side": "LONG",
            "timeframe": timeframe,
            "trade": self.trade,
            "bars": bars,
            "notice": None,
        }


@pytest.fixture(scope="module")
def journal_server_url() -> Any:
    """Khởi chạy server Admin Web cho Playwright test CRY-373."""
    app = create_test_admin_app()
    app.include_router(trade_journal_router)
    app.include_router(trade_journal_html_router)
    app.state.bot_reporting_service = _JournalReportingService()
    app.state.trade_journal_service = _InMemoryJournalService()

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


def _login_admin(page: Any, server_url: str) -> None:
    page.goto(f"{server_url}/admin/login", wait_until="networkidle")
    page.get_by_label("Mã không gian làm việc").fill("demo-workspace")
    page.get_by_label("Địa chỉ thư điện tử").fill("admin@crypidian.io")
    page.get_by_label("Mật khẩu").fill("correct_password")
    page.get_by_label("Mã xác thực 6 chữ số").fill("123456")
    page.get_by_role("button", name="Đăng nhập").click()
    page.wait_for_url(f"{server_url}/admin", timeout=10000)


def test_cry373_journal_tags_and_mistake_attribution_desktop_mobile(
    journal_server_url: str,
    browser_violations: list[str],  # noqa: F811 - fixture pytest
) -> None:
    """Lưu journal + tags trên /admin/reports, kiểm Mistake Attribution & responsive mobile."""
    report_url = (
        f"{journal_server_url}/admin/reports"
        f"?scope=bot&bot_id={BOT1_ID}"
        f"&start=2026-11-01T00:00:00Z&end=2026-11-03T00:00:00Z"
        f"&chart_trade_id={TRADE_ID}&chart_timeframe=1h"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)

        # 1. Desktop 1440x900: điền form lưu journal và xác minh Mistake Attribution cập nhật
        desktop = browser.new_context(viewport={"width": 1440, "height": 900})
        page = desktop.new_page()
        watch_browser_violations(page)
        _login_admin(page, journal_server_url)
        page.goto(report_url, wait_until="networkidle")

        panel = page.locator("#trade-journal-panel")
        expect(panel).to_be_visible()
        panel.locator('textarea[name="note_vi"]').fill("Vào lệnh sớm khi chưa đóng nến xác nhận.")
        panel.locator('input[name="setup_tags"]').fill("breakout_20")
        panel.locator('input[name="mistake_tags"]').fill("fomo_entry")
        panel.locator('textarea[name="checklist_lines"]').fill(
            "[x] Đúng xu hướng\n[ ] Chờ nến đóng"
        )
        panel.locator('select[name="privacy_mode"]').select_option("workspace_team")
        panel.locator('input[name="retention_days"]').fill("180")
        panel.get_by_role("button", name="Lưu Nhật Ký Giao Dịch").click()
        page.wait_for_load_state("networkidle")

        mistake_row = page.locator('[data-mistake-row="fomo_entry"]').first
        expect(mistake_row).to_be_visible()
        expect(mistake_row).to_contain_text("50.00 USDT")
        expect(mistake_row).to_contain_text("100.00%")
        expect(page.locator('[data-setup-row="breakout_20"]').first).to_contain_text("50.00%")
        expect(page.locator(".trade-journal-audit").first).to_contain_text("CREATED")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        desktop.close()

        # 2. Mobile 390x844: kiểm tra hiển thị responsive không tràn ngang
        mobile = browser.new_context(viewport={"width": 390, "height": 844})
        m_page = mobile.new_page()
        watch_browser_violations(m_page)
        _login_admin(m_page, journal_server_url)
        m_page.goto(report_url, wait_until="networkidle")
        expect(m_page.locator("#report-mistake-attribution").first).to_be_visible()
        expect(m_page.locator("#trade-journal-panel").first).to_be_visible()
        assert m_page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        mobile.close()
        browser.close()

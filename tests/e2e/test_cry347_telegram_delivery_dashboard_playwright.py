"""Playwright E2E cho dashboard trạng thái liên kết, lịch sử delivery và test message (CRY-347)."""

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
from tests.e2e.test_admin_dashboard_playwright import create_test_admin_app


@pytest.fixture(scope="module")
def tg_dashboard_server_url() -> Any:
    """Khởi chạy server Admin Web có route /admin/telegram và API delivery mock cho Playwright."""
    app = create_test_admin_app()

    # Gắn dữ liệu mock cho /admin/api/telegram/deliveries và /admin/api/telegram/test-message
    # khi không kết nối PostgreSQL trong Playwright test nhẹ.
    @app.get("/admin/api/telegram/deliveries")
    async def _mock_deliveries(bot_id: str | None = None) -> dict[str, Any]:
        all_rows = [
            {
                "delivery_id": "del-1",
                "bot_id": "bot-1",
                "bot_name": "Binance BTC Breakout H03",
                "decision_id": "dec-1",
                "status": "SENT",
                "status_vi": "Đã gửi",
                "attempts": 1,
                "last_error_code": None,
                "created_at": "2026-11-24T12:00:00+00:00",
                "sent_at": "2026-11-24T12:00:02+00:00",
            },
            {
                "delivery_id": "del-2",
                "bot_id": "bot-2",
                "bot_name": "Binance ETH Trend H01",
                "decision_id": "dec-2",
                "status": "DEAD",
                "status_vi": "Lỗi hẳn (Dead-letter)",
                "attempts": 1,
                "last_error_code": "TELEGRAM_HTTP_403",
                "created_at": "2026-11-24T11:50:00+00:00",
                "sent_at": None,
            },
        ]
        rows = [r for r in all_rows if not bot_id or r["bot_id"] == bot_id]
        return {
            "bots": [
                {"bot_id": "bot-1", "bot_name": "Binance BTC Breakout H03"},
                {"bot_id": "bot-2", "bot_name": "Binance ETH Trend H01"},
            ],
            "summary": {
                "total": len(rows),
                "sent_count": sum(1 for r in rows if r["status"] == "SENT"),
                "pending_count": 0,
                "dead_count": sum(1 for r in rows if r["status"] == "DEAD"),
                "expired_count": 0,
                "filtered_count": 1,
                "suppressed_count": 1,
                "last_sent_at": "2026-11-24T12:00:02+00:00",
                "last_error_code": "TELEGRAM_HTTP_403",
            },
            "subscriptions": [],
            "deliveries": rows,
        }

    # Đẩy 2 mock route lên trước router chính để không bị 503 khi session_factory=None
    app.router.routes.insert(0, app.router.routes.pop())

    @app.post("/admin/api/telegram/test-message")
    async def _mock_test_message() -> dict[str, Any]:
        return {
            "sent": True,
            "binding_id": "bind-1",
            "sent_at": "2026-11-24T13:20:00+00:00",
            "message_vi": "Đã gửi tin nhắn thử Telegram thành công (không tạo tín hiệu hay lệnh).",
        }

    app.router.routes.insert(0, app.router.routes.pop())

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
def test_telegram_delivery_dashboard_desktop_and_mobile(
    tg_dashboard_server_url: str,
    width: int,
    height: int,
) -> None:
    """Kiểm tra /admin/telegram hiển thị lịch sử delivery, lọc bot, test message, không tràn."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height})
        page = context.new_page()
        watch_browser_violations(page)

        _login_admin(page, tg_dashboard_server_url)
        page.goto(f"{tg_dashboard_server_url}/admin/telegram", wait_until="networkidle")

        panel = page.locator("#tg-delivery-panel")
        panel.scroll_into_view_if_needed()
        expect(panel).to_be_visible()
        expect(panel.locator(".tg-kpi-card")).to_have_count(6, timeout=10000)
        expect(panel).to_contain_text("Đã gửi (SENT)")
        expect(panel).to_contain_text("TELEGRAM_HTTP_403")
        expect(panel.locator("tbody tr")).to_have_count(2)

        # Lọc theo bot-1
        page.locator("#tg-delivery-bot-filter").select_option("bot-1")
        expect(panel.locator("tbody tr")).to_have_count(1, timeout=10000)

        # Bấm nút gửi tin nhắn thử
        page.locator("#tg-test-message-btn").click()
        expect(page.locator("#tg-test-message-status")).to_contain_text(
            "Đã gửi tin nhắn thử Telegram thành công", timeout=10000
        )

        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, height)
        context.close()
        browser.close()

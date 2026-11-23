"""Playwright E2E cho panel chỉ số WP-07E và cảnh báo tiếng Việt trên Desktop & Mobile."""

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
def wp07e_server_url() -> Any:
    """Khởi chạy server Admin Web cho kịch bản Playwright WP-07E."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app = create_test_admin_app()
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
def test_workbench_metrics_panel_desktop_and_mobile(
    wp07e_server_url: str,
    width: int,
    height: int,
) -> None:
    """Kiểm tra panel WP-07E tải API thật, làm mới được và không tràn ngang trên mobile."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height})
        page = context.new_page()
        watch_browser_violations(page)

        _login_admin(page, wp07e_server_url)
        page.locator("#tab-controls").click()
        panel = page.locator("#workbench-metrics-panel")
        panel.scroll_into_view_if_needed()
        expect(panel).to_be_visible()
        expect(panel.locator(".workbench-kpi-card")).to_have_count(6, timeout=10000)
        expect(panel).to_contain_text("Cảnh báo đang mở")
        expect(panel).to_contain_text("Latency gửi Telegram (p95)")

        with page.expect_response(
            lambda r: "/admin/api/workbench/metrics-alerts" in r.url and r.status == 200
        ):
            page.locator("#workbench-metrics-refresh").click()

        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, height)
        context.close()
        browser.close()

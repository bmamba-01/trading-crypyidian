"""Playwright E2E cho CRY-414: hiển thị báo cáo kiểm định SIG-5 từ DB trên /admin/backtests."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from tests.e2e.csp_harness import (
    browser_violations,  # noqa: F401 - fixture pytest
    watch_browser_violations,
)
from tests.e2e.test_admin_dashboard_playwright import WORKSPACE_ID, create_test_admin_app
from tests.unit.test_cry414_scalping_validation_report_db import _sample_report


class _StubBacktestService:
    async def list(self, *_args: Any, **_kwargs: Any) -> Sequence[Any]:
        return ()

    async def list_bundles(self, *_args: Any, **_kwargs: Any) -> Sequence[Any]:
        return ()


class _StubScalpingValidationService:
    async def list_reports(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        report = _sample_report()
        return [
            {
                "id": str(uuid4()),
                "workspace_id": str(WORKSPACE_ID),
                "release_id": report["release_id"],
                "release_content_hash": report["release_content_hash"],
                "symbol": report["symbol"],
                "costs_version": report["costs_version"],
                "evaluation_start": report["evaluation_start"],
                "range_end": report["range_end"],
                "recommend_paper": True,
                "out_of_sample_n": 120,
                "stress_expectancy_r": "0.14",
                "validation_hash": report["validation_hash"],
                "report_payload": report,
                "created_at": "2026-11-24T12:00:00+00:00",
            }
        ]


@pytest.fixture(scope="module")
def backtest_server_url() -> Any:
    """Khởi chạy Admin Web với stub báo cáo SIG-5 cho Playwright test CRY-414."""
    from trading_bot.admin.backtest_replay_router import router as backtest_replay_router
    from trading_bot.config.settings import AppSettings

    prev_env_file = AppSettings.model_config.get("env_file")
    AppSettings.model_config["env_file"] = None
    try:
        app = create_test_admin_app()
    finally:
        AppSettings.model_config["env_file"] = prev_env_file
    app.include_router(backtest_replay_router)
    app.state.backtest_replay_job_service = _StubBacktestService()
    app.state.scalping_validation_report_service = _StubScalpingValidationService()

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


@pytest.mark.parametrize(("width", "height"), [(1440, 900), (390, 844)])
def test_cry414_sig5_validation_reports_visible_desktop_mobile(
    backtest_server_url: str,
    browser_violations: list[str],  # noqa: F811 - fixture pytest
    width: int,
    height: int,
) -> None:
    """Kiểm tra bảng Báo Cáo Kiểm Định Scalping (SIG-5) hiển thị chuẩn và không tràn ngang."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height})
        page = context.new_page()
        watch_browser_violations(page)
        _login_admin(page, backtest_server_url)
        page.goto(f"{backtest_server_url}/admin/backtests", wait_until="networkidle")

        section = page.locator("#sig5-validation-reports")
        expect(section).to_be_visible()
        expect(section).to_contain_text("h18-v1")
        expect(section).to_contain_text("BTCUSDT")
        expect(section).to_contain_text("120")
        expect(section).to_contain_text("0.14")
        expect(section).to_contain_text("ĐẠT")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, height)
        context.close()
        browser.close()

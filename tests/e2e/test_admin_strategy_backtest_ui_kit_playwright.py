"""Playwright (CSP thật) cho UI kit v1 ở nhóm trang Chiến lược và Backtest.

Kiểm:
- Không tràn ngang, điều khiển ≥ 44px ở 360/768/1280px, cả giao diện sáng và tối.
- Popup giải thích tầng L0–L6 và popup chi tiết thẻ mở bằng nút, đóng bằng nút đóng.
- Microscope dựng tầng L3 từ vết thật (hồi quy lỗi Jinja `set` trong vòng lặp luôn báo
  "không dùng L3"), số hiển thị 2 chữ số kiểu Việt Nam và từ chối nhập liệu sai ở trình duyệt.
- Backtest: một bản DOM cho mỗi lượt chạy, mã rút gọn có nút sao chép, gợi ý phần trăm.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from tests.e2e import test_admin_backtest_replays_playwright as backtest_module
from tests.e2e import test_admin_microscope_playwright as microscope_module
from tests.e2e.csp_harness import (
    browser_violations,  # noqa: F401 - fixture nạp qua usefixtures
    watch_browser_violations,
)

pytestmark = pytest.mark.usefixtures("browser_violations")

MICROSCOPE_COOKIE = f"{microscope_module.WORKSPACE_ID}:valid_test_token_microscope"
BACKTEST_COOKIE = f"{backtest_module.WORKSPACE_ID}:valid_test_token_backtest_replays"


def _serve(app: Any) -> Iterator[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=3)


@pytest.fixture(scope="module")
def microscope_url() -> Iterator[str]:
    """App thẻ chiến lược + microscope đã có header CSP thật."""
    yield from _serve(microscope_module.create_test_microscope_app())


@pytest.fixture(scope="module")
def backtest_url() -> Iterator[str]:
    """App backtest (đã gắn header CSP thật trong factory)."""
    yield from _serve(backtest_module.create_test_backtest_app())


def _open(base: str, path: str, cookie: str, *, width: int, scheme: str) -> tuple[Any, Page]:
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
    context.add_cookies(
        [{"name": "crypidian_session", "value": cookie, "domain": "127.0.0.1", "path": "/"}]
    )
    page = context.new_page()
    watch_browser_violations(page)
    page.goto(f"{base}{path}", wait_until="networkidle")
    return (playwright, browser), page


def _close(handles: tuple[Any, Any]) -> None:
    playwright, browser = handles
    browser.close()
    playwright.stop()


def _assert_layout(page: Page, width: int) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - innerWidth")
    assert overflow <= 0, (width, overflow)
    small = page.evaluate(
        """[...document.querySelectorAll(
              'main input:not([type=hidden]), main select, main button')]
            .filter(e => { const r = e.getBoundingClientRect();
                           return r.width > 0 && r.height > 0 && r.height < 44; })
            .map(e => e.outerHTML.slice(0, 80))"""
    )
    assert small == [], (width, small[:3])


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [360, 768, 1280])
@pytest.mark.parametrize(
    "target",
    [
        "/admin/strategy-cards",
        "/admin/strategy-cards/SC-BINANCE-USDM-MEAN-REVERT-002/microscope",
        "/admin/backtests",
    ],
)
def test_strategy_and_backtest_pages_fit_every_viewport(
    microscope_url: str, backtest_url: str, target: str, width: int, scheme: str
) -> None:
    """Không tràn ngang và vùng chạm ≥ 44px ở mọi bề rộng, sáng/tối."""
    is_backtest = target == "/admin/backtests"
    handles, page = _open(
        backtest_url if is_backtest else microscope_url,
        target,
        BACKTEST_COOKIE if is_backtest else MICROSCOPE_COOKIE,
        width=width,
        scheme=scheme,
    )
    try:
        _assert_layout(page, width)
    finally:
        _close(handles)


def test_strategy_cards_dialogs_and_tabs(microscope_url: str) -> None:
    """Popup chi tiết thẻ, tab kiến trúc và popup giải thích tầng hoạt động dưới CSP."""
    handles, page = _open(
        microscope_url, "/admin/strategy-cards", MICROSCOPE_COOKIE, width=1280, scheme="light"
    )
    try:
        page.get_by_role("button", name="Chi tiết thẻ Donchian Trend & Volatility Breakout").click()
        dialog = page.locator("#card-dialog-1")
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text("Kiểm định gồm những gì")
        expect(dialog).to_contain_text("giữ chỗ")
        dialog.get_by_role("button", name="Đóng chi tiết thẻ").click()
        expect(dialog).to_be_hidden()

        page.get_by_role("tab", name="Kiến trúc L0–L6").click()
        page.get_by_role("button", name="Giải thích tầng L4 Cổng bối cảnh").click()
        layer_dialog = page.locator("#layer-dialog-l4")
        expect(layer_dialog).to_be_visible()
        expect(layer_dialog).to_contain_text("Hạn chế đã biết")
        expect(layer_dialog).to_contain_text("BLOCK")
        page.keyboard.press("Escape")
        expect(layer_dialog).to_be_hidden()
    finally:
        _close(handles)


def test_microscope_renders_l3_and_rejects_invalid_bar(microscope_url: str) -> None:
    """Thẻ có nút L3 phải hiện giá trị L3; nhập liệu âm bị chặn trước khi gọi API."""
    handles, page = _open(
        microscope_url,
        "/admin/strategy-cards/SC-BINANCE-USDM-MEAN-REVERT-002/microscope",
        MICROSCOPE_COOKIE,
        width=1280,
        scheme="dark",
    )
    try:
        l3 = page.locator("#l3-trace-content")
        expect(l3).to_contain_text("l3_zscore")
        expect(l3).not_to_contain_text("không khai báo nút")
        expect(page.locator("#l3-status-badge")).to_contain_text("HOÀN THÀNH (SUCCESS)")
        # L5 không phát tín hiệu → banner trung tính, không tuyên bố đã đóng gói ứng viên.
        banner = page.locator("#status-banner")
        expect(banner).to_have_class("alert-banner banner-neutral")
        expect(page.locator("#l5-status-badge")).to_contain_text("KHÔNG TÍN HIỆU (NO_SIGNAL)")
        expect(page.locator("#empty-zones-msg")).to_be_visible()
        # Số hiển thị gọn 2 chữ số kiểu Việt Nam; giá trị gốc ở data-value.
        value = page.locator("#l3-trace-content dd").first
        raw = float(value.get_attribute("data-value") or "nan")
        assert value.inner_text() == f"{raw:.2f}".rstrip("0").rstrip(".").replace(".", ",")

        requests: list[str] = []
        page.on(
            "request",
            lambda request: requests.append(request.url) if "inspect-bar" in request.url else None,
        )
        page.fill("#input-low", "-5")
        page.click("#btn-inspect-submit")
        expect(page.locator(".app-toast").first).to_contain_text("Giá trị nến chưa hợp lệ")
        assert requests == []

        page.locator('[data-rail-layer="l4"] button').click()
        expect(page.locator("#layer-dialog-l4")).to_be_visible()
    finally:
        _close(handles)


def test_backtest_jobs_single_dom_copy_and_percent_hint(backtest_url: str) -> None:
    """Mỗi lượt chỉ có một bản DOM; mã rút gọn có nút sao chép; tỷ lệ hiện phần trăm."""
    handles, page = _open(
        backtest_url, "/admin/backtests", BACKTEST_COOKIE, width=390, scheme="light"
    )
    try:
        assert page.locator("[data-job-id]").count() == 3
        expect(page.locator("[data-job-id]:visible")).to_have_count(3)
        first = page.locator("[data-job-id]").first
        expect(first.locator("button.copy-btn").first).to_be_visible()
        expect(first).to_contain_text("BTCUSDT")
        expect(page.locator("#risk-fraction-percent")).to_have_text("= 1%")
        page.fill("#risk-fraction", "0.025")
        expect(page.locator("#risk-fraction-percent")).to_have_text("= 2,5%")
        succeeded = page.locator('[data-job-id="22222222-2222-2222-2222-222222222222"]')
        expect(succeeded.locator('button.copy-btn[data-copy="run-btc-oct2026-001"]')).to_have_count(
            1
        )
    finally:
        _close(handles)

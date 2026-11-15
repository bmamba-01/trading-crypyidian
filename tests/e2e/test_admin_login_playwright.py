"""Playwright cho trang đăng nhập dưới CSP thật: ghi nhớ trường, ẩn/hiện mật khẩu, cờ kiểm thử.

Server là `create_app` thật (gồm `SecurityHeadersMiddleware`), không có DB: chỉ kiểm phần
giao diện chạy trong trình duyệt. Luồng đăng nhập với DB thật nằm ở
`tests/e2e/test_admin_web_workflows.py`.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from tests.e2e.csp_harness import (
    browser_violations,  # noqa: F401 - fixture nạp qua usefixtures
    watch_browser_violations,
)
from trading_bot.app import create_app
from trading_bot.config.settings import AppSettings

SCREENSHOTS_DIR = Path(__file__).resolve().parent / "evidence" / "admin-playwright"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

pytestmark = pytest.mark.usefixtures("browser_violations")


def _serve(settings: AppSettings) -> Iterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings), host="127.0.0.1", port=port, log_level="error", access_log=False
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(0.6)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=3)


@pytest.fixture(scope="module")
def login_url() -> Iterator[str]:
    yield from _serve(AppSettings(app_env="test"))


@pytest.fixture(scope="module")
def test_mode_login_url() -> Iterator[str]:
    yield from _serve(AppSettings(app_env="test", auth_test_totp_enabled=True))


def test_login_remembers_workspace_and_email_but_never_secrets(login_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1280, "height": 860}).new_page()
        watch_browser_violations(page)
        page.goto(f"{login_url}/admin/login", wait_until="networkidle")

        expect(page.get_by_label("Mã không gian làm việc")).to_be_focused()
        expect(page.locator("[data-test-totp]")).to_have_count(0)
        page.get_by_label("Mã không gian làm việc").fill("paper-workspace")
        page.get_by_label("Địa chỉ thư điện tử").fill("owner@example.test")
        page.get_by_label("Mật khẩu").fill("not-a-real-password")
        page.get_by_label("Mã xác thực 6 chữ số").fill("12 34 56")
        expect(page.get_by_label("Mã xác thực 6 chữ số")).to_have_value("123456")

        toggle = page.get_by_role("button", name="Hiện")
        toggle.click()
        expect(page.get_by_label("Mật khẩu")).to_have_attribute("type", "text")
        expect(page.get_by_role("button", name="Ẩn")).to_have_attribute("aria-pressed", "true")
        page.get_by_role("button", name="Ẩn").click()
        expect(page.get_by_label("Mật khẩu")).to_have_attribute("type", "password")

        page.screenshot(path=str(SCREENSHOTS_DIR / "30_login_desktop.png"), full_page=True)
        page.get_by_label("Ghi nhớ không gian làm việc và email trên trình duyệt này").check()
        page.get_by_role("button", name="Đăng nhập").click()
        # Không có DB nên máy chủ trả lỗi; điều cần kiểm là giá trị đã nhớ ở phía trình duyệt.
        page.wait_for_load_state("networkidle")
        stored = page.evaluate("() => window.localStorage.getItem('crypidian.login.remembered')")
        assert stored is not None
        assert "paper-workspace" in stored
        assert "owner@example.test" in stored
        assert "not-a-real-password" not in stored
        assert "123456" not in stored

        page.goto(f"{login_url}/admin/login", wait_until="networkidle")
        expect(page.get_by_label("Mã không gian làm việc")).to_have_value("paper-workspace")
        expect(page.get_by_label("Địa chỉ thư điện tử")).to_have_value("owner@example.test")
        expect(page.get_by_label("Mật khẩu")).to_have_value("")
        # Hai trường đầu đã có giá trị nên con trỏ nhảy thẳng tới ô mật khẩu.
        expect(page.get_by_label("Mật khẩu")).to_be_focused()

        page.get_by_label("Ghi nhớ không gian làm việc và email trên trình duyệt này").uncheck()
        page.get_by_label("Mật khẩu").fill("not-a-real-password")
        page.get_by_label("Mã xác thực 6 chữ số").fill("654321")
        page.get_by_role("button", name="Đăng nhập").click()
        page.wait_for_load_state("networkidle")
        assert (
            page.evaluate("() => window.localStorage.getItem('crypidian.login.remembered')") is None
        )
        browser.close()


def test_test_mode_prefills_code_and_mobile_layout_fits(test_mode_login_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_context(
            viewport={"width": 360, "height": 780}, is_mobile=True, has_touch=True
        ).new_page()
        watch_browser_violations(page)
        page.goto(f"{test_mode_login_url}/admin/login", wait_until="networkidle")

        expect(page.locator("[data-test-totp]")).to_contain_text("123456")
        expect(page.get_by_label("Mã xác thực 6 chữ số")).to_have_value("123456")
        # Trên di động không tự focus để trang không cuộn qua phần giới thiệu.
        assert page.evaluate("() => window.scrollY") == 0
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
        submit = page.get_by_role("button", name="Đăng nhập").bounding_box()
        assert submit is not None
        assert submit["height"] >= 44
        page.screenshot(path=str(SCREENSHOTS_DIR / "31_login_test_mode_mobile.png"), full_page=True)
        browser.close()


def test_login_errors_are_shown_as_specific_toasts(login_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 860})
        page = context.new_page()
        watch_browser_violations(page)
        page.goto(f"{login_url}/admin/login", wait_until="networkidle")

        posts: list[str] = []
        page.on(
            "request",
            lambda request: posts.append(request.url) if request.method == "POST" else None,
        )

        # Thiếu trường: toast nói rõ trường nào, không gửi gì lên máy chủ.
        page.get_by_label("Mã không gian làm việc").fill("paper-workspace")
        page.get_by_role("button", name="Đăng nhập").click()
        toast = page.locator(".app-toast--error").last
        expect(toast).to_contain_text("Chưa thể đăng nhập: Địa chỉ thư điện tử")
        expect(toast).to_contain_text("không được để trống")
        assert posts == []

        # Hết hạn biểu mẫu: máy chủ trả lại form kèm toast có tiêu đề và gợi ý cụ thể.
        page.get_by_label("Địa chỉ thư điện tử").fill("owner@example.test")
        page.get_by_label("Mật khẩu").fill("not-a-real-password")
        page.get_by_label("Mã xác thực 6 chữ số").fill("123456")
        context.clear_cookies()
        page.get_by_role("button", name="Đăng nhập").click()
        page.wait_for_load_state("networkidle")
        expect(page.get_by_role("alert")).to_contain_text("Biểu mẫu đăng nhập đã hết hạn")
        server_toast = page.locator(".app-toast--error")
        expect(server_toast).to_contain_text("Biểu mẫu đã hết hạn")
        # Toast chỉ nhân bản khung lỗi nên ẩn với trình đọc màn hình, tránh đọc hai lần.
        expect(server_toast).to_have_attribute("aria-hidden", "true")
        page.screenshot(path=str(SCREENSHOTS_DIR / "32_login_error_toast.png"))
        page.keyboard.press("Escape")
        expect(server_toast).to_have_count(0)

        # Trang khôi phục từ bộ nhớ đệm (Back) không được giữ nút ở trạng thái vô hiệu.
        page.evaluate(
            """() => {
                const button = document.getElementById('login-submit');
                button.disabled = true;
                button.textContent = 'Đang đăng nhập…';
                window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
            }"""
        )
        expect(page.get_by_role("button", name="Đăng nhập")).to_be_enabled()
        browser.close()

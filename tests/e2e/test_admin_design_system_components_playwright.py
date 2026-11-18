"""Kiểm E2E thành phần Design System (Toast, Dialog, Alert, Form, Error, Theme Sync)."""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException
from playwright.sync_api import Page, expect

from tests.e2e.csp_harness import install_security_headers, watch_browser_violations
from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings

pytest_plugins = ("tests.e2e.browser_fixtures", "tests.e2e.csp_harness")
SCREENSHOTS = Path(__file__).resolve().parent / "evidence" / "admin-playwright"
WORKSPACE_ID = UUID("1b6f7d3e-5c55-4e2a-9b55-2f3c1b0d9e11")
MEMBERSHIP_ID = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
USER_ID = UUID("2b6f7d3e-5c55-4e2a-9b55-2f3c1b0d9e22")
CSRF = "ds-playwright-csrf-token"


class _AuthStub:
    async def authenticate(self, _cookie: str | None) -> AuthIdentity:
        raise AuthenticationFailure(401, "Phiên làm việc đã hết hạn.")

    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return CSRF

    async def verify_csrf(self, _cookie: str | None, token: str | None) -> None:
        if token != CSRF:
            raise AuthenticationFailure(403, "Thiếu hoặc sai mã chống giả mạo CSRF.")


class _BotAdminStub:
    async def list_bots(self, _principal: Principal) -> list[dict[str, object]]:
        return []


def _owner_identity() -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({Role.OWNER}),
            mfa_verified_at=datetime.now(UTC),
        ),
        email="owner@example.test",
    )


def _serve_app(
    *, authenticated: bool = True, forbidden: bool = False
) -> tuple[str, uvicorn.Server, threading.Thread, socket.socket]:
    application: FastAPI = create_app(AppSettings(app_env="test"))
    install_security_headers(application)
    application.state.auth_service = _AuthStub()
    application.state.bot_admin_service = _BotAdminStub()
    if authenticated:
        application.dependency_overrides[current_identity] = lambda: _owner_identity()
    if forbidden:
        application.dependency_overrides[current_identity] = _forbidden_identity

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(application, log_level="error", lifespan="off"))
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
        name="admin-ds-components-playwright",
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.025)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        listener.close()
        raise RuntimeError("Admin web chưa khởi động trong thời hạn E2E.")
    return f"http://127.0.0.1:{port}", server, thread, listener


def _forbidden_identity() -> None:
    """Phiên hợp lệ nhưng không đủ quyền: trang lỗi HTML vẫn hiển thị (khác 401 về login)."""
    raise HTTPException(status_code=403, detail="Không đủ quyền")


def _stop_app(server: uvicorn.Server, thread: threading.Thread, listener: socket.socket) -> None:
    server.should_exit = True
    thread.join(timeout=5)
    listener.close()


def _assert_touch_and_no_overflow(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    controls = page.locator(
        "input:not([type=hidden]):not([type=checkbox]):not([type=radio]),"
        "select,textarea,button,a.btn"
    ).all()
    touch_viewport = page.viewport_size["width"] <= 767
    for control in controls:
        if control.is_visible():
            box = control.bounding_box()
            assert box is not None
            if touch_viewport:
                assert box["height"] >= 44
            elif control.evaluate("el => el.matches('button, a.btn')"):
                assert box["height"] >= 32
            else:
                assert box["height"] >= 40


def test_error_page_light_dark_and_responsive(
    browser_page: Page, browser_violations: list[str]
) -> None:
    """Trang lỗi HTML (403) đồng bộ Dark/Light, không tràn lề và chạm >= 44px.

    401 khi mở trang nay chuyển về đăng nhập (xem `test_admin_session_expiry_playwright.py`)
    nên trang lỗi được chụp với 403; 401 chỉ còn được kiểm là chuyển hướng.
    """
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    expired_url, expired_server, expired_thread, expired_listener = _serve_app(authenticated=False)
    try:
        response = browser_page.goto(f"{expired_url}/admin/bots/new", wait_until="networkidle")
        assert response is not None
        assert response.status == 200
        assert browser_page.url.startswith(f"{expired_url}/admin/login?next=")
    finally:
        _stop_app(expired_server, expired_thread, expired_listener)
    app_url, server, thread, listener = _serve_app(authenticated=False, forbidden=True)
    try:
        watch_browser_violations(browser_page)
        browser_page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(app_url + "/") else route.abort()
            ),
        )
        browser_page.emulate_media(color_scheme="light")
        browser_page.set_viewport_size({"width": 1280, "height": 800})
        response = browser_page.goto(f"{app_url}/admin/bots/new", wait_until="networkidle")
        assert response is not None
        assert response.status == 403
        expect(browser_page.get_by_role("alert")).to_be_visible()
        expect(browser_page.get_by_text("Mã phản hồi: 403")).to_be_visible()
        expect(browser_page.get_by_role("link", name="Đăng nhập lại").last).to_be_visible()
        _assert_touch_and_no_overflow(browser_page)
        browser_page.screenshot(
            path=str(SCREENSHOTS / "ds-error-desktop-light.png"),
            full_page=True,
        )

        browser_page.emulate_media(color_scheme="dark")
        expect(browser_page.locator("html")).to_have_attribute("data-theme", "dark")
        browser_page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => resolve()))"
        )
        _assert_touch_and_no_overflow(browser_page)
        browser_page.screenshot(
            path=str(SCREENSHOTS / "ds-error-desktop-dark.png"),
            full_page=True,
        )

        browser_page.set_viewport_size({"width": 390, "height": 844})
        browser_page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => resolve()))"
        )
        _assert_touch_and_no_overflow(browser_page)
        browser_page.screenshot(
            path=str(SCREENSHOTS / "ds-error-mobile-dark.png"),
            full_page=True,
        )
        assert browser_violations == []
    finally:
        _stop_app(server, thread, listener)


def test_unified_form_error_summary_toast_dialog_and_theme_sync(
    browser_page: Page, browser_violations: list[str]
) -> None:
    """Kiểm form error summary, select chevron, toast vị trí, flash reload và dialog.js."""
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    app_url, server, thread, listener = _serve_app(authenticated=True)
    try:
        watch_browser_violations(browser_page)
        browser_page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(app_url + "/") else route.abort()
            ),
        )
        browser_page.emulate_media(color_scheme="dark")
        browser_page.set_viewport_size({"width": 1280, "height": 860})
        browser_page.goto(f"{app_url}/admin/bots/new", wait_until="networkidle")

        # Chưa khóa localStorage -> đồng bộ tự động theo prefers-color-scheme.
        theme = browser_page.evaluate("document.documentElement.getAttribute('data-theme')")
        assert theme == "dark"
        expect(browser_page.get_by_role("switch", name="Giao diện tối")).to_have_attribute(
            "aria-checked", "true"
        )
        browser_page.emulate_media(color_scheme="light")
        expect(browser_page.locator("html")).to_have_attribute("data-theme", "light")
        expect(browser_page.get_by_role("switch", name="Giao diện tối")).to_have_attribute(
            "aria-checked", "false"
        )
        browser_page.emulate_media(color_scheme="dark")
        expect(browser_page.locator("html")).to_have_attribute("data-theme", "dark")

        # <select> không cần .form-select vẫn có mũi tên chevron từ cùng origin.
        select_bg = browser_page.locator("#max_notional").evaluate(
            "el => getComputedStyle(el).backgroundImage"
        )
        assert "/static/icons/chevron-down.svg" in select_bg

        # Nút .btn-primary ở Dark Mode dùng chữ trắng (#ffffff = rgb(255, 255, 255)).
        submit_btn = browser_page.get_by_role("button", name="Tạo bot mô phỏng")
        assert submit_btn.evaluate("el => getComputedStyle(el).color") == "rgb(255, 255, 255)"

        # Gửi form bỏ trống tên -> render .error-summary + mirrored Toast dưới topbar.
        browser_page.evaluate("document.getElementById('name').removeAttribute('required')")
        submit_btn.click()
        browser_page.wait_for_load_state("networkidle")

        expect(browser_page.locator(".error-summary")).to_be_visible()
        toast = browser_page.locator(".app-toast--error")
        expect(toast).to_be_visible()
        expect(toast).to_contain_text("Chưa thể tạo bot")

        topbar_box = browser_page.locator(".app-topbar").bounding_box()
        toast_box = toast.bounding_box()
        assert topbar_box is not None
        assert toast_box is not None
        assert toast_box["y"] >= topbar_box["y"] + topbar_box["height"]

        _assert_touch_and_no_overflow(browser_page)
        browser_page.screenshot(
            path=str(SCREENSHOTS / "ds-bot-create-error-desktop-dark.png"), full_page=True
        )

        # Kiểm dialog.js dùng chung: openDetail, Escape close, khôi phục focus về opener.
        theme_switch = browser_page.get_by_role("switch", name="Giao diện tối")
        theme_switch.focus()
        browser_page.evaluate(
            """() => {
              const opener = document.getElementById('theme-toggle');
              window.crypidianDialog.openDetail({
                title: 'Kiểm tra hộp thoại chuẩn hóa',
                lead: 'Xác nhận hộp thoại glassmorphism và khôi phục focus.',
                rows: [{ label: 'Mã chuẩn', value: 'UI-KIT-V1.2', mono: true }],
                rawText: '{"status":"ok","csp":"self"}',
                trigger: opener
              });
            }"""
        )
        dialog = browser_page.locator("#crypidian-detail-dialog")
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text("UI-KIT-V1.2")
        browser_page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        expect(theme_switch).to_be_focused()

        # Kiểm toast.flash() sống sót qua reload trên di động Light Mode.
        browser_page.emulate_media(color_scheme="light")
        browser_page.set_viewport_size({"width": 390, "height": 844})
        browser_page.evaluate(
            """() => window.crypidianToast.flash({
              tone: 'success',
              title: 'Đã đồng bộ sau tải lại',
              message: 'Thông báo nổi vẫn hiển thị sau reload.'
            })"""
        )
        browser_page.reload(wait_until="networkidle")
        flash_toast = browser_page.locator(".app-toast--success")
        expect(flash_toast).to_be_visible()
        expect(flash_toast).to_contain_text("Đã đồng bộ sau tải lại")
        _assert_touch_and_no_overflow(browser_page)
        browser_page.screenshot(
            path=str(SCREENSHOTS / "ds-bot-create-flash-mobile-light.png"), full_page=True
        )
        assert browser_violations == []
    finally:
        _stop_app(server, thread, listener)

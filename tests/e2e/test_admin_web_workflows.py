"""E2E browser cho ranh giới session, CSRF, Telegram và khả năng truy cập."""

import re
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pyotp
from playwright.sync_api import Page, expect
from tests.e2e.multibot_runtime_harness import ROOT, ComposeHarness

from trading_bot.auth.cookies import session_cookie_name
from trading_bot.config.settings import AppSettings

pytest_plugins = ("tests.e2e.browser_fixtures",)


class AdminWebHarness(ComposeHarness):
    def __init__(self) -> None:
        super().__init__("admin-browser")
        self.environment["TRADING_BOT_TELEGRAM_BOT_USERNAME"] = "crypidian_test_bot"
        base = (ROOT / "tests/e2e/probes/multibot_runtime.py").read_text("utf-8")
        marker = 'if __name__ == "__main__":\n    _main()'
        assert marker in base
        extra = (ROOT / "tests/e2e/probes/bot_control_api.py").read_text("utf-8")
        lifecycle = (ROOT / "tests/e2e/probes/flatten_worker_lifecycle.py").read_text("utf-8")
        dispatch = "_main()"
        extra_without_dispatch = extra.rstrip()[: -len(dispatch)]
        self.probe_code = (
            base.replace(marker, "pass")
            + "\n\n"
            + extra_without_dispatch
            + "\n\n"
            + lifecycle
            + "\n\n_main()\n"
        )


def test_admin_browser_session_navigation_accessibility_and_telegram_link(
    browser_page: Page,
) -> None:
    harness = AdminWebHarness()
    try:
        harness.start("admin-web")
        base_url = f"http://127.0.0.1:{harness.environment['TRADING_BOT_WEB_PORT']}"
        bootstrap_response = browser_page.request.post(
            f"{base_url}/api/v1/auth/bootstrap",
            data={
                "workspace_name": "Browser login workspace",
                "workspace_slug": "browser-login",
                "email": "browser-owner@example.test",
                "password": "a long browser owner password",
                "mode": "PAPER",
            },
            headers={"X-Bootstrap-Token": (ROOT / ".secrets/bootstrap_token").read_text()},
        )
        assert bootstrap_response.status == 201, bootstrap_response.text()
        setup_uri = bootstrap_response.json()["mfa_setup_uri"]
        totp_secret = parse_qs(urlparse(setup_uri).query)["secret"][0]
        verify_response = browser_page.request.post(
            f"{base_url}/api/v1/auth/bootstrap/verify",
            data={
                "workspace_slug": "browser-login",
                "email": "browser-owner@example.test",
                "code": pyotp.TOTP(totp_secret).at(datetime.now(UTC) - timedelta(seconds=30)),
            },
        )
        assert verify_response.status == 200, verify_response.text()
        harness.state = harness.probe("seed", "migrator", variants={"main": "1", "other": "1"})
        harness.probe("genesis")
        harness.state |= harness.probe("mb07b_flatten_single_position_setup")
        harness.state |= harness.probe("mb07a_auth_seed", "migrator")
        harness.probe("mb07b_seed_fresh_mark", "migrator")
        workspace_id = harness.state["workspaces"]["main"]["workspace"]
        harness.environment["TRADING_BOT_RUNTIME_WORKSPACE_ID"] = workspace_id
        harness.compose("up", "--detach", "--wait", "--wait-timeout", "180", "trading-runtime")
        session = harness.state["mb07a_sessions"]["owner"]
        login_response = browser_page.goto(f"{base_url}/admin/login", wait_until="networkidle")
        assert login_response is not None
        assert login_response.status == 200
        assert login_response.headers.get("cache-control") == "no-store"
        assert "default-src 'self'" in login_response.headers.get("content-security-policy", "")
        expect(browser_page.get_by_role("heading", name="Đăng nhập")).to_be_visible()
        expect(browser_page.get_by_role("navigation", name="Điều hướng chính")).to_have_count(0)
        expect(browser_page.get_by_label("Mã workspace")).to_be_visible()
        expect(browser_page.get_by_label("Mã xác thực 6 chữ số")).to_be_visible()
        browser_page.get_by_label("Mã workspace").fill("paper-workspace")
        browser_page.get_by_label("Email").fill("owner@example.test")
        browser_page.get_by_label("Mật khẩu").fill("sai trong browser E2E")
        browser_page.get_by_label("Mã xác thực 6 chữ số").fill("000000")
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/admin/login")
            )
        ) as login_failure_info:
            browser_page.get_by_role("button", name="Đăng nhập").click()
        assert login_failure_info.value.status == 401
        expect(browser_page.get_by_role("alert")).to_have_text("Thông tin đăng nhập không hợp lệ.")
        assert "sai trong browser E2E" not in browser_page.content()

        browser_page.get_by_label("Mã workspace").fill("browser-login")
        browser_page.get_by_label("Email").fill("browser-owner@example.test")
        browser_page.get_by_label("Mật khẩu").fill("a long browser owner password")
        browser_page.get_by_label("Mã xác thực 6 chữ số").fill(pyotp.TOTP(totp_secret).now())
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/admin/login")
            )
        ) as successful_login_info:
            browser_page.get_by_role("button", name="Đăng nhập").click()
        assert successful_login_info.value.status == 303
        expect(browser_page).to_have_url(f"{base_url}/admin")
        expect(browser_page.get_by_text("browser-owner@example.test", exact=True)).to_be_visible()
        browser_page.context.clear_cookies()

        browser_page.context.add_cookies(
            [
                {
                    "name": session_cookie_name(AppSettings(app_env="local")),
                    "value": f"{session['workspace']}:{session['token']}",
                    "url": base_url,
                    "httpOnly": True,
                    "sameSite": "Strict",
                }
            ]
        )
        dashboard_response = browser_page.goto(f"{base_url}/admin", wait_until="networkidle")
        assert dashboard_response is not None
        assert dashboard_response.status == 200
        assert dashboard_response.headers.get("cache-control") == "no-store"
        assert "default-src 'self'" in dashboard_response.headers.get("content-security-policy", "")
        assert dashboard_response.headers.get("referrer-policy") == "no-referrer"

        expect(browser_page.get_by_role("main")).to_be_visible()
        expect(browser_page.get_by_role("heading", name="Tổng quan workspace")).to_be_visible()
        expect(browser_page.get_by_role("navigation", name="Điều hướng chính")).to_be_visible()
        expect(
            browser_page.get_by_role("link", name="Bỏ qua điều hướng, tới nội dung chính")
        ).to_be_attached()
        browser_page.keyboard.press("Tab")
        expect(browser_page.locator(":focus")).to_have_class("skip-link")
        browser_page.keyboard.press("Enter")
        expect(browser_page).to_have_url(f"{base_url}/admin#main-content")

        bot_id = harness.state["workspaces"]["main"]["bots"]["a"]
        browser_page.get_by_role("link", name="Bot", exact=True).click()
        expect(browser_page.get_by_role("heading", name="Bot trong workspace")).to_be_visible()
        bot_link = browser_page.get_by_role("link", name=re.compile("main.*a", re.IGNORECASE))
        expect(bot_link).to_be_visible()
        bot_link.click()
        expect(browser_page).to_have_url(f"{base_url}/admin/bots/{bot_id}")
        expect(
            browser_page.get_by_role("heading", name=re.compile("main.*a", re.IGNORECASE))
        ).to_be_visible()

        browser_page.get_by_label("Lý do pause").fill("Kiểm thử thao tác quản trị PAPER")
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith(f"/admin/bots/{bot_id}/pause")
            )
        ) as pause_response_info:
            browser_page.get_by_role("button", name="Pause bot").click()
        assert pause_response_info.value.status == 303
        expect(browser_page.get_by_text("PAUSED", exact=True)).to_be_visible()

        browser_page.get_by_role("link", name="Flatten workspace", exact=True).click()
        expect(browser_page.get_by_role("heading", name="Flatten workspace PAPER")).to_be_visible()
        # Các bước giao diện làm mark hết hạn; seed lại ngay trước preview.
        harness.probe("mb07b_seed_fresh_mark", "migrator")
        browser_page.get_by_label("Lý do", exact=True).fill(
            "Kiểm thử preview PAPER bằng trình duyệt"
        )
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith("/admin/flatten/preview")
            )
        ) as preview_response_info:
            browser_page.get_by_role("button", name="Xem preview").click()
        assert preview_response_info.value.status == 200
        expect(browser_page.get_by_role("heading", name="Rà soát flatten PAPER")).to_be_visible()
        expect(browser_page.get_by_role("table").get_by_role("caption")).to_have_text(
            "Các mục trong snapshot"
        )
        expect(browser_page.get_by_label("Lý do xác nhận")).to_be_visible()

        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith("/admin/flatten/confirm")
            )
        ) as confirm_response_info:
            browser_page.get_by_role("button", name="Xác nhận flatten").click()
        assert confirm_response_info.value.status == 303
        expect(browser_page).to_have_url(re.compile(r"/admin/flatten/operations/[0-9a-f-]+$"))
        status = browser_page.locator("#operation-status")
        if status.count() == 0:
            operation_response = browser_page.goto(browser_page.url, wait_until="networkidle")
            assert operation_response is not None
            assert operation_response.status == 200, (
                operation_response.status,
                browser_page.locator("body").inner_text(),
            )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            operation_status = status.inner_text()
            if "COMPLETED" in operation_status:
                break
            if "PARTIAL" in operation_status or "BLOCKED" in operation_status:
                raise AssertionError(browser_page.locator("body").inner_text())
            browser_page.reload(wait_until="networkidle")
            time.sleep(0.25)
        assert "COMPLETED" in status.inner_text(), browser_page.locator("body").inner_text()
        expect(status).to_contain_text("COMPLETED", timeout=1_000)

        browser_page.goto(f"{base_url}/admin/bots/{bot_id}", wait_until="networkidle")
        browser_page.get_by_label("Lý do disable").fill("Bot đã phẳng sau operation PAPER")
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith(f"/admin/bots/{bot_id}/disable")
            )
        ) as disable_response_info:
            browser_page.get_by_role("button", name="Disable bot").click()
        assert disable_response_info.value.status == 303
        expect(browser_page).to_have_url(f"{base_url}/admin/bots/{bot_id}")
        expect(browser_page.get_by_role("button", name="Bật bot sau đối soát")).to_be_visible()

        browser_page.get_by_label("Lý do bật lại bot").fill("Kiểm tra deny trước publish")
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith(f"/admin/bots/{bot_id}/enable")
            )
        ) as denied_enable_info:
            browser_page.get_by_role("button", name="Bật bot sau đối soát").click()
        assert denied_enable_info.value.status == 409
        assert denied_enable_info.value.json()["detail"] == "Thao tác vận hành bị từ chối."
        denial_audit = harness.probe("mb07a_inspect", "migrator")["audits"]
        assert any(
            row[0] == "bot.lifecycle.enable_denied" and row[1] == "BOT_ENABLE_CONFIG_REQUIRED"
            for row in denial_audit
        ), denial_audit
        browser_page.goto(f"{base_url}/admin/bots/{bot_id}", wait_until="networkidle")
        expect(browser_page.get_by_text("DISABLED / DISABLED / PAPER", exact=True)).to_be_visible()

        browser_page.get_by_label("Overlay JSON").fill('{"schema_version": 1}')
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith(f"/admin/bots/{bot_id}/config-drafts")
            )
        ) as draft_response_info:
            browser_page.get_by_role("button", name="Tạo bản nháp").click()
        assert draft_response_info.value.status == 303
        expect(
            browser_page.get_by_text(re.compile(r"Draft [0-9a-f-]+ — revision 1"))
        ).to_be_visible()

        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and "/config-drafts/" in response.url
                and response.url.endswith("/validate")
            )
        ) as validate_response_info:
            browser_page.get_by_role("button", name="Kiểm định bản nháp").click()
        assert validate_response_info.value.status == 303
        expect(browser_page.get_by_role("status")).to_have_text("Bản nháp đã qua kiểm định.")

        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and "/config-drafts/" in response.url
                and response.url.endswith("/publish")
            )
        ) as publish_response_info:
            browser_page.get_by_role("button", name="Publish bản nháp").click()
        assert publish_response_info.value.status == 303
        expect(browser_page.get_by_role("button", name="Bật bot sau đối soát")).to_be_visible()

        browser_page.get_by_label("Lý do bật lại bot").fill("Đã publish và đối soát runtime")
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith(f"/admin/bots/{bot_id}/enable")
            )
        ) as enabled_response_info:
            browser_page.get_by_role("button", name="Bật bot sau đối soát").click()
        assert enabled_response_info.value.status == 303
        expect(browser_page.get_by_text("ENABLED / ENABLED / PAPER", exact=True)).to_be_visible()
        entry_state = (
            browser_page.locator("dt")
            .filter(has_text="Trạng thái entry")
            .locator("xpath=following-sibling::dd[1]")
        )
        expect(entry_state).to_have_text("PAUSED")
        browser_page.get_by_role("link", name="Tổng quan", exact=True).click()
        expect(browser_page.get_by_text("Điều khiển workspace: ACTIVE", exact=True)).to_be_visible()

        browser_page.get_by_role("link", name="Telegram", exact=True).click()
        expect(browser_page.get_by_role("heading", name="Liên kết Telegram")).to_be_visible()
        telegram_page_text = browser_page.locator("body").inner_text()
        assert "7000002" not in telegram_page_text
        assert session["token"] not in browser_page.content()
        expect(browser_page.get_by_text("7000002", exact=True)).to_have_count(0)

        owner_membership = harness.state["workspaces"]["main"]["members"]["owner"]
        revoke_form = browser_page.locator(
            f'form[action="/admin/telegram/revoke/{owner_membership}"]'
        )
        revoke_reason = revoke_form.get_by_label("Lý do thu hồi")
        revoke_reason.fill("Thu hồi binding seed trong E2E browser")
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith(f"/admin/telegram/revoke/{owner_membership}")
            )
        ) as revoke_response_info:
            revoke_form.get_by_role("button", name="Thu hồi").click()
        assert revoke_response_info.value.status == 303
        expect(browser_page).to_have_url(f"{base_url}/admin/telegram")
        expect(
            browser_page.locator(f'form[action="/admin/telegram/revoke/{owner_membership}"]')
        ).to_have_count(0)

        link_form = browser_page.locator('form[action="/admin/telegram/link"]')
        link_form.locator('input[name="csrf_token"]').evaluate(
            "element => element.value = 'csrf-sai'"
        )
        browser_page.get_by_label("Lý do", exact=True).fill("Thử CSRF sai")
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/admin/telegram/link")
            )
        ) as invalid_csrf_info:
            browser_page.get_by_role("button", name="Tạo mã liên kết").click()
        assert invalid_csrf_info.value.status == 403
        browser_page.goto(f"{base_url}/admin/telegram", wait_until="networkidle")

        expect(browser_page.get_by_label("Lý do", exact=True)).to_be_visible()

        browser_page.get_by_label("Lý do", exact=True).fill("Kiểm tra liên kết bằng trình duyệt")
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/admin/telegram/link")
            )
        ) as link_response_info:
            browser_page.get_by_role("button", name="Tạo mã liên kết").click()
        link_response = link_response_info.value
        assert link_response.status == 200
        assert link_response.headers.get("cache-control") == "no-store"
        assert link_response.headers.get("referrer-policy") == "no-referrer"
        expect(browser_page.get_by_role("heading", name="Mã liên kết đã tạo")).to_be_visible()
        link = browser_page.get_by_role("link", name="Mở bot Telegram trong tin nhắn riêng")
        href = link.get_attribute("href")
        assert href is not None
        assert href.startswith("https://t.me/crypidian_test_bot?start=")
        assert "7000002" not in href
        assert session["token"] not in browser_page.content()
        expect(browser_page.get_by_role("status")).to_be_visible()
    finally:
        harness.down()

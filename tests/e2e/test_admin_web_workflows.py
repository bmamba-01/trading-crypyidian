"""E2E browser cho ranh giới session, CSRF, Telegram và khả năng truy cập."""

import json
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlparse

import pyotp
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.multibot_runtime_harness import ROOT, ComposeHarness
from trading_bot.auth.cookies import session_cookie_name
from trading_bot.config.settings import AppSettings

pytest_plugins = ("tests.e2e.browser_fixtures",)


UI_REPORT_GRANT_PROBE = """
def mb07a_chart_bars_seed():
    trade_id = UUID(STATE["debug_chart_trade_id"])
    workspace = _ws("main")
    with _connect() as connection, connection.transaction():
        trade = connection.execute(
            "SELECT symbol, opened_at, closed_at FROM trade "
            "WHERE workspace_id=%s AND id=%s AND status='CLOSED'",
            (UUID(workspace["workspace"]), trade_id),
        ).fetchone()
        assert trade is not None, "Cần giao dịch PAPER đã đóng để seed chart fixture."
        instrument = connection.execute(
            "SELECT id FROM market_instrument WHERE workspace_id=%s AND symbol=%s "
            "AND source='BINANCE_USDM' ORDER BY available_at DESC LIMIT 1",
            (UUID(workspace["workspace"]), trade[0]),
        ).fetchone()
        assert instrument is not None, "Thiếu instrument fixture cho chart."
        now = datetime.now(UTC)
        opened = trade[1]
        aligned_open = datetime.fromtimestamp(int(opened.timestamp()) // 300 * 300, UTC)
        first_open = aligned_open - timedelta(minutes=15)
        last_open = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, UTC) - timedelta(
            minutes=5
        )
        inserted = 0
        cursor = first_open
        while cursor <= last_open:
            closed = cursor + timedelta(minutes=5)
            if closed <= now:
                key = f"mb07a-chart:{trade_id}:{int(cursor.timestamp())}"
                checksum = hashlib.sha256(key.encode()).hexdigest()
                connection.execute(
                    "INSERT INTO market_bar (id, workspace_id, instrument_id, symbol, "
                    "timeframe, source, logical_key, idempotency_key, revision, checksum, "
                    "open_time, close_time, event_at, received_at, available_at, open, high, "
                    "low, close, volume, is_closed) "
                    "VALUES (%s,%s,%s,%s,'5m','BINANCE_USDM',%s,%s,1,%s,%s,%s,%s,%s,%s,"
                    "100,101,99,100,5,TRUE)",
                    (
                        uuid4(),
                        UUID(workspace["workspace"]),
                        instrument[0],
                        trade[0],
                        key,
                        key,
                        checksum,
                        cursor,
                        closed,
                        closed,
                        closed,
                        closed,
                    ),
                )
                inserted += 1
            cursor += timedelta(minutes=5)
    assert inserted >= 2, "Cần tối thiểu hai nến 5m đã đóng quanh giao dịch."
    return {"chart_bars_seeded": inserted, "chart_trade_id": str(trade_id)}
PHASES["mb07a_chart_bars_seed"] = mb07a_chart_bars_seed

def mb07b_ui_flatten_inspect():
    operation_id = UUID(STATE["debug_operation_id"])
    workspace = _ws("main")
    with _connect() as connection:
        operation = connection.execute(
            "SELECT status FROM flatten_operation WHERE workspace_id=%s AND id=%s",
            (workspace["workspace"], operation_id),
        ).fetchone()
        items = connection.execute(
            "SELECT status, last_error_code, mark_record_id, mark_revision, mark_checksum, "
            "mark_close_time, result "
            "FROM flatten_operation_item WHERE workspace_id=%s AND operation_id=%s",
            (workspace["workspace"], operation_id),
        ).fetchall()
        latest_mark = connection.execute(
            "SELECT id, revision, checksum, close_time FROM market_bar "
            "WHERE workspace_id=%s AND symbol='BTCUSDT' AND source='BINANCE_USDM' "
            "AND timeframe='1m' AND is_closed IS TRUE ORDER BY close_time DESC, "
            "available_at DESC, revision DESC LIMIT 1",
            (workspace["workspace"],),
        ).fetchone()
    return {
        "status": operation[0],
        "items": [list(row) for row in items],
        "latest_mark": list(latest_mark) if latest_mark else None,
    }
PHASES["mb07b_ui_flatten_inspect"] = mb07b_ui_flatten_inspect

def mb07b_ui_report_grant():
    workspace = _ws("main")
    with _connect() as connection, connection.transaction():
        _insert(connection, "bot_grant", id=uuid4(),
            workspace_id=UUID(workspace["workspace"]),
            bot_id=UUID(workspace["bots"]["a"]),
            membership_id=UUID(workspace["members"]["analyst_reviewer"]),
            permission="bot:report:read", granted_by=UUID(workspace["members"]["owner"]))
    return {"granted": True}
PHASES["mb07b_ui_report_grant"] = mb07b_ui_report_grant
"""


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
            + UI_REPORT_GRANT_PROBE
            + "\n\n_main()\n"
        )


@pytest.mark.parametrize(
    "interaction_viewport", [(360, 800), (390, 844), (768, 1024), (1024, 768), (1280, 800)]
)
def test_admin_browser_session_navigation_accessibility_and_telegram_link(
    browser_page: Page,
    interaction_viewport: tuple[int, int],
) -> None:
    browser_page.set_viewport_size(
        {"width": interaction_viewport[0], "height": interaction_viewport[1]}
    )
    harness = AdminWebHarness()
    try:
        harness.start("admin-web")
        base_url = f"http://127.0.0.1:{harness.environment['TRADING_BOT_WEB_PORT']}"
        external_requests: list[str] = []
        browser_page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(base_url + "/") else route.abort()
            ),
        )
        browser_page.on(
            "request",
            lambda request: (
                external_requests.append(request.url)
                if not request.url.startswith(base_url + "/")
                else None
            ),
        )
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
        harness.state = harness.probe(
            "seed",
            "migrator",
            variants={"main": "1", "other": "1"},
            fresh_mark_only=True,
        )
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
        expect(browser_page.get_by_label("Mã không gian làm việc")).to_be_visible()
        expect(browser_page.get_by_label("Mã xác thực 6 chữ số")).to_be_visible()
        assert_responsive(browser_page, "login")
        browser_page.get_by_label("Mã không gian làm việc").fill("paper-workspace")
        browser_page.get_by_label("Địa chỉ thư điện tử").fill("owner@example.test")
        browser_page.get_by_label("Mật khẩu").fill("sai trong browser E2E")
        browser_page.get_by_label("Mã xác thực 6 chữ số").fill("000000")
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/admin/login")
            )
        ) as login_failure_info:
            browser_page.get_by_role("button", name="Đăng nhập").press("Enter")
        assert login_failure_info.value.status == 401
        expect(browser_page.get_by_role("alert")).to_have_text("Thông tin đăng nhập không hợp lệ.")
        assert "sai trong browser E2E" not in browser_page.content()

        browser_page.get_by_label("Mã không gian làm việc").fill("browser-login")
        browser_page.get_by_label("Địa chỉ thư điện tử").fill("browser-owner@example.test")
        browser_page.get_by_label("Mật khẩu").fill("a long browser owner password")
        browser_page.get_by_label("Mã xác thực 6 chữ số").fill(pyotp.TOTP(totp_secret).now())
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/admin/login")
            )
        ) as successful_login_info:
            browser_page.get_by_role("button", name="Đăng nhập").press("Enter")
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
        if browser_page.get_by_text("Thống kê hiệu quả chưa sẵn sàng", exact=False).count():
            report_probe = browser_page.request.get(
                f"{base_url}/api/v1/reports",
                params={
                    "scope": "bot",
                    "bot_id": harness.state["workspaces"]["main"]["bots"]["a"],
                    "start": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
                    "end": datetime.now(UTC).isoformat(),
                },
            )
            raise AssertionError(
                "Dashboard không tải metric; report API probe "
                f"{report_probe.status}: {report_probe.text()}"
            )

        expect(browser_page.get_by_role("main")).to_be_visible()
        expect(
            browser_page.get_by_role("heading", name="Tổng quan không gian làm việc")
        ).to_be_visible()
        # Màn hình hẹp: điều hướng chính nằm trong ngăn kéo, chỉ nút "Menu" hiển thị sẵn.
        menu_button = browser_page.get_by_role("button", name="Menu")
        if menu_button.is_visible():
            expect(menu_button).to_have_attribute("aria-expanded", "false")
            expect(browser_page.locator("nav.main-nav")).to_have_count(1)
        else:
            expect(browser_page.get_by_role("navigation", name="Điều hướng chính")).to_be_visible()
        expect(
            browser_page.get_by_role("link", name="Bỏ qua điều hướng, tới nội dung chính")
        ).to_be_attached()
        browser_page.keyboard.press("Tab")
        expect(browser_page.locator(":focus")).to_have_class("skip-link")
        browser_page.keyboard.press("Enter")
        expect(browser_page).to_have_url(f"{base_url}/admin#main-content")
        expect(browser_page.locator(":focus")).to_have_id("main-content")

        bot_id = harness.state["workspaces"]["main"]["bots"]["a"]
        for path, name in [
            ("/admin", "dashboard"),
            ("/admin/bots", "bots"),
            (f"/admin/bots/{bot_id}", "bot-detail"),
            ("/admin/bots/new", "bot-create"),
            ("/admin/reports", "reports"),
            ("/admin/flatten", "flatten"),
            ("/admin/telegram", "telegram"),
        ]:
            page_response = browser_page.goto(f"{base_url}{path}", wait_until="networkidle")
            assert page_response is not None
            assert page_response.status == 200
            assert_responsive(browser_page, name)
        browser_page.goto(f"{base_url}/admin", wait_until="domcontentloaded")
        assert_font_and_fallback(browser_page)

        _open_mobile_menu(browser_page)
        browser_page.get_by_role("link", name="Bot", exact=True).press("Enter")
        expect(
            browser_page.get_by_role("heading", name="Bot trong không gian làm việc")
        ).to_be_visible()
        bot_link = browser_page.get_by_role("link", name=re.compile("main.*a", re.IGNORECASE))
        expect(bot_link).to_be_visible()
        bot_link.press("Enter")
        expect(browser_page).to_have_url(f"{base_url}/admin/bots/{bot_id}")
        expect(
            browser_page.get_by_role("heading", level=1, name=re.compile("main.*a", re.IGNORECASE))
        ).to_be_visible()

        browser_page.get_by_label("Lý do tạm dừng").fill("Kiểm thử thao tác quản trị PAPER")
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith(f"/admin/bots/{bot_id}/pause")
            )
        ) as pause_response_info:
            browser_page.get_by_role("button", name="Tạm dừng bot").press("Enter")
        assert pause_response_info.value.status == 303
        expect(browser_page.locator("dd").get_by_text("Tạm dừng", exact=True)).to_be_visible()

        _open_mobile_menu(browser_page)
        browser_page.get_by_role("link", name="Đóng toàn bộ vị thế", exact=True).press("Enter")
        expect(
            browser_page.get_by_role("heading", name="Đóng toàn bộ vị thế mô phỏng")
        ).to_be_visible()
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
            browser_page.get_by_role("button", name="Xem trước").press("Enter")
        assert preview_response_info.value.status == 200
        expect(
            browser_page.get_by_role("heading", name="Rà soát đóng toàn bộ vị thế mô phỏng")
        ).to_be_visible()
        assert_responsive(browser_page, "flatten-preview")
        browser_page.set_viewport_size({"width": 1280, "height": 800})
        expect(browser_page.get_by_role("table").get_by_role("caption")).to_have_text(
            "Các mục trong ảnh chụp trạng thái"
        )
        browser_page.set_viewport_size(
            {"width": interaction_viewport[0], "height": interaction_viewport[1]}
        )
        expect(browser_page.get_by_label("Lý do xác nhận")).to_be_visible()

        # Đo đủ năm viewport có thể vượt tuổi mark; tạo snapshot mới trước thao tác xác nhận.
        harness.probe("mb07b_seed_fresh_mark", "migrator")
        browser_page.goto(f"{base_url}/admin/flatten", wait_until="networkidle")
        browser_page.get_by_label("Lý do", exact=True).fill(
            "Kiểm thử preview PAPER bằng trình duyệt"
        )
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith("/admin/flatten/preview")
            )
        ) as fresh_preview_response_info:
            browser_page.get_by_role("button", name="Xem trước").press("Enter")
        assert fresh_preview_response_info.value.status == 200
        expect(
            browser_page.get_by_role("heading", name="Rà soát đóng toàn bộ vị thế mô phỏng")
        ).to_be_visible()

        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith("/admin/flatten/confirm")
            )
        ) as confirm_response_info:
            browser_page.get_by_role("button", name="Xác nhận đóng toàn bộ").press("Enter")
        assert confirm_response_info.value.status == 303, confirm_response_info.value.text()
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
            if "Đã hoàn tất" in operation_status:
                break
            if "Hoàn tất một phần" in operation_status or "Bị chặn" in operation_status:
                operation_id = browser_page.url.rsplit("/", 1)[-1]
                diagnostic = harness.probe(
                    "mb07b_ui_flatten_inspect",
                    "migrator",
                    debug_operation_id=operation_id,
                )
                raise AssertionError(
                    f"{browser_page.locator('body').inner_text()}\nDB: {diagnostic}"
                )
            browser_page.reload(wait_until="networkidle")
            time.sleep(0.25)
        assert "Đã hoàn tất" in status.inner_text(), browser_page.locator("body").inner_text()
        expect(status).to_contain_text("Đã hoàn tất", timeout=1_000)
        assert_responsive(browser_page, "flatten-operation")
        browser_page.goto(f"{base_url}/admin", wait_until="domcontentloaded")
        if browser_page.get_by_text("Thống kê hiệu quả chưa sẵn sàng", exact=False).count():
            report_probe = browser_page.request.get(
                f"{base_url}/api/v1/reports",
                params={
                    "scope": "bot",
                    "bot_id": bot_id,
                    "start": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
                    "end": datetime.now(UTC).isoformat(),
                },
            )
            raise AssertionError(
                "Dashboard không tải metric sau flatten; report API probe "
                f"{report_probe.status}: {report_probe.text()}; entry fixture="
                f"{harness.state.get('single_position_entry')}, fill fixture="
                f"{harness.state.get('single_position_fill')}"
            )
        expect(
            browser_page.get_by_text("Thống kê hiệu quả chưa sẵn sàng", exact=False)
        ).to_have_count(0)
        # Thống kê và biểu đồ hiệu suất nằm trong tab "Hiệu suất 30 ngày" của dashboard.
        browser_page.get_by_role("tab", name="Hiệu suất 30 ngày").click()
        expect(
            browser_page.locator(".asset-report").get_by_role(
                "heading", name="Tài sản USDT", exact=True
            )
        ).to_be_visible()
        assert_pnl_color_semantics(browser_page)
        assert_responsive(browser_page, "dashboard-metrics")
        expect(browser_page.locator("#dashboard-performance-chart")).to_have_attribute(
            "data-chart-ready", "true"
        )
        expect(browser_page.locator("#dashboard-chart-event-detail")).to_be_visible()
        expect(browser_page.locator(".chart-event-list")).to_contain_text("Vì sao bắt tín hiệu")
        expect(browser_page.locator(".chart-event-list")).to_contain_text("Vì sao đóng")
        analyst = harness.state["mb07a_sessions"]["analyst_reviewer"]
        browser_page.context.clear_cookies()
        browser_page.context.add_cookies(
            [
                {
                    "name": session_cookie_name(AppSettings(app_env="local")),
                    "value": f"{analyst['workspace']}:{analyst['token']}",
                    "url": base_url,
                    "httpOnly": True,
                    "sameSite": "Strict",
                }
            ]
        )
        browser_page.goto(f"{base_url}/admin", wait_until="domcontentloaded")
        expect(browser_page.locator(".bot-report")).to_have_count(0)
        expect(
            browser_page.get_by_text("Chưa có bot đang hoạt động được cấp quyền xem báo cáo.")
        ).to_be_visible()
        harness.probe("mb07b_ui_report_grant", "migrator")
        browser_page.reload(wait_until="domcontentloaded")
        expect(browser_page.locator(".bot-report")).to_have_count(1)
        expect(browser_page.locator(".bot-report h3").first).to_contain_text("main-a")
        expect(
            browser_page.get_by_text("Thống kê hiệu quả chưa sẵn sàng", exact=False)
        ).to_have_count(0)
        denied_workspace = browser_page.goto(
            f"{base_url}/admin/reports?"
            + urlencode(
                {
                    "scope": "workspace",
                    "start": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
                    "end": datetime.now(UTC).isoformat(),
                }
            ),
            wait_until="networkidle",
        )
        assert denied_workspace is not None
        assert denied_workspace.status == 403
        expect(browser_page.get_by_role("alert")).to_be_visible()
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
        report_start = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        report_end = datetime.now(UTC).isoformat()
        browser_page.goto(
            f"{base_url}/admin/reports?"
            + urlencode(
                {"scope": "bot", "bot_id": bot_id, "start": report_start, "end": report_end}
            ),
            wait_until="networkidle",
        )
        expect(
            browser_page.locator(".asset-report").get_by_role(
                "heading", name="Tài sản USDT", exact=True
            )
        ).to_be_visible()
        expect(
            browser_page.get_by_role("heading", name="Lịch lãi/lỗ theo ngày", exact=True)
        ).to_be_visible()
        expect(browser_page.locator(".daily-performance-card")).to_have_count(1)
        expect(browser_page.locator(".daily-performance-card")).to_contain_text("Giao dịch đã đóng")
        assert_pnl_color_semantics(browser_page)
        assert_responsive(browser_page, "reports-metrics")

        if interaction_viewport == (1280, 800):
            chart_trade_id = harness.state["single_position_fill"]["trade_id"]
            assert chart_trade_id is not None
            chart_fixture = harness.probe(
                "mb07a_chart_bars_seed", "migrator", debug_chart_trade_id=chart_trade_id
            )
            assert chart_fixture["chart_bars_seeded"] >= 2
            browser_page.goto(
                f"{base_url}/admin/reports?"
                + urlencode(
                    {
                        "scope": "bot",
                        "bot_id": bot_id,
                        "start": report_start,
                        "end": report_end,
                        "chart_trade_id": chart_trade_id,
                        "chart_timeframe": "5m",
                    }
                ),
                wait_until="networkidle",
            )
            expect(browser_page.locator("#trade-chart")).to_have_attribute(
                "data-chart-ready", "true"
            )
            assert_responsive(browser_page, "reports-chart")
            expect(browser_page.locator("#trade-chart-event-detail")).to_be_visible()
            chart_data = json.loads(
                browser_page.locator("#trade-chart-data").text_content() or "{}"
            )
            assert chart_data["bars"]
            expected_details = {
                "Vào lệnh BTCUSDT" if fill_event["role"] == "ENTRY" else "Khớp đóng lệnh BTCUSDT"
                for fill_event in chart_data["trade"]["fills"]
            }
            assert "Vào lệnh BTCUSDT" in expected_details
            assert "Khớp đóng lệnh BTCUSDT" in expected_details
            browser_page.locator("#trade-chart").scroll_into_view_if_needed()
            chart_box = browser_page.locator("#trade-chart").bounding_box()
            assert chart_box is not None
            browser_page.evaluate(
                """() => {
                    const detail = document.querySelector('#trade-chart-event-detail');
                    const chart = document.querySelector('#trade-chart');
                    window.__chartPointerDetails = new Set([detail.textContent]);
                    window.__chartDomMoves = 0;
                    chart.addEventListener('pointermove', () => window.__chartDomMoves++, true);
                    new MutationObserver(() =>
                        window.__chartPointerDetails.add(detail.textContent)
                    ).observe(detail, { childList: true, characterData: true, subtree: true });
                }"""
            )
            plot_y = chart_box["y"] + chart_box["height"] / 2
            plot_left = chart_box["x"] + 60
            plot_right = chart_box["x"] + chart_box["width"] - 60
            browser_page.mouse.move(plot_left, plot_y)
            for x_position in range(int(plot_left) + 1, int(plot_right), 4):
                browser_page.mouse.move(x_position, plot_y)
                browser_page.wait_for_timeout(16)
            observed_details = browser_page.evaluate("Array.from(window.__chartPointerDetails)")
            pointer_moves = browser_page.evaluate("window.__chartDomMoves")
            assert pointer_moves > 0, (chart_box, pointer_moves)
            for expected_detail in expected_details:
                assert any(expected_detail in detail for detail in observed_details), (
                    expected_detail,
                    observed_details,
                    pointer_moves,
                    chart_data["bars"],
                )

        browser_page.goto(f"{base_url}/admin/bots/{bot_id}", wait_until="networkidle")
        browser_page.get_by_label("Lý do tắt").fill("Bot đã phẳng sau operation PAPER")
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith(f"/admin/bots/{bot_id}/disable")
            )
        ) as disable_response_info:
            browser_page.get_by_role("button", name="Tắt bot").press("Enter")
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
            browser_page.get_by_role("button", name="Bật bot sau đối soát").press("Enter")
        assert denied_enable_info.value.status == 409
        assert "Trạng thái đã thay đổi" in denied_enable_info.value.text()
        expect(browser_page.get_by_role("alert")).to_be_visible()
        denial_audit = harness.probe("mb07a_inspect", "migrator")["audits"]
        assert any(
            row[0] == "bot.lifecycle.enable_denied" and row[1] == "BOT_ENABLE_CONFIG_REQUIRED"
            for row in denial_audit
        ), denial_audit
        browser_page.goto(f"{base_url}/admin/bots/{bot_id}", wait_until="networkidle")
        expect(browser_page.get_by_text("Đã tắt / Đã tắt / Mô phỏng", exact=True)).to_be_visible()

        browser_page.get_by_label("Cấu hình bổ sung (JSON)").fill('{"schema_version": 1}')
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith(f"/admin/bots/{bot_id}/config-drafts")
            )
        ) as draft_response_info:
            browser_page.get_by_role("button", name="Tạo bản nháp").press("Enter")
        assert draft_response_info.value.status == 303
        expect(
            browser_page.get_by_text(re.compile(r"Bản nháp [0-9a-f-]+ — phiên bản 1"))
        ).to_be_visible()

        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and "/config-drafts/" in response.url
                and response.url.endswith("/validate")
            )
        ) as validate_response_info:
            browser_page.get_by_role("button", name="Kiểm định bản nháp").press("Enter")
        assert validate_response_info.value.status == 303
        expect(
            browser_page.get_by_role("status").filter(has_text="Bản nháp đã qua kiểm định.")
        ).to_be_visible()

        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and "/config-drafts/" in response.url
                and response.url.endswith("/publish")
            )
        ) as publish_response_info:
            browser_page.get_by_role("button", name="Công bố bản nháp").press("Enter")
        assert publish_response_info.value.status == 303
        expect(browser_page.get_by_role("button", name="Bật bot sau đối soát")).to_be_visible()

        browser_page.get_by_label("Lý do bật lại bot").fill("Đã publish và đối soát runtime")
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith(f"/admin/bots/{bot_id}/enable")
            )
        ) as enabled_response_info:
            browser_page.get_by_role("button", name="Bật bot sau đối soát").press("Enter")
        assert enabled_response_info.value.status == 303
        expect(browser_page.get_by_text("Đã bật / Đã bật / Mô phỏng", exact=True)).to_be_visible()
        entry_state = (
            browser_page.locator("dt")
            .filter(has_text="Trạng thái mở vị thế")
            .locator("xpath=following-sibling::dd[1]")
        )
        expect(entry_state).to_have_text("Tạm dừng")
        _open_mobile_menu(browser_page)
        browser_page.get_by_role("navigation", name="Điều hướng chính").get_by_role(
            "link", name="Tổng quan", exact=True
        ).press("Enter")
        expect(
            browser_page.get_by_text("Điều khiển không gian làm việc: Đang hoạt động", exact=True)
        ).to_be_visible()

        _open_mobile_menu(browser_page)
        browser_page.get_by_role("link", name="Telegram", exact=True).press("Enter")
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
            revoke_form.get_by_role("button", name="Thu hồi").press("Enter")
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
            browser_page.get_by_role("button", name="Tạo mã liên kết").press("Enter")
        assert invalid_csrf_info.value.status == 403
        assert_responsive(browser_page, "csrf-error")
        browser_page.goto(f"{base_url}/admin/telegram", wait_until="networkidle")

        expect(browser_page.get_by_label("Lý do", exact=True)).to_be_visible()

        browser_page.get_by_label("Lý do", exact=True).fill("Kiểm tra liên kết bằng trình duyệt")
        with browser_page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/admin/telegram/link")
            )
        ) as link_response_info:
            browser_page.get_by_role("button", name="Tạo mã liên kết").press("Enter")
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
        expect(browser_page.get_by_role("main").get_by_role("status")).to_be_visible()
        assert_responsive(browser_page, "telegram-link")
        assert external_requests == []
    finally:
        harness.down()


VIEWPORTS = [(360, 800), (390, 844), (768, 1024), (1024, 768), (1280, 800)]
EVIDENCE = (
    ROOT / ".superpowers/sdd/2026-11-02-wp04-mb07-admin-mobile-vietnamese-ui-plan/ui-evidence"
)
PNL_EVIDENCE = ROOT / "tests/e2e/evidence"


def assert_pnl_color_semantics(page: Page) -> None:
    """Kiểm tra dấu hiển thị, màu chữ tương phản và nội dung không phụ thuộc màu."""
    values = page.locator(".pnl-value[data-pnl-tone]").all()
    assert values, "Trang không có giá trị lãi/lỗ để kiểm tra màu ngữ nghĩa."
    for value in values:
        tone = value.get_attribute("data-pnl-tone")
        state = value.evaluate(
            "element => ({"
            "color: getComputedStyle(element).color, "
            "text: element.textContent.trim()"
            "})"
        )
        if tone == "positive":
            assert state["text"].startswith("+"), state
            assert state["color"] == "rgb(8, 122, 85)", state
        elif tone == "negative":
            assert state["text"].startswith("-"), state
            assert state["color"] == "rgb(180, 35, 58)", state
        elif tone in {"flat", "unknown"}:
            assert state["color"] == "rgb(66, 88, 107)", state
        else:
            raise AssertionError(f"Trạng thái PnL không nhận diện: {tone!r}")


# Liệt kê phần tử vượt mép phải mà không nằm trong vùng cuộn riêng (để chẩn đoán tràn ngang).
_OVERFLOW_DIAGNOSTIC_JS = """() => [...document.querySelectorAll('body *')].filter((e) => {
  const r = e.getBoundingClientRect();
  if (r.width === 0 || r.right <= innerWidth + 1) return false;
  for (let p = e.parentElement; p; p = p.parentElement) {
    if (getComputedStyle(p).overflowX !== 'visible') return false;
  }
  return true;
}).slice(0, 8).map((e) => {
  const right = Math.round(e.getBoundingClientRect().right);
  return `${e.tagName}.${e.className}#${e.id} right=${right} viewport=${innerWidth}`;
})"""


def _open_mobile_menu(page: Page) -> None:
    """Màn hình hẹp: mở ngăn kéo điều hướng trước khi chọn một mục trong đó."""
    menu = page.get_by_role("button", name="Menu")
    if menu.is_visible():
        menu.click()
        expect(page.get_by_role("navigation", name="Điều hướng chính")).to_be_visible()


def assert_responsive(page: Page, name: str) -> None:
    """Đọc các trang thật ở năm kích thước và kiểm tra reflow tương đương phóng đại 200%."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    original_viewport = page.viewport_size
    assert original_viewport is not None
    for width, height in VIEWPORTS:
        page.set_viewport_size({"width": width, "height": height})
        page.evaluate(
            """() => new Promise(resolve =>
                requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
        )
        assert page.locator("html").get_attribute("lang") == "vi"
        expect(page.get_by_role("main")).to_be_visible()
        expect(page.get_by_role("heading", level=1)).to_have_count(1)
        if name == "reports-metrics":
            assert page.locator(".mobile-trade-list").is_visible() == (width < 1024)
            assert page.locator(".desktop-trades").is_visible() == (width >= 1024)
        fits_viewport = page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if not fits_viewport:
            overflow_diagnostic = page.evaluate(
                """() => {
                    const describe = element => {
                        const rect = element.getBoundingClientRect();
                        return {
                            tag: element.tagName,
                            id: element.id,
                            className: typeof element.className === 'string'
                                ? element.className : '',
                            left: Math.round(rect.left),
                            right: Math.round(rect.right),
                            width: Math.round(rect.width),
                            scrollWidth: element.scrollWidth,
                            clientWidth: element.clientWidth,
                            overflowX: getComputedStyle(element).overflowX,
                        };
                    };
                    const overflowing = Array.from(document.querySelectorAll('body *'))
                        .filter(element => {
                            const rect = element.getBoundingClientRect();
                            if (rect.right <= innerWidth + 1 && rect.left >= -1) return false;
                            for (
                                let parent = element.parentElement;
                                parent;
                                parent = parent.parentElement
                            ) {
                                if (getComputedStyle(parent).overflowX !== 'visible') return false;
                            }
                            return true;
                        })
                        .map(describe)
                        .slice(0, 12);
                    return JSON.stringify(overflowing);
                }"""
            )
            assert fits_viewport, f"{name}@{width}: {overflow_diagnostic}"
        for element in page.locator("input:not([type=hidden]), select, textarea, button").all():
            if element.is_visible():
                bounds = element.bounding_box()
                assert bounds is not None
                assert bounds["height"] >= 44, (name, width, bounds)
        if name in {"dashboard-metrics", "flatten-preview", "reports-metrics", "login"}:
            page.screenshot(path=str(EVIDENCE / f"{name}-{width}.png"), full_page=True)
        if name in {"dashboard-metrics", "reports-metrics"} and width in {390, 1280}:
            PNL_EVIDENCE.mkdir(parents=True, exist_ok=True)
            view = "dashboard" if name == "dashboard-metrics" else "report"
            device = "mobile" if width == 390 else "desktop"
            page.screenshot(
                path=str(PNL_EVIDENCE / f"binance-pnl-{view}-{device}.png"), full_page=True
            )
        if name == "reports-metrics" and width == 360:
            page.evaluate("() => document.fonts.ready")
            measurements = page.evaluate(
                """() => {
                    const canvas = document.createElement('canvas');
                    const context = canvas.getContext('2d');
                    return ['start', 'end'].map(id => {
                        const input = document.getElementById(id);
                        const style = getComputedStyle(input);
                        context.font = style.font;
                        const horizontalPadding = parseFloat(style.paddingLeft)
                            + parseFloat(style.paddingRight);
                        return {
                            id,
                            textWidth: context.measureText(input.value).width,
                            availableWidth: input.clientWidth - horizontalPadding,
                        };
                    });
                }"""
            )
            assert all(item["textWidth"] <= item["availableWidth"] for item in measurements), (
                name,
                width,
                measurements,
            )
    # Mô phỏng zoom trình duyệt bằng viewport CSS hẹp hơn, để media query được áp dụng.
    zoom_width = max(320, original_viewport["width"] // 2)
    zoom_height = max(320, original_viewport["height"] // 2)
    page.set_viewport_size({"width": zoom_width, "height": zoom_height})
    page.evaluate(
        """() => new Promise(resolve =>
            requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
        name,
        "200%",
    )
    page.keyboard.press("Tab")
    assert page.locator(":focus").count() == 1
    assert (
        page.locator(":focus").evaluate("element => getComputedStyle(element).outlineStyle")
        != "none"
    )
    page.set_viewport_size(original_viewport)
    page.evaluate(
        """() => new Promise(resolve =>
            requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
    )


def assert_font_and_fallback(page: Page) -> None:
    """Chứng minh font cục bộ có glyph tiếng Việt và fallback vẫn đọc được khi chặn tải font."""
    sample = "Tiếng Việt: ă â ê ô ơ ư đ ắ ằ ấ ầ ễ ệ"
    page.evaluate(
        """sample => {
        const p = document.createElement('p'); p.id = 'font-proof'; p.textContent = sample;
        p.style.fontSize = '14px'; document.querySelector('main').prepend(p);
    }""",
        sample,
    )
    for weight in [400, 600, 700]:
        loaded = page.evaluate(
            """async ({weight, sample}) => {
            const faces = await document.fonts.load(`${weight} 14px "Be Vietnam Pro"`, sample);
            return faces.length > 0 && faces.every(face => face.status === 'loaded');
        }""",
            {"weight": weight, "sample": sample},
        )
        assert loaded
    cdp = page.context.new_cdp_session(page)
    cdp.send("DOM.enable")
    cdp.send("CSS.enable")
    root = cdp.send("DOM.getDocument")["root"]["nodeId"]
    node = cdp.send("DOM.querySelector", {"nodeId": root, "selector": "#font-proof"})["nodeId"]
    fonts = cdp.send("CSS.getPlatformFontsForNode", {"nodeId": node})["fonts"]
    assert any(
        font["isCustomFont"] and font["familyName"] == "Be Vietnam Pro" and font["glyphCount"] > 20
        for font in fonts
    ), fonts
    (EVIDENCE / "font-proof.txt").write_text(str(fonts), encoding="utf-8")
    page.screenshot(path=str(EVIDENCE / "vietnamese-font-14px.png"), full_page=True)
    page.locator("#font-proof").screenshot(path=str(EVIDENCE / "vietnamese-glyphs-14px.png"))
    page.locator("#font-proof").evaluate("element => element.remove()")
    cdp.detach()
    page.route("**/static/fonts/*.woff2", lambda route: route.abort())
    page.reload(wait_until="networkidle")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(page.get_by_role("heading", name="Tổng quan không gian làm việc")).to_be_visible()
    page.screenshot(path=str(EVIDENCE / "font-fallback.png"), full_page=True)
    page.unroute("**/static/fonts/*.woff2")
    page.reload(wait_until="networkidle")


DASHBOARD_LIVE_PROBE = """
async def dashboard_live_seed():
    async with runtime_app(tick=20) as app:
        closed_entry = await entry(app, "b", 0, 1,
            at=datetime.now(UTC) - timedelta(minutes=10))
        closed_fill = await fill(app, "b", closed_entry)
        assert closed_fill["trade_id"], closed_fill
        await stop_out(app, "b", closed_entry, closed_fill["trade_id"])
        opened_entry = await entry(app, "a", 0, 1,
            at=datetime.now(UTC) - timedelta(minutes=1))
        opened_fill = await fill(app, "a", opened_entry)
        assert opened_fill["trade_id"], opened_fill
    return {"live_open_trade_id": opened_fill["trade_id"],
        "live_closed_trade_id": closed_fill["trade_id"]}
PHASES["dashboard_live_seed"] = dashboard_live_seed
"""


@pytest.mark.parametrize("interaction_viewport", [(390, 844), (1280, 800)])
def test_admin_dashboard_live_snapshot_selection_polling_and_stale(
    browser_page: Page, interaction_viewport: tuple[int, int]
) -> None:
    """Bắt lỗi trộn bot, mất ledger khi lỗi poll, phản hồi cũ và tràn ngang."""
    browser_page.set_viewport_size(
        {"width": interaction_viewport[0], "height": interaction_viewport[1]}
    )
    harness = AdminWebHarness()
    harness.probe_code = harness.probe_code.replace(
        "\n_main()\n", DASHBOARD_LIVE_PROBE + "\n_main()\n"
    )
    try:
        harness.start("admin-web")
        base_url = f"http://127.0.0.1:{harness.environment['TRADING_BOT_WEB_PORT']}"
        external_requests: list[str] = []
        browser_page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(base_url + "/") else route.abort()
            ),
        )
        browser_page.on(
            "request",
            lambda request: (
                external_requests.append(request.url)
                if not request.url.startswith(base_url + "/")
                else None
            ),
        )
        harness.state = harness.probe(
            "seed", "migrator", variants={"main": "1", "other": "1"}, fresh_mark_only=True
        )
        harness.probe("genesis")
        harness.state |= harness.probe("dashboard_live_seed")
        harness.state |= harness.probe("mb07a_auth_seed", "migrator")
        session = harness.state["mb07a_sessions"]["owner"]
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
        bot_ids = harness.state["workspaces"]["main"]["bots"]
        snapshots = {
            key: browser_page.request.get(
                f"{base_url}/admin/dashboard/bots/{bot_ids[key]}/snapshot"
            )
            for key in ("a", "b")
        }
        for response in snapshots.values():
            assert response.status == 200, response.text()
            assert response.headers["cache-control"] == "no-store"
        snapshot_a = snapshots["a"].json()
        snapshot_b = snapshots["b"].json()
        assert snapshot_a["source"] == "PAPER_LEDGER"
        assert snapshot_a["open_trades"][0]["trade_id"] == harness.state["live_open_trade_id"]
        # Vốn ban đầu đọc qua quyền SELECT theo cột + RLS của admin-web (migration d2e4f6a8b0c1);
        # thiếu quyền thì snapshot âm thầm trả None, nên E2E phải thấy giá trị thật.
        for snapshot in (snapshot_a, snapshot_b):
            assert snapshot["account"] is not None, snapshot.get("account")
            assert snapshot["account"]["quote_asset"] == "USDT"
            assert Decimal(snapshot["account"]["initial_equity"]) > 0
        assert (
            snapshot_b["recent_closed_trades"][0]["trade_id"]
            == harness.state["live_closed_trade_id"]
        )
        # Giữ một phản hồi thật để mô phỏng đường truyền không tuân thủ AbortSignal.
        browser_page.add_init_script(
            """
            window.__snapshotRequests = [];
            const originalFetch = window.fetch.bind(window);
            window.fetch = async (url, options = {}) => {
                if (!String(url).includes('/snapshot')) return originalFetch(url, options);
                window.__snapshotRequests.push({url: String(url),
                    credentials: options.credentials, cache: options.cache});
                const hold = window.__holdNextSnapshot;
                window.__holdNextSnapshot = false;
                const response = await originalFetch(url,
                    hold ? {...options, signal: undefined} : options);
                if (hold) {
                    window.__heldSnapshotReady = true;
                    await new Promise(resolve => window.__releaseSnapshot = resolve);
                }
                return response;
            };
            """
        )
        browser_page.goto(f"{base_url}/admin", wait_until="networkidle")
        panel = browser_page.get_by_role("region", name="Bot đang theo dõi")
        buttons = browser_page.locator("button[data-dashboard-bot]")
        expect(buttons).to_have_count(2)
        selected = browser_page.locator('button[data-dashboard-bot][aria-pressed="true"]')
        expect(selected).to_have_count(1)
        button_a = browser_page.locator(f'button[data-dashboard-bot="{bot_ids["a"]}"]')
        button_b = browser_page.locator(f'button[data-dashboard-bot="{bot_ids["b"]}"]')
        button_a.press("Enter")
        expect(panel).to_contain_text("PAPER ledger snapshot")
        expect(panel.locator("[data-observed-at]")).to_contain_text("UTC")
        session_select = panel.get_by_role("combobox", name="Phiên PAPER đang mở")
        expect(session_select).to_be_enabled()
        session_select.select_option(harness.state["live_open_trade_id"])
        detail = panel.locator("[data-open-trade-detail]")
        for field in ("trade_id", "symbol", "quote_asset", "opened_at"):
            value = snapshot_a["open_trades"][0][field]
            if field != "opened_at":
                expect(detail).to_contain_text(value)
        expect(detail).to_contain_text("UTC")
        session_select.focus()
        session_select.press("ArrowDown")
        expect(session_select).to_be_focused()
        first_observation = panel.locator("[data-observed-at]").inner_text()
        with browser_page.expect_response(
            lambda response: response.url.endswith(f"/{bot_ids['a']}/snapshot")
        ):
            browser_page.wait_for_timeout(5200)
        expect(panel.locator("[data-observed-at]")).not_to_have_text(first_observation)
        requests = browser_page.evaluate("window.__snapshotRequests")
        assert all(bot_ids["a"] in row["url"] for row in requests), requests
        assert all(
            row["credentials"] == "same-origin" and row["cache"] == "no-store" for row in requests
        ), requests
        expect(session_select).to_have_value(harness.state["live_open_trade_id"])
        expect(detail).to_contain_text(harness.state["live_open_trade_id"])
        expect(session_select.locator("option")).to_have_count(1)
        assert browser_page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        device = "mobile" if interaction_viewport[0] == 390 else "desktop"
        PNL_EVIDENCE.mkdir(parents=True, exist_ok=True)
        panel.scroll_into_view_if_needed()
        browser_page.screenshot(
            path=str(PNL_EVIDENCE / f"wp07-live-dashboard-{device}.png"), full_page=True
        )
        browser_page.evaluate("window.__holdNextSnapshot = true")
        browser_page.wait_for_function("() => window.__heldSnapshotReady === true", timeout=8000)
        selection_request_count = browser_page.evaluate("window.__snapshotRequests.length")
        button_b.focus()
        button_b.press("Enter")
        expect(selected).to_have_attribute("data-dashboard-bot", bot_ids["b"])
        expect(panel.locator("[data-closed-trades]")).to_contain_text(
            harness.state["live_closed_trade_id"]
        )
        expect(panel.locator("[data-open-trade-detail]")).not_to_contain_text(
            harness.state["live_open_trade_id"]
        )
        browser_page.evaluate("window.__releaseSnapshot()")
        browser_page.wait_for_timeout(100)
        expect(panel.locator("[data-selected-bot-name]")).to_have_text(snapshot_b["bot"]["name"])
        expect(panel.locator("[data-totals]")).to_contain_text("USDT")
        expect(panel.locator("[data-closed-trades]")).to_contain_text("UTC")
        last_observation = panel.locator("[data-observed-at]").inner_text()
        browser_page.route(
            f"**/dashboard/bots/{bot_ids['b']}/snapshot",
            lambda route: route.fulfill(status=503, json={"detail": "Không sẵn sàng"}),
        )
        expect(panel.locator("[data-snapshot-status]")).to_contain_text("Dữ liệu cũ", timeout=8000)
        expect(panel.locator("[data-observed-at]")).to_have_text(last_observation)
        expect(panel.locator("[data-closed-trades]")).to_contain_text(
            harness.state["live_closed_trade_id"]
        )
        browser_page.unroute(f"**/dashboard/bots/{bot_ids['b']}/snapshot")
        expect(panel.locator("[data-snapshot-status]")).to_contain_text("Đã cập nhật", timeout=8000)
        # Dùng visibilitychange xác định thay cho phụ thuộc quản lý tab của headless Chromium.
        browser_page.evaluate(
            """() => {
                Object.defineProperty(document, 'hidden', {configurable: true, value: true});
                document.dispatchEvent(new Event('visibilitychange'));
            }"""
        )
        request_count = browser_page.evaluate("window.__snapshotRequests.length")
        browser_page.wait_for_timeout(5300)
        assert browser_page.evaluate("window.__snapshotRequests.length") == request_count
        browser_page.evaluate(
            """() => {
                Object.defineProperty(document, 'hidden', {configurable: true, value: false});
                document.dispatchEvent(new Event('visibilitychange'));
            }"""
        )
        expect(panel.locator("[data-snapshot-status]")).to_contain_text("Đã cập nhật")
        assert browser_page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            browser_page.evaluate(_OVERFLOW_DIAGNOSTIC_JS)
        )
        for button in buttons.all():
            assert button.bounding_box()["height"] >= 44
        browser_page.route(
            f"**/dashboard/bots/{bot_ids['b']}/snapshot",
            lambda route: route.fulfill(status=404, json={"detail": "Không thể tải"}),
        )
        expect(panel.locator("[data-snapshot-status]")).to_contain_text(
            "Đã dừng cập nhật", timeout=8000
        )
        request_count = browser_page.evaluate("window.__snapshotRequests.length")
        browser_page.wait_for_timeout(5300)
        assert browser_page.evaluate("window.__snapshotRequests.length") == request_count
        requests_after_selection = browser_page.evaluate(
            "window.__snapshotRequests",
        )[selection_request_count:]
        assert requests_after_selection
        assert all(bot_ids["b"] in row["url"] for row in requests_after_selection)
        assert external_requests == []
    finally:
        harness.down()

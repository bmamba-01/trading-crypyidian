"""E2E LIVE-MON (CRY-429): màn hình theo dõi bot trực tiếp trên Compose + PostgreSQL thật.

Kiểm: SSE đẩy sự kiện PAPER mới lên trang trong vài giây, quyền theo workspace/grant (bot của
workspace khác và người không có grant không thấy gì), fallback poll khi luồng SSE bị chặn,
không gọi ra ngoài và không tràn ngang ở màn hình hẹp.
"""

import time

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_admin_web_workflows import (
    _OVERFLOW_DIAGNOSTIC_JS,
    PNL_EVIDENCE,
    AdminWebHarness,
)
from trading_bot.auth.cookies import session_cookie_name
from trading_bot.config.settings import AppSettings

pytest_plugins = ("tests.e2e.browser_fixtures",)

# Độ trễ tối đa chấp nhận từ lúc ledger ghi sự kiện tới khi trang hiển thị (SSE tick 1,5 s).
MAX_UPDATE_SECONDS = 8.0

LIVE_MONITOR_PROBE = """
async def live_monitor_seed():
    async with runtime_app(tick=20) as app:
        closed_entry = await entry(app, "b", 0, 1,
            at=datetime.now(UTC) - timedelta(minutes=10))
        closed_fill = await fill(app, "b", closed_entry)
        assert closed_fill["trade_id"], closed_fill
        await stop_out(app, "b", closed_entry, closed_fill["trade_id"])
        opened_entry = await entry(app, "a", 0, 1,
            at=datetime.now(UTC) - timedelta(minutes=10))
        opened_fill = await fill(app, "a", opened_entry)
        assert opened_fill["trade_id"], opened_fill
    return {"live_submitted": opened_entry, "live_trade_id": opened_fill["trade_id"],
        "live_closed_trade_id": closed_fill["trade_id"]}
PHASES["live_monitor_seed"] = live_monitor_seed

async def live_monitor_close():
    async with runtime_app(tick=20) as app:
        result = await stop_out(app, "a", STATE["live_submitted"], STATE["live_trade_id"])
    assert result["closed"], result
    return {"live_close": result}
PHASES["live_monitor_close"] = live_monitor_close
"""


def _session_cookie(page: Page, base_url: str, session: dict[str, str]) -> None:
    page.context.add_cookies(
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


@pytest.mark.parametrize("viewport", [(390, 844), (1280, 800)])
def test_live_monitor_streams_paper_events_scoped_and_responsive(
    browser_page: Page, viewport: tuple[int, int]
) -> None:
    browser_page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    harness = AdminWebHarness()
    harness.probe_code = harness.probe_code.replace(
        "\n_main()\n", LIVE_MONITOR_PROBE + "\n_main()\n"
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
        harness.state |= harness.probe("live_monitor_seed")
        harness.state |= harness.probe("mb07a_auth_seed", "migrator")
        bots = harness.state["workspaces"]["main"]["bots"]
        foreign_bot = harness.state["workspaces"]["other"]["bots"]["a"]
        _session_cookie(browser_page, base_url, harness.state["mb07a_sessions"]["owner"])

        # --- API poll dự phòng: phạm vi workspace + nội dung sự kiện/trạng thái ---------------
        snapshot = browser_page.request.get(
            f"{base_url}/admin/api/live/snapshot?bot={bots['a']}&bot={bots['b']}&bot={foreign_bot}"
        )
        assert snapshot.status == 200, snapshot.text()
        assert snapshot.headers["cache-control"] == "no-store"
        payload = snapshot.json()
        assert payload["bot_ids"] == [bots["a"], bots["b"]]
        kinds = {event["kind"] for event in payload["events"]}
        assert {"RESERVATION", "ORDER_ACCEPTED", "ENTRY_FILL", "POSITION_CLOSED"} <= kinds, kinds
        assert all(event["bot_id"] in {bots["a"], bots["b"]} for event in payload["events"])
        closed = [event for event in payload["events"] if event["kind"] == "POSITION_CLOSED"]
        assert closed[0]["bot_id"] == bots["b"]
        assert closed[0]["metrics"]["exit_reason"] == "STOP_LOSS"
        assert closed[0]["severity"] == "alert"
        assert closed[0]["metrics"]["net_pnl"] is not None
        state = payload["state"]
        (position,) = state["positions"]
        assert position["trade_id"] == harness.state["live_trade_id"]
        assert position["stop_price"] is not None
        assert position["hold_seconds"] > 0
        assert {bot["id"] for bot in state["bots"]} == {bots["a"], bots["b"]}
        assert all(bot["equity"]["equity"] is not None for bot in state["bots"])
        assert set(state["health"]) >= {"feed", "mark", "clock", "outbox", "breaker"}
        assert "secret" not in snapshot.text().lower()

        # Thành viên không có grant bot (analyst_reviewer) không thấy bot nào.
        analyst = harness.state["mb07a_sessions"]["analyst_reviewer"]
        denied = browser_page.request.get(
            f"{base_url}/admin/api/live/snapshot?bot={bots['a']}",
            headers={
                "Cookie": f"{session_cookie_name(AppSettings(app_env='local'))}="
                f"{analyst['workspace']}:{analyst['token']}"
            },
        )
        assert denied.status == 200, denied.text()
        assert denied.json()["bot_ids"] == []
        assert denied.json()["events"] == []

        # --- Trang Live nhận dữ liệu qua SSE -------------------------------------------------
        browser_page.goto(
            f"{base_url}/admin/live?bot={bots['a']}&bot={bots['b']}", wait_until="domcontentloaded"
        )
        connection = browser_page.locator("[data-live-connection]")
        expect(connection).to_have_attribute("data-state", "sse", timeout=15_000)
        feed = browser_page.locator("[data-live-feed]")
        expect(feed.locator('[data-kind="POSITION_CLOSED"]')).to_have_count(1)
        row = browser_page.locator(f'[data-trade-id="{harness.state["live_trade_id"]}"]')
        expect(row).to_be_visible()
        expect(browser_page.locator(f'[data-bot-row="{bots["a"]}"]')).to_be_visible()
        expect(browser_page.locator("[data-live-kill]")).to_contain_text("Đang hoạt động")
        expect(browser_page.locator("[data-live-equity-value]")).not_to_have_text("—")

        # --- Sự kiện PAPER mới: chạm SL → trang cập nhật trong vài giây ------------------------
        harness.state |= harness.probe("live_monitor_close")
        written_at = time.monotonic()
        expect(feed.locator('[data-kind="POSITION_CLOSED"]')).to_have_count(
            2, timeout=int(MAX_UPDATE_SECONDS * 1000)
        )
        latency = time.monotonic() - written_at
        assert latency <= MAX_UPDATE_SECONDS, latency
        newest = feed.locator('[data-kind="POSITION_CLOSED"]').first
        expect(newest).to_have_attribute("data-severity", "alert")
        expect(newest).to_have_class("live-mon__event live-mon__event--alert is-new")
        expect(browser_page.locator("[data-live-alerts]")).to_contain_text("Dừng lỗ")
        expect(row).to_have_count(0, timeout=int(MAX_UPDATE_SECONDS * 1000))
        expect(browser_page.locator("[data-live-pos-count]")).to_have_text("0")

        # Bộ lọc "tín hiệu bị bỏ" và âm thanh là tùy chọn của người xem, không gửi gì lên server.
        browser_page.get_by_label("Hiện tín hiệu bị bỏ").uncheck()
        assert browser_page.evaluate(
            "[...document.querySelectorAll('[data-severity=\"muted\"]')].every((e) => e.hidden)"
        )

        assert browser_page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            browser_page.evaluate(_OVERFLOW_DIAGNOSTIC_JS)
        )
        device = "mobile" if viewport[0] < 768 else "desktop"
        PNL_EVIDENCE.mkdir(parents=True, exist_ok=True)
        browser_page.screenshot(
            path=str(PNL_EVIDENCE / f"cry429-live-monitor-{device}.png"), full_page=True
        )

        # --- Fallback poll khi proxy/trình duyệt chặn SSE -------------------------------------
        browser_page.route("**/admin/api/live/stream*", lambda route: route.abort())
        browser_page.reload(wait_until="domcontentloaded")
        expect(connection).to_have_attribute("data-state", "poll", timeout=20_000)
        expect(feed.locator('[data-kind="POSITION_CLOSED"]')).to_have_count(2)
        expect(browser_page.locator("[data-live-pos-count]")).to_have_text("0")
        assert external_requests == []
    finally:
        harness.down()

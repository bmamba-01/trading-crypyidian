"""E2E Playwright CRY-428: ghi đè thông số thoát lệnh theo bot trên trang chi tiết bot.

Hành trình (owner quyết định 17/11/2026: chỉ trường release vốn có; bắt buộc backtest bản gốc vs
bản nháp trước khi công bố, owner tự quyết):
1. Ô "Thông số thoát lệnh" chỉ hiện trường release H03 có (SL, trailing, giữ tối đa), gợi ý giá
   trị gốc; nhập SL "1,5" và giữ tối đa 60 → tạo bản nháp chứa đúng khối ``exit``.
2. Công bố khi chưa backtest → quay lại trang với thông báo chặn + lệnh chạy backtest.
3. Có kết quả backtest OWNER_DRAFT của đúng bản nháp → trang hiện kết luận, công bố thành công.
App dựng trong tiến trình với header bảo mật thật (CSP); service bot giả giữ trạng thái; gate
thật trên DB được E2E Compose RT-1 phủ.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn
from playwright.sync_api import Page, sync_playwright

from tests.e2e.csp_harness import (
    browser_violations,  # noqa: F401 - fixture nạp qua usefixtures
    watch_browser_violations,
)
from tests.e2e.test_admin_trading_revamp_playwright import (
    BOT_ID,
    MockRevampAdminService,
    create_revamp_test_app,
)
from trading_bot.admin.bot_service import EXIT_BACKTEST_REQUIRED_MESSAGE, BotAdminError
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay

SCREENSHOTS_DIR = Path(__file__).resolve().parent / "evidence" / "admin-playwright"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
COOKIE = "9b2299f1-a58e-45c7-b0ad-83feb0862510:exit-override-e2e"

pytestmark = pytest.mark.usefixtures("browser_violations")


class _BotService(MockRevampAdminService):
    """Bot H03 v3 1h đang publish; bản nháp/publish/backtest giữ trong bộ nhớ."""

    def __init__(self) -> None:
        self.published = {
            "id": str(uuid4()),
            "status": "PUBLISHED",
            "revision": 1,
            "overlay": {"schema_version": 1, "strategy": {"release_id": "h03-v3-1h"}},
        }
        self.draft: dict[str, Any] | None = None
        self.backtests: list[dict[str, Any]] = []
        self.publish_calls = 0

    async def get_bot(self, principal: Principal, *, bot_id: UUID) -> dict[str, Any]:
        base = await super().get_bot(principal, bot_id=bot_id)
        versions = [self.draft] if self.draft else []
        return base | {"versions": [*versions, self.published]}

    async def create_draft(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        overlay: BotConfigOverlay,
        expected_parent_version_id: UUID | None,
        **_: Any,
    ) -> dict[str, Any]:
        assert str(expected_parent_version_id) == self.published["id"]
        self.draft = {
            "id": str(uuid4()),
            "status": "DRAFT",
            "revision": 1,
            "overlay": canonical_overlay(overlay),
        }
        return self.draft

    async def publish(
        self, principal: Principal, *, bot_id: UUID, draft_id: UUID, expected_revision: int
    ) -> dict[str, Any]:
        self.publish_calls += 1
        assert self.draft is not None
        assert str(draft_id) == self.draft["id"]
        if not any(item["draft_id"] == self.draft["id"] for item in self.backtests):
            raise BotAdminError(
                409,
                "Cần backtest.",
                [{"field": "exit", "message": EXIT_BACKTEST_REQUIRED_MESSAGE}],
            )
        self.published = self.draft | {"status": "PUBLISHED"}
        self.draft = None
        return self.published

    async def list_proposal_backtests(
        self, principal: Principal, *, bot_id: UUID, limit: int = 10
    ) -> list[dict[str, Any]]:
        return list(self.backtests)


@pytest.fixture
def server() -> Iterator[tuple[str, _BotService]]:
    app = create_revamp_test_app()
    service = _BotService()
    app.state.bot_admin_service = service
    principal = Principal(
        user_id=UUID(int=1),
        membership_id=UUID(int=2),
        workspace_id=UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510"),
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )
    app.state.auth_service.sessions[COOKIE] = AuthIdentity(
        principal=principal, email="owner@crypidian.io"
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    uv_server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv_server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert uv_server.started
    try:
        yield f"http://127.0.0.1:{port}", service
    finally:
        uv_server.should_exit = True
        thread.join(timeout=5)


def _open(base: str, page: Page) -> None:
    page.context.add_cookies(
        [
            {
                "name": "crypidian_session",
                "value": COOKIE,
                "domain": "127.0.0.1",
                "path": "/",
                "httpOnly": True,
                "sameSite": "Lax",
            }
        ]
    )
    watch_browser_violations(page)
    page.goto(f"{base}/admin/bots/{BOT_ID}", wait_until="networkidle")


def test_owner_tunes_bot_exit_and_publish_requires_backtest(
    server: tuple[str, _BotService],
) -> None:
    base, service = server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_context(viewport={"width": 1366, "height": 900}).new_page()
            _open(base, page)
            fieldset = page.locator("#overlay-exit")
            assert "h03-v3-1h" in fieldset.locator("legend").inner_text()
            names = fieldset.locator("input[name^='exit_']").evaluate_all(
                "els => els.map(e => e.name)"
            )
            assert names == [
                "exit_stop_atr_multiple",
                "exit_trailing_atr_multiple",
                "exit_max_hold_bars",
            ]
            stop = page.locator("#overlay-exit-stop_atr_multiple")
            assert stop.get_attribute("placeholder") == "2 (release)"
            assert stop.input_value() == ""

            stop.fill("1,5")
            page.fill("#overlay-exit-max_hold_bars", "60")
            page.locator(".bot-overlay-form").get_by_role("button", name="Tạo bản nháp").click()
            page.wait_for_load_state("networkidle")
            assert service.draft is not None
            assert service.draft["overlay"]["exit"] == {
                "stop_atr_multiple": "1.5",
                "max_hold_bars": 60,
            }
            review = page.locator("#exit-review")
            assert "Bản nháp đổi thông số thoát lệnh" in review.inner_text()
            assert f"--bot-draft {BOT_ID}" in review.inner_text()
            # Form giữ giá trị ghi đè của bản nháp để tạo nháp mới không làm mất khối exit.
            assert page.locator("#overlay-exit-stop_atr_multiple").input_value() == "1.5"

            page.get_by_role("button", name="Công bố bản nháp").click()
            page.wait_for_url("**draft_validation=exit_backtest_required")
            assert "chưa có backtest" in page.locator("#exit-backtest-required").inner_text()
            assert service.publish_calls == 1
            assert service.published["revision"] == 1  # chưa đổi

            service.backtests.append(
                {
                    "id": str(uuid4()),
                    "source": "OWNER_DRAFT",
                    "draft_id": service.draft["id"],
                    "verdict": "WORSE",
                    "changes": [{"field": "exit", "to": {"stop_atr_multiple": "1.5"}}],
                    "rationale": "",
                    "baseline": {"trades": 12, "net_pnl": "35.10", "max_drawdown": "20.00"},
                    "variant": {"trades": 15, "net_pnl": "-4.20", "max_drawdown": "31.00"},
                    "notes": [],
                    "window": None,
                    "draftable": False,
                }
            )
            page.goto(f"{base}/admin/bots/{BOT_ID}", wait_until="networkidle")
            text = page.locator("#exit-review").inner_text()
            assert "Kém hơn" in text
            assert "35.10 → -4.20" in text
            assert "Bản nháp owner" in page.locator("#ai-proposals").inner_text()
            page.screenshot(
                path=str(SCREENSHOTS_DIR / "cry428_bot_exit_desktop.png"), full_page=True
            )
            # Owner tự quyết: kết luận kém hơn vẫn công bố được.
            page.get_by_role("button", name="Công bố bản nháp").click()
            page.wait_for_load_state("networkidle")
            assert service.draft is None
            assert service.published["overlay"]["exit"]["stop_atr_multiple"] == "1.5"
            assert page.locator("#exit-review").count() == 0
        finally:
            browser.close()


def test_exit_fields_fit_mobile_without_horizontal_scroll(
    server: tuple[str, _BotService],
) -> None:
    base, _service = server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
            )
            page = context.new_page()
            _open(base, page)
            field = page.locator("#overlay-exit-stop_atr_multiple")
            field.scroll_into_view_if_needed()
            box = field.bounding_box()
            assert box is not None
            assert box["height"] >= 44
            assert page.evaluate(
                "() => document.documentElement.scrollWidth <= "
                "document.documentElement.clientWidth + 1"
            )
        finally:
            browser.close()

"""Playwright kiểm trang chẩn đoán PAPER tiếng Việt trên desktop và mobile."""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, expect

from trading_bot.app import create_app
from trading_bot.application.learning_paper_performance import PaperPerformanceReportSummary
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.learning.paper_performance import PaperPerformanceStatus

pytest_plugins = ("tests.e2e.browser_fixtures",)

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOTS = ROOT / "tests" / "e2e" / "evidence"
WORKSPACE_ID = UUID("c5bfbca4-b0ba-49f5-a7ee-19d07066e45f")
BOT_ID = UUID("ddbdc199-d67d-4cea-9f6a-5a0e1342e921")
MEMBERSHIP_ID = UUID("7e89ab53-481a-4982-a142-a613f18c13a3")
USER_ID = UUID("f363d2c8-a842-4b73-99cb-7637ed651f3e")
CSRF_TOKEN = "playwright-paper-performance-csrf-token"


def _summary() -> PaperPerformanceReportSummary:
    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    cohorts = []
    for index, (start, end) in enumerate(
        (
            ("2026-08-15T12:00:00+00:00", "2026-09-14T12:00:00+00:00"),
            ("2026-09-14T12:00:00+00:00", "2026-10-14T12:00:00+00:00"),
        ),
        start=1,
    ):
        cohorts.append(
            {
                "window_start": start,
                "window_end": end,
                "eligible_decision_count": 1500,
                "covered_outcome_count": 1300,
                "coverage": "0.8666666666666666666666666667",
                "class_support": [
                    {"class": -1, "count": 430},
                    {"class": 0, "count": 440},
                    {"class": 1, "count": 430},
                ],
                "observed_brier": "0.31",
                "baseline_brier": "0.26",
                "brier_regression": "0.05",
                "observed_log_loss": "1.05",
                "baseline_log_loss": "0.92",
                "log_loss_regression": "0.13",
                "membership_digest": str(index) * 64,
                "status": "degraded",
                "reasons": [],
            }
        )
    return PaperPerformanceReportSummary(
        report_id=UUID("25ba8f4b-3871-4c7d-9da6-8004b7159917"),
        bot_id=BOT_ID,
        as_of=now,
        status=PaperPerformanceStatus.DEGRADED,
        policy_version="wp06-paper-performance-v1",
        policy_hash="a" * 64,
        target_fingerprint="b" * 64,
        baseline_report_hash="c" * 64,
        report_hash="d" * 64,
        report={
            "selection_bias_note": (
                "Chỉ gồm intent PAPER được duyệt, qua risk gate và có giao dịch đóng đúng hạn; "
                "không suy diễn kết quả cho quyết định không vào lệnh hoặc lợi nhuận tương lai."
            ),
            "cohorts": cohorts,
        },
    )


class _AuthService:
    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return CSRF_TOKEN

    async def verify_csrf(self, _cookie: str | None, token: str | None) -> None:
        if token != CSRF_TOKEN:
            raise AssertionError("Token CSRF không khớp form PAPER performance.")


class _PerformanceFacade:
    def __init__(self, principal: Principal, calls: list[tuple[UUID, UUID]]) -> None:
        self.principal = principal
        self.calls = calls

    async def list_recent(self, *, limit: int = 50):
        assert limit == 50
        return (_summary(),)

    async def assess(self, *, bot_id: UUID, idempotency_key: UUID):
        assert bot_id == BOT_ID
        self.calls.append((bot_id, idempotency_key))
        return _summary()


class _PerformanceService:
    def __init__(self) -> None:
        self.facade: _PerformanceFacade | None = None
        self.calls: list[tuple[UUID, UUID]] = []

    def for_principal(self, principal: Principal) -> _PerformanceFacade:
        self.facade = _PerformanceFacade(principal, self.calls)
        return self.facade


class _BotAdminService:
    async def list_bots(self, _principal: Principal):
        return [{"id": str(BOT_ID), "name": "Bot PAPER chính", "account_mode": "PAPER"}]


def _serve_app() -> tuple[str, uvicorn.Server, threading.Thread, socket.socket, FastAPI]:
    application = create_app(AppSettings(app_env="test"))
    identity = AuthIdentity(
        principal=Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({Role.ANALYST_REVIEWER}),
            mfa_verified_at=datetime.now(UTC),
            mfa_method=MfaMethod.TOTP,
        ),
        email="reviewer@example.test",
    )
    application.dependency_overrides[current_identity] = lambda: identity
    application.state.auth_service = _AuthService()
    application.state.learning_paper_performance_service = _PerformanceService()
    application.state.bot_admin_service = _BotAdminService()
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
        name="learning-paper-performance-playwright",
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.025)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        raise RuntimeError("Admin web chưa khởi động trong thời hạn E2E.")
    return f"http://127.0.0.1:{port}", server, thread, listener, application


def _assert_layout(page: Page, *, width: int, height: int) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => resolve()))")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
        width,
        page.evaluate("({scroll:document.documentElement.scrollWidth,inner:innerWidth})"),
    )
    for button in page.get_by_role("button").all():
        if button.is_visible():
            bounds = button.bounding_box()
            assert bounds is not None
            assert bounds["height"] >= 44, (width, bounds)


def test_paper_performance_admin_is_vietnamese_responsive_and_explicit(
    browser_page: Page,
) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    app_url, server, thread, listener, application = _serve_app()
    try:
        browser_page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(app_url + "/") else route.abort()
            ),
        )
        response = browser_page.goto(
            f"{app_url}/admin/learning/paper-performance", wait_until="load"
        )
        assert response is not None
        assert response.status == 200
        assert response.headers["cache-control"] == "no-store"
        expect(
            browser_page.get_by_role("heading", name="Chẩn đoán hiệu năng PAPER")
        ).to_be_visible()
        expect(
            browser_page.get_by_text("Chỉ số dự báo trên các giao dịch PAPER đã quy kết")
        ).to_be_visible()
        expect(browser_page.get_by_text("Cần xem xét hai chỉ số cùng suy giảm")).to_be_visible()
        expect(browser_page.get_by_text("TEST baseline đã đóng băng")).to_be_visible()
        expect(browser_page.get_by_text("PnL âm").first).to_be_visible()
        expect(browser_page.get_by_text("PnL bằng 0").first).to_be_visible()
        expect(browser_page.get_by_text("PnL dương").first).to_be_visible()
        expect(browser_page.get_by_label("Bot PAPER")).to_have_value(str(BOT_ID))
        expect(browser_page.get_by_role("button", name="Tạo snapshot chẩn đoán")).to_be_visible()
        assert "probabilities" not in browser_page.content()
        assert "trade_payload" not in browser_page.content()
        font_family = browser_page.locator("body").evaluate(
            "element => getComputedStyle(element).fontFamily"
        )
        assert "Be Vietnam Pro" in font_family

        _assert_layout(browser_page, width=1280, height=900)
        browser_page.screenshot(
            path=str(SCREENSHOTS / "wp06-paper-performance-desktop.png"), full_page=True
        )
        _assert_layout(browser_page, width=390, height=844)
        browser_page.screenshot(
            path=str(SCREENSHOTS / "wp06-paper-performance-mobile.png"), full_page=True
        )
        with browser_page.expect_response(
            lambda item: (
                item.request.method == "POST"
                and item.url.endswith("/api/v1/learning/paper-performance/assess")
            )
        ) as assessed:
            browser_page.get_by_role("button", name="Tạo snapshot chẩn đoán").click()
        assert assessed.value.status == 200
        request = assessed.value.request
        assert request.headers["x-csrf-token"] == CSRF_TOKEN
        assert UUID(request.headers["idempotency-key"])
        assert request.post_data_json == {"bot_id": str(BOT_ID)}
        service = application.state.learning_paper_performance_service
        assert len(service.calls) == 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()

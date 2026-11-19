"""Playwright kiểm giao diện operator block; service facade không chứng minh DB audit."""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, expect

from trading_bot.admin.learning_paper_entry_block_router import (
    router as paper_entry_block_router,
)
from trading_bot.admin.learning_scoring_page_router import router as scoring_page_router
from trading_bot.app import create_app
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.learning.scorer_score_event_summary import (
    ScorerPaperEntryBlockAuditSummary,
    ScorerScoreEventPage,
    ScorerScoreEventSummary,
)
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentAdminSnapshot,
    ScorerDeploymentHealth,
    ScorerDeploymentMode,
)

pytest_plugins = ("tests.e2e.browser_fixtures",)

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOTS = ROOT / "tests" / "e2e" / "evidence"
WORKSPACE_ID = UUID("bba2a6ed-7b24-43d3-a297-2ac617be105f")
MEMBERSHIP_ID = UUID("4c1fd40f-a99c-4a5c-a713-4ec54f33ddfd")
USER_ID = UUID("9b55d8b2-8f86-4ed7-9941-3310ec1d18db")
BOT_ID = UUID("dbfdcd27-0f38-4554-b270-92505c90df4d")
BOT_DECISION_ID = UUID("f87dfc53-7b4f-45bf-b9f4-4989ae74e55e")
SCORE_EVENT_ID = UUID("723cba6a-e0b8-4a1c-bd08-d0f0a2b5df0d")


class _AuthService:
    """CSRF token thật đi qua middleware/router; danh tính là fixture UI test."""

    def __init__(self) -> None:
        self.verified_tokens: list[str | None] = []

    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return "playwright-paper-block-csrf-token"

    async def verify_csrf(self, _cookie: str | None, token: str | None) -> None:
        self.verified_tokens.append(token)
        if token != "playwright-paper-block-csrf-token":
            raise AssertionError("Token CSRF không khớp form operator block.")


class _DeploymentService:
    def __init__(self, principal: Principal) -> None:
        self.principal = principal

    def for_principal(self, principal: Principal):
        assert principal.workspace_id == WORKSPACE_ID
        return self

    async def load_admin_scoring_deployment(self) -> ScorerDeploymentAdminSnapshot:
        return ScorerDeploymentAdminSnapshot(
            workspace_id=WORKSPACE_ID,
            mode=ScorerDeploymentMode.DISABLED,
            deployment_event_id=None,
            deployment_revision=0,
            active_pointer_version=0,
            health=ScorerDeploymentHealth.DISABLED,
            runtime_usable=False,
        )


class _ScoreEventReadModel:
    """Projection tổng hợp giả phục vụ kiểm route/render, không phải PostgreSQL evidence."""

    def __init__(self) -> None:
        self.audit: ScorerPaperEntryBlockAuditSummary | None = None
        self.page_calls: list[tuple[UUID, int]] = []
        self.link_calls: list[tuple[UUID, UUID]] = []

    async def load_page(self, workspace_id: UUID, *, page: int, page_size: int = 20):
        del page_size
        self.page_calls.append((workspace_id, page))
        item = ScorerScoreEventSummary(
            event_id=SCORE_EVENT_ID,
            created_at=datetime(2026, 11, 18, 8, 40, tzinfo=UTC),
            mode="PAPER_GATE",
            decision="APPROVE",
            reason_code=None,
            probability=0.72,
            confidence=0.68,
            uncertainty=0.18,
            bot_id=BOT_ID,
            bot_decision_id=BOT_DECISION_ID,
            paper_entry_block=self.audit,
        )
        return ScorerScoreEventPage(items=(item,), page=page, has_next=False)

    async def get_blockable_decision_id(
        self, workspace_id: UUID, score_event_id: UUID
    ) -> UUID | None:
        self.link_calls.append((workspace_id, score_event_id))
        return (
            BOT_DECISION_ID
            if (workspace_id, score_event_id) == (WORKSPACE_ID, SCORE_EVENT_ID)
            else None
        )


class _PaperBlockService:
    """Facade chỉ để kiểm browser/router; DB append/audit được kiểm riêng ở Compose."""

    def __init__(self, read_model: _ScoreEventReadModel, principal: Principal) -> None:
        self.read_model = read_model
        self.principal = principal
        self.calls: list[tuple[UUID, str, UUID]] = []
        self.error: Exception | None = None

    def for_principal(self, principal: Principal):
        assert principal.workspace_id == WORKSPACE_ID
        assert principal.roles & {Role.OWNER, Role.RISK_ADMIN}
        return self

    async def eligible_decision_ids(self, bot_decision_ids: tuple[UUID, ...]) -> frozenset[UUID]:
        return frozenset({BOT_DECISION_ID} & set(bot_decision_ids))

    async def block(self, *, bot_decision_id: UUID, reason: str, idempotency_key: UUID):
        assert bot_decision_id == BOT_DECISION_ID
        self.calls.append((bot_decision_id, reason, idempotency_key))
        if self.error is not None:
            raise self.error
        now = datetime.now(UTC)
        self.read_model.audit = ScorerPaperEntryBlockAuditSummary(
            actor_user_id=self.principal.user_id,
            mfa_verified_at=self.principal.mfa_verified_at,
            reason=reason,
            created_at=now,
        )
        return SimpleNamespace(score_event_id=SCORE_EVENT_ID, bot_decision_id=bot_decision_id)


class _ReportReadModel:
    async def load_latest_summary(self, _workspace_id: UUID):
        return None


def _identity() -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({Role.OWNER}),
            mfa_verified_at=datetime.now(UTC),
            mfa_method=MfaMethod.TOTP,
        ),
        email="owner@example.test",
    )


def _serve_app():
    application: FastAPI = create_app(AppSettings(app_env="test"))
    identity = _identity()
    application.dependency_overrides[current_identity] = lambda: identity
    auth_service = _AuthService()
    read_model = _ScoreEventReadModel()
    block_service = _PaperBlockService(read_model, identity.principal)
    application.state.auth_service = auth_service
    application.state.learning_scoring_deployment_service = _DeploymentService(identity.principal)
    application.state.learning_scoring_report_read_model = _ReportReadModel()
    application.state.learning_scoring_score_event_read_model = read_model
    application.state.learning_paper_entry_block_service = block_service

    paths = application.openapi()["paths"]
    if "/admin/learning/scoring" not in paths:
        application.include_router(scoring_page_router)
    if "/admin/learning/scoring/events/{score_event_id}/paper-block" not in paths:
        application.include_router(paper_entry_block_router)

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
        name="learning-paper-entry-block-playwright",
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.025)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        raise RuntimeError("Admin web chưa khởi động trong thời hạn Playwright.")
    return (
        f"http://127.0.0.1:{port}",
        server,
        thread,
        listener,
        auth_service,
        read_model,
        block_service,
    )


def _stop_app(server: uvicorn.Server, thread: threading.Thread, listener: socket.socket) -> None:
    server.should_exit = True
    thread.join(timeout=5)
    listener.close()


def _assert_no_horizontal_overflow(page: Page, *, width: int, height: int) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    dimensions = page.evaluate(
        """() => ({
          scrollWidth: document.documentElement.scrollWidth,
          innerWidth,
          desktopRuleMatches: matchMedia('(min-width: 768px)').matches,
          mobileRuleMatches: matchMedia('(max-width: 767.98px)').matches,
          table: (() => {
            const element = document.querySelector('.lx-table--scoring-history');
            const style = getComputedStyle(element);
            return {width: element.getBoundingClientRect().width, minWidth: style.minWidth,
              display: style.display};
          })(),
          wrap: (() => {
            const element = document.querySelector('.lx-table-wrap');
            return {width: element.getBoundingClientRect().width,
              scrollWidth: element.scrollWidth, minWidth: getComputedStyle(element).minWidth};
          })(),
        })"""
    )
    assert dimensions["scrollWidth"] <= dimensions["innerWidth"], (width, dimensions)


def test_paper_entry_block_admin_ui_is_vietnamese_responsive_and_audits_operator_action(
    browser_page: Page,
) -> None:
    """Kiểm UI/route qua facade; không tuyên bố audit đã lưu trong DB."""
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    app_url, server, thread, listener, auth_service, read_model, block_service = _serve_app()
    try:
        browser_page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(app_url + "/") else route.abort()
            ),
        )
        response = browser_page.goto(f"{app_url}/admin/learning/scoring", wait_until="load")
        assert response is not None
        assert response.status == 200
        assert response.headers["cache-control"] == "no-store"
        expect(
            browser_page.get_by_role("heading", name="Điều phối chấm điểm mô hình")
        ).to_be_visible()
        expect(browser_page.get_by_text("Duyệt ứng viên", exact=True)).to_be_visible()
        expect(browser_page.get_by_text("Cổng PAPER", exact=True).first).to_be_visible()
        toggle = browser_page.locator(".paper-entry-block-confirm > summary")
        expect(toggle).to_have_text("Chặn entry PAPER")
        toggle.focus()
        expect(toggle).to_be_focused()
        browser_page.keyboard.press("Enter")
        expect(browser_page.locator(".paper-entry-block-confirm")).to_have_attribute("open", "")

        reason = browser_page.get_by_label("Lý do chặn")
        confirm = browser_page.get_by_label("Tôi xác nhận chặn entry PAPER cho quyết định này.")
        assert reason.evaluate("element => element.required")
        assert confirm.evaluate("element => element.required")
        form = browser_page.locator(".paper-entry-block-form")
        post_requests: list[str] = []
        browser_page.on(
            "request",
            lambda request: post_requests.append(request.url) if request.method == "POST" else None,
        )
        assert not form.evaluate("element => element.checkValidity()")
        browser_page.get_by_role("button", name="Xác nhận chặn").click()
        expect(reason).to_be_focused()
        assert post_requests == []
        reason.fill("Rà soát lại thông tin tín hiệu trước khi vào lệnh.")
        assert not form.evaluate("element => element.checkValidity()")
        browser_page.get_by_role("button", name="Xác nhận chặn").click()
        expect(confirm).to_be_focused()
        assert post_requests == []
        confirm.check()
        font_family = browser_page.locator("body").evaluate(
            "element => getComputedStyle(element).fontFamily"
        )
        assert "Be Vietnam Pro" in font_family
        _assert_no_horizontal_overflow(browser_page, width=1280, height=800)
        desktop_table = browser_page.locator(".lx-table--scoring-history")
        desktop_table_width = desktop_table.evaluate(
            "element => element.getBoundingClientRect().width"
        )
        assert desktop_table_width >= 1060
        table_region = browser_page.get_by_role(
            "region", name="Lịch sử điểm chấm; bảng có thể cuộn ngang trên màn hình lớn"
        )
        assert table_region.evaluate("element => element.scrollWidth > element.clientWidth")
        table_region.focus()
        expect(table_region).to_be_focused()
        table_region.evaluate("element => element.scrollLeft = element.scrollWidth")
        assert table_region.evaluate("element => element.scrollLeft > 0")
        table_region.evaluate("element => element.scrollLeft = 0")
        browser_page.set_viewport_size({"width": 1366, "height": 900})
        _assert_no_horizontal_overflow(browser_page, width=1366, height=900)
        assert table_region.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
        table_region.evaluate("element => element.blur()")
        action_cell_width = browser_page.locator(".paper-entry-block-cell").evaluate(
            "element => element.getBoundingClientRect().width"
        )
        assert action_cell_width >= 320
        assert browser_page.locator(".paper-entry-block-cell").evaluate(
            "element => element.getBoundingClientRect().right <= "
            "element.closest('.lx-table-wrap').getBoundingClientRect().right + 1"
        ), "Cột thao tác phải hiển thị trọn trong vùng bảng ở desktop."
        browser_page.evaluate("window.scrollTo(0, 0)")
        browser_page.screenshot(
            path=str(SCREENSHOTS / "wp06-paper-block-desktop.png"), full_page=True
        )
        browser_page.set_viewport_size({"width": 390, "height": 844})
        browser_page.reload(wait_until="load")
        toggle = browser_page.locator(".paper-entry-block-confirm > summary")
        toggle.click()
        expect(browser_page.locator(".paper-entry-block-confirm")).to_have_attribute("open", "")
        browser_page.get_by_label("Lý do chặn").fill(
            "Rà soát lại thông tin tín hiệu trước khi vào lệnh."
        )
        browser_page.get_by_label("Tôi xác nhận chặn entry PAPER cho quyết định này.").check()
        _assert_no_horizontal_overflow(browser_page, width=390, height=844)
        expect(toggle).to_be_visible()
        action_cell = browser_page.locator(".paper-entry-block-cell")
        assert action_cell.evaluate("element => getComputedStyle(element).display") == "block"
        action_cell_width = action_cell.evaluate("element => element.getBoundingClientRect().width")
        form_width = browser_page.locator(".paper-entry-block-form").evaluate(
            "element => element.getBoundingClientRect().width"
        )
        assert form_width >= action_cell_width * 0.9
        checkbox = browser_page.locator(".paper-entry-block-confirm-check input")
        checkbox_size = checkbox.bounding_box()
        assert checkbox_size is not None
        assert checkbox_size["width"] <= 24
        assert checkbox_size["height"] <= 24
        confirm_target = browser_page.locator(".paper-entry-block-confirm-check")
        assert confirm_target.evaluate("element => element.getBoundingClientRect().height") >= 44
        browser_page.evaluate("window.scrollTo(0, 0)")
        browser_page.screenshot(
            path=str(SCREENSHOTS / "wp06-paper-block-mobile.png"), full_page=True
        )

        block_service.error = ValueError("Target scorer đã thay đổi.")
        with browser_page.expect_response(
            lambda item: (
                item.request.method == "POST"
                and item.url.endswith(
                    f"/admin/learning/scoring/events/{SCORE_EVENT_ID}/paper-block"
                )
            )
        ) as rejected:
            browser_page.get_by_role("button", name="Xác nhận chặn").click()
        assert rejected.value.status == 409
        assert rejected.value.headers["cache-control"] == "no-store"
        first_request = rejected.value.request
        retry_key = UUID(first_request.headers["idempotency-key"])
        assert first_request.headers["x-csrf-token"] == "playwright-paper-block-csrf-token"
        assert first_request.post_data_json == {
            "reason": "Rà soát lại thông tin tín hiệu trước khi vào lệnh.",
            "confirm": True,
        }

        expect(browser_page.locator(".paper-entry-block-error")).to_have_text(
            "Quyết định hoặc lý do không còn đủ điều kiện chặn entry PAPER."
        )
        expect(browser_page.locator(".paper-entry-block-status")).to_have_text(
            "Thao tác chưa được ghi nhận."
        )
        expect(browser_page.get_by_role("button", name="Xác nhận chặn")).to_be_enabled()
        block_service.error = None
        with browser_page.expect_response(
            lambda item: (
                item.request.method == "POST"
                and item.url.endswith(
                    f"/admin/learning/scoring/events/{SCORE_EVENT_ID}/paper-block"
                )
            )
        ) as result:
            browser_page.get_by_role("button", name="Xác nhận chặn").click()
        assert result.value.status == 200
        request = result.value.request
        assert request.headers["x-csrf-token"] == "playwright-paper-block-csrf-token"
        assert UUID(request.headers["idempotency-key"]) == retry_key
        assert request.post_data_json == first_request.post_data_json
        expect(browser_page.get_by_text("Entry PAPER bị chặn thủ công")).to_be_visible()
        expect(
            browser_page.get_by_text("Rà soát lại thông tin tín hiệu trước khi vào lệnh.")
        ).to_be_visible()
        expect(browser_page.get_by_text("Duyệt ứng viên", exact=True)).to_be_visible()
        assert len(block_service.calls) == 2
        assert block_service.calls[0][0] == BOT_DECISION_ID
        assert block_service.calls[0][1] == request.post_data_json["reason"]
        assert block_service.calls[0][2] == retry_key
        assert block_service.calls[1][2] == retry_key
        assert read_model.link_calls == [(WORKSPACE_ID, SCORE_EVENT_ID)] * 2
        assert auth_service.verified_tokens == ["playwright-paper-block-csrf-token"] * 2
        _assert_no_horizontal_overflow(browser_page, width=390, height=844)
    finally:
        _stop_app(server, thread, listener)

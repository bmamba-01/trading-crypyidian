"""Playwright kiểm trang registry mô hình trên desktop và mobile."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from playwright.sync_api import Page, expect

from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.learning.model_registry import (
    LearningModelActivePointer,
    LearningModelRegistryAction,
    LearningModelRegistryEntry,
    LearningModelRegistryEvent,
    LearningModelRegistryState,
)

pytest_plugins = ("tests.e2e.browser_fixtures",)

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOTS = ROOT / "tests" / "e2e" / "evidence"
WORKSPACE_ID = UUID("bba2a6ed-7b24-43d3-a297-2ac617be105f")
MEMBERSHIP_ID = UUID("4c1fd40f-a99c-4a5c-a713-4ec54f33ddfd")
USER_ID = UUID("9b55d8b2-8f86-4ed7-9941-3310ec1d18db")
ENTRY_ID = UUID("a24c4a55-7176-4bcc-9f50-297ed57d17e2")
ARTIFACT_ID = UUID("99cb93f0-dc18-4525-8c15-06acb60810b2")
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


class _RegistryFacade:
    def __init__(self) -> None:
        self.entry = LearningModelRegistryEntry(
            id=ENTRY_ID,
            workspace_id=WORKSPACE_ID,
            artifact_id=ARTIFACT_ID,
            artifact_hash="a" * 64,
            manifest_hash="b" * 64,
            fold_plan_hash="c" * 64,
            approval_hash="d" * 64,
            state=LearningModelRegistryState.EVALUATED,
            revision=2,
            evaluation_report_id=uuid4(),
            evaluation_report_hash="e" * 64,
            created_by_membership_id=MEMBERSHIP_ID,
            created_at=NOW,
        )
        self.approvals: list[dict[str, object]] = []

    async def list_entries(self) -> tuple[LearningModelRegistryEntry, ...]:
        return (self.entry,)

    async def get_active_pointer(self) -> LearningModelActivePointer:
        return LearningModelActivePointer(
            workspace_id=WORKSPACE_ID,
            registry_id=None,
            version=0,
            updated_by_membership_id=None,
            changed_at=NOW,
        )

    async def list_events(self, registry_id: UUID) -> tuple[LearningModelRegistryEvent, ...]:
        if registry_id != self.entry.id:
            raise LookupError("Registry không thuộc workspace hiện tại.")
        event = LearningModelRegistryEvent(
            id=uuid4(),
            workspace_id=WORKSPACE_ID,
            registry_id=registry_id,
            sequence=1,
            action=LearningModelRegistryAction.REGISTER,
            from_state=None,
            to_state=LearningModelRegistryState.DRAFT,
            actor_membership_id=MEMBERSHIP_ID,
            policy_version="v1",
            reason_code="registered",
            reason="Đăng ký artifact sau khi kiểm tra nguồn gốc. " + "a" * 400,
            idempotency_key=uuid4(),
            mfa_verified_at=None,
            expected_revision=None,
            expected_pointer_version=None,
            evaluation_report_id=None,
            evaluation_report_hash=None,
            created_at=NOW,
        )
        return (
            event,
            replace(
                event,
                id=uuid4(),
                sequence=2,
                action=LearningModelRegistryAction.APPROVE,
                from_state=LearningModelRegistryState.EVALUATED,
                to_state=LearningModelRegistryState.EVALUATED,
                reason_code="approved",
                reason="Đã rà soát báo cáo và nguồn gốc mô hình.",
                mfa_verified_at=NOW,
                expected_revision=2,
            ),
        )

    async def approve(
        self,
        *,
        registry_id: UUID,
        expected_revision: int,
        idempotency_key: UUID,
        reason: str,
    ) -> LearningModelRegistryEntry:
        assert registry_id == self.entry.id
        assert expected_revision == self.entry.revision
        self.approvals.append({"idempotency_key": idempotency_key, "reason": reason})
        self.entry = replace(self.entry, revision=self.entry.revision + 1)
        return self.entry


class _RegistryService:
    def __init__(self) -> None:
        self.facade = _RegistryFacade()

    def for_principal(self, principal: Principal) -> _RegistryFacade:
        assert principal.workspace_id == WORKSPACE_ID
        return self.facade


class _AuthService:
    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return "playwright-local-csrf-token"

    async def verify_csrf(self, _cookie: str | None, token: str | None) -> None:
        assert token == "playwright-local-csrf-token"


def _identity() -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({Role.OWNER}),
            mfa_verified_at=NOW,
        ),
        email="owner@example.test",
    )


def _serve_app() -> tuple[str, uvicorn.Server, threading.Thread, socket.socket, _RegistryService]:
    application = create_app(AppSettings(app_env="test"))
    identity = _identity()
    application.dependency_overrides[current_identity] = lambda: identity
    application.state.auth_service = _AuthService()
    service = _RegistryService()
    application.state.learning_model_registry_service = service

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
        name="learning-model-registry-playwright",
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.025)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        raise RuntimeError("Admin web chưa khởi động trong thời hạn E2E.")
    return f"http://127.0.0.1:{port}", server, thread, listener, service


def _stop_app(server: uvicorn.Server, thread: threading.Thread, listener: socket.socket) -> None:
    server.should_exit = True
    thread.join(timeout=5)
    listener.close()


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
            assert bounds is not None, (width, bounds)
            assert bounds["height"] >= 44, (width, bounds)


def test_learning_model_registry_admin_is_responsive_and_captures_screenshots(
    browser_page: Page,
) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    app_url, server, thread, listener, service = _serve_app()
    try:
        mutations: list[str] = []
        browser_page.on(
            "request",
            lambda request: mutations.append(request.url) if request.method == "POST" else None,
        )
        browser_page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(app_url + "/") else route.abort()
            ),
        )
        response = browser_page.goto(
            f"{app_url}/admin/learning/models?artifact_id={ARTIFACT_ID}", wait_until="load"
        )
        assert response is not None
        assert response.status == 200
        expect(browser_page.get_by_role("heading", name="Quản lý mô hình học")).to_be_visible()
        assert browser_page.locator("[data-action]").count() == 4
        expect(browser_page.get_by_label("Mã artifact", exact=True)).to_have_value(str(ARTIFACT_ID))
        assert service.facade.approvals == []
        assert mutations == []
        history = browser_page.get_by_role("region", name="Lịch sử thay đổi")
        expect(history.get_by_text("Chưa đăng ký → Bản nháp")).to_be_visible()
        expect(history.get_by_text(str(MEMBERSHIP_ID))).to_have_count(2)
        expect(history.get_by_text("Chưa xác thực MFA")).to_be_visible()
        expect(history.get_by_text("Đã xác thực MFA")).to_be_visible()
        expect(history.get_by_text("Đã rà soát báo cáo và nguồn gốc mô hình.")).to_be_visible()
        expect(history.get_by_text("08/11/2026 12:00 UTC", exact=True)).to_have_count(2)
        event_response = browser_page.request.get(
            f"{app_url}/api/v1/learning-models/{ENTRY_ID}/events"
        )
        assert event_response.status == 200
        assert len(event_response.json()) == 2
        foreign_response = browser_page.request.get(
            f"{app_url}/api/v1/learning-models/{uuid4()}/events"
        )
        assert foreign_response.status == 404
        assert (
            not {"canonical_artifact", "samples", "feature_values", "payload"}
            & event_response.json()[0].keys()
        )
        expect(browser_page.get_by_text("Chưa nối bộ chấm điểm vào bot")).to_be_visible()
        expect(browser_page.get_by_role("heading", name="Đã đánh giá · chờ duyệt")).to_be_visible()
        expect(browser_page.get_by_text("b" * 12)).to_be_visible()
        browser_page.get_by_text("Thao tác quản trị").click()
        expect(browser_page.get_by_role("button", name="Duyệt mô hình")).to_be_visible()

        font_family = browser_page.locator("body").evaluate(
            "element => getComputedStyle(element).fontFamily"
        )
        assert "Be Vietnam Pro" in font_family
        _assert_layout(browser_page, width=1280, height=800)
        browser_page.screenshot(path=str(SCREENSHOTS / "wp06-registry-desktop.png"), full_page=True)

        reason = browser_page.get_by_label("Lý do thao tác")
        reason.fill("Đã rà soát báo cáo và lineage của artifact.")
        assert reason.evaluate("element => element.validity.valid")
        assert reason.evaluate("element => element.reportValidity()")
        assert (
            browser_page.locator("#learning-registry").get_attribute("data-csrf-token")
            == "playwright-local-csrf-token"
        )
        assert browser_page.evaluate("typeof crypto.randomUUID") == "function"
        with browser_page.expect_response(
            lambda item: item.request.method == "POST" and item.url.endswith("/approve")
        ) as approval_response:
            browser_page.get_by_role("button", name="Duyệt mô hình").click()
        assert approval_response.value.status == 200
        assert len(service.facade.approvals) == 1
        expect(browser_page.get_by_text("Phiên bản 3 · tạo")).to_be_visible()
        approval = service.facade.approvals[0]
        assert isinstance(approval["idempotency_key"], UUID)
        assert approval["reason"] == "Đã rà soát báo cáo và lineage của artifact."

        _assert_layout(browser_page, width=390, height=844)
        expect(browser_page.get_by_role("heading", name="Quản lý mô hình học")).to_be_visible()
        browser_page.screenshot(path=str(SCREENSHOTS / "wp06-registry-mobile.png"), full_page=True)
    finally:
        _stop_app(server, thread, listener)

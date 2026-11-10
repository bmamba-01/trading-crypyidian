"""Playwright kiểm trang vận hành scorer tiếng Việt trên desktop và mobile."""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, expect

from trading_bot.admin.learning_scoring_page_router import router as scoring_page_router
from trading_bot.app import create_app
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentAdminSnapshot,
    ScorerDeploymentEvent,
    ScorerDeploymentHealth,
    ScorerDeploymentMode,
)

pytest_plugins = ("tests.e2e.browser_fixtures",)

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOTS = ROOT / "tests" / "e2e" / "evidence"
WORKSPACE_ID = UUID("bba2a6ed-7b24-43d3-a297-2ac617be105f")
MEMBERSHIP_ID = UUID("4c1fd40f-a99c-4a5c-a713-4ec54f33ddfd")
USER_ID = UUID("9b55d8b2-8f86-4ed7-9941-3310ec1d18db")


class _AuthService:
    def __init__(self) -> None:
        self.verified_tokens: list[str | None] = []

    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return "playwright-scorer-csrf-token"

    async def verify_csrf(self, _cookie: str | None, token: str | None) -> None:
        self.verified_tokens.append(token)
        if token != "playwright-scorer-csrf-token":
            raise AssertionError("Token CSRF không khớp form scorer.")


class _ScorerDeploymentFacade:
    def __init__(self, principal: Principal) -> None:
        self.principal = principal
        self.snapshot = ScorerDeploymentAdminSnapshot(
            workspace_id=WORKSPACE_ID,
            mode=ScorerDeploymentMode.SHADOW,
            deployment_event_id=uuid4(),
            deployment_revision=2,
            active_pointer_version=4,
            health=ScorerDeploymentHealth.RIGHTS_RECHECK_REQUIRED,
            runtime_usable=False,
        )
        self.calls: list[dict[str, object]] = []

    async def load_admin_scoring_deployment(self) -> ScorerDeploymentAdminSnapshot:
        return self.snapshot

    async def set_mode(
        self,
        *,
        mode: ScorerDeploymentMode,
        expected_pointer_version: int,
        expected_deployment_revision: int,
        idempotency_key: UUID,
        reason: str,
    ) -> ScorerDeploymentEvent:
        self.calls.append(
            {
                "mode": mode,
                "expected_pointer_version": expected_pointer_version,
                "expected_deployment_revision": expected_deployment_revision,
                "idempotency_key": idempotency_key,
                "reason": reason,
                "actor_membership_id": self.principal.membership_id,
            }
        )
        assert mode is ScorerDeploymentMode.DISABLED
        now = time_now()
        event = ScorerDeploymentEvent(
            event_id=uuid4(),
            workspace_id=self.principal.workspace_id,
            revision=expected_deployment_revision + 1,
            mode=mode,
            target=None,
            actor_membership_id=self.principal.membership_id,
            mfa_verified_at=self.principal.mfa_verified_at,
            mfa_method="TOTP",
            expected_pointer_version=expected_pointer_version,
            expected_deployment_revision=expected_deployment_revision,
            reason=reason,
            idempotency_key=idempotency_key,
            previous_event_id=self.snapshot.deployment_event_id,
            previous_event_hash="a" * 64,
            created_at=now,
        )
        self.snapshot = ScorerDeploymentAdminSnapshot(
            workspace_id=self.principal.workspace_id,
            mode=ScorerDeploymentMode.DISABLED,
            deployment_event_id=event.event_id,
            deployment_revision=event.revision,
            active_pointer_version=expected_pointer_version,
            health=ScorerDeploymentHealth.DISABLED,
            runtime_usable=False,
        )
        return event


class _ScorerDeploymentService:
    def __init__(self, principal: Principal) -> None:
        self.facade = _ScorerDeploymentFacade(principal)
        self.principals: list[Principal] = []

    def for_principal(self, principal: Principal) -> _ScorerDeploymentFacade:
        assert principal.workspace_id == WORKSPACE_ID
        self.principals.append(principal)
        return self.facade


def time_now():
    return datetime.now(UTC)


def _identity() -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({Role.ANALYST_REVIEWER}),
            mfa_verified_at=time_now(),
            mfa_method=MfaMethod.TOTP,
        ),
        email="reviewer@example.test",
    )


def _serve_app() -> tuple[
    str,
    uvicorn.Server,
    threading.Thread,
    socket.socket,
    _ScorerDeploymentService,
    _AuthService,
]:
    application: FastAPI = create_app(AppSettings(app_env="test"))
    identity = _identity()
    application.dependency_overrides[current_identity] = lambda: identity
    auth_service = _AuthService()
    application.state.auth_service = auth_service
    service = _ScorerDeploymentService(identity.principal)
    application.state.learning_scoring_deployment_service = service
    if not any(
        getattr(route, "path", None) == "/admin/learning/scoring" for route in application.routes
    ):
        application.include_router(scoring_page_router)

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
        name="learning-scorer-admin-playwright",
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.025)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        raise RuntimeError("Admin web chưa khởi động trong thời hạn E2E.")
    return f"http://127.0.0.1:{port}", server, thread, listener, service, auth_service


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


def test_scorer_admin_is_locked_responsive_and_uses_cas_disable(
    browser_page: Page,
) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    app_url, server, thread, listener, service, auth_service = _serve_app()
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
        expect(browser_page.get_by_text("Quyền runtime chưa được xác minh")).to_be_visible()
        expect(
            browser_page.get_by_text(
                "Chế độ chỉ quan sát (SHADOW) và cổng PAPER (PAPER_GATE) đang khóa."
            )
        ).to_be_visible()
        expect(browser_page.get_by_text("Cần xác minh lại quyền hiện hành")).to_be_visible()
        expect(browser_page.get_by_text("Chỉ quan sát").first).to_be_visible()
        expect(browser_page.get_by_role("button", name="Tắt chấm điểm")).to_be_visible()
        expect(browser_page.locator("#learning-scoring-status")).to_be_hidden()
        assert browser_page.locator("select").count() == 0
        assert "feature_schema_hash" not in browser_page.content()
        assert "test_payload" not in browser_page.content()
        assert (
            "Giao diện quản trị chưa hỗ trợ lấy danh sách sự kiện điểm chấm"
            in browser_page.content()
        )
        assert "score event" not in browser_page.content().lower()
        assert "feature vector" not in browser_page.content().lower()
        assert "workspace" not in browser_page.content().lower()

        font_family = browser_page.locator("body").evaluate(
            "element => getComputedStyle(element).fontFamily"
        )
        assert "Be Vietnam Pro" in font_family
        browser_page.keyboard.press("Tab")
        expect(browser_page.locator(".skip-link")).to_be_focused()
        _assert_layout(browser_page, width=1280, height=900)
        browser_page.screenshot(path=str(SCREENSHOTS / "wp06-scoring-desktop.png"), full_page=True)
        _assert_layout(browser_page, width=390, height=844)
        browser_page.screenshot(path=str(SCREENSHOTS / "wp06-scoring-mobile.png"), full_page=True)

        reason = browser_page.get_by_label("Lý do tắt")
        reason.fill("Quyền hiện hành chưa xác minh, tắt deployment scorer.")
        expect(browser_page.get_by_role("button", name="Tắt chấm điểm")).to_be_enabled()
        with browser_page.expect_request(
            lambda item: (
                item.method == "POST" and item.url.endswith("/api/v1/learning-scorer/deployment")
            )
        ) as disable_request:
            with browser_page.expect_response(
                lambda item: (
                    item.request.method == "POST"
                    and item.url.endswith("/api/v1/learning-scorer/deployment")
                )
            ) as disable_response:
                browser_page.get_by_role("button", name="Tắt chấm điểm").click()
        request = disable_request.value
        assert request.headers["x-csrf-token"] == "playwright-scorer-csrf-token"
        assert UUID(request.headers["idempotency-key"])
        assert request.post_data_json == {
            "mode": "DISABLED",
            "expected_pointer_version": 4,
            "expected_deployment_revision": 2,
            "reason": "Quyền hiện hành chưa xác minh, tắt deployment scorer.",
        }
        assert disable_response.value.status == 200
        assert disable_response.value.headers["cache-control"] == "no-store"
        assert auth_service.verified_tokens == ["playwright-scorer-csrf-token"]
        assert len(service.facade.calls) == 1
        assert service.facade.calls[0]["actor_membership_id"] == MEMBERSHIP_ID
        assert service.facade.calls[0]["expected_pointer_version"] == 4
        assert service.facade.calls[0]["expected_deployment_revision"] == 2
        assert isinstance(service.facade.calls[0]["idempotency_key"], UUID)
        expect(browser_page.get_by_text("Đang tắt").first).to_be_visible()
        assert len(service.principals) == 3
        assert all(principal.workspace_id == WORKSPACE_ID for principal in service.principals)
    finally:
        _stop_app(server, thread, listener)

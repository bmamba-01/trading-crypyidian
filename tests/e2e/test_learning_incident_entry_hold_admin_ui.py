"""Playwright kiểm incident hold trên desktop/mobile với hai vai trò quản lý."""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from playwright.sync_api import Page, expect

from tests.unit.test_learning_incident_entry_hold import _hold
from tests.unit.test_learning_observations_admin import NOW
from trading_bot.app import create_app
from trading_bot.application.learning_incident_entry_hold import LearningIncidentHoldCandidate
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings

pytest_plugins = ("tests.e2e.browser_fixtures",)

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOTS = ROOT / "tests" / "e2e" / "evidence"
WORKSPACE_ID = UUID("1f234ff3-f65f-4516-a8b7-0fc997c29001")
MEMBERSHIP_ID = UUID("c8a6a632-d6c3-49d1-8c48-179456e99218")
USER_ID = UUID("409b17ca-5e93-4d9a-a29f-64fcac0f9a57")


def _identity(role: Role, *, stale_mfa: bool = True) -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({role}),
            mfa_verified_at=datetime.now(UTC) - timedelta(minutes=30)
            if stale_mfa
            else datetime.now(UTC),
            mfa_method=MfaMethod.TOTP,
        ),
        email=f"{role.value}@example.test",
    )


def _candidate() -> LearningIncidentHoldCandidate:
    return LearningIncidentHoldCandidate(
        proposal_id=uuid4(),
        assessment_id=uuid4(),
        assessment_hash="a" * 64,
        reason_codes=("coverage_below_threshold", "quality_below_threshold"),
        created_at=NOW,
        expected_revision=0,
        expected_deployment_revision=7,
        expected_active_pointer_version=5,
    )


class _Facade:
    def __init__(self, candidate: LearningIncidentHoldCandidate, event) -> None:
        self.candidate = candidate
        self.event = event

    async def list_actionable_proposals(self, *, limit: int = 20):
        return (self.candidate,)[:limit] if self.candidate is not None else ()

    async def list_history(self, *, limit: int = 200):
        return (self.event,)[:limit]

    async def list_open_holds(self, *, limit: int = 100):
        return (self.event,)[:limit]


class _HoldService:
    def __init__(self, facade: _Facade) -> None:
        self.facade = facade

    def for_principal(self, _principal: Principal) -> _Facade:
        return self.facade


class _AuthService:
    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return "browser-fixture-csrf"


class _ObservationsFacade:
    async def list_recent(self, *, limit: int = 50):
        return ()[:limit]


class _ObservationsService:
    def for_principal(self, _principal: Principal) -> _ObservationsFacade:
        return _ObservationsFacade()


def _serve_app(
    role: Role, *, include_candidate: bool = True
) -> tuple[str, uvicorn.Server, threading.Thread, socket.socket]:
    application = create_app(AppSettings(app_env="test", process_role="admin-web"))
    identity = _identity(role)
    application.dependency_overrides[current_identity] = lambda: identity
    candidate = _candidate() if include_candidate else None
    application.state.learning_incident_entry_hold_service = _HoldService(
        _Facade(candidate, _hold(reason="Xác nhận độ bao phủ dữ liệu thấp"))
    )
    application.state.learning_observations_admin_service = _ObservationsService()
    application.state.auth_service = _AuthService()

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
        name="learning-incident-hold-playwright",
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.025)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        raise RuntimeError("Admin web chưa khởi động trong thời hạn E2E.")
    return f"http://127.0.0.1:{port}", server, thread, listener


def _stop_app(server: uvicorn.Server, thread: threading.Thread, listener: socket.socket) -> None:
    server.should_exit = True
    thread.join(timeout=5)
    listener.close()


def _assert_no_overflow(page: Page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 920})
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => resolve()))")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def _request_interceptor(request_log):
    def intercept(route) -> None:
        request = route.request
        body = request.post_data_json if request.post_data else None
        request_log.append((request.url, request.headers, body))
        if request.url.endswith("/auth/reauthenticate"):
            route.fulfill(status=204, body="")
        else:
            route.fulfill(status=200, json={"action": "HOLD"})

    return intercept


def test_incident_hold_admin_is_vietnamese_responsive_and_role_limited(
    browser_page: Page,
) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    browser_page.emulate_media(color_scheme="light")
    app_url, server, thread, listener = _serve_app(Role.RISK_ADMIN)
    try:
        browser_page.goto(f"{app_url}/admin/learning/observations")
        expect(
            browser_page.get_by_role("heading", name="Sự cố dữ liệu và tạm chặn lệnh vào")
        ).to_be_visible()
        expect(browser_page.get_by_text("Xác nhận chặn lệnh vào", exact=True)).to_be_visible()
        expect(browser_page.get_by_text("Tháo chặn lệnh vào", exact=True)).to_be_visible()
        expect(browser_page.get_by_text("Phiên bản con trỏ mô hình", exact=True)).to_be_visible()
        expect(browser_page.get_by_role("button", name="Xác nhận chặn lệnh vào")).to_be_disabled()
        assert "Quan sát gần đây" not in browser_page.locator("body").inner_text()
        assert "source_payload" not in browser_page.locator("body").inner_text()
        assert "Be Vietnam Pro" in browser_page.locator("body").evaluate(
            "element => getComputedStyle(element).fontFamily"
        )

        _assert_no_overflow(browser_page, 1280)
        browser_page.screenshot(
            path=str(SCREENSHOTS / "wp06-incident-hold-desktop.png"), full_page=True
        )
        _assert_no_overflow(browser_page, 390)
        browser_page.screenshot(
            path=str(SCREENSHOTS / "wp06-incident-hold-mobile.png"), full_page=True
        )
        _assert_no_overflow(browser_page, 375)
        _assert_no_overflow(browser_page, 844)
    finally:
        _stop_app(server, thread, listener)


def test_noneligible_candidate_has_no_hold_action_and_unprivileged_role_is_denied(
    browser_page: Page,
) -> None:
    app_url, server, thread, listener = _serve_app(Role.RISK_ADMIN, include_candidate=False)
    try:
        response = browser_page.goto(f"{app_url}/admin/learning/observations")
        assert response is not None
        assert response.status == 200
        expect(
            browser_page.get_by_text("Không có đề xuất mới nhất nào đủ điều kiện", exact=False)
        ).to_be_visible()
        assert browser_page.get_by_role("button", name="Xác nhận chặn lệnh vào").count() == 0
    finally:
        _stop_app(server, thread, listener)

    app_url, server, thread, listener = _serve_app(Role.OPERATOR)
    try:
        response = browser_page.goto(f"{app_url}/admin/learning/observations")
        assert response is not None
        assert response.status == 403
        assert "Sự cố dữ liệu và tạm chặn lệnh vào" not in browser_page.locator("body").inner_text()
    finally:
        _stop_app(server, thread, listener)


def test_owner_and_risk_admin_confirm_with_recent_mfa_and_reason(browser_page: Page) -> None:
    for role in (Role.OWNER, Role.RISK_ADMIN):
        app_url, server, thread, listener = _serve_app(role)
        requests: list[tuple[str, dict[str, str], dict[str, object] | None]] = []
        intercept = _request_interceptor(requests)

        try:
            browser_page.route("**/api/v1/auth/reauthenticate", intercept)
            browser_page.route("**/api/v1/learning/incident-entry-holds/**", intercept)
            browser_page.goto(f"{app_url}/admin/learning/observations")
            expect(
                browser_page.get_by_text("Lịch sử chặn và tháo chặn", exact=False)
            ).to_be_visible()
            expect(
                browser_page.get_by_role("button", name="Xác nhận chặn lệnh vào")
            ).to_be_disabled()
            browser_page.get_by_label("Mật khẩu tài khoản").fill("fixture-password")
            browser_page.get_by_label("Mã xác thực 6 chữ số").fill("123456")
            browser_page.get_by_role("button", name="Xác thực bảo mật").click()
            expect(browser_page.locator("#learning-incident-reauth-status")).to_contain_text(
                "Đã xác thực"
            )
            button = browser_page.get_by_role("button", name="Xác nhận chặn lệnh vào")
            expect(button).to_be_enabled()
            hold_reason = browser_page.get_by_label("Lý do xác nhận chặn lệnh vào")
            button.click()
            expect(hold_reason).to_have_attribute("aria-invalid", "true")
            expect(browser_page.locator("[data-reason-error]").first).to_be_visible()
            hold_reason.fill("Chất lượng snapshot đã giảm dưới ngưỡng.")
            button.click()
            expect(browser_page.locator("#learning-incident-status")).to_contain_text(
                "Đã ghi nhận thao tác chặn"
            )
            release_reason = browser_page.get_by_label("Lý do tháo chặn lệnh vào")
            release_reason.fill("Đã xác minh lại chất lượng snapshot.")
            browser_page.get_by_role("button", name="Tháo chặn lệnh vào").click()
            expect(browser_page.locator("#learning-incident-status")).to_contain_text(
                "Đã ghi nhận thao tác tháo chặn"
            )
            assert len(requests) == 3
            reauth_url, reauth_headers, reauth_body = requests[0]
            assert reauth_url.endswith("/api/v1/auth/reauthenticate")
            assert reauth_headers["x-csrf-token"] == "browser-fixture-csrf"
            assert reauth_body == {"password": "fixture-password", "code": "123456"}
            hold_url, hold_headers, hold_body = requests[1]
            assert hold_url.endswith("/hold")
            assert hold_headers["x-csrf-token"] == "browser-fixture-csrf"
            assert UUID(hold_headers["idempotency-key"])
            assert hold_body is not None
            assert hold_body["reason"] == "Chất lượng snapshot đã giảm dưới ngưỡng."
            assert "workspace_id" not in hold_body
            release_url, release_headers, release_body = requests[2]
            assert release_url.endswith("/release")
            assert release_headers["x-csrf-token"] == "browser-fixture-csrf"
            assert UUID(release_headers["idempotency-key"])
            assert release_body == {
                "expected_revision": 1,
                "reason": "Đã xác minh lại chất lượng snapshot.",
            }
            browser_page.unroute("**/api/v1/auth/reauthenticate", intercept)
            browser_page.unroute("**/api/v1/learning/incident-entry-holds/**", intercept)
        finally:
            _stop_app(server, thread, listener)

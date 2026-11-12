"""Playwright kiểm màn WP06 Task8C trên desktop/mobile với dữ liệu metadata giả lập."""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from playwright.sync_api import Page, expect

from trading_bot.app import create_app
from trading_bot.application.learning_observations_admin import (
    LearningObservationAdminItem,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings

pytest_plugins = ("tests.e2e.browser_fixtures",)

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOTS = ROOT / "tests" / "e2e" / "evidence"
WORKSPACE_ID = UUID("bba2a6ed-7b24-43d3-a297-2ac617be105f")
MEMBERSHIP_ID = UUID("4c1fd40f-a99c-4a5c-a713-4ec54f33ddfd")
USER_ID = UUID("9b55d8b2-8f86-4ed7-9941-3310ec1d18db")
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def _items() -> tuple[LearningObservationAdminItem, ...]:
    return (
        LearningObservationAdminItem(
            kind="observation",
            record_id=uuid4(),
            occurred_at=NOW,
            metadata={
                "challenger_registry_id": str(uuid4()),
                "challenger_revision": 4,
                "challenger_reason": "UNCALIBRATED_SCORE",
                "incumbent_status": "CANDIDATE",
                "snapshot_coverage": 0.94,
                "snapshot_quality_ok": True,
                "schema_compatible": True,
                "rights_eligible": True,
                "active_pointer_version": 7,
            },
        ),
        LearningObservationAdminItem(
            kind="assessment",
            record_id=uuid4(),
            occurred_at=NOW,
            metadata={
                "cohort_registry_id": str(uuid4()),
                "cohort_revision": 4,
                "member_count": 2,
                "policy_version": "coverage-v2-30d",
                "observation_window_days": 30,
                "window_start": "2026-11-01T12:00:00+00:00",
                "window_end": "2026-11-08T12:00:00+00:00",
                "minimum_observations": 3,
                "eligible_count": 2,
                "snapshot_coverage_rate": None,
                "quality_rate": None,
                "abstention_rate": 1.0,
                "data_quality_status": "inconclusive",
                "performance_status": "inconclusive",
                "reason_codes": ["low_sample", "performance_not_evaluable"],
            },
        ),
        LearningObservationAdminItem(
            kind="assessment",
            record_id=uuid4(),
            occurred_at=NOW,
            metadata={
                "cohort_registry_id": str(uuid4()),
                "cohort_revision": 3,
                "member_count": 4,
                "policy_version": "coverage-v1",
                "observation_window_days": None,
                "window_start": "2026-11-04T12:00:00+00:00",
                "window_end": "2026-11-08T11:59:00+00:00",
                "minimum_observations": 3,
                "eligible_count": 4,
                "snapshot_coverage_rate": 0.94,
                "quality_rate": 0.9,
                "abstention_rate": 0.1,
                "data_quality_status": "no_drift",
                "performance_status": "inconclusive",
                "reason_codes": [],
            },
        ),
        LearningObservationAdminItem(
            kind="proposal",
            record_id=uuid4(),
            occurred_at=NOW,
            metadata={
                "assessment_id": str(uuid4()),
                "policy_version": "coverage-v1",
                "reason_codes": ["coverage_below_threshold"],
                "membership_digest": "a" * 64,
            },
        ),
    )


class _ObservationsFacade:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def list_recent(self, *, limit: int = 50) -> tuple[LearningObservationAdminItem, ...]:
        self.calls.append(limit)
        return _items()[:limit]


class _ObservationsService:
    def __init__(self) -> None:
        self.facade = _ObservationsFacade()
        self.principals: list[Principal] = []

    def for_principal(self, principal: Principal) -> _ObservationsFacade:
        self.principals.append(principal)
        return self.facade


def _identity() -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({Role.ANALYST_REVIEWER}),
            mfa_verified_at=NOW,
        ),
        email="reviewer@example.test",
    )


def _serve_app() -> tuple[
    str, uvicorn.Server, threading.Thread, socket.socket, _ObservationsService
]:
    application = create_app(AppSettings(app_env="test"))
    application.dependency_overrides[current_identity] = lambda: _identity()
    service = _ObservationsService()
    application.state.learning_observations_admin_service = service

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
        name="learning-observations-playwright",
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


def _assert_no_overflow(page: Page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => resolve()))")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_learning_observations_page_is_read_only_vietnamese_and_responsive(
    browser_page: Page,
) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    app_url, server, thread, listener, service = _serve_app()
    try:
        browser_page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(app_url + "/") else route.abort()
            ),
        )
        response = browser_page.goto(
            f"{app_url}/admin/learning/observations?workspace_id={uuid4()}",
            wait_until="load",
        )
        assert response is not None
        assert response.status == 200
        assert response.headers.get("cache-control") == "no-store"
        expect(
            browser_page.get_by_role("heading", name="Quan sát mô hình và chất lượng dữ liệu")
        ).to_be_visible()
        expect(
            browser_page.get_by_text("ABSTAIN — chưa đánh giá hiệu năng", exact=True)
        ).to_be_visible()
        expect(browser_page.get_by_role("heading", name="Quan sát gần đây")).to_be_visible()
        expect(
            browser_page.get_by_role("heading", name="Đánh giá chất lượng dữ liệu")
        ).to_be_visible()
        expect(
            browser_page.get_by_role("heading", name="Đề xuất xem xét huấn luyện lại")
        ).to_be_visible()
        expect(browser_page.get_by_text("Chờ người đánh giá", exact=False)).to_be_visible()
        expect(
            browser_page.get_by_text("Số quan sát / ngưỡng tối thiểu", exact=True).first
        ).to_be_visible()
        expect(browser_page.get_by_text("2 / 3", exact=True)).to_be_visible()
        expect(browser_page.get_by_text("30 ngày", exact=True)).to_be_visible()
        expect(browser_page.get_by_text("08/11/2026 12:00:00", exact=True)).to_be_visible()
        expect(browser_page.get_by_text("coverage-v2-30d", exact=True)).to_be_visible()
        expect(browser_page.get_by_text("Mốc đánh giá (UTC)", exact=True).first).to_be_visible()
        expect(browser_page.get_by_text("Mốc quan sát cuối (UTC)", exact=True)).to_be_visible()
        expect(
            browser_page.get_by_text("Chưa phát hiện vấn đề theo đánh giá này", exact=True).first
        ).to_be_visible()
        expect(
            browser_page.get_by_text("Chưa phát hiện vấn đề theo policy hiện hành", exact=True)
        ).to_have_count(0)
        expect(browser_page.get_by_text("giờ Việt Nam (UTC+7)", exact=False).first).to_be_visible()
        assert "Asia/Ho_Chi_Minh" not in browser_page.locator("body").inner_text()
        expect(browser_page.get_by_role("link", name="Quan sát mô hình")).to_have_attribute(
            "aria-current", "page"
        )
        assert "Be Vietnam Pro" in browser_page.locator("body").evaluate(
            "element => getComputedStyle(element).fontFamily"
        )
        assert service.principals == [_identity().principal]
        assert service.facade.calls == [50]
        assert "workspace_id" not in browser_page.url.split("?", maxsplit=1)[0]
        expect(browser_page.get_by_role("button", name="Chuyển chế độ sáng/tối")).to_be_visible()
        assert browser_page.get_by_role("button").count() == 1
        assert browser_page.locator("form").count() == 0

        page_text = browser_page.locator("body").inner_text().lower()
        assert all(
            term not in page_text
            for term in ("feature vector", "source_refs", "label_value", "enqueue", "promote")
        )
        assert "hàng đợi" not in page_text

        _assert_no_overflow(browser_page, 1280)
        browser_page.screenshot(
            path=str(SCREENSHOTS / "wp06-observations-desktop.png"), full_page=True
        )
        _assert_no_overflow(browser_page, 390)
        browser_page.screenshot(
            path=str(SCREENSHOTS / "wp06-observations-mobile.png"), full_page=True
        )
    finally:
        _stop_app(server, thread, listener)

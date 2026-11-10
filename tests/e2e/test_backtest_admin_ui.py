"""Kiểm tra trình duyệt cho trang replay lịch sử trên desktop và mobile."""

from __future__ import annotations

import hashlib
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from playwright.sync_api import Page, expect

from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings

pytest_plugins = ("tests.e2e.browser_fixtures",)

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOTS = ROOT / "tests" / "e2e" / "evidence"
WORKSPACE_ID = UUID("bba2a6ed-7b24-43d3-a297-2ac617be105f")
MEMBERSHIP_ID = UUID("4c1fd40f-a99c-4a5c-a713-4ec54f33ddfd")
USER_ID = UUID("9b55d8b2-8f86-4ed7-9941-3310ec1d18db")
CSRF_TOKEN = "playwright-local-csrf-token"
BUNDLE_HASH = hashlib.sha256(b"wp07c-btcusdt-1h-4h").hexdigest()
SECOND_BUNDLE_HASH = hashlib.sha256(b"wp07c-ethusdt-4h").hexdigest()


@dataclass
class _ReplayJob:
    id: UUID
    workspace_id: UUID
    bundle_hash: str
    strategy_release_id: str
    execution_interval: str
    learning_intent: str = "RESEARCH"
    status: str = "QUEUED"
    progress: int = 0
    stage: str = "queued"
    cancel_requested: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None
    result_run_ids: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        return self.status


@dataclass(frozen=True, slots=True)
class _BundleChoice:
    dataset_hash: str = BUNDLE_HASH
    symbol: str = "BTCUSDT"
    evaluation_start: datetime = datetime(2026, 11, 3, tzinfo=UTC)
    range_end: datetime = datetime(2026, 11, 4, tzinfo=UTC)
    intervals: tuple[str, ...] = ("1h", "4h")

    @property
    def bundle_hash(self) -> str:
        return self.dataset_hash


class _BacktestReplayJobService:
    """Giả lập cổng job để kiểm tra hành trình trình duyệt độc lập với DB."""

    def __init__(self) -> None:
        self.jobs: list[_ReplayJob] = []
        self.submissions: list[object] = []

    async def list_bundles(self, principal: Principal) -> tuple[_BundleChoice, ...]:
        assert principal.workspace_id == WORKSPACE_ID
        return (
            _BundleChoice(),
            _BundleChoice(
                dataset_hash=SECOND_BUNDLE_HASH,
                symbol="ETHUSDT",
                evaluation_start=datetime(2026, 11, 2, tzinfo=UTC),
                range_end=datetime(2026, 11, 3, tzinfo=UTC),
                intervals=("4h",),
            ),
        )

    async def submit(self, principal: Principal, request: object) -> _ReplayJob:
        assert principal.workspace_id == WORKSPACE_ID
        self.submissions.append(request)

        def value(name: str, default: str = "") -> object:
            return getattr(request, name, default)

        job = _ReplayJob(
            id=uuid4(),
            workspace_id=principal.workspace_id,
            bundle_hash=str(value("bundle_hash", BUNDLE_HASH)),
            strategy_release_id=str(value("strategy_release_id", "h01-v1")),
            execution_interval=str(value("execution_interval", "1h")),
            learning_intent=str(value("learning_intent", "RESEARCH")),
        )
        self.jobs.insert(0, job)
        return job

    async def list(self, principal: Principal) -> tuple[_ReplayJob, ...]:
        assert principal.workspace_id == WORKSPACE_ID
        return tuple(job for job in self.jobs if job.workspace_id == principal.workspace_id)

    async def cancel(self, principal: Principal, job_id: UUID) -> _ReplayJob:
        assert principal.workspace_id == WORKSPACE_ID
        job = next(
            job
            for job in self.jobs
            if job.workspace_id == principal.workspace_id and job.id == job_id
        )
        job.cancel_requested = True
        job.updated_at = datetime.now(UTC)
        return job

    def simulate_worker_poll(self) -> None:
        """Mô phỏng worker xác nhận hủy ở lần claim kế tiếp."""
        for job in self.jobs:
            if job.status == "QUEUED" and job.cancel_requested:
                job.status = "CANCELLED"
                job.stage = "cancelled"
                job.progress = 100
                job.updated_at = datetime.now(UTC)
                job.completed_at = job.updated_at

    def simulate_worker_progress(self) -> None:
        """Mô phỏng job đang replay để kiểm tra màn hình theo dõi tiến độ."""
        job = self.jobs[0]
        job.status = "RUNNING"
        job.stage = "replay"
        job.progress = 65
        job.started_at = datetime.now(UTC)
        job.updated_at = job.started_at

    def simulate_worker_success(self) -> None:
        """Mô phỏng worker lưu xong kết quả PAPER."""
        job = self.jobs[0]
        job.status = "SUCCEEDED"
        job.stage = "complete"
        job.progress = 100
        job.result_run_ids = ("paper-run-e2e-01",)
        job.updated_at = datetime.now(UTC)
        job.completed_at = job.updated_at


class _AuthService:
    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return CSRF_TOKEN

    async def verify_csrf(self, _cookie: str | None, token: str | None) -> None:
        if token != CSRF_TOKEN:
            raise AssertionError("Token CSRF không khớp với fixture Playwright.")


def _identity() -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({Role.OWNER}),
            mfa_verified_at=datetime.now(UTC),
        ),
        email="owner@example.test",
    )


def _serve_app() -> tuple[
    str, uvicorn.Server, threading.Thread, socket.socket, _BacktestReplayJobService
]:
    application = create_app(AppSettings(app_env="test"))
    identity = _identity()
    service = _BacktestReplayJobService()
    application.dependency_overrides[current_identity] = lambda: identity
    application.dependency_overrides[require_csrf] = lambda: identity
    application.state.auth_service = _AuthService()
    application.state.backtest_replay_job_service = service

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
        name="backtest-admin-playwright",
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


def _fill_replay_form(page: Page, *, learning_intent: str = "RESEARCH") -> None:
    values = {
        "bundle_hash": BUNDLE_HASH,
        "strategy_release_id": "h01-v1",
        "execution_interval": "1h",
        "starting_equity": "10000",
        "per_trade_risk_fraction": "0.01",
        "fee_rate": "0.001",
        "slippage_rate": "0",
        "seed": "7",
        "learning_intent": learning_intent,
    }
    for name, value in values.items():
        control = page.locator(f'[name="{name}"]').first
        expect(control).to_be_visible()
        if control.evaluate("element => element.tagName.toLowerCase()") == "select":
            options = control.locator("option").evaluate_all(
                "items => items.map(item => ({value:item.value, disabled:item.disabled}))"
            )
            available = [item["value"] for item in options if not item["disabled"]]
            selected = (
                value if value in available else next((item for item in available if item), None)
            )
            assert selected, f"Select {name} không có lựa chọn khả dụng."
            control.select_option(selected)
        else:
            control.fill(value)


def _assert_layout(page: Page, *, width: int, height: int) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => resolve()))")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
        width,
        page.evaluate("({scroll:document.documentElement.scrollWidth,inner:innerWidth})"),
    )
    for control in page.locator("input:not([type=hidden]), select, button").all():
        if control.is_visible():
            bounds = control.bounding_box()
            assert bounds is not None, (width, bounds)
            assert bounds["height"] >= 44, (width, bounds)


def test_admin_backtest_replay_is_responsive_and_captures_desktop_mobile_screenshots(
    browser_page: Page,
) -> None:
    """Tạo job, hủy job, rồi lưu ảnh UI thật tại kích thước desktop và điện thoại."""
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    app_url, server, thread, listener, service = _serve_app()
    try:
        browser_page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(app_url + "/") else route.abort()
            ),
        )
        browser_page.goto(f"{app_url}/admin/backtests", wait_until="load")
        expect(
            browser_page.get_by_role("heading", name="Kiểm thử dữ liệu lịch sử (Backtest)")
        ).to_be_visible()
        bundle_select = browser_page.locator('[name="bundle_hash"]')
        interval_select = browser_page.locator('[name="execution_interval"]')
        bundle_help = browser_page.locator("#bundle-hash-help")
        expect(bundle_select).to_have_value(BUNDLE_HASH)
        expect(bundle_help).to_contain_text("BTCUSDT")
        expect(bundle_help).to_contain_text("1h, 4h")
        expect(interval_select.locator("option")).to_have_count(2)
        expect(interval_select.locator('option[value="1h"]')).to_have_count(1)
        expect(interval_select.locator('option[value="4h"]')).to_have_count(1)

        bundle_select.select_option(SECOND_BUNDLE_HASH)
        expect(bundle_help).to_contain_text("ETHUSDT")
        expect(bundle_help).to_contain_text("4h")
        expect(interval_select.locator("option")).to_have_count(1)
        expect(interval_select.locator('option[value="4h"]')).to_have_count(1)
        expect(interval_select.locator('option[value="1h"]')).to_have_count(0)

        bundle_select.select_option(BUNDLE_HASH)
        expect(bundle_help).to_contain_text("BTCUSDT")
        expect(interval_select.locator("option")).to_have_count(2)
        _fill_replay_form(browser_page)
        submit = browser_page.get_by_role("button", name="Chạy kiểm thử")
        expect(submit).to_be_visible()
        submit.click()
        job_row = browser_page.locator("[data-job-id]:visible").first
        expect(job_row).to_be_visible()
        expect(job_row).to_contain_text("Đang chờ")
        expect(job_row).to_contain_text("0%")

        _assert_layout(browser_page, width=1280, height=900)
        browser_page.screenshot(
            path=str(SCREENSHOTS / "wp07c-backtests-desktop.png"), full_page=True
        )

        service.simulate_worker_progress()
        browser_page.reload(wait_until="load")
        running_row = browser_page.locator("[data-job-id]:visible").first
        expect(running_row).to_contain_text("Đang chạy")
        expect(running_row).to_contain_text("65%")
        expect(running_row.get_by_role("button", name="Hủy")).to_be_visible()

        service.simulate_worker_success()
        browser_page.reload(wait_until="load")
        succeeded_row = browser_page.locator("[data-job-id]:visible").first
        expect(succeeded_row).to_contain_text("Hoàn tất")
        expect(succeeded_row).to_contain_text("100%")
        expect(succeeded_row).to_contain_text("paper-run-e2e-01")
        expect(succeeded_row.get_by_role("button", name="Hủy")).to_have_count(0)

        _fill_replay_form(browser_page, learning_intent="TRAINING_BASE")
        browser_page.get_by_role("button", name="Chạy kiểm thử").click()
        job_row = browser_page.locator("[data-job-id]:visible").first
        expect(job_row).to_contain_text("Đang chờ")
        expect(job_row).to_contain_text("Tạo dữ liệu học (BASE) · quarantine")

        service.simulate_worker_success()
        browser_page.reload(wait_until="load")
        quarantined_row = browser_page.locator("[data-job-id]:visible").first
        expect(quarantined_row).to_contain_text("Kết quả đang quarantine")
        expect(quarantined_row).not_to_contain_text("paper-run-e2e-01")

        _fill_replay_form(browser_page)
        browser_page.get_by_role("button", name="Chạy kiểm thử").click()
        job_row = browser_page.locator("[data-job-id]:visible").first
        expect(job_row).to_contain_text("Đang chờ")

        cancel_button = job_row.get_by_role("button", name="Hủy")
        expect(cancel_button).to_be_visible()
        cancel_button.click()
        requested_row = browser_page.locator("[data-job-id]:visible").first
        expect(requested_row).to_contain_text("Đang yêu cầu hủy")
        expect(requested_row.get_by_role("button", name="Hủy")).to_have_count(0)

        service.simulate_worker_poll()
        browser_page.reload(wait_until="load")
        cancelled_row = browser_page.locator("[data-job-id]:visible").first
        expect(cancelled_row).to_contain_text("Đã hủy")
        expect(cancelled_row).to_contain_text("100%")

        _assert_layout(browser_page, width=390, height=844)
        expect(
            browser_page.get_by_role("heading", name="Kiểm thử dữ liệu lịch sử (Backtest)")
        ).to_be_visible()
        browser_page.screenshot(
            path=str(SCREENSHOTS / "wp07c-backtests-mobile.png"), full_page=True
        )
    finally:
        _stop_app(server, thread, listener)

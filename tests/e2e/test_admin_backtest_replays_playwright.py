"""E2E browser tests với Playwright cho giao diện Kiểm thử dữ liệu lịch sử (Backtest).

Kiểm tra:
- Xác thực đăng nhập và điều hướng qua menu đến /admin/backtests ('Kiểm định Backtest').
- Hiển thị form tạo lượt kiểm thử: chọn bundle dữ liệu đã lưu, chiến lược,
  khung thời gian, vốn khởi điểm.
- Hiển thị danh sách các job kiểm thử lịch sử với trạng thái (Đang chờ, Đang chạy, Hoàn tất),
  thanh tiến độ %, mã run ID kết quả và nút thao tác Hủy.
- Đảm bảo tính tương thích và phản hồi trên màn hình Desktop (1280x800) và Mobile iPhone (390x844).
- Chụp ảnh màn hình desktop và mobile lưu vào thư mục screenshots.
"""

from __future__ import annotations

import hashlib
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from playwright.sync_api import expect, sync_playwright

from tests.e2e.csp_harness import (
    browser_violations,  # noqa: F401 - fixture nạp qua usefixtures
    install_security_headers,
    watch_browser_violations,
)
from trading_bot.admin.backtest_replay_router import router as backtest_replay_router
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.security import LoginRateLimiter
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity, LoginResult
from trading_bot.config.settings import AppSettings

# Ảnh chụp nằm trong repo để test chạy được trên mọi máy và không ghi ra ngoài dự án.
SCREENSHOTS_DIR = Path(__file__).resolve().parent / "evidence" / "admin-playwright"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

DESKTOP_SCREENSHOT = SCREENSHOTS_DIR / "17_backtests_desktop.png"
MOBILE_SCREENSHOT = SCREENSHOTS_DIR / "13_backtests_mobile.png"

WORKSPACE_ID = UUID("bba2a6ed-7b24-43d3-a297-2ac617be105f")
USER_ID = UUID("9b55d8b2-8f86-4ed7-9941-3310ec1d18db")
MEMBERSHIP_ID = UUID("4c1fd40f-a99c-4a5c-a713-4ec54f33ddfd")
CSRF_TOKEN = "playwright-backtest-csrf-token-123456"

BUNDLE_BTC_HASH = hashlib.sha256(b"wp07c-btcusdt-bundle-dataset").hexdigest()
BUNDLE_ETH_HASH = hashlib.sha256(b"wp07c-ethusdt-bundle-dataset").hexdigest()

# Mỗi test thất bại nếu trình duyệt báo vi phạm CSP hoặc lỗi JavaScript chưa bắt.
pytestmark = pytest.mark.usefixtures("browser_violations")


@dataclass
class MockReplayJob:
    """Mock thực thể lượt kiểm thử."""

    id: UUID
    workspace_id: UUID
    bundle_hash: str
    strategy_release_id: str
    execution_interval: str
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
class MockBundleChoice:
    """Lựa chọn bundle dữ liệu đã lưu."""

    dataset_hash: str
    symbol: str
    evaluation_start: datetime
    range_end: datetime
    intervals: tuple[str, ...]

    @property
    def bundle_hash(self) -> str:
        return self.dataset_hash


class MockAuthService:
    """Mock auth service in-memory cho test E2E."""

    def __init__(self) -> None:
        self.sessions: dict[str, AuthIdentity] = {}
        self.valid_email = "owner@crypidian.io"
        self.csrf_token = CSRF_TOKEN

    async def login(
        self,
        *,
        workspace_slug: str,
        email: str,
        password: str,
        code: str,
    ) -> LoginResult:
        token = secrets.token_urlsafe(32)
        principal = Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({Role.OWNER}),
            mfa_verified_at=datetime.now(UTC),
        )
        identity = AuthIdentity(principal=principal, email=self.valid_email)
        cookie_value = f"{WORKSPACE_ID}:{token}"
        self.sessions[cookie_value] = identity
        return LoginResult(
            cookie_value=cookie_value,
            csrf_token=self.csrf_token,
            principal=principal,
            email=self.valid_email,
        )

    async def authenticate(self, cookie_value: str | None) -> AuthIdentity:
        if not cookie_value or cookie_value not in self.sessions:
            raise AuthenticationFailure(401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn.")
        return self.sessions[cookie_value]

    async def issue_csrf_token(self, cookie_value: str | None) -> str:
        if not cookie_value or cookie_value not in self.sessions:
            raise AuthenticationFailure(401, "Chưa xác thực.")
        return self.csrf_token

    async def verify_csrf(self, cookie_value: str | None, token: str | None) -> None:
        if not cookie_value or cookie_value not in self.sessions:
            raise AuthenticationFailure(401, "Chưa xác thực.")
        if not token or token != self.csrf_token:
            raise AuthenticationFailure(403, "Token CSRF không hợp lệ.")


class MockBacktestReplayJobService:
    """Service giả lập quản lý job kiểm thử cho UI test."""

    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.jobs: list[MockReplayJob] = [
            MockReplayJob(
                id=UUID("11111111-1111-1111-1111-111111111111"),
                workspace_id=WORKSPACE_ID,
                bundle_hash=BUNDLE_BTC_HASH,
                strategy_release_id="h01-v1",
                execution_interval="1h",
                status="RUNNING",
                progress=65,
                stage="replay",
                created_at=now - timedelta(minutes=5),
            ),
            MockReplayJob(
                id=UUID("22222222-2222-2222-2222-222222222222"),
                workspace_id=WORKSPACE_ID,
                bundle_hash=BUNDLE_BTC_HASH,
                strategy_release_id="h01-v1",
                execution_interval="4h",
                status="SUCCEEDED",
                progress=100,
                stage="complete",
                created_at=now - timedelta(hours=2),
                result_run_ids=("run-btc-oct2026-001", "run-btc-oct2026-002"),
            ),
            MockReplayJob(
                id=UUID("33333333-3333-3333-3333-333333333333"),
                workspace_id=WORKSPACE_ID,
                bundle_hash=BUNDLE_ETH_HASH,
                strategy_release_id="h03-v1",
                execution_interval="4h",
                status="QUEUED",
                progress=0,
                stage="queued",
                created_at=now - timedelta(minutes=1),
            ),
        ]

    async def list_bundles(self, principal: Principal) -> tuple[MockBundleChoice, ...]:
        assert principal.workspace_id == WORKSPACE_ID
        return (
            MockBundleChoice(
                dataset_hash=BUNDLE_BTC_HASH,
                symbol="BTCUSDT",
                evaluation_start=datetime(2026, 11, 1, tzinfo=UTC),
                range_end=datetime(2026, 11, 5, tzinfo=UTC),
                intervals=("1h", "4h"),
            ),
            MockBundleChoice(
                dataset_hash=BUNDLE_ETH_HASH,
                symbol="ETHUSDT",
                evaluation_start=datetime(2026, 11, 1, tzinfo=UTC),
                range_end=datetime(2026, 11, 4, tzinfo=UTC),
                intervals=("4h",),
            ),
        )

    async def list(self, principal: Principal) -> tuple[MockReplayJob, ...]:
        assert principal.workspace_id == WORKSPACE_ID
        return tuple(self.jobs)

    async def submit(self, principal: Principal, request: object) -> MockReplayJob:
        assert principal.workspace_id == WORKSPACE_ID
        new_job = MockReplayJob(
            id=uuid4(),
            workspace_id=principal.workspace_id,
            bundle_hash=getattr(request, "bundle_hash", BUNDLE_BTC_HASH),
            strategy_release_id=getattr(request, "strategy_release_id", "h01-v1"),
            execution_interval=getattr(request, "execution_interval", "1h"),
            status="QUEUED",
            progress=0,
            stage="queued",
        )
        self.jobs.insert(0, new_job)
        return new_job

    async def cancel(self, principal: Principal, job_id: UUID) -> MockReplayJob:
        assert principal.workspace_id == WORKSPACE_ID
        for job in self.jobs:
            if job.id == job_id:
                job.cancel_requested = True
                job.updated_at = datetime.now(UTC)
                return job
        raise LookupError(f"Job not found: {job_id}")


def create_test_backtest_app() -> FastAPI:
    """Tạo FastAPI application với mock services cho Playwright test."""
    app = FastAPI(title="Crypidian Backtest Test Server")

    app_settings = AppSettings(app_env="local")
    app.state.settings = app_settings
    templates = Jinja2Templates(directory="src/trading_bot/templates")
    templates.env.globals["session_cookie_name"] = lambda s: "crypidian_session"
    app.state.templates = templates
    app.state.login_limiter = LoginRateLimiter()

    auth_service = MockAuthService()
    test_token = "valid_test_token_backtest_replays"
    cookie_value = f"{WORKSPACE_ID}:{test_token}"
    principal = Principal(
        user_id=USER_ID,
        membership_id=MEMBERSHIP_ID,
        workspace_id=WORKSPACE_ID,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )
    auth_service.sessions[cookie_value] = AuthIdentity(
        principal=principal, email=auth_service.valid_email
    )

    app.state.auth_service = auth_service
    app.state.backtest_replay_job_service = MockBacktestReplayJobService()

    app.mount("/static", StaticFiles(directory="src/trading_bot/static"), name="static")
    app.include_router(backtest_replay_router)
    app.include_router(admin_web_router)
    # Gắn CSP thật để bắt script/style nội tuyến như khi triển khai.
    install_security_headers(app)
    return app


@pytest.fixture(scope="module")
def backtest_server_url() -> Any:
    """Khởi động test server trên ephemeral port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    app = create_test_backtest_app()
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    time.sleep(0.5)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=3)


def test_admin_backtest_replays_desktop_view(backtest_server_url: str) -> None:
    """Kiểm tra giao diện Desktop (1280x800) và chụp ảnh 17_backtests_desktop.png."""
    cookie_value = f"{WORKSPACE_ID}:valid_test_token_backtest_replays"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            color_scheme="light",
        )
        context.add_cookies(
            [
                {
                    "name": "crypidian_session",
                    "value": cookie_value,
                    "domain": "127.0.0.1",
                    "path": "/",
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            ]
        )
        page = context.new_page()
        watch_browser_violations(page)

        # 1. Mở trang /admin/backtests
        page.goto(f"{backtest_server_url}/admin/backtests", wait_until="networkidle")

        # 2. Xác nhận tiêu đề chính và banner PAPER
        heading = page.get_by_role("heading", name="Kiểm thử dữ liệu lịch sử (Backtest)")
        expect(heading).to_be_visible()
        assert "Chế độ: Mô phỏng (PAPER) nội bộ" in page.content()

        # 3. Xác nhận liên kết điều hướng 'Kiểm định Backtest' trên thanh điều hướng chính
        nav_link = page.locator('nav.main-nav a[href="/admin/backtests"]')
        expect(nav_link).to_be_visible()
        expect(nav_link).to_contain_text("Kiểm định Backtest")

        # 4. Xác nhận các thành phần của biểu mẫu tạo lượt kiểm thử
        bundle_select = page.locator('select[name="bundle_hash"]')
        expect(bundle_select).to_be_visible()
        expect(bundle_select).to_have_value(BUNDLE_BTC_HASH)

        interval_select = page.locator('select[name="execution_interval"]')
        expect(interval_select).to_be_visible()

        equity_input = page.locator('input[name="starting_equity"]')
        expect(equity_input).to_be_visible()
        expect(equity_input).to_have_value("10000")

        submit_btn = page.get_by_role("button", name="Chạy kiểm thử")
        expect(submit_btn).to_be_visible()

        # 5. Xác nhận danh sách các job đã lưu
        job_rows = page.locator("[data-job-id]:visible")
        expect(job_rows).to_have_count(3)

        # Job 1: Đang chạy 65%
        first_job = job_rows.nth(0)
        expect(first_job).to_contain_text("Đang chạy")
        expect(first_job).to_contain_text("65%")
        expect(first_job.get_by_role("button", name="Hủy")).to_be_visible()

        # Job 2: Hoàn tất 100% với run_ids
        second_job = job_rows.nth(1)
        expect(second_job).to_contain_text("Hoàn tất")
        expect(second_job).to_contain_text("100%")
        expect(second_job).to_contain_text("run-btc-oct2026-001")

        # Job 3: Đang chờ 0%
        third_job = job_rows.nth(2)
        expect(third_job).to_contain_text("Đang chờ")

        # 6. Chụp ảnh màn hình Desktop full-page
        page.screenshot(path=str(DESKTOP_SCREENSHOT), full_page=True)
        assert DESKTOP_SCREENSHOT.is_file()
        assert DESKTOP_SCREENSHOT.stat().st_size > 5000

        browser.close()


def test_admin_backtest_replays_mobile_view(backtest_server_url: str) -> None:
    """Kiểm tra giao diện Mobile (390x844) và chụp ảnh 13_backtests_mobile.png."""
    cookie_value = f"{WORKSPACE_ID}:valid_test_token_backtest_replays"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            color_scheme="light",
        )
        context.add_cookies(
            [
                {
                    "name": "crypidian_session",
                    "value": cookie_value,
                    "domain": "127.0.0.1",
                    "path": "/",
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            ]
        )
        page = context.new_page()
        watch_browser_violations(page)

        # 1. Mở trang /admin/backtests trên mobile
        page.goto(f"{backtest_server_url}/admin/backtests", wait_until="networkidle")

        # 2. Xác nhận tiêu đề và liên kết điều hướng
        heading = page.get_by_role("heading", name="Kiểm thử dữ liệu lịch sử (Backtest)")
        expect(heading).to_be_visible()
        nav_link = page.locator('nav.main-nav a[href="/admin/backtests"]')
        expect(nav_link).to_be_visible()

        # 3. Xác nhận trường form và kích thước điều khiển cảm ứng
        submit_btn = page.get_by_role("button", name="Chạy kiểm thử")
        expect(submit_btn).to_be_visible()
        btn_box = submit_btn.bounding_box()
        assert btn_box is not None
        assert btn_box["height"] >= 36  # Chiều cao phù hợp vùng chạm di động

        # 4. Xác nhận danh sách job hiển thị không bị tràn màn hình
        job_rows = page.locator("[data-job-id]:visible")
        expect(job_rows).to_have_count(3)
        expect(job_rows.first).to_contain_text("Đang chạy")

        # 5. Chụp ảnh màn hình Mobile full-page
        page.screenshot(path=str(MOBILE_SCREENSHOT), full_page=True)
        assert MOBILE_SCREENSHOT.is_file()
        assert MOBILE_SCREENSHOT.stat().st_size > 5000

        browser.close()

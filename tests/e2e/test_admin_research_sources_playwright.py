"""E2E browser tests với Playwright cho giao diện Nguồn nghiên cứu và Quyền sử dụng (WP-05).

Kiểm tra:
- Hiển thị danh sách nguồn nghiên cứu và badge trạng thái phân quyền.
- Hiển thị chi tiết nguồn đã duyệt (Verified): Nhà xuất bản, Endpoint HTTPS, Evidence,
  Quota và Lịch sử policy.
- Kiểm tra thanh điều hướng có mục 'Nguồn dữ liệu' liên kết tới /admin/research-sources.
- Mở form 'Tạo nguồn ứng viên' và xác nhận các trường dữ liệu bắt buộc.
- Kiểm tra tính đáp ứng hiển thị trên Desktop (1280x800) và Mobile iPhone (390x844).
- Chuyển đổi xem chi tiết nguồn khác (Macro Economic Calendar - Chờ duyệt).
- Chụp ảnh màn hình lưu vào thư mục artifacts:
  * 16_research_sources_desktop.png
  * 12_research_sources_mobile.png
"""

from __future__ import annotations

import hashlib
import secrets
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from playwright.sync_api import sync_playwright

from trading_bot.admin.presentation import (
    METRICS,
    PNL_METRIC_KEYS,
    label_vi,
    local_time,
    number_vi,
    pnl_tone,
)
from trading_bot.admin.research_source_router import admin_router as research_source_admin_router
from trading_bot.admin.research_source_router import router as research_source_router
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.application.source_policy_admin import SourceLicenseEvidence
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.security import LoginRateLimiter
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity, LoginResult
from trading_bot.config.settings import AppSettings
from trading_bot.domain.research.license_scope import SourceLicenseScope
from trading_bot.domain.research.source_policy import (
    SourcePolicyState,
    SourceUsePurpose,
)
from trading_bot.domain.research.source_registry import (
    SourceBudget,
    SourceQuotaSnapshot,
    SourceRegistryEntry,
    SourceUsage,
)

SCREENSHOTS_DIR = Path(
    "/Users/macbook/.gemini/antigravity/brain/4f23167d-60fd-49c3-8248-ddb2b669a2a7/screenshots"
)
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

WORKSPACE_ID = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
USER_ID = UUID("c3b4a5d6-e7f8-4a9b-0c1d-2e3f4a5b6c7d")
MEMBERSHIP_ID = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")

NOW = datetime.now(UTC)
UI_SCOPE = SourceLicenseScope(
    source_id="macro-economic-calendar",
    provider="FEDERAL_RESERVE",
    product="ECONOMIC_CALENDAR",
    venue="CRYPIDIAN",
    contract="SPOT",
    symbols=("BTCUSDT",),
    intervals=("1h",),
    data_start=datetime(2024, 1, 1, tzinfo=UTC),
    data_end=datetime(2035, 1, 1, tzinfo=UTC),
    allowed_purposes=(
        SourceUsePurpose.METADATA,
        SourceUsePurpose.BACKTEST_RESEARCH,
    ),
    allowed_outputs=("aggregate_report", "derived_features", "trained_model"),
    max_retention_days=None,
    attribution="Nguồn fixture chờ duyệt; chỉ dùng để kiểm giao diện.",
    derived_use=("aggregate_reporting", "feature_extraction", "model_training"),
)
MOCK_PENDING_EVIDENCE: dict[str, SourceLicenseEvidence] = {}
MOCK_PENDING_COMMANDS: list[tuple[str, Any]] = []

# Nguồn 1: Binance Official Research (Đã duyệt / Verified)
BUDGET_1 = SourceBudget(
    window=timedelta(hours=1),
    max_requests=100,
    max_bytes=100_000,
    max_cost=Decimal("5.00"),
    cost_unit="USD",
)
ENTRY_1 = SourceRegistryEntry.discover(
    source_id="binance-news-feed",
    publisher="Binance Official Research",
    endpoint_url="https://api.binance.com/research/feed",
    access_method="GET",
    auth_scope="public",
    budget=BUDGET_1,
    actor_id=str(uuid4()),
    at=NOW - timedelta(days=3),
    reason="Ứng viên dữ liệu tin tức và nghiên cứu Binance",
)
ENTRY_1 = ENTRY_1.transition(
    policy_id="policy:binance:pending-001",
    state=SourcePolicyState.RIGHTS_PENDING,
    reason="Trình hồ sơ thẩm định quyền khai thác dữ liệu",
    actor_id=str(uuid4()),
    at=NOW - timedelta(days=2),
    budget=BUDGET_1,
)
ENTRY_1 = ENTRY_1.transition(
    policy_id="policy:binance:verified-001",
    state=SourcePolicyState.VERIFIED,
    reason="Đã hoàn tất rà soát điều khoản API và cấp quyền nhúng nội bộ",
    actor_id=str(uuid4()),
    at=NOW - timedelta(days=1),
    reviewed_at=NOW - timedelta(days=1),
    allowed_purposes=frozenset(
        {
            SourceUsePurpose.METADATA,
            SourceUsePurpose.EMBEDDING,
            SourceUsePurpose.BACKTEST_RESEARCH,
            SourceUsePurpose.PAPER_DEMO,
        }
    ),
    budget=BUDGET_1,
    evidence_ref="https://binance.com/terms/api-research-v2",
    valid_until=NOW + timedelta(days=365),
    approved_by="Chuyên viên Tuân thủ",
)
SNAPSHOT_1 = SourceQuotaSnapshot(
    source_id="binance-news-feed",
    policy_sequence=3,
    as_of=NOW,
    window_started_at=NOW - timedelta(minutes=25),
    budget=BUDGET_1,
    usage=SourceUsage(
        window_started_at=NOW - timedelta(minutes=25),
        requests_used=15,
        bytes_used=12_400,
        cost_used=Decimal("0.75"),
        cost_unit="USD",
    ),
)

# Nguồn 2: Macro Economic Calendar (Chờ duyệt / Rights Pending)
BUDGET_2 = SourceBudget(
    window=timedelta(hours=2),
    max_requests=50,
    max_bytes=50_000,
    max_cost=Decimal("0.00"),
    cost_unit="USD",
)
ENTRY_2 = SourceRegistryEntry.discover(
    source_id="macro-economic-calendar",
    publisher="Federal Reserve Board",
    endpoint_url="https://www.federalreserve.gov/feeds",
    access_method="GET",
    auth_scope="public",
    budget=BUDGET_2,
    actor_id=str(uuid4()),
    at=NOW - timedelta(days=1),
    reason="Đề xuất nguồn lịch kinh tế vĩ mô FOMC",
)
ENTRY_2 = ENTRY_2.transition(
    policy_id="policy:macro:pending-001",
    state=SourcePolicyState.RIGHTS_PENDING,
    reason="Đang chờ thẩm định quyền lưu trữ và tái phân phối",
    actor_id=str(uuid4()),
    at=NOW - timedelta(hours=6),
    allowed_purposes=frozenset(
        {
            SourceUsePurpose.METADATA,
            SourceUsePurpose.BACKTEST_RESEARCH,
        }
    ),
    budget=BUDGET_2,
    evidence_ref="fed-policy-fomc-2026",
    valid_until=NOW + timedelta(days=180),
    provider=UI_SCOPE.provider,
    product=UI_SCOPE.product,
    venue=UI_SCOPE.venue,
    contract=UI_SCOPE.contract,
    license_scope=UI_SCOPE,
    attribution=UI_SCOPE.attribution,
)


class MockAuthService:
    """Mock auth service in-memory cho test E2E."""

    def __init__(self) -> None:
        self.sessions: dict[str, AuthIdentity] = {}
        self.valid_email = "admin@crypidian.io"
        self.valid_password = "correct_password"
        self.valid_workspace = "demo-workspace"
        self.csrf_token = "csrf-token-test-secret-sources-123456"

    async def login(
        self,
        *,
        workspace_slug: str,
        email: str,
        password: str,
        code: str,
    ) -> LoginResult:
        if (
            workspace_slug.strip().casefold() != self.valid_workspace
            or email.strip().casefold() != self.valid_email
            or password != self.valid_password
            or len(code.strip()) != 6
        ):
            raise AuthenticationFailure(401, "Thông tin đăng nhập không hợp lệ.")

        token = secrets.token_urlsafe(32)
        principal = Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({Role.OWNER, Role.RISK_ADMIN}),
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


class MockPrincipalSourcePolicyAdminFacade:
    """Mock facade phục vụ thao tác policy nguồn của một Principal."""

    def __init__(self, principal: Principal) -> None:
        self.principal = principal

    async def list_sources(self) -> tuple[SourceRegistryEntry, ...]:
        return (ENTRY_1, ENTRY_2)

    async def get_source(
        self, source_id: str
    ) -> tuple[SourceRegistryEntry, SourceQuotaSnapshot | None] | None:
        if source_id == ENTRY_1.source_id:
            return (ENTRY_1, SNAPSHOT_1)
        if source_id == ENTRY_2.source_id:
            return (ENTRY_2, None)
        return None

    async def get_pending_license_evidence(
        self, source_id: str, policy_id: str
    ) -> SourceLicenseEvidence | None:
        if source_id == ENTRY_2.source_id and policy_id == ENTRY_2.current.policy.policy_id:
            return MOCK_PENDING_EVIDENCE.get(policy_id)
        return None

    async def upload_license_evidence(
        self,
        source_id: str,
        policy_id: str,
        stream: Any,
        *,
        media_type: str,
        content_length: str | None,
        idempotency_key: UUID,
    ) -> SourceLicenseEvidence:
        del content_length
        if source_id != ENTRY_2.source_id or policy_id != ENTRY_2.current.policy.policy_id:
            raise LookupError("Không tìm thấy policy fixture.")
        digest = hashlib.sha256()
        byte_count = 0
        async for chunk in stream:
            digest.update(chunk)
            byte_count += len(chunk)
        evidence = SourceLicenseEvidence(
            evidence_id=UUID("b1c2d3e4-f5a6-4789-8abc-def012345678"),
            source_id=source_id,
            policy_id=policy_id,
            idempotency_key=idempotency_key,
            evidence_ref=ENTRY_2.current.policy.evidence_ref or "",
            digest=digest.hexdigest(),
            byte_count=byte_count,
            media_type=media_type,
            license_scope=UI_SCOPE,
            actor_id=str(self.principal.user_id),
            created_at=datetime.now(UTC),
        )
        MOCK_PENDING_EVIDENCE[policy_id] = evidence
        return evidence

    async def create_source(self, command: Any, key: Any) -> Any:
        return ENTRY_1.current

    async def submit_pending(self, source_id: str, command: Any, key: Any) -> Any:
        MOCK_PENDING_COMMANDS.append((source_id, command))
        return ENTRY_2.current

    async def change_disabled_state(self, source_id: str, state: Any, reason: str, key: Any) -> Any:
        return ENTRY_1.current

    async def approve_pending(self, source_id: str, pending_id: str, command: Any, key: Any) -> Any:
        return ENTRY_1.current


class MockSourcePolicyAdminService:
    """Mock service điều phối facade theo Principal."""

    def for_principal(self, principal: Principal) -> MockPrincipalSourcePolicyAdminFacade:
        return MockPrincipalSourcePolicyAdminFacade(principal)


class MockBotAdminStub:
    async def list_bots(self, _principal: Principal) -> list[dict[str, Any]]:
        return []


class MockBotOperationsStub:
    def for_principal(self, _principal: Principal) -> MockBotOperationsStub:
        return self

    async def list_bots(self) -> list[dict[str, Any]]:
        return []

    async def workspace_status(self) -> dict[str, Any]:
        return {"kill_state": "ACTIVE", "revision": 1, "bots": []}


def create_test_research_sources_app() -> FastAPI:
    """Khởi tạo FastAPI app phục vụ Playwright E2E test cho Research Sources UI."""
    app = FastAPI(title="Crypidian Research Sources Test Server")

    app_settings = AppSettings(app_env="local")
    app.state.settings = app_settings
    templates = Jinja2Templates(directory="src/trading_bot/templates")
    templates.env.globals["session_cookie_name"] = lambda s: "crypidian_session"
    templates.env.filters.update(
        label_vi=label_vi,
        local_time=local_time,
        number_vi=number_vi,
        pnl_tone=pnl_tone,
    )
    templates.env.globals["report_metrics"] = METRICS
    templates.env.globals["pnl_metric_keys"] = PNL_METRIC_KEYS
    app.state.templates = templates
    app.state.login_limiter = LoginRateLimiter()

    auth_service = MockAuthService()
    test_token = "valid_test_token_sources"
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
    app.state.bot_admin_service = MockBotAdminStub()
    app.state.bot_operations_service = MockBotOperationsStub()
    app.state.source_policy_admin_service = MockSourcePolicyAdminService()

    app.mount("/static", StaticFiles(directory="src/trading_bot/static"), name="static")
    app.include_router(research_source_router)
    app.include_router(research_source_admin_router)
    app.include_router(admin_web_router)
    return app


@pytest.fixture(scope="module")
def sources_server_url() -> Any:
    """Khởi chạy uvicorn server in-process trên cổng ngẫu nhiên cho Playwright test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app = create_test_research_sources_app()
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

    time.sleep(0.6)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=3)


def test_research_sources_desktop_view(sources_server_url: str) -> None:
    """Kiểm tra giao diện Desktop (1280x800), hiển thị chi tiết nguồn và lưu screenshot."""
    cookie_value = f"{WORKSPACE_ID}:valid_test_token_sources"
    screenshot_path = SCREENSHOTS_DIR / "16_research_sources_desktop.png"

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

        # 1. Truy cập trang Nguồn nghiên cứu
        target_url = f"{sources_server_url}/admin/research-sources?source_id=binance-news-feed"
        page.goto(target_url, wait_until="networkidle")

        # 2. Xác nhận tiêu đề và nội dung chính
        content = page.content()
        assert "Nguồn nghiên cứu và quyền sử dụng" in content
        assert "Chỉ quản lý metadata và policy ngoại tuyến" in content
        assert "Evidence tham chiếu" in content

        # 3. Xác nhận liên kết 'Nguồn dữ liệu' trong thanh điều hướng
        nav_link = page.locator("nav.main-nav a[href='/admin/research-sources']")
        assert nav_link.count() >= 1
        assert "Nguồn dữ liệu" in nav_link.first.inner_text()

        # 4. Xác nhận danh sách nguồn chứa cả 2 nguồn mẫu
        assert "binance-news-feed" in content
        assert "macro-economic-calendar" in content

        # 5. Xác nhận chi tiết nguồn binance-news-feed
        assert "Binance Official Research" in content
        assert "https://api.binance.com/research/feed" in content
        assert "Quota hiện hành" in content
        assert "Số yêu cầu: 15 / 100" in content

        # 6. Mở khối 'Tạo nguồn ứng viên'
        create_details = page.locator("details summary:has-text('Tạo nguồn ứng viên')")
        assert create_details.count() >= 1
        create_details.click()
        page.wait_for_timeout(200)

        assert page.locator("#create-source_id").is_visible()
        assert page.locator("#create-publisher").is_visible()

        # 7. Chụp ảnh màn hình Desktop toàn trang
        page.screenshot(path=str(screenshot_path), full_page=True)
        assert screenshot_path.exists()
        assert screenshot_path.stat().st_size > 10_000

        browser.close()


def test_research_sources_mobile_view(sources_server_url: str) -> None:
    """Kiểm tra giao diện Mobile (390x844), bố cục responsive và lưu screenshot."""
    cookie_value = f"{WORKSPACE_ID}:valid_test_token_sources"
    screenshot_path = SCREENSHOTS_DIR / "12_research_sources_mobile.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
            ),
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

        # 1. Truy cập trang Nguồn nghiên cứu
        target_url = f"{sources_server_url}/admin/research-sources?source_id=binance-news-feed"
        page.goto(target_url, wait_until="networkidle")

        # 2. Xác nhận phần tử chính trên Mobile
        content = page.content()
        assert "Nguồn nghiên cứu và quyền sử dụng" in content
        assert "binance-news-feed" in content

        # 3. Mở details và kiểm tra touch target của checkbox mục đích sử dụng
        policy_details = page.locator("details summary:has-text('Gửi policy chờ duyệt')")
        if policy_details.count() > 0:
            policy_details.first.click()
            page.wait_for_timeout(200)
            checkboxes = page.locator(".source-purpose-options input[type='checkbox']")
            if checkboxes.count() > 0:
                box = checkboxes.first.bounding_box()
                assert box is not None
                # Touch target chuẩn >= 40px
                assert box["width"] >= 40
                assert box["height"] >= 40

        # 4. Chụp ảnh màn hình Mobile toàn trang
        page.screenshot(path=str(screenshot_path), full_page=True)
        assert screenshot_path.exists()
        assert screenshot_path.stat().st_size > 10_000

        browser.close()


def test_research_sources_source_selection_flow(sources_server_url: str) -> None:
    """Kiểm tra luồng người dùng click chọn nguồn khác trong danh sách."""
    cookie_value = f"{WORKSPACE_ID}:valid_test_token_sources"

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

        # 1. Truy cập trang chính
        page.goto(f"{sources_server_url}/admin/research-sources", wait_until="networkidle")

        # 2. Click chọn nguồn macro-economic-calendar
        macro_link = page.locator("a:has-text('macro-economic-calendar')")
        assert macro_link.count() >= 1
        macro_link.first.click()
        page.wait_for_load_state("networkidle")

        # 3. Xác nhận hiển thị chi tiết nguồn thứ hai
        content = page.content()
        assert "Federal Reserve Board" in content
        assert "https://www.federalreserve.gov/feeds" in content
        assert "Đang chờ thẩm định quyền lưu trữ" in content

        browser.close()


def test_license_scope_digest_upload_and_review_selection_desktop_mobile(
    sources_server_url: str,
) -> None:
    """Kiểm form quyền typed, raw upload, digest sau reload và bố cục responsive."""
    MOCK_PENDING_EVIDENCE.clear()
    MOCK_PENDING_COMMANDS.clear()
    cookie_value = f"{WORKSPACE_ID}:valid_test_token_sources"
    file_bytes = b"private synthetic license body that must not be rendered"
    expected_digest = hashlib.sha256(file_bytes).hexdigest()
    evidence_id = "b1c2d3e4-f5a6-4789-8abc-def012345678"
    evidence_dir = Path(__file__).parent / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for name, width, height in (
            ("desktop", 1280, 800),
            ("mobile", 390, 844),
        ):
            context = browser.new_context(
                viewport={"width": width, "height": height},
                is_mobile=width < 500,
                has_touch=width < 500,
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
            page.goto(
                f"{sources_server_url}/admin/research-sources?source_id=macro-economic-calendar",
                wait_until="networkidle",
            )

            assert page.locator("html").get_attribute("lang") == "vi"
            assert page.get_by_text("Phạm vi quyền đã chuẩn hóa", exact=True).is_visible()
            assert page.locator("#scope-provider").input_value() == UI_SCOPE.provider
            assert page.locator("#scope-symbols").input_value() == "BTCUSDT"
            upload_form = page.get_by_role("form", name="Tải lên bằng chứng quyền")
            assert upload_form.count() == 1
            upload_form.locator('input[type="file"]').set_input_files(
                {
                    "name": "contract-private.txt",
                    "mimeType": "text/plain",
                    "buffer": file_bytes,
                }
            )
            upload_form.get_by_role("button", name="Tải lên và tính SHA-256").click()
            page.get_by_text(expected_digest, exact=True).wait_for(state="visible", timeout=5000)
            page.reload(wait_until="networkidle")
            page.get_by_text(expected_digest, exact=True).wait_for(state="visible", timeout=5000)
            assert "contract-private.txt" not in page.content()
            assert file_bytes.decode() not in page.content()

            evidence_select = page.get_by_label("Bằng chứng dùng để duyệt")
            assert evidence_select.locator(f'option[value="{evidence_id}"]').count() == 1
            page.locator("details summary:has-text('Duyệt quyền độc lập')").click()
            evidence_select.select_option(evidence_id)
            assert page.get_by_role("button", name="Sao chép SHA-256 đầy đủ").is_visible()
            page.locator("details summary:has-text('Gửi policy chờ duyệt')").click()
            pending_form = page.get_by_role("form", name="Gửi policy chờ duyệt")
            pending_form.locator("#pending-reason").fill("Kiểm thử gửi scope đã chuẩn hóa")
            pending_form.get_by_role("button", name="Gửi chờ duyệt").click()
            page.wait_for_load_state("networkidle")
            submitted_source, submitted = MOCK_PENDING_COMMANDS[-1]
            assert submitted_source == ENTRY_2.source_id
            assert submitted.license_scope.symbols == UI_SCOPE.symbols
            assert submitted.license_scope.intervals == UI_SCOPE.intervals
            assert submitted.license_scope.allowed_outputs == UI_SCOPE.allowed_outputs
            assert submitted.license_scope.derived_use == UI_SCOPE.derived_use
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

            page.screenshot(
                path=str(evidence_dir / f"wp06-license-evidence-{name}.png"),
                full_page=True,
            )
            context.close()
        browser.close()

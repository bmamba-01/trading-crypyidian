"""Kiểm thử giao diện Admin Web Dashboard & Responsive UI (WP-04).

Bao gồm:
- Banner thông báo chế độ PAPER nội bộ (theo ruling 2026-11-02).
- Các thẻ KPI (Tổng số bot, Bot sẵn sàng, Trạng thái khẩn cấp, Chế độ vận hành).
- Danh sách bot responsive (hỗ trợ hiển thị dạng card trên màn hình nhỏ và bảng trên màn hình lớn).
- Thuộc tính accessibility (ARIA landmarks, skip link, aria-live, aria-label).
- Tự động escape chống XSS và không rò rỉ thông tin nhạy cảm.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings

WORKSPACE_ID = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
MEMBERSHIP_ID = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
USER_ID = UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57")
BOT_ID_1 = UUID("77117cc4-6c7f-407e-8f23-948d740a46c2")
BOT_ID_2 = UUID("88228dd5-7d8e-518f-9034-059e851b57d3")


class _MockBotAdminService:
    def __init__(self, bots: list[dict[str, Any]] | None = None) -> None:
        self.bots: list[dict[str, Any]] = bots if bots is not None else []

    async def list_bots(self, _principal: Principal) -> list[dict[str, Any]]:
        return self.bots


class _MockBotOperationsService:
    def __init__(
        self,
        runtime_bots: list[dict[str, Any]] | None = None,
        kill_state: str = "ACTIVE",
    ) -> None:
        self.runtime_bots: list[dict[str, Any]] = runtime_bots if runtime_bots is not None else []
        self.kill_state = kill_state

    def for_principal(self, _principal: Principal) -> _MockBotOperationsService:
        return self

    async def list_bots(self) -> list[dict[str, Any]]:
        return self.runtime_bots

    async def workspace_status(self) -> dict[str, Any]:
        return {
            "kill_state": self.kill_state,
            "revision": 3,
            "bots": self.runtime_bots,
        }


class _MockAuthService:
    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return "csrf-test-token-safe-12345"


def _create_test_client(
    bots: list[dict[str, Any]] | None = None,
    runtime_bots: list[dict[str, Any]] | None = None,
    kill_state: str = "ACTIVE",
) -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    principal = Principal(
        workspace_id=WORKSPACE_ID,
        membership_id=MEMBERSHIP_ID,
        user_id=USER_ID,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=principal, email="admin@crypidian.test"
    )
    app.state.bot_admin_service = _MockBotAdminService(bots)
    app.state.bot_operations_service = _MockBotOperationsService(
        runtime_bots, kill_state=kill_state
    )
    app.state.auth_service = _MockAuthService()
    return TestClient(app)


def test_dashboard_renders_mode_banner_and_disclaimer() -> None:
    """Kiểm tra banner chế độ PAPER và giải thích an toàn theo ruling 2026-11-02."""
    client = _create_test_client()
    response = client.get("/admin")

    assert response.status_code == 200
    html = response.text

    # Banner / Badge chế độ PAPER nội bộ
    assert "Mô phỏng (PAPER) nội bộ" in html
    # Giải thích rõ ràng không gọi API hay đặt lệnh lên Binance
    has_db_explanation = (
        "mô phỏng nội bộ trong database" in html.lower()
        or "mô phỏng nội bộ trong cơ sở dữ liệu" in html.lower()
    )
    assert has_db_explanation
    has_no_binance_api = (
        "không gọi api hay đặt lệnh lên binance" in html.lower() or "không gọi api" in html.lower()
    )
    assert has_no_binance_api


def test_dashboard_renders_kpi_cards_with_counts() -> None:
    """Kiểm tra KPI cards hiển thị đúng tổng bot, bot sẵn sàng, kill state và chế độ."""
    bots: list[dict[str, Any]] = [
        {
            "id": str(BOT_ID_1),
            "name": "Alpha-Trend-01",
            "status": "ENABLED",
            "account": {"mode": "PAPER", "status": "ACTIVE"},
        },
        {
            "id": str(BOT_ID_2),
            "name": "Beta-MeanRev-02",
            "status": "ENABLED",
            "account": {"mode": "PAPER", "status": "ACTIVE"},
        },
    ]
    runtime_bots: list[dict[str, Any]] = [
        {
            "id": str(BOT_ID_1),
            "readiness": "READY",
            "entry_state": "ENABLED",
            "entry_allowed": True,
            "control_revision": 1,
        },
        {
            "id": str(BOT_ID_2),
            "readiness": "DEGRADED",
            "entry_state": "PAUSED",
            "entry_allowed": False,
            "control_revision": 2,
        },
    ]

    client = _create_test_client(bots=bots, runtime_bots=runtime_bots, kill_state="ACTIVE")
    response = client.get("/admin")

    assert response.status_code == 200
    html = response.text

    # KPI 1: Tổng số bot
    assert "Tổng số bot" in html
    assert ">2<" in html or "2 bot" in html or 'class="kpi-value">2' in html

    # KPI 2: Số bot sẵn sàng
    assert "sẵn sàng" in html.lower()
    assert ">1<" in html or "1 bot" in html or 'class="kpi-value">1' in html

    # KPI 3: Trạng thái khẩn cấp (Emergency Kill Switch)
    assert "khẩn cấp" in html.lower() or "kill switch" in html.lower()
    assert "ACTIVE" in html

    # KPI 4: Chế độ vận hành
    assert "Chế độ vận hành" in html or "Chế độ" in html
    assert "PAPER" in html


def test_dashboard_killed_state_renders_alert_indicator() -> None:
    """Kiểm tra khi workspace bị KILLED, trạng thái khẩn cấp hiển thị KILLED và có cảnh báo."""
    client = _create_test_client(kill_state="KILLED")
    response = client.get("/admin")

    assert response.status_code == 200
    html = response.text
    assert "KILLED" in html


def test_dashboard_accessibility_landmarks_and_skip_links() -> None:
    """Kiểm tra các thuộc tính trợ năng: skip link, aria-label, aria-live, landmarks."""
    bots: list[dict[str, Any]] = [
        {
            "id": str(BOT_ID_1),
            "name": "Bot-Accessible-01",
            "status": "ENABLED",
            "account": {"mode": "PAPER", "status": "ACTIVE"},
        }
    ]
    runtime_bots: list[dict[str, Any]] = [
        {
            "id": str(BOT_ID_1),
            "readiness": "READY",
            "entry_state": "ENABLED",
            "entry_allowed": True,
            "control_revision": 1,
        }
    ]
    client = _create_test_client(bots=bots, runtime_bots=runtime_bots)
    response = client.get("/admin")

    assert response.status_code == 200
    html = response.text

    # Skip link
    assert 'class="skip-link"' in html
    assert 'href="#main-content"' in html

    # ARIA landmark chính
    assert 'id="main-content"' in html
    assert "<nav" in html
    assert 'aria-label="Điều hướng chính"' in html

    # Live region cho trạng thái khẩn cấp / chế độ
    assert 'aria-live="polite"' in html or 'role="status"' in html

    # data-label cho bảng/card responsive
    assert 'data-label="Tên bot"' in html or 'data-label="Bot"' in html


def test_dashboard_escapes_xss_safely() -> None:
    """Kiểm tra autoescape chống tấn công XSS trong tên bot."""
    malicious_name = '<script>alert("xss-test")</script>'
    bots: list[dict[str, Any]] = [
        {
            "id": str(BOT_ID_1),
            "name": malicious_name,
            "status": "ENABLED",
            "account": {"mode": "PAPER", "status": "ACTIVE"},
        }
    ]
    client = _create_test_client(bots=bots)
    response = client.get("/admin")

    assert response.status_code == 200
    html = response.text

    assert "<script>alert" not in html
    assert "&lt;script&gt;alert" in html


def test_admin_css_supports_responsive_and_dark_mode() -> None:
    """Kiểm tra CSS có dark mode prefers-color-scheme và hit target >= 44px."""
    css_path = Path("src/trading_bot/static/admin.css")
    assert css_path.exists(), "Tệp admin.css phải tồn tại"

    css_content = css_path.read_text(encoding="utf-8")

    # Dark mode support
    assert "prefers-color-scheme: dark" in css_content

    # Hit target tối thiểu >= 44px (2.75rem = 44px khi base là 16px)
    assert "min-height: 2.75rem" in css_content or "min-height: 44px" in css_content

    # Mobile responsive rules (media query cho màn hình nhỏ)
    assert "@media (max-width:" in css_content or "@media (min-width:" in css_content


def test_dashboard_renders_live_status_bar_and_pulse_elements() -> None:
    """Kiểm tra thanh trạng thái trực tiếp, pulse dot và script realtime không dùng CDN."""
    client = _create_test_client()
    response = client.get("/admin")

    assert response.status_code == 200
    html = response.text

    # Live status bar và accessibility
    assert 'class="live-status-bar"' in html
    assert 'id="live-pulse-dot"' in html
    assert 'id="live-updated-at"' in html
    assert 'id="toggle-realtime-btn"' in html
    assert 'aria-pressed="true"' in html
    assert "Tự động cập nhật 5s" in html

    # IDs và data attributes phục vụ cập nhật DOM
    assert 'id="kpi-bot-count"' in html
    assert 'id="kpi-ready-count"' in html
    assert 'id="kpi-kill-status"' in html
    assert 'id="kpi-workspace-revision"' in html
    assert 'id="workspace-release-revision"' in html

    # Không sử dụng CDN ngoài
    assert "cdn." not in html
    assert "cdnjs." not in html
    assert "unpkg." not in html
    assert "jsdelivr." not in html

    # Có script gọi polling endpoint
    assert "/admin/api/dashboard/realtime" in html
    assert "pollIntervalMs = 5000" in html or "5000" in html

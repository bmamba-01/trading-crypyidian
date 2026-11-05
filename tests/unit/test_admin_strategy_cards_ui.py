"""Kiểm thử giao diện Admin Web và API Quản lý Thẻ Chiến lược (WP-SC.5).

Bao quát:
1. Xác thực truy cập: Yêu cầu đăng nhập, chặn truy cập trái phép khi không có session.
2. Giao diện Web HTML (/admin/strategy-cards):
   - Banner chế độ PAPER mô phỏng an toàn nội bộ (theo ruling 2026-11-02).
   - Thẻ tóm tắt KPI: Tổng số chiến lược, Đang kích hoạt, Đã kiểm định (Orthogonal).
   - Danh sách thẻ Strategy Card với tên, phiên bản SemVer, mã định danh duy nhất.
   - Băm xác thực SHA-256 bất biến rút gọn kèm tooltip.
   - Bảng phân tầng 7 lớp kiến trúc L0→L6 (Data Hygiene đến Zones).
   - Huy hiệu kiểm định: "Đã kiểm định (Orthogonal)", cảnh báo tương quan cao / nút mồ côi.
   - Semantic HTML và các thuộc tính Accessibility WCAG 2.2 AA (ARIA landmarks, labels, skip link).
3. Giao diện REST API (/admin/api/strategy-cards):
   - Trả về JSON hợp lệ với status: "success", tổng số cards và danh sách đầy đủ.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient

from trading_bot.admin.strategy_card_service import StrategyCardAdminService
from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings

WORKSPACE_ID = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
MEMBERSHIP_ID = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
USER_ID = UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57")


class _MockUnauthService:
    async def authenticate(self, _cookie: str | None) -> AuthIdentity:
        raise AuthenticationFailure(401, "Yêu cầu đăng nhập.")


def _create_unauthenticated_client() -> TestClient:
    """Khởi tạo TestClient với dịch vụ xác thực từ chối phiên (401)."""
    app = create_app(AppSettings(app_env="test"))
    app.state.auth_service = _MockUnauthService()
    return TestClient(app)


def _create_authenticated_client(
    service: StrategyCardAdminService | None = None,
) -> TestClient:
    """Khởi tạo TestClient với authentication override cho principal OWNER."""
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
    if service is not None:
        app.state.strategy_card_service = service
    return TestClient(app)


class TestAdminStrategyCardsUI:
    """Bộ kiểm thử đơn vị cho trang giao diện HTML Strategy Cards."""

    def test_strategy_cards_page_requires_auth(self) -> None:
        """Truy cập không có thông tin đăng nhập bị từ chối 401."""
        client = _create_unauthenticated_client()
        response = client.get("/admin/strategy-cards", follow_redirects=False)
        assert response.status_code == 401

    def test_strategy_cards_page_renders_successfully(self) -> None:
        """Giao diện HTML hiển thị đầy đủ tiêu đề, banner PAPER, KPI và danh sách card."""
        client = _create_authenticated_client()
        response = client.get("/admin/strategy-cards")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        html = response.text

        # 1. Tiêu đề và điều hướng
        assert "Quản lý Thẻ Chiến lược (Strategy Cards)" in html
        assert 'href="/admin/strategy-cards"' in html
        assert "Chiến lược" in html

        # 2. Banner chế độ PAPER nội bộ
        assert "Mô phỏng (PAPER) nội bộ" in html
        assert "không gọi API hay đặt lệnh lên Binance" in html

        # 3. Thẻ tóm tắt KPI
        assert "Tổng số chiến lược" in html
        assert "Đang kích hoạt" in html
        assert "Đã kiểm định (Orthogonal)" in html

        # 4. Tên chiến lược và mã định danh
        has_donchian = (
            "Donchian Trend &amp; Volatility Breakout" in html
            or "Donchian Trend & Volatility Breakout" in html
        )
        assert has_donchian
        assert "SC-BINANCE-USDM-VOL-BREAKOUT-001" in html

        has_reversion = (
            "Adaptive Wick Rejection &amp; Mean Reversion" in html
            or "Adaptive Wick Rejection & Mean Reversion" in html
        )
        assert has_reversion
        assert "SC-BINANCE-USDM-MEAN-REVERT-002" in html

        # 5. Băm xác thực SHA-256 rút gọn
        assert "Băm xác thực (SHA-256)" in html
        assert "hash-code" in html

        # 6. Huy hiệu kiểm định trực giao
        assert "Đã kiểm định (Orthogonal)" in html

        # 7. Phân tầng kiến trúc L0 -> L6 đầy đủ
        assert "L0" in html
        assert "Data Hygiene" in html
        assert "L1" in html
        assert "Candle Anatomy" in html
        assert "L2" in html
        assert "Indicators" in html
        assert "L3" in html
        assert "Algorithms" in html
        assert "L4" in html
        assert "Context Gate" in html
        assert "L5" in html
        assert "Trigger" in html
        assert "L6" in html
        assert "Zones" in html

        # 8. Cặp giao dịch và tác giả
        assert "BTCUSDT" in html
        assert "Crypidian Core Quant Team" in html

    def test_strategy_cards_accessibility_and_semantic_structure(self) -> None:
        """Kiểm tra cấu trúc ngữ nghĩa WCAG 2.2 AA và ARIA landmarks."""
        client = _create_authenticated_client()
        response = client.get("/admin/strategy-cards")
        html = response.text

        # Skip link cho bàn phím và screen reader
        assert 'class="skip-link"' in html
        assert 'href="#main-content"' in html

        # ARIA landmarks & roles
        assert 'role="banner"' in html
        assert 'role="region"' in html
        assert 'role="list"' in html
        assert 'role="listitem"' in html
        assert 'role="contentinfo"' in html

        # ARIA labels
        assert 'aria-label="Chỉ số tổng quan chiến lược"' in html
        assert 'aria-label="Danh sách các thẻ chiến lược"' in html


class TestAdminStrategyCardsAPI:
    """Bộ kiểm thử cho REST API /admin/api/strategy-cards."""

    def test_api_requires_auth(self) -> None:
        """Endpoint API yêu cầu xác thực phiên."""
        client = _create_unauthenticated_client()
        response = client.get("/admin/api/strategy-cards")
        assert response.status_code == 401

    def test_api_returns_strategy_cards_json(self) -> None:
        """Endpoint API trả về danh sách thẻ chiến lược định dạng JSON chuẩn mực."""
        client = _create_authenticated_client()
        response = client.get("/admin/api/strategy-cards")

        assert response.status_code == 200
        assert "application/json" in response.headers["content-type"]
        data = response.json()

        assert data["status"] == "success"
        assert data["total"] >= 2
        cards = data["cards"]
        assert len(cards) >= 2

        card0 = cards[0]
        assert "card_id" in card0
        assert "version" in card0
        assert "content_hash" in card0
        assert "layers_breakdown" in card0
        assert "is_verified" in card0
        assert card0["is_verified"] is True
        assert "L0_DATA_HYGIENE" in card0["layers_breakdown"]
        assert "L6_ZONES" in card0["layers_breakdown"]

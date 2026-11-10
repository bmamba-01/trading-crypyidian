"""Hợp đồng trình bày tiếng Việt, tài sản và lỗi của giao diện quản trị."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_admin_web_views import BOT_ID, AuthServiceStub, _client


def test_mobile_layout_loads_only_local_styles_and_vietnamese_font() -> None:
    client = _client()
    page = client.get("/admin/login")
    assert "/static/vendor/bootstrap-5.3.8.min.css" in page.text
    assert "<h1>Đăng nhập</h1>" in page.text
    css = client.get("/static/admin.css").text
    assert "Be Vietnam Pro" in css
    assert "font-display: swap" in css
    assert "--market-up: #0ECB81" in css
    assert "--market-down: #F6465D" in css
    assert "--pnl-positive-text: #087A55" in css
    assert "--pnl-negative-text: #B4233A" in css
    assert "https://" not in css
    assert client.get("/static/fonts/be-vietnam-pro-400.woff2").status_code == 200


@pytest.mark.parametrize(
    ("code", "label"),
    [
        ("READY", "Sẵn sàng"),
        ("PAUSED", "Tạm dừng"),
        ("NEW_STATE", "Chưa xác định"),
        ("OPEN", "Đang mở"),
        ("STARTING", "Đang khởi động"),
        ("STANDBY", "Đang chờ tiếp quản"),
        ("RECOVERY_REQUIRED", "Cần khôi phục"),
        ("PARTIALLY_FILLED", "Đã khớp một phần"),
        ("FILLED", "Đã khớp toàn bộ"),
    ],
)
def test_bot_labels_translate_known_states_and_fail_closed_for_unknown(
    code: str, label: str
) -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()
    service = client.app.state.bot_operations_service

    async def status(_bot_id: object) -> dict[str, object]:
        return {"readiness": code, "entry_state": code}

    service.bot_status = status
    page = client.get(f"/admin/bots/{BOT_ID}")
    assert page.status_code == 200
    assert label in page.text
    assert f">{code}<" not in page.text
    assert "Mô phỏng" in page.text
    assert "Tạm dừng bot" in page.text


def test_dashboard_metrics_fail_closed_when_reporting_service_is_unavailable() -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Thống kê hiệu quả chưa sẵn sàng" in page.text
    assert "Xem báo cáo theo phạm vi được cấp quyền" in page.text
    assert "Điều khiển không gian làm việc: Đang hoạt động" in page.text


def test_report_renders_authoritative_metrics_separately_per_asset() -> None:
    client = _client()

    class Reporting:
        async def get_bot_report(self, query: object, principal: object) -> object:
            return SimpleNamespace(
                as_dict=lambda: {
                    "scope": "bot",
                    "window": {"start": "2026-10-01T00:00:00Z", "end": "2026-11-01T00:00:00Z"},
                    "totals": {
                        "USDT": {
                            "net_pnl": "12.345",
                            "closed_trade_count": 2,
                            "fee_total": "0.2",
                            "funding_total": "0.1",
                            "slippage_total": "0.01",
                            "max_drawdown": "1.25",
                        },
                        "USDC": {"net_pnl": "-4.5", "closed_trade_count": 1},
                    },
                }
            )

    client.app.state.bot_reporting_service = Reporting()
    page = client.get(
        "/admin/reports",
        params={
            "scope": "bot",
            "bot_id": str(BOT_ID),
            "start": "2026-10-01T00:00:00Z",
            "end": "2026-11-01T00:00:00Z",
        },
    )
    assert page.status_code == 200
    assert "Lãi/lỗ ròng đã thực hiện" in page.text
    assert "12,345 USDT" in page.text
    assert "-4,5 USDC" in page.text
    assert 'data-pnl-tone="positive"' in page.text
    assert 'data-pnl-tone="negative"' in page.text
    assert "pnl-value--positive" in page.text
    assert "pnl-value--negative" in page.text
    assert "+12,345 USDT" in page.text
    assert "Asia/Ho_Chi_Minh" in page.text
    assert "01/10/2026 07:00:00" in page.text
    assert "<summary>Dữ liệu kỹ thuật JSON</summary>" in page.text
    assert '<option value="">Không chọn bot</option>' in page.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [("12.5", "positive"), ("-4.5", "negative"), ("0", "flat"), (None, "unknown")],
)
def test_pnl_tone_distinguishes_gain_loss_flat_and_unknown(value: object, expected: str) -> None:
    from trading_bot.admin.presentation import pnl_tone

    assert pnl_tone(value) == expected


def test_admin_errors_are_accessible_html_without_internal_detail_or_changed_status() -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()
    page = client.post("/admin/flatten/preview", data={"reason": "x"})
    assert page.status_code == 403
    assert page.headers["content-type"].startswith("text/html")
    assert 'role="alert"' in page.text
    assert "Tải lại trang" in page.text
    assert "Cache-Control" in page.headers
    assert "Xác thực hoặc quyền truy cập chưa đáp ứng yêu cầu" in page.text


def test_display_time_preserves_offset_and_rejects_naive_dates() -> None:
    from trading_bot.admin.presentation import local_time

    assert (
        local_time(datetime(2026, 11, 2, 0, tzinfo=UTC)) == "02/11/2026 07:00:00 (Asia/Ho_Chi_Minh)"
    )
    assert local_time("2026-11-02T00:00:00") == "Chưa xác định thời gian"


def test_dashboard_expired_csrf_session_preserves_auth_status_in_html() -> None:
    from trading_bot.auth.service import AuthenticationFailure

    client = _client()

    class ExpiredAuth:
        async def issue_csrf_token(self, cookie: str | None) -> str:
            raise AuthenticationFailure(401, "Phiên đã hết hạn")

    client.app.state.auth_service = ExpiredAuth()
    page = client.get("/admin")
    assert page.status_code == 401
    assert 'role="alert"' in page.text
    assert "Đăng nhập lại" in page.text


def test_skip_target_is_programmatically_focusable_without_adding_a_tab_stop() -> None:
    client = _client()
    page = client.get("/admin/login")
    assert '<main id="main-content" tabindex="-1">' in page.text

"""Unit tests kiểm thử giao diện Bot Detail Trading Revamp & Endpoint Realtime 5s."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from starlette.testclient import TestClient

from trading_bot.admin.bot_service import BotAdminError
from trading_bot.admin.presentation import label_vi, local_time, number_vi, pnl_tone
from trading_bot.admin.web_router import current_identity, router
from trading_bot.auth.service import AuthIdentity, Principal


@pytest.fixture
def mock_identity() -> AuthIdentity:
    workspace_id = uuid4()
    user_id = uuid4()
    principal = Principal(
        workspace_id=workspace_id,
        user_id=user_id,
        membership_id=uuid4(),
        roles=frozenset({"trader", "owner"}),
        mfa_verified_at=datetime.now(UTC),
    )
    return AuthIdentity(
        principal=principal,
        email="trader@crypidian.io",
    )


@pytest.fixture
def test_app(mock_identity: AuthIdentity) -> tuple[FastAPI, uuid4]:
    from types import SimpleNamespace

    app = FastAPI()
    app.state.settings = SimpleNamespace(app_env="test")
    templates = Jinja2Templates(directory="src/trading_bot/templates")
    templates.env.filters["local_time"] = local_time
    templates.env.filters["number_vi"] = number_vi
    templates.env.filters["label_vi"] = label_vi
    templates.env.filters["pnl_tone"] = pnl_tone
    app.state.templates = templates

    bot_id = uuid4()
    bot_data = {
        "id": bot_id,
        "name": "Bot Alpha Momentum",
        "symbol": "BTCUSDT",
        "status": "RUNNING",
        "account": {"mode": "PAPER", "status": "ACTIVE"},
        "versions": [
            {
                "id": str(uuid4()),
                "status": "PUBLISHED",
                "revision": 1,
                "overlay": {"schema_version": 1},
            }
        ],
    }

    mock_admin_service = MagicMock()
    mock_admin_service.get_bot = AsyncMock(return_value=bot_data)
    app.state.bot_admin_service = mock_admin_service

    mock_service = MagicMock()
    mock_operations = MagicMock()
    mock_operations.bot_status = AsyncMock(
        return_value={
            "readiness": "READY",
            "entry_state": "POSITION_OPEN",
            "entry_allowed": True,
            "control_revision": 3,
            "needs_manual_enable": False,
        }
    )
    mock_service.for_principal.return_value = mock_operations
    app.state.bot_operations_service = mock_service

    mock_auth_service = MagicMock()
    mock_auth_service.issue_csrf_token = AsyncMock(return_value="test_csrf_token_123")
    app.state.auth_service = mock_auth_service

    app.dependency_overrides[current_identity] = lambda: mock_identity
    app.include_router(router)
    return app, bot_id


def test_bot_detail_html_renders_trading_revamp(test_app: tuple[FastAPI, uuid4]) -> None:
    app, bot_id = test_app
    client = TestClient(app)

    response = client.get(f"/admin/bots/{bot_id}")
    assert response.status_code == 200
    html = response.text

    # 1. Tiêu đề và Header Bot
    assert "Bot Alpha Momentum" in html
    assert "BTCUSDT" in html
    assert "ĐANG CHẠY" in html
    assert "PAPER" in html

    # 2. Thanh cập nhật trực tiếp 5s
    assert 'class="live-status-bar"' in html
    assert 'id="live-pulse-dot"' in html
    assert 'id="live-updated-at"' in html
    assert "Tự động cập nhật 5s" in html
    assert 'id="toggle-realtime-btn"' in html

    # 3. Lưới Thẻ Chỉ Số KPI Trading
    assert 'id="kpi-open-positions"' in html
    assert 'id="kpi-win-rate"' in html
    assert 'id="kpi-profit-factor"' in html
    assert 'id="kpi-net-pnl"' in html
    assert 'id="kpi-max-drawdown"' in html

    # 4. Biểu đồ nến TradingView Lightweight Charts
    assert 'id="trade-chart"' in html
    assert 'id="trade-chart-data"' in html
    assert 'id="trade-chart-event-detail"' in html
    assert "Chốt lời (🎯 TP)" in html
    assert "Dừng lỗ (🛑 SL)" in html

    # 5. Bảng Vị Thế Đang Mở và Reasoning L4-L6
    assert 'id="open-positions-heading"' in html
    assert "Khối Lượng" in html
    assert "Đòn Bẩy" in html
    assert "Giá Vào Lệnh" in html
    assert "L4 Cửa Bối Cảnh (Context Gate)" in html
    assert "L5 Điểm Kích Hoạt (Trigger Score)" in html
    assert "Tỷ Lệ R:R" in html

    # 6. Bảng Lịch Sử Khớp Lệnh & Nút 1-click Tiếp Nhận Phạt AI
    assert 'id="recent-trades-heading"' in html
    assert "Vì sao vào:" in html
    assert "Vì sao thoát:" in html
    assert "Chi phí:" in html
    assert "📥 Đưa Ca Này Vào Dữ Liệu Phạt AI" in html

    # 7. Các Form Điều Khiển Bot
    assert f'action="/admin/bots/{bot_id}/pause"' in html
    assert f'action="/admin/bots/{bot_id}/resume"' in html
    assert f'action="/admin/bots/{bot_id}/disable"' in html


def test_bot_detail_realtime_api_returns_structured_trading_data(
    test_app: tuple[FastAPI, uuid4],
) -> None:
    app, bot_id = test_app
    client = TestClient(app)

    response = client.get(f"/admin/api/bots/{bot_id}/realtime")
    assert response.status_code == 200
    data = response.json()

    # Kiểm tra các khối cấu trúc dữ liệu JSON cốt lõi
    assert "server_time" in data
    assert "vn" in data["server_time"]
    assert "vn_time" in data["server_time"]

    assert "bot" in data
    assert data["bot"]["id"] == str(bot_id)
    assert data["bot"]["name"] == "Bot Alpha Momentum"
    assert data["bot"]["status"] == "RUNNING"
    assert data["bot"]["symbol"] == "BTCUSDT"

    assert "metrics" in data
    metrics = data["metrics"]
    assert "open_positions_count" in metrics
    assert "win_rate_percent" in metrics
    assert "profit_factor" in metrics
    assert "net_pnl" in metrics

    assert "positions" in data
    assert isinstance(data["positions"], list)
    assert len(data["positions"]) > 0
    pos = data["positions"][0]
    assert pos["symbol"] == "BTCUSDT"
    assert pos["side"] == "LONG"
    assert "target_leverage" in pos
    assert "reasoning" in pos
    assert "l4_regime" in pos["reasoning"]
    assert "l5_trigger" in pos["reasoning"]
    assert "l6_risk_reward" in pos["reasoning"]

    assert "recent_trades" in data
    assert isinstance(data["recent_trades"], list)
    assert len(data["recent_trades"]) > 0
    trade = data["recent_trades"][0]
    assert "trade_id" in trade
    assert "quality" in trade
    assert "post_mortem" in trade
    assert "why_enter" in trade["post_mortem"]
    assert "why_exit" in trade["post_mortem"]

    assert "chart_data" in data
    chart = data["chart_data"]
    assert chart["symbol"] == "BTCUSDT"
    assert "bars" in chart
    assert len(chart["bars"]) > 0
    assert "trade" in chart


def test_bot_detail_realtime_api_returns_404_for_unknown_bot(
    test_app: tuple[FastAPI, uuid4],
) -> None:
    app, _ = test_app
    app.state.bot_admin_service.get_bot.side_effect = BotAdminError(
        status_code=404, message="Không tìm thấy bot."
    )
    client = TestClient(app)

    random_id = uuid4()
    response = client.get(f"/admin/api/bots/{random_id}/realtime")
    assert response.status_code == 404
    assert response.json()["detail"] == "Không tìm thấy nội dung trong phạm vi được cấp quyền."

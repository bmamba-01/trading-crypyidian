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


CLOSED_TRADE_ID = "11111111-2222-4333-8444-555555555555"
OPEN_TRADE_ID = "66666666-7777-4888-8999-aaaaaaaaaaaa"


class StubBotReport:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def as_dict(self) -> dict[str, object]:
        return self._payload


class StubReportingService:
    """Trả báo cáo ledger PAPER cố định để kiểm tra Bot Detail đọc đúng nguồn."""

    def __init__(self, *, bars: list[dict[str, object]] | None = None) -> None:
        self.bars = (
            bars
            if bars is not None
            else [{"time": 1_763_000_000, "open": 100, "high": 110, "low": 95, "close": 105}]
        )
        self.chart_calls: list[tuple[str, str]] = []

    def _trade(self, bot_id: object) -> dict[str, object]:
        return {
            "trade_id": CLOSED_TRADE_ID,
            "bot_id": str(bot_id),
            "symbol": "ETHUSDT",
            "side": "SHORT",
            "quote_asset": "USDT",
            "opened_at": "2026-11-10T01:00:00+00:00",
            "closed_at": "2026-11-10T03:00:00+00:00",
            "gross_pnl": "50",
            "fee_total": "1.5",
            "funding_total": "0.25",
            "slippage_total": "0.75",
            "total_cost": "2.5",
            "net_pnl": "47.5",
            "entry_reason": {"strategy_id": "H03_BREAKOUT", "detail_vi": "Phá vỡ vùng tích lũy."},
            "exit_reason": "TAKE_PROFIT",
            "fills": [
                {"role": "ENTRY", "quantity": "2", "price": "3000"},
                {"role": "ENTRY", "quantity": "2", "price": "3100"},
                {"role": "EXIT", "quantity": "4", "price": "3037.5"},
            ],
        }

    async def get_bot_report(self, query: object, principal: object) -> StubBotReport:
        bot_id = query.bot_id  # type: ignore[attr-defined]
        return StubBotReport(
            {
                "totals": {
                    "USDT": {
                        "closed_trade_count": 4,
                        "win_rate_percent": "75",
                        "profit_factor": "3.2",
                        "expectancy_per_trade": "11.875",
                        "net_pnl": "47.5",
                        "max_drawdown": "12.25",
                    }
                },
                "trades": [self._trade(bot_id)],
                "open_trades": [
                    {
                        "trade_id": OPEN_TRADE_ID,
                        "symbol": "SOLUSDT",
                        "side": "LONG",
                        "quote_asset": "USDT",
                        "opened_at": "2026-11-12T05:00:00+00:00",
                    }
                ],
            }
        )

    async def get_trade_plan_summaries(
        self, query: object, principal: object, *, trade_ids: object
    ) -> dict[object, dict[str, object]]:
        return {}

    async def get_trade_chart(
        self, query: object, principal: object, *, trade_id: object, timeframe: str = "1h"
    ) -> dict[str, object]:
        self.chart_calls.append((str(trade_id), timeframe))
        return {
            "symbol": "ETHUSDT",
            "side": "SHORT",
            "timeframe": timeframe,
            "trade": self._trade(query.bot_id),  # type: ignore[attr-defined]
            "bars": self.bars,
            "notice": None,
        }


FABRICATED_SAMPLE_VALUES = (
    "66.7",
    "320.50",
    "0.050 BTC",
    "3,391.00",
    "67,250.00",
    "TREND_HAMMER_V2",
    "Pinbar",
    "c1a2b3c4-d5e6-4f7a-8b9c-0d1e2f3a4b5c",
    "ĐÃ DUYỆT TÍN HIỆU L4-L6",
)


def test_bot_detail_without_reporting_source_shows_empty_states_not_sample_numbers(
    test_app: tuple[FastAPI, uuid4],
) -> None:
    """Không có nguồn ledger thì trang phải nói rõ là chưa có dữ liệu, không dựng số mẫu."""
    app, bot_id = test_app
    response = TestClient(app).get(f"/admin/bots/{bot_id}")
    assert response.status_code == 200
    html = response.text

    assert "Bot Alpha Momentum" in html
    assert 'class="live-status-bar"' in html
    assert 'id="toggle-realtime-btn"' in html
    assert 'id="bot-trading-data-unavailable"' in html
    assert 'id="bot-chart-empty"' in html
    assert 'id="trade-chart"' not in html
    assert 'id="trade-chart-data"' not in html
    assert "Chưa có dữ liệu vị thế." in html
    assert "Chưa có dữ liệu lịch sử lệnh." in html
    for sample in FABRICATED_SAMPLE_VALUES:
        assert sample not in html
    # Các khối điều khiển vẫn hoạt động độc lập với nguồn báo cáo.
    assert f'action="/admin/bots/{bot_id}/pause"' in html
    assert f'action="/admin/bots/{bot_id}/resume"' in html
    assert f'action="/admin/bots/{bot_id}/disable"' in html


def test_bot_detail_html_renders_ledger_metrics_positions_trades_and_chart(
    test_app: tuple[FastAPI, uuid4],
) -> None:
    app, bot_id = test_app
    reporting = StubReportingService()
    app.state.bot_reporting_service = reporting
    response = TestClient(app).get(f"/admin/bots/{bot_id}")
    assert response.status_code == 200
    html = response.text

    assert 'id="bot-trading-data-unavailable"' not in html
    # KPI lấy từ totals của báo cáo, định dạng số tiếng Việt.
    assert 'id="kpi-open-positions">1<' in html
    assert 'id="kpi-win-rate">75%<' in html
    assert 'id="kpi-closed-trades">4<' in html
    assert 'id="kpi-profit-factor">3,2<' in html
    # Tiền làm tròn 2 chữ số để hiển thị, số gốc giữ ở title; USDT hiển thị bằng biểu tượng ₮.
    assert 'title="11.875 USDT">+11,88<span class="asset-usdt"' in html
    assert 'data-pnl-tone="positive" title="47.5 USDT">+47,5<span class="asset-usdt"' in html
    assert 'title="12.25 USDT">12,25<span class="asset-usdt"' in html

    # Vị thế mở chỉ gồm trường ledger có; ghi rõ chưa mark-to-market.
    assert "SOLUSDT" in html
    assert OPEN_TRADE_ID in html
    assert "chưa có giá tham chiếu và PnL chưa chốt" in html

    # Lệnh đã đóng: khối lượng và giá bình quân tính từ fills, lý do từ ledger.
    assert "ETHUSDT" in html
    assert ">4<" in html
    assert ">3050<" in html
    assert ">3037,5<" in html
    assert "Chiến thuật H03_BREAKOUT — Phá vỡ vùng tích lũy." in html
    assert 'title="1.5 USDT">1,5<span class="asset-usdt"' in html
    assert 'title="0.25 USDT">0,25<span class="asset-usdt"' in html
    assert 'title="0.75 USDT">0,75<span class="asset-usdt"' in html
    # Mã cặp lấy từ ledger (vị thế mở + lệnh đóng), hiển thị bằng chip coin.
    assert 'data-coin="SOLUSDT"' in html
    assert 'data-coin="ETHUSDT"' in html
    assert '<script src="/static/coins.js" defer></script>' in html
    assert '<link rel="stylesheet" href="/static/pages/bots.css">' in html

    # Biểu đồ dùng nến của giao dịch đã đóng gần nhất và nạp thư viện từ /static.
    assert reporting.chart_calls == [(CLOSED_TRADE_ID, "1h")]
    assert 'id="trade-chart"' in html
    assert 'id="trade-chart-data" type="application/json"' in html
    assert "/static/vendor/lightweight-charts-5.2.1.standalone.production.js" in html
    assert '<script src="/static/trade-chart.js" defer></script>' in html
    for sample in FABRICATED_SAMPLE_VALUES:
        assert sample not in html


def test_bot_detail_hides_chart_when_candle_store_has_no_bars(
    test_app: tuple[FastAPI, uuid4],
) -> None:
    app, bot_id = test_app
    app.state.bot_reporting_service = StubReportingService(bars=[])
    html = TestClient(app).get(f"/admin/bots/{bot_id}").text

    assert 'id="bot-chart-empty"' in html
    assert 'id="trade-chart"' not in html
    assert "ETHUSDT" in html


def test_bot_detail_realtime_api_returns_ledger_backed_trading_data(
    test_app: tuple[FastAPI, uuid4],
) -> None:
    app, bot_id = test_app
    app.state.bot_reporting_service = StubReportingService()
    response = TestClient(app).get(f"/admin/api/bots/{bot_id}/realtime")
    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "updated"
    assert "vn" in data["server_time"]
    assert "vn_time" in data["server_time"]
    assert data["bot"]["id"] == str(bot_id)
    assert data["bot"]["name"] == "Bot Alpha Momentum"
    assert data["bot"]["status"] == "RUNNING"
    assert data["bot"]["symbol"] == "BTCUSDT"

    assert data["data_available"] is True
    assert data["metrics"] == {
        "quote_asset": "USDT",
        "other_quote_assets": [],
        "open_positions_count": 1,
        "closed_trade_count": 4,
        "win_rate_percent": "75",
        "profit_factor": "3.2",
        "expectancy_per_trade": "11.875",
        "net_pnl": "47.5",
        "max_drawdown": "12.25",
    }
    assert data["positions"] == [
        {
            "trade_id": OPEN_TRADE_ID,
            "symbol": "SOLUSDT",
            "side": "LONG",
            "quote_asset": "USDT",
            "opened_at": "2026-11-12T05:00:00+00:00",
        }
    ]
    trade = data["recent_trades"][0]
    assert trade["trade_id"] == CLOSED_TRADE_ID
    assert trade["quantity"] == "4"
    assert trade["entry_price"] == "3050"
    assert trade["exit_price"] == "3037.5"
    assert trade["strategy_id"] == "H03_BREAKOUT"
    assert trade["exit_reason"] == "TAKE_PROFIT"
    assert data["chart_data"]["symbol"] == "ETHUSDT"
    assert data["chart_data"]["bars"]


def test_bot_detail_realtime_api_without_reporting_source_returns_empty_state(
    test_app: tuple[FastAPI, uuid4],
) -> None:
    app, bot_id = test_app
    data = TestClient(app).get(f"/admin/api/bots/{bot_id}/realtime").json()

    assert data["data_available"] is False
    assert data["metrics"] is None
    assert data["positions"] == []
    assert data["recent_trades"] == []
    assert data["chart_data"] is None


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


def test_bot_view_does_not_invent_a_symbol_when_the_bot_has_none() -> None:
    from trading_bot.admin.web_router import _bot_view

    bot = {"id": "b1", "name": "Bot", "status": "ENABLED", "account": {}}
    assert _bot_view(bot).symbol is None
    assert _bot_view({**bot, "symbol": "ETHUSDT"}).symbol == "ETHUSDT"

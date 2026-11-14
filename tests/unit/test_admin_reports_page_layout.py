"""Unit test bố cục trang báo cáo theo UI kit v1: tab, KPI, đối soát, phân quyền gắn nhãn."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from starlette.testclient import TestClient

from trading_bot.auth.service import AuthIdentity, Principal

BOT_ID = str(uuid4())
TRADE_ID = str(uuid4())
NOW = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
START = (NOW - timedelta(days=7)).isoformat()
END = NOW.isoformat()

TRADE: dict[str, Any] = {
    "trade_id": TRADE_ID,
    "bot_id": BOT_ID,
    "symbol": "ETHUSDT",
    "side": "SHORT",
    "quote_asset": "USDT",
    "opened_at": (NOW - timedelta(hours=3)).isoformat(),
    "closed_at": (NOW - timedelta(hours=1)).isoformat(),
    "gross_pnl": "-58.3333",
    "fee_total": "1.2",
    "funding_total": "0",
    "slippage_total": "0.4567",
    "total_cost": "1.6567",
    "net_pnl": "-59.99",
    "entry_reason": {"strategy_id": "H02", "detail_vi": "Phá vỡ kênh xuống."},
    "exit_reason": "STOP_LOSS",
    "fills": [
        {
            "role": "ENTRY",
            "quantity": "0.123456",
            "price": "0.000012345",
            "occurred_at": (NOW - timedelta(hours=3)).isoformat(),
        }
    ],
}


def _identity(*roles: str) -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            workspace_id=uuid4(),
            user_id=uuid4(),
            membership_id=uuid4(),
            roles=frozenset(roles),
            mfa_verified_at=NOW,
        ),
        email="reviewer@crypidian.io",
    )


class _Report:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def as_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._payload))


def _bot_payload(totals: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_version": "bot-report.v2",
        "scope": "bot",
        "mode": "PAPER",
        "bot_id": BOT_ID,
        "window": {"start": START, "end": END, "boundary": "[start,end) theo trade.closed_at"},
        "totals": {"USDT": totals},
        "trades": [dict(TRADE)],
        "open_trades": [
            {
                "trade_id": "aa000000-1111-2222-3333-444455556666",
                "symbol": "SOLUSDT",
                "side": "LONG",
                "quote_asset": "USDT",
                "opened_at": NOW.isoformat(),
            }
        ],
        "reconciliation": {"closed_trade_rows": 1, "trade_cost_rows": 3},
        "report_hash": "9f86d081884c7d659a2feaa0c55ad015",
    }


def _client(identity: AuthIdentity, service: AsyncMock) -> TestClient:
    from trading_bot.admin.web_router import router
    from trading_bot.auth.router import current_identity

    admin = AsyncMock()
    admin.list_bots.return_value = [
        {"id": BOT_ID, "name": "Bot ETH ngắn hạn", "status": "ENABLED", "account": {}}
    ]
    app = FastAPI()
    app.state.templates = Jinja2Templates(directory="src/trading_bot/templates")
    app.state.bot_reporting_service = service
    app.state.bot_admin_service = admin
    app.include_router(router)
    app.dependency_overrides[current_identity] = lambda: identity
    return TestClient(app)


def _bot_service(totals: dict[str, Any]) -> AsyncMock:
    service = AsyncMock()
    service.get_bot_report.return_value = _Report(_bot_payload(totals))
    service.get_trade_plan_summaries.return_value = {}
    service.get_trade_chart.return_value = {
        "symbol": "ETHUSDT",
        "side": "SHORT",
        "timeframe": "15m",
        "trade": dict(TRADE),
        "bars": [],
        "notice": None,
    }
    return service


FULL_TOTALS = {
    "closed_trade_count": 1,
    "winning_trade_count": 0,
    "losing_trade_count": 1,
    "breakeven_trade_count": 0,
    "gross_pnl": "-58.3333",
    "total_cost": "1.6567",
    "net_pnl": "-59.99",
    "max_drawdown": "59.99",
    "win_rate_percent": "0",
    "profit_factor": None,
    "expectancy_per_trade": "-59.99",
    "average_holding_seconds": 7200,
    "max_consecutive_wins": 0,
    "max_consecutive_losses": 1,
}


def _get(client: TestClient, **extra: str) -> str:
    params = {"scope": "bot", "bot_id": BOT_ID, "start": START, "end": END} | extra
    response = client.get("/admin/reports", params=params)
    assert response.status_code == 200, response.text
    return response.text


def test_bot_report_has_tabs_kpis_and_reconciliation_without_duplicates() -> None:
    html = _get(_client(_identity("owner"), _bot_service(FULL_TOTALS)))

    # CSS/JS riêng của trang và icon coin được nạp; không còn khối JSON trùng lặp.
    assert '<link rel="stylesheet" href="/static/pages/reports.css">' in html
    assert '<script src="/static/coins.js" defer></script>' in html
    assert html.count("<summary>Dữ liệu kỹ thuật JSON</summary>") == 1
    # Tab chi tiết theo mẫu WAI-ARIA, panel không đặt `hidden` sẵn (tabs.js tự xử lý).
    for tab in ("tab-report-closed", "tab-report-open", "tab-report-raw"):
        assert f'id="{tab}"' in html
    assert re.search(r'role="tabpanel"[^>]*hidden', html) is None
    # Lệnh đang mở lấy từ `open_trades` của báo cáo, không tự tính lãi/lỗ tạm.
    assert 'data-coin="SOLUSDT"' in html
    assert "Chưa có giá đánh dấu" in html
    # KPI: tiền 2 chữ số + ₮, bản đầy đủ ở title; drawdown hiện dấu âm; PF không xác định.
    assert 'title="-59,99 USDT">-59,99<span class="asset-usdt"' in html
    assert 'title="59,99 USDT">-59,99' in html
    assert "Không xác định" in html
    assert "2 giờ 0 phút" in html
    # Mã băm báo cáo rút gọn + nút sao chép, giá trị đầy đủ vẫn nằm trong văn bản.
    assert 'data-copy="9f86d081884c7d659a2feaa0c55ad015"' in html
    assert "Hàng chi phí đã đối soát" in html
    # Bảng chỉ số đối soát vẫn giữ số đầy đủ (number_vi).
    assert "-58,3333 USDT" in html
    assert "Bot ETH ngắn hạn" in html


def test_legacy_totals_do_not_render_fabricated_zero_win_rate() -> None:
    legacy = {
        key: value
        for key, value in FULL_TOTALS.items()
        if key not in {"win_rate_percent", "profit_factor", "average_holding_seconds"}
    }
    html = _get(_client(_identity("owner"), _bot_service(legacy)))
    kpis = html.split('class="report-kpis"', 1)[1].split("report-summary", 1)[0]
    assert "0%" not in kpis
    assert kpis.count("Chưa có dữ liệu") >= 3


def test_post_mortem_keeps_full_price_precision_and_sends_real_trade_fields() -> None:
    html = _get(
        _client(_identity("analyst_reviewer"), _bot_service(FULL_TOTALS)),
        chart_trade_id=TRADE_ID,
        chart_timeframe="15m",
    )
    # Giá của mã giá nhỏ không được làm tròn 2 chữ số.
    assert "0,000012345 × 0,123456" in html
    assert 'id="btn-ingest-ai"' in html
    payload_match = re.search(
        r'<script id="ingest-trade-payload" type="application/json">(.*?)</script>', html
    )
    assert payload_match is not None
    payload = json.loads(payload_match.group(1))
    assert payload["net_pnl"] == "-59.99"
    assert payload["exit_reason"] == "STOP_LOSS"
    # Khung hiện tại được chọn sẵn trong bộ đổi khung; có liên kết đóng biểu đồ.
    assert '<option value="15m" selected>15m</option>' in html
    assert "Đóng biểu đồ" in html
    assert 'class="is-selected" aria-current="true"' in html


def test_ingest_button_hidden_for_roles_without_labeling_permission() -> None:
    html = _get(_client(_identity("operator"), _bot_service(FULL_TOTALS)), chart_trade_id=TRADE_ID)
    assert 'id="btn-ingest-ai"' not in html
    assert "Vai trò hiện tại không được gắn nhãn ca" in html


def test_workspace_report_lists_bots_with_drilldown_and_hides_bot_picker() -> None:
    service = AsyncMock()
    service.get_workspace_report.return_value = _Report(
        {
            "report_version": "bot-report.v2",
            "scope": "workspace",
            "mode": "PAPER",
            "window": {"start": START, "end": END},
            "totals": {"USDT": FULL_TOTALS},
            "bots": [
                {"bot_id": BOT_ID, "totals": {"USDT": FULL_TOTALS}},
                {"bot_id": "00000000-0000-0000-0000-000000000009", "totals": {}},
            ],
            "reconciliation": {
                "bot_count": 2,
                "closed_trade_rows": 1,
                "ledger": {"USDT": {"closed_trade_rows": 1, "net_pnl": "-59.99"}},
            },
            "report_hash": "abc",
        }
    )
    response = _client(_identity("owner"), service).get(
        "/admin/reports", params={"scope": "workspace", "start": START, "end": END}
    )
    assert response.status_code == 200
    html = response.text
    assert 'id="bot-select-wrap" hidden' in html
    assert 'id="tab-report-bots"' in html
    assert 'id="tab-report-closed"' not in html
    assert "<strong>Bot ETH ngắn hạn</strong>" in html
    assert f"scope=bot&bot_id={BOT_ID}" in html
    assert "Không có giao dịch đã đóng trong khoảng." in html
    assert "Số bot tổng hợp" in html


def test_empty_reports_page_shows_guidance_without_null_json() -> None:
    html = _client(_identity("owner"), AsyncMock()).get("/admin/reports").text
    assert "Chọn phạm vi, bot và khoảng thời gian" in html
    assert "<pre>null</pre>" not in html
    assert "Dữ liệu kỹ thuật JSON" not in html

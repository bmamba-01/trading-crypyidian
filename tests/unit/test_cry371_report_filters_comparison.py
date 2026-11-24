"""Unit test cho filter, phân rã chiều, so sánh khoảng và report hash WP-07D (CRY-371)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_bot.admin.bot_reports_router import router as bot_reports_router
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.domain.portfolio.report_analytics import (
    ReportFilterCriteria,
    breakdown_trades,
    compare_report_periods,
    filter_report_trades,
    filtered_report_hash,
    reconcile_filtered_totals,
)


def _sample_trades() -> list[dict[str, Any]]:
    return [
        {
            "trade_id": str(uuid4()),
            "bot_id": str(uuid4()),
            "symbol": "BTCUSDT",
            "side": "LONG",
            "quote_asset": "USDT",
            "strategy_release_id": "h01-v1",
            "timeframe": "1h",
            "closed_at": "2026-11-23T02:00:00Z",  # Thứ 2, phiên ASIA
            "gross_pnl": "120.00",
            "net_pnl": "110.00",
            "fees": "6.00",
            "funding": "2.00",
            "slippage": "2.00",
            "fills": [
                {"role": "ENTRY", "quantity": "0.1", "price": "50000"},
                {"role": "EXIT", "quantity": "0.1", "price": "51200"},
            ],
        },
        {
            "trade_id": str(uuid4()),
            "bot_id": str(uuid4()),
            "symbol": "ETHUSDT",
            "side": "SHORT",
            "quote_asset": "USDT",
            "strategy_release_id": "h03-v3-4h",
            "timeframe": "4h",
            "closed_at": "2026-11-24T14:00:00Z",  # Thứ 3, phiên OVERLAP_LON_NY
            "gross_pnl": "-40.00",
            "net_pnl": "-45.00",
            "fees": "3.00",
            "funding": "1.00",
            "slippage": "1.00",
            "fills": [
                {"role": "ENTRY", "quantity": "1.0", "price": "3000"},
                {"role": "EXIT", "quantity": "1.0", "price": "3040"},
            ],
        },
        {
            "trade_id": str(uuid4()),
            "bot_id": str(uuid4()),
            "symbol": "BTCUSDT",
            "side": "LONG",
            "quote_asset": "USDT",
            "strategy_release_id": "h01-v1",
            "timeframe": "1h",
            "closed_at": "2026-11-24T17:00:00Z",  # Thứ 3, phiên NEW_YORK, hòa vốn
            "gross_pnl": "2.00",
            "net_pnl": "0.00",
            "fees": "2.00",
            "funding": "0.00",
            "slippage": "0.00",
            "fills": [
                {"role": "ENTRY", "quantity": "0.1", "price": "51000"},
                {"role": "EXIT", "quantity": "0.1", "price": "51020"},
            ],
        },
    ]


def test_filter_reconcile_breakdown_compare_and_hash() -> None:
    trades = _sample_trades()

    # 1. Filter theo symbol & side
    c1 = ReportFilterCriteria(symbol="btcusdt", side="long", timeframe="1H", strategy="h01-v1")
    btc_long = filter_report_trades(trades, c1)
    assert len(btc_long) == 2

    # 2. Filter theo session & weekday (UTC)
    c_asia = ReportFilterCriteria(session_name="ASIA", weekday=0)
    assert len(filter_report_trades(trades, c_asia)) == 1

    # 3. Reconcile với ledger: hòa vốn không tính thắng/thua; profit_factor=None khi không có lỗ
    totals_btc = reconcile_filtered_totals(btc_long)
    assert totals_btc["trade_count"] == 2
    assert totals_btc["winning_trades"] == 1
    assert totals_btc["losing_trades"] == 0
    assert totals_btc["breakeven_trades"] == 1
    assert Decimal(totals_btc["net_pnl"]) == Decimal("110")
    assert totals_btc["win_rate"] == "0.5"
    assert totals_btc["profit_factor"] is None

    # 4. Phân rã 6 chiều
    bd = breakdown_trades(trades)
    assert [r["key"] for r in bd["by_symbol"]] == ["BTCUSDT", "ETHUSDT"]
    assert [r["key"] for r in bd["by_side"]] == ["LONG", "SHORT"]
    assert [r["key"] for r in bd["by_timeframe"]] == ["1h", "4h"]
    assert len(bd["by_session"]) == 3
    assert len(bd["by_weekday"]) == 2

    # 5. So sánh hai khoảng
    cmp_res = compare_report_periods(btc_long, [trades[1]])
    assert Decimal(cmp_res["delta"]["net_pnl_diff"]) == Decimal("155")
    assert cmp_res["delta"]["trade_count_diff"] == 1

    # 6. Hash tất định & đổi khi filter đổi
    h1 = filtered_report_hash(
        source_report_hash="a" * 64,
        criteria=c1,
        filtered_totals=totals_btc,
        trade_ids=[t["trade_id"] for t in btc_long],
    )
    h2 = filtered_report_hash(
        source_report_hash="a" * 64,
        criteria=ReportFilterCriteria(),
        filtered_totals=totals_btc,
        trade_ids=[t["trade_id"] for t in btc_long],
    )
    assert len(h1) == 64
    assert h1 != h2


class _FakeReport:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def as_dict(self) -> dict[str, Any]:
        return dict(self._payload)


class _StubReportingService:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def get_bot_report(self, _query: Any, _principal: Principal) -> _FakeReport:
        return _FakeReport(self._payload)

    async def get_workspace_report(self, _query: Any, _principal: Principal) -> _FakeReport:
        return _FakeReport(self._payload)


def test_reports_api_and_csv_export_filters_and_hash() -> None:
    bot_id = uuid4()
    ws_id = uuid4()
    trades = _sample_trades()
    base_payload = {
        "report_version": "v2",
        "scope": "bot",
        "bot_id": str(bot_id),
        "workspace_id": str(ws_id),
        "mode": "PAPER",
        "report_hash": "b" * 64,
        "trades": trades,
    }

    app = FastAPI()
    app.include_router(bot_reports_router)
    app.include_router(admin_web_router)
    app.state.bot_reporting_service = _StubReportingService(base_payload)
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=Principal(
            user_id=uuid4(),
            membership_id=uuid4(),
            workspace_id=ws_id,
            roles=frozenset({Role.OWNER}),
            mfa_verified_at=datetime.now(UTC),
        ),
        email="owner@crypidian.test",
    )

    client = TestClient(app)
    r_api = client.get(
        "/api/v1/reports",
        params={
            "scope": "bot",
            "bot_id": str(bot_id),
            "start": "2026-11-22T00:00:00Z",
            "end": "2026-11-25T00:00:00Z",
            "symbol": "BTCUSDT",
            "side": "LONG",
        },
    )
    assert r_api.status_code == 200
    body = r_api.json()["report"]
    assert len(body["trades"]) == 2
    assert body["filtered_totals"]["trade_count"] == 2
    assert len(body["filtered_report_hash"]) == 64
    assert body["breakdown"]["by_symbol"][0]["key"] == "BTCUSDT"

    r_csv = client.get(
        "/admin/reports/export.csv",
        params={
            "scope": "bot",
            "bot_id": str(bot_id),
            "start": "2026-11-22T00:00:00Z",
            "end": "2026-11-25T00:00:00Z",
            "symbol": "ETHUSDT",
        },
    )
    assert r_csv.status_code == 200
    assert len(r_csv.headers.get("x-report-hash", "")) == 64
    assert r_csv.headers.get("x-report-source-hash") == "b" * 64
    lines = [ln for ln in r_csv.text.strip().splitlines() if ln]
    assert len(lines) == 2  # Header + 1 dòng ETHUSDT
    assert "ETHUSDT" in lines[1]

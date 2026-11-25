"""Unit test cho WP-07A coverage: data-quality, khoảng thời gian, close_event (CRY-339)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from trading_bot.application.bot_reporting import (
    BotReportingService,
    BotReportQuery,
    ReportScope,
    build_trade_chart_data_quality,
    extract_close_event_provenance,
)
from trading_bot.auth.policy import Principal, Role


def test_build_trade_chart_data_quality_full_coverage() -> None:
    """Đủ nến trong khoảng [chart_start, chart_end] thì has_gaps=False và không cảnh báo."""
    start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=10)
    bars = [
        {
            "time": int((start + timedelta(hours=idx)).timestamp()),
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
        }
        for idx in range(10)
    ]
    dq = build_trade_chart_data_quality(bars=bars, chart_start=start, chart_end=end, timeframe="1h")
    assert dq["expected_bars"] == 10
    assert dq["actual_bars"] == 10
    assert dq["missing_bars"] == 0
    assert dq["coverage_ratio"] == 1.0
    assert dq["has_gaps"] is False
    assert dq["warning_vi"] is None


def test_build_trade_chart_data_quality_partial_and_empty() -> None:
    """Thiếu nến hoặc rỗng phải bật has_gaps=True và sinh cảnh báo tiếng Việt rõ ràng."""
    start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=10)
    partial_bars = [
        {
            "time": int((start + timedelta(hours=idx)).timestamp()),
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
        }
        for idx in range(4)
    ]
    partial_dq = build_trade_chart_data_quality(
        bars=partial_bars, chart_start=start, chart_end=end, timeframe="1h"
    )
    assert partial_dq["expected_bars"] == 10
    assert partial_dq["actual_bars"] == 4
    assert partial_dq["missing_bars"] == 6
    assert partial_dq["coverage_ratio"] == 0.4
    assert partial_dq["has_gaps"] is True
    assert partial_dq["warning_vi"] is not None
    assert "Thiếu dữ liệu nến 1h" in partial_dq["warning_vi"]

    empty_dq = build_trade_chart_data_quality(
        bars=[], chart_start=start, chart_end=end, timeframe="1h"
    )
    assert empty_dq["actual_bars"] == 0
    assert empty_dq["missing_bars"] == 10
    assert empty_dq["coverage_ratio"] == 0.0
    assert empty_dq["has_gaps"] is True
    assert empty_dq["warning_vi"] is not None
    assert "Không có dữ liệu nến 1h" in empty_dq["warning_vi"]


def test_extract_close_event_provenance_truthful_and_no_guessing() -> None:
    """Trích xuất đúng exit_fill_key/bar_key/provenance; thiếu giữ None, không đoán."""
    full_trade = {
        "closed_at": "2026-11-01T12:00:00+00:00",
        "exit_reason": "TAKE_PROFIT",
        "fills": [
            {"role": "ENTRY", "fill_id": "fill-entry-1", "position_event_type": "OPENED"},
            {
                "role": "EXIT",
                "fill_key": "fill-exit-99",
                "position_event_type": "CLOSED",
                "bar_key": "BTCUSDT:1h:1793534400",
            },
        ],
        "lineage": {"provenance": "binance-paper-ledger:v2"},
    }
    ce_full = extract_close_event_provenance(full_trade)
    assert ce_full["closed_at"] == "2026-11-01T12:00:00+00:00"
    assert ce_full["exit_reason"] == "TAKE_PROFIT"
    assert ce_full["exit_fill_key"] == "fill-exit-99"
    assert ce_full["bar_key"] == "BTCUSDT:1h:1793534400"
    assert ce_full["provenance"] == "binance-paper-ledger:v2"

    sparse_trade = {
        "closed_at": "2026-11-01T12:00:00+00:00",
        "exit_reason": "STOP_LOSS",
        "fills": [{"role": "ENTRY", "fill_id": "fill-entry-1"}],
    }
    ce_sparse = extract_close_event_provenance(sparse_trade)
    assert ce_sparse["exit_reason"] == "STOP_LOSS"
    assert ce_sparse["exit_fill_key"] is None
    assert ce_sparse["bar_key"] is None
    assert ce_sparse["provenance"] is None


@pytest.mark.asyncio
async def test_get_trade_chart_returns_window_data_quality_and_close_event() -> None:
    """BotReportingService.get_trade_chart phải trả thêm window, data_quality và close_event."""
    ws_id = uuid4()
    bot_id = uuid4()
    trade_id = uuid4()
    opened = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    closed = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    trade_payload = {
        "trade_id": str(trade_id),
        "symbol": "BTCUSDT",
        "side": "LONG",
        "opened_at": opened.isoformat(),
        "closed_at": closed.isoformat(),
        "exit_reason": "TAKE_PROFIT",
        "net_pnl": "25.00",
        "quote_asset": "USDT",
        "fills": [
            {
                "role": "ENTRY",
                "fill_id": "entry-1",
                "occurred_at": opened.isoformat(),
                "price": "68000",
                "position_event_type": "OPENED",
            },
            {
                "role": "EXIT",
                "fill_key": "exit-key-77",
                "occurred_at": closed.isoformat(),
                "price": "69500",
                "position_event_type": "CLOSED",
                "bar_key": "BTCUSDT:1h:closed",
            },
        ],
        "explanation": {"evidence_id": "ev-77"},
    }

    class _FakeRepo:
        async def load_closed_trade(self, **_kwargs: Any) -> dict[str, Any]:
            return trade_payload

        async def load_trade_bars(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [
                {
                    "time": int(opened.timestamp()),
                    "open": 68000,
                    "high": 68500,
                    "low": 67900,
                    "close": 68400,
                },
                {
                    "time": int(closed.timestamp()),
                    "open": 69000,
                    "high": 69600,
                    "low": 68900,
                    "close": 69500,
                },
            ]

    class _FakeGrants:
        async def require(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class _FakeUow:
        session = object()

        async def __aenter__(self) -> _FakeUow:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    service = BotReportingService(
        sessions=object(),  # type: ignore[arg-type]
        unit_of_work=lambda *_args, **_kwargs: _FakeUow(),
        repository=lambda _s: _FakeRepo(),
        grants=lambda _s: _FakeGrants(),
    )

    class _FakeReport:
        def as_dict(self) -> dict[str, Any]:
            return {"trades": [trade_payload]}

    async def _fake_get_bot_report(*_args: Any, **_kwargs: Any) -> _FakeReport:
        return _FakeReport()

    service.get_bot_report = _fake_get_bot_report  # type: ignore[assignment]
    query = BotReportQuery(
        workspace_id=ws_id,
        scope=ReportScope.BOT,
        bot_id=bot_id,
        start=opened - timedelta(days=1),
        end=closed + timedelta(days=1),
    )
    principal = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=ws_id,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=opened,
    )
    chart = await service.get_trade_chart(query, principal, trade_id=trade_id, timeframe="1h")
    assert "window" in chart
    assert chart["window"]["start"] < chart["window"]["end"]
    assert chart["data_quality"]["has_gaps"] is True
    assert chart["data_quality"]["actual_bars"] == 2
    assert chart["close_event"]["exit_fill_key"] == "exit-key-77"
    assert chart["close_event"]["bar_key"] == "BTCUSDT:1h:closed"
    assert chart["close_event"]["provenance"] == "ev-77"

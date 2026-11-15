"""CK-1/CK-2: JSON tổng quan cho dải thông số và ma trận đa khung trên trang chart."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from trading_bot.application.market_overview import MTF_ROLES, build_market_overview

NOW = datetime(2026, 11, 15, 15, 7, 30, tzinfo=UTC)
QUOTE: dict[str, Any] = {
    "bid_price": Decimal("96000.10"),
    "ask_price": Decimal("96000.30"),
    "bid_quantity": Decimal("1"),
    "ask_quantity": Decimal("2"),
    "event_at": NOW - timedelta(seconds=3),
}


def _bars(
    minutes: int, count: int, *, end: datetime, start_price: float = 100.0
) -> list[dict[str, Any]]:
    """Nến zigzag tăng dần kết thúc trước ``end``; đủ swing để xác định xu hướng."""
    interval = timedelta(minutes=minutes)
    first_open = end - interval * count
    pattern = [0.0, 2.5, 5.0, 7.5, 10.0, 8.5, 7.0, 5.5, 4.0]
    rows: list[dict[str, Any]] = []
    for index in range(count):
        cycle, step = divmod(index, len(pattern))
        close = start_price + cycle * 6 + pattern[step]
        open_ = close - 0.2
        rows.append(
            {
                "time": int((first_open + interval * index).timestamp()),
                "open": open_,
                "high": close + 0.5,
                "low": open_ - 0.5,
                "close": close,
                "volume": 10.0,
                "taker_buy_volume": None,
                "source": "BINANCE_USDM_REST_BACKFILL",
            }
        )
    return rows


def _overview(**overrides: Any) -> dict[str, Any]:
    end = datetime(2026, 11, 15, 15, 5, tzinfo=UTC)
    params: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "now": NOW,
        "bars_by_timeframe": {
            "5m": _bars(5, 300, end=end),
            "15m": _bars(15, 200, end=datetime(2026, 11, 15, 15, 0, tzinfo=UTC)),
            "1h": _bars(60, 72, end=datetime(2026, 11, 15, 15, 0, tzinfo=UTC)),
        },
        "quote": dict(QUOTE),
        "mark": {
            "mark_price": Decimal("96001.00"),
            "index_price": Decimal("95999.50"),
            "funding_rate": Decimal("0.0001"),
            "next_funding_time": datetime(2026, 11, 15, 16, 0, tzinfo=UTC),
            "event_at": NOW - timedelta(seconds=20),
        },
    }
    params.update(overrides)
    return build_market_overview(**params)


def test_header_reports_quote_candle_clock_mark_and_funding_countdown() -> None:
    data = _overview()
    quote = data["quote"]
    assert quote["bid"] == 96000.10
    assert quote["ask"] == 96000.30
    assert round(quote["spread"], 2) == 0.20
    assert quote["age_seconds"] == 3
    assert quote["stale"] is False

    candle = data["candle"]
    assert candle["timeframe"] == "5m"
    assert candle["seconds_left"] == 150
    assert candle["price_source"] == "quote_mid"
    assert candle["price"] == 96000.20

    mark = data["mark"]
    assert mark["funding_seconds_left"] == 52 * 60 + 30
    assert mark["funding_rate_pct"] == 0.01

    assert [item["key"] for item in data["sessions"]] == ["london", "new_york"]
    assert data["formula_versions"] == {"overview": "ck1-overview-v1", "mtf": "ck2-mtf-v1"}


def test_stale_or_missing_live_data_falls_back_with_explicit_source_and_notice() -> None:
    stale = _overview(quote={**QUOTE, "event_at": NOW - timedelta(minutes=5)})
    assert stale["quote"]["stale"] is True
    assert stale["candle"]["price_source"] == "mark"

    bare = _overview(quote=None, mark=None)
    assert bare["quote"] is None
    assert bare["mark"] is None
    assert bare["candle"]["price_source"] == "last_close"
    assert any("bid/ask" in notice for notice in bare["notices"])


def test_mtf_matrix_lists_roles_in_order_and_marks_missing_timeframes() -> None:
    data = _overview()
    cells = data["mtf"]["cells"]
    assert [cell["timeframe"] for cell in cells] == [timeframe for timeframe, _ in MTF_ROLES]
    four_hour = cells[0]
    assert four_hour["timeframe"] == "4h"
    assert four_hour["trend"] == "unknown"
    assert four_hour["statement"] == "Chưa có nến 4H đã đóng trong kho dữ liệu."
    hourly = next(cell for cell in cells if cell["timeframe"] == "1h")
    assert hourly["trend"] == "up"
    assert hourly["statement"].startswith("1H giữ TĂNG khi chưa đóng cửa dưới ")
    # 4H chưa có dữ liệu → khung chính là khung có dữ liệu đầu tiên theo thứ tự vai trò.
    assert data["mtf"]["consensus"]["primary"] == "1h"


def test_day_range_uses_live_price_and_hourly_bars() -> None:
    data = _overview()
    day = data["day"]
    assert day["high"] >= 96000.2  # giá trực tiếp vượt mọi nến mẫu
    assert day["previous_high"] is not None
    assert day["previous_complete"] is True


def test_overview_lists_open_killzones_in_new_york_time() -> None:
    # 15:07:30 UTC ngày 15/11 = 10:07:30 EST → NY AM đã đóng (07–10), Silver Bullet sáng và macro
    # 09:50–10:10 đang mở.
    keys = [item["key"] for item in _overview()["killzones"]]
    assert keys == ["macro_0950", "silver_bullet_am"]

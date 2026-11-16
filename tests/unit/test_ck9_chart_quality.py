"""CK-9: kiểm chất lượng nến đang xem trên chart và trạng thái dữ liệu."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from trading_bot.domain.market.chart_quality import (
    CHART_QUALITY_VERSION,
    data_status,
    inspect_chart_bars,
)

START = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
STEP = 300


def _bar(index: int, **overrides: Any) -> dict[str, Any]:
    bar = {
        "time": int(START.timestamp()) + index * STEP,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "taker_buy_volume": 6.0,
    }
    return bar | overrides


def test_clean_series_passes() -> None:
    bars = [_bar(i) for i in range(10)]
    now = START + timedelta(minutes=50, seconds=10)
    report = inspect_chart_bars(bars, interval_seconds=STEP, now=now)
    assert CHART_QUALITY_VERSION == "ck9-quality-v1"
    assert report["ok"] is True
    assert report["checked"] == 10
    assert report["missing_bars"] == 0
    assert report["summary"] == "Đạt: 10 nến liên tục, OHLC hợp lệ."


def test_detects_hole_invalid_ohlc_taker_and_staleness() -> None:
    bars = [_bar(i) for i in range(4)]
    bars += [_bar(7), _bar(8, high=99.5), _bar(9, taker_buy_volume=12.0), _bar(10, volume=-1.0)]
    now = START + timedelta(hours=2)  # nến cuối đóng lúc 00:55 → trễ ~65 phút
    report = inspect_chart_bars(bars, interval_seconds=STEP, now=now, revised_bars=2)
    assert report["ok"] is False
    assert report["missing_bars"] == 3
    assert report["missing"] == [
        {
            "from": (START + timedelta(minutes=20)).isoformat(),
            "to": (START + timedelta(minutes=30)).isoformat(),  # mốc mở của nến thiếu cuối
            "bars": 3,
        }
    ]
    # high 99.5 < max(open, close) = 100.5 → OHLC sai.
    assert report["invalid_ohlc"] == [(START + timedelta(minutes=40)).isoformat()]
    assert report["taker_exceeds_volume"] == [(START + timedelta(minutes=45)).isoformat()]
    assert report["negative_volume"] == [(START + timedelta(minutes=50)).isoformat()]
    assert report["stale"] is True
    assert report["revised_bars"] == 2
    assert "thiếu 3 nến" in report["summary"]


def test_duplicates_and_unclosed_future_bars() -> None:
    bars = [_bar(0), _bar(1), _bar(1), _bar(2)]
    now = START + timedelta(minutes=12)  # nến 2 (00:10–00:15) chưa đóng
    report = inspect_chart_bars(bars, interval_seconds=STEP, now=now)
    assert report["duplicates"] == [(START + timedelta(minutes=5)).isoformat()]
    assert report["unclosed"] == 1
    assert report["ok"] is False


def test_data_status_reports_delay_and_open_gaps() -> None:
    bars = [_bar(i) for i in range(3)]
    now = START + timedelta(minutes=16)
    status = data_status(
        bars,
        interval_seconds=STEP,
        now=now,
        source_label="Binance USDⓈ-M · REST công khai",
        open_gaps=[{"start": START.isoformat(), "end": (START + timedelta(hours=1)).isoformat()}],
    )
    assert status["bar_count"] == 3
    assert status["last_close_at"] == (START + timedelta(minutes=15)).isoformat()
    assert status["delay_seconds"] == 60
    assert status["delayed"] is False
    assert status["open_gaps"] == 1
    empty = data_status([], interval_seconds=STEP, now=now, source_label="—", open_gaps=[])
    assert empty["bar_count"] == 0
    assert empty["delay_seconds"] is None

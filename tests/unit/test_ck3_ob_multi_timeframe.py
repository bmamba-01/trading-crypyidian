"""CK-3/ob-da-khung: golden OB đa khung, chỉ dùng nến lớn đã đóng và giới hạn mobile."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading_bot.application.market_chart import (
    ICT_MOBILE_ZONE_LIMIT,
    build_market_chart_payload,
    chart_fingerprint_with_higher,
    higher_ob_timeframe,
)

START = datetime(2026, 11, 20, tzinfo=UTC)


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def _flat_current_bars(count: int = 60) -> list[dict[str, float | int]]:
    """60 nến 15m từ 00:00 đến 14:45; nến cuối đóng lúc 15:00 UTC."""
    return [
        {
            "time": _epoch(START + timedelta(minutes=15 * index)),
            "open": 100.0 + 0.01 * index,
            "high": 100.05 + 0.01 * index,
            "low": 99.95 + 0.01 * index,
            "close": 100.0 + 0.01 * index,
            "volume": 1,
        }
        for index in range(count)
    ]


def _impulse_bars(count: int) -> tuple[list[float], list[float], list[float], list[float]]:
    """Dataset golden của test_ck3_ict: đỉnh nến 3, nến giảm cuối 7, phá đỉnh nến 10."""
    closes = [10, 12, 13.5, 14.5, 13.5, 12.5, 11.5, 10.8, 12.5, 14.0, 16.0, 16.5, 17.0, 16.8, 17.5]
    opens = [
        9.8,
        10.2,
        12.0,
        13.6,
        14.4,
        13.4,
        12.4,
        11.6,
        10.9,
        12.6,
        14.1,
        16.0,
        16.4,
        17.0,
        16.8,
    ]
    series = closes * 2 if count > len(closes) else closes[:count]
    o_series = opens * 2 if count > len(opens) else opens[:count]
    highs = [max(open_, close) + 0.3 for open_, close in zip(o_series, series, strict=True)]
    lows = [min(open_, close) - 0.3 for open_, close in zip(o_series, series, strict=True)]
    highs[3] = 15.0
    return o_series, highs, lows, [float(value) for value in series]


def _bars(timeframe: str, count: int) -> list[dict[str, float | int]]:
    opens, highs, lows, closes = _impulse_bars(count)
    step = timedelta(hours=1) if timeframe == "1h" else timedelta(minutes=15)
    return [
        {
            "time": _epoch(START + step * index),
            "open": opens[index],
            "high": highs[index],
            "low": lows[index],
            "close": closes[index],
            "volume": 1,
        }
        for index in range(count)
    ]


def test_ob_khung_lon_golden_va_chi_doc_nen_da_dong() -> None:
    higher = _bars("1h", 15)
    payload = build_market_chart_payload(
        symbol="BTCUSDT", timeframe="15m", bars=_flat_current_bars(), higher_bars=higher
    )
    ict = payload["ict"]
    assert ict["timeframes"] == {"current": "15m", "higher": "1h"}
    assert ict["counts"]["order_block_higher"] == 1
    ob = next(
        zone
        for zone in ict["zones"]
        if zone["kind"] == "order_block" and zone["source"] == "higher"
    )
    # Giá trị tính tay từ dataset: nến giảm cuối index 7 có low 10.5 / high 11.9,
    # xác nhận tại nến phá cấu trúc index 10 (07:00 / 10:00 UTC ngày 20/11).
    assert (ob["bottom"], ob["top"]) == (10.5, 11.9)
    assert ob["direction"] == "bullish"
    assert ob["timeframe"] == "1h"
    assert ob["start_time"] == _epoch(START + timedelta(hours=7))
    assert ob["confirmed_time"] == _epoch(START + timedelta(hours=10))
    assert ob["state"] == "active"

    # Nến 16:00 chưa đóng tại thời điểm khung 15m đóng nến cuối (15:00) → loại.
    unclosed = dict(higher[-1])
    unclosed["time"] = _epoch(START + timedelta(hours=15))
    unclosed["high"] = 999.0
    unclosed["low"] = -999.0
    again = build_market_chart_payload(
        symbol="BTCUSDT",
        timeframe="15m",
        bars=_flat_current_bars(),
        higher_bars=[*higher, unclosed],
    )
    higher_again = [zone for zone in again["ict"]["zones"] if zone["source"] == "higher"]
    assert higher_again == [ob]


def test_gioi_han_mobile_va_fingerprint_theo_khung_lon() -> None:
    current = _bars("15m", 30)
    higher = _bars("1h", 15)
    first = build_market_chart_payload(
        symbol="BTCUSDT", timeframe="15m", bars=current, higher_bars=higher
    )
    ict = first["ict"]
    assert ICT_MOBILE_ZONE_LIMIT == 4
    assert len(ict["panel_zones"]) <= ICT_MOBILE_ZONE_LIMIT
    assert ict["panel_zones"] == ict["mobile_zones"]
    assert ict["counts"]["active"] >= len(ict["panel_zones"])
    assert all(zone["state"] in {"active", "partial", "inverted"} for zone in ict["panel_zones"])

    changed = dict(higher[-1])
    changed["close"] = changed["close"] + 5.0
    changed["high"] = changed["high"] + 5.0
    second = build_market_chart_payload(
        symbol="BTCUSDT", timeframe="15m", bars=current, higher_bars=[*higher[:-1], changed]
    )
    assert first["fingerprint"] != second["fingerprint"]
    assert (
        chart_fingerprint_with_higher("BTCUSDT", "15m", current, "1h", higher)
        == (first["fingerprint"])
    )

    assert higher_ob_timeframe("1d") is None
    largest = build_market_chart_payload(
        symbol="BTCUSDT", timeframe="1d", bars=current, higher_bars=higher
    )
    assert largest["ict"]["timeframes"] == {"current": "1d", "higher": None}
    assert all(zone["source"] == "current" for zone in largest["ict"]["zones"])

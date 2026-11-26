"""Golden 'đi ngang' và streaming=batch cho CK-2 ma trận đa khung (CRY-220).

Ngữ nghĩa đi ngang dùng theo phương án đã chốt ở CRY-241: không có hướng, không gán
"độ mạnh %", không đổi điểm đã xác nhận khi có thêm nến.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading_bot.application.market_overview import (
    MTF_ROLES,
    build_market_overview,
    resolve_mtf_roles,
)
from trading_bot.domain.market.overview import mtf_consensus, timeframe_trend
from trading_bot.domain.market.structure import market_structure


def _times(count: int, minutes: int = 60) -> list[datetime]:
    start = datetime(2026, 11, 10, tzinfo=UTC)
    return [start + timedelta(minutes=minutes * index) for index in range(count)]


def _flat(
    count: int, price: float = 100.0
) -> tuple[list[float], list[float], list[float], list[float]]:
    return (
        [price + 0.5] * count,
        [price - 0.5] * count,
        [price] * count,
        [price] * count,
    )


def test_golden_di_ngang_khong_co_huong_va_khong_do_manh() -> None:
    highs, lows, closes, opens = _flat(24)
    cell = timeframe_trend("1h", _times(24), highs, lows, closes, opens=opens)
    assert cell.trend == "range"
    assert cell.label_vi == "Đi ngang (chưa phá cấu trúc)"
    assert cell.invalidation_price is None
    assert cell.position_pct is None
    assert cell.zone is None
    assert cell.last_break is None
    assert cell.last_change_pct == pytest.approx(0.0)
    assert "độ mạnh" not in cell.label_vi

    consensus = mtf_consensus([cell], primary="1h")
    assert consensus.direction == "unknown"
    assert (consensus.aligned, consensus.known) == (0, 0)
    assert "Chưa đủ dữ liệu" in consensus.label_vi


def test_chip_phan_tram_lay_tu_gia_mo_khung_lon() -> None:
    highs, lows, closes, opens = _flat(24)
    closes[-1] = 110.0
    highs[-1] = 110.5
    cell = timeframe_trend("1h", _times(24), highs, lows, closes, opens=opens)
    # (110 - 100) / 100 * 100 = 10%, lấy từ giá mở nến khung lớn, không phải "độ mạnh".
    assert cell.last_change_pct == pytest.approx(10.0)
    assert "độ mạnh" not in cell.label_vi


def test_streaming_bang_batch_cho_swing_da_xac_nhan() -> None:
    legs = [
        (100, 110),
        (110, 104),
        (104, 116),
        (116, 109),
        (109, 122),
        (122, 115),
        (115, 120),
        (120, 112),
        (112, 126),
        (126, 118),
        (118, 130),
    ]
    closes: list[float] = []
    for start, end in legs:
        for index in range(4):
            closes.append(start + (end - start) * index / 4)
    closes.append(legs[-1][1])
    highs = [value + 0.5 for value in closes]
    lows = [value - 0.5 for value in closes]

    cut = 28
    short = market_structure(highs[:cut], lows[:cut], closes[:cut])
    full = market_structure(highs, lows, closes)
    # Swing đã xác nhận trước biên cắt không được đổi khi nạp thêm nến.
    settled_short = tuple(item for item in short.swings if item.confirmed_index <= cut - 4)
    settled_full = tuple(item for item in full.swings if item.confirmed_index <= cut - 4)
    assert settled_short
    assert settled_short == settled_full

    short_cell = timeframe_trend("1h", _times(cut), highs[:cut], lows[:cut], closes[:cut])
    full_cell = timeframe_trend("1h", _times(len(closes)), highs, lows, closes)
    assert short_cell.trend in {"up", "down", "range"}
    assert full_cell.trend in {"up", "down", "range"}


def test_vai_tro_khung_duoc_khai_bao_theo_setup() -> None:
    assert resolve_mtf_roles("h03-v1") == (
        ("1h", "Xu hướng chính"),
        ("15m", "Động lượng"),
        ("5m", "Thời điểm"),
    )
    assert resolve_mtf_roles("H06:range") == (
        ("15m", "Xu hướng chính"),
        ("5m", "Thời điểm"),
    )
    assert resolve_mtf_roles("h01-trend") == MTF_ROLES
    assert resolve_mtf_roles(None) == MTF_ROLES


def test_payload_dung_vai_tro_setup_va_chip_pham_tram_tu_gia_mo() -> None:
    now = datetime(2026, 11, 25, 12, tzinfo=UTC)
    bars = {
        frame: [
            {
                "time": int((now - timedelta(minutes=60 * (6 - index))).timestamp()),
                "open": 100.0,
                "high": 100.5,
                "low": 99.5,
                "close": 100.0 if index < 5 else 110.0,
            }
            for index in range(6)
        ]
        for frame in ("1h", "15m", "5m", "4h")
    }
    roles = resolve_mtf_roles("h03-v1")
    payload = build_market_overview(
        symbol="BTCUSDT",
        timeframe="1h",
        now=now,
        bars_by_timeframe=bars,
        quote=None,
        mark=None,
        mtf_roles=roles,
    )
    assert [cell["timeframe"] for cell in payload["mtf"]["cells"]] == ["1h", "15m", "5m"]
    assert payload["mtf"]["cells"][0]["role"] == "Xu hướng chính"
    assert payload["mtf"]["roles_source"] == "setup"
    assert payload["mtf"]["consensus"]["primary"] == "1h"
    last_cell = payload["mtf"]["cells"][0]
    assert last_cell["change_pct"] == pytest.approx(10.0)
    assert "độ mạnh" not in last_cell["label"]

    default_payload = build_market_overview(
        symbol="BTCUSDT",
        timeframe="1h",
        now=now,
        bars_by_timeframe=bars,
        quote=None,
        mark=None,
    )
    assert default_payload["mtf"]["roles_source"] == "default"
    assert [cell["timeframe"] for cell in default_payload["mtf"]["cells"]] == [
        frame for frame, _ in MTF_ROLES
    ]

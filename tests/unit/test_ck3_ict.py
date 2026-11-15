"""CK-3: killzone theo giờ New York, FVG/IFVG/BPR, order block, đỉnh/đáy bằng nhau, ITH/ITL.

Dữ liệu là golden dataset viết tay: mỗi vùng phải xuất hiện đúng nến xác nhận, không sớm hơn.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_bot.domain.market.ict import (
    DEFAULT_KILLZONES,
    ICT_FORMULA_VERSION,
    KILLZONE_TABLE_VERSION,
    balanced_price_ranges,
    equal_levels,
    fair_value_gaps,
    intermediate_swings,
    killzone_windows,
    order_blocks,
)
from trading_bot.domain.market.structure import SwingPoint, market_structure


def test_versions_are_pinned() -> None:
    assert ICT_FORMULA_VERSION == "ck3-ict-v1"
    assert KILLZONE_TABLE_VERSION == "ck3-killzone-v1"


def test_killzones_follow_new_york_dst_and_cross_midnight() -> None:
    # 10/03/2026: New York đã sang giờ mùa hè (EDT, UTC−4) từ 08/03, London vẫn GMT.
    start = datetime(2026, 3, 10, 0, 0, tzinfo=UTC)
    end = datetime(2026, 3, 11, 0, 0, tzinfo=UTC)
    windows = {
        (item.key, item.open_at.hour, item.open_at.minute) for item in killzone_windows(start, end)
    }
    assert ("london", 6, 0) in windows  # 02:00 EDT = 06:00 UTC
    assert ("ny_am", 11, 0) in windows  # 07:00 EDT
    assert ("silver_bullet_am", 14, 0) in windows  # 10:00 EDT
    assert ("macro_0950", 13, 50) in windows
    # Asia 20:00–00:00 EDT của ngày 09/03 = 00:00–04:00 UTC ngày 10/03 (qua nửa đêm NY).
    asia = [item for item in killzone_windows(start, end) if item.key == "asia"]
    assert any(item.open_at == datetime(2026, 3, 10, 0, 0, tzinfo=UTC) for item in asia)
    assert all(item.close_at > item.open_at for item in asia)
    # 15/11 (EST, UTC−5): NY AM mở 12:00 UTC.
    winter = killzone_windows(
        datetime(2026, 11, 15, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC)
    )
    assert any(item.key == "ny_am" and item.open_at.hour == 12 for item in winter)
    assert {item.kind for item in DEFAULT_KILLZONES} == {"killzone", "silver_bullet", "macro"}


def test_killzones_reject_naive_time() -> None:
    with pytest.raises(ValueError, match="timezone"):
        killzone_windows(datetime(2026, 1, 1), datetime(2026, 1, 2, tzinfo=UTC))  # noqa: DTZ001


# Nến:   0      1      2      3      4      5      6
HIGHS = [10.0, 13.0, 14.0, 13.5, 12.0, 11.0, 12.2]
LOWS = [9.0, 10.0, 11.5, 12.0, 10.5, 9.5, 11.0]
OPENS = [9.5, 10.2, 11.8, 13.2, 11.8, 10.8, 11.2]
CLOSES = [9.8, 12.8, 13.8, 12.2, 10.6, 9.8, 12.0]


def test_bullish_fvg_confirmed_on_third_bar_then_filled_and_inverted() -> None:
    gaps = fair_value_gaps(OPENS, HIGHS, LOWS, CLOSES, atr_values=None)
    bull = [gap for gap in gaps if gap.direction == "bullish"]
    assert len(bull) == 1
    gap = bull[0]
    # Nến 0 high 10, nến 2 low 11.5 → khe [10, 11.5], biết khi nến 2 đóng.
    assert (gap.bottom, gap.top) == (10.0, 11.5)
    assert (gap.created_index, gap.confirmed_index) == (0, 2)
    # Nến 4 low 10.5 chạm một phần; nến 5 low 9.5 lấp đầy và đóng 9.8 < 10 → đảo vai (IFVG).
    assert gap.first_touch_index == 4
    assert gap.filled_index == 5
    assert gap.inverted_index == 5
    # Nến 6 đóng 12.0 > 11.5 → IFVG (giảm) mất hiệu lực.
    assert gap.invalidated_index == 6
    assert gap.state == "invalidated"
    assert gap.state_at(4) == "partial"
    assert gap.state_at(5) == "inverted"
    assert gap.state_at(1) is None  # chưa xác nhận thì chưa tồn tại


def test_fvg_ignores_gaps_smaller_than_atr_threshold() -> None:
    atr_values = [None, None, 20.0, 20.0, 20.0, 20.0, 20.0]  # khe 1,5 < 0,1 × ATR 20
    assert [
        gap
        for gap in fair_value_gaps(OPENS, HIGHS, LOWS, CLOSES, atr_values=atr_values)
        if gap.direction == "bullish"
    ] == []


def test_bearish_fvg_and_balanced_price_range() -> None:
    # Khe giảm ở nến 2–4 (high[4]=7 < low[2]=8), sau đó khe tăng ở nến 4–6 chồng lên.
    highs = [11.0, 10.0, 10.0, 8.0, 7.0, 9.0, 10.0]
    lows = [10.0, 8.5, 8.0, 6.0, 5.0, 6.5, 7.5]
    opens = [10.5, 9.8, 9.5, 7.8, 6.8, 6.6, 8.8]
    closes = [10.2, 8.8, 8.2, 6.2, 5.5, 8.8, 9.8]
    gaps = fair_value_gaps(opens, highs, lows, closes, atr_values=None)
    bear = next(gap for gap in gaps if gap.direction == "bearish" and gap.created_index == 2)
    assert (bear.bottom, bear.top) == (7.0, 8.0)
    assert bear.confirmed_index == 4
    bull = next(gap for gap in gaps if gap.direction == "bullish" and gap.created_index == 4)
    assert (bull.bottom, bull.top) == (7.0, 7.5)
    ranges = balanced_price_ranges(gaps, closes, max_distance_bars=6)
    assert len(ranges) == 1
    bpr = ranges[0]
    # Giao của [7, 8] và [7, 7.5]; hướng theo FVG xuất hiện sau (tăng).
    assert (bpr.bottom, bpr.top, bpr.direction) == (7.0, 7.5, "bullish")
    assert bpr.confirmed_index == 6


def _impulse_series() -> tuple[list[float], list[float], list[float], list[float]]:
    """Đỉnh swing ở nến 3 (15), nhịp hồi có nến giảm cuối ở nến 7, phá đỉnh ở nến 10."""
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
    highs = [max(o, c) + 0.3 for o, c in zip(opens, closes, strict=True)]
    lows = [min(o, c) - 0.3 for o, c in zip(opens, closes, strict=True)]
    highs[3] = 15.0
    return [float(v) for v in opens], highs, lows, [float(v) for v in closes]


def test_order_block_is_last_opposite_candle_before_break() -> None:
    opens, highs, lows, closes = _impulse_series()
    structure = market_structure(highs, lows, closes, left_bars=2, right_bars=2)
    blocks = order_blocks(opens, highs, lows, closes, structure.breaks)
    bullish = [block for block in blocks if block.direction == "bullish"]
    assert bullish, structure.breaks
    block = bullish[0]
    assert block.created_index == 7  # nến giảm cuối cùng trước nhịp tăng phá đỉnh
    assert (block.bottom, block.top) == (lows[7], highs[7])
    # Chỉ biết OB khi nến phá cấu trúc đóng.
    assert block.confirmed_index == next(
        b.break_index for b in structure.breaks if b.direction == "bullish"
    )
    assert block.state == "active"
    assert block.invalidated_index is None


def test_order_block_invalidated_by_close_through_zone() -> None:
    opens, highs, lows, closes = _impulse_series()
    opens += [17.0, 12.0]
    closes += [12.0, 9.0]
    highs += [17.2, 12.2]
    lows += [11.8, 8.8]
    structure = market_structure(highs, lows, closes, left_bars=2, right_bars=2)
    block = next(
        b
        for b in order_blocks(opens, highs, lows, closes, structure.breaks)
        if b.direction == "bullish"
    )
    assert block.mitigated_index == 15  # râu nến 15 chạm vùng
    assert block.invalidated_index == 16  # đóng 9.0 dưới đáy vùng
    assert block.state == "invalidated"


def _swing(kind: str, index: int, price: float, confirmed: int) -> SwingPoint:
    return SwingPoint(kind=kind, index=index, price=price, confirmed_index=confirmed, label=None)  # type: ignore[arg-type]


def test_equal_highs_within_atr_tolerance_and_swept_later() -> None:
    swings = [
        _swing("high", 2, 100.0, 4),
        _swing("low", 5, 95.0, 7),
        _swing("high", 8, 100.2, 10),
        _swing("high", 12, 103.0, 14),
    ]
    highs = [99.0] * 16
    highs[15] = 100.5  # nến 15 quét qua đỉnh bằng nhau
    lows = [94.0] * 16
    atr_values = [1.0] * 16
    levels = equal_levels(swings, highs, lows, atr_values, tolerance_atr=0.25)
    assert len(levels) == 1
    level = levels[0]
    assert level.side == "high"
    assert level.price == 100.2
    assert level.swing_indices == (2, 8)
    assert level.confirmed_index == 10
    assert level.swept_index == 15


def test_intermediate_swings_need_lower_neighbours() -> None:
    swings = [
        _swing("high", 2, 100.0, 4),
        _swing("high", 6, 105.0, 8),
        _swing("high", 10, 102.0, 12),
        _swing("low", 4, 95.0, 6),
        _swing("low", 8, 93.0, 10),
        _swing("low", 12, 96.0, 14),
    ]
    found = intermediate_swings(swings)
    assert [(item.kind, item.index, item.confirmed_index) for item in found] == [
        ("high", 6, 12),
        ("low", 8, 14),
    ]

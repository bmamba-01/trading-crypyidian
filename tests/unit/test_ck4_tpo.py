"""CK-4: hồ sơ giá theo thời gian (TPO), POC/vùng giá trị TPO và POC "trần" (naked POC)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading_bot.domain.market.tpo import (
    TPO_FORMULA_VERSION,
    naked_levels,
    tpo_profile,
)

DAY = datetime(2026, 11, 14, tzinfo=UTC)


def test_version_is_pinned() -> None:
    assert TPO_FORMULA_VERSION == "ck4-tpo-v1"


def test_tpo_counts_blocks_per_row_poc_and_value_area() -> None:
    # Bốn khối 30 phút, mỗi khối là một nến 30m: [10,12] [11,13] [11,12] [12,14].
    times = [DAY + timedelta(minutes=30 * i) for i in range(4)]
    highs = [12.0, 13.0, 12.0, 14.0]
    lows = [10.0, 11.0, 11.0, 12.0]
    profile = tpo_profile(times, highs, lows, timedelta(minutes=30), row_size=1.0)
    assert profile is not None
    assert [(row.price_low, row.count) for row in profile.rows] == [
        (10.0, 1),
        (11.0, 3),
        (12.0, 2),
        (13.0, 1),
    ]
    assert profile.block_count == 4
    assert profile.total_tpo == 7
    assert profile.poc_price == 11.5
    # VA 70% của 7 TPO: hàng POC (3) + hàng trên (2) = 5 ≥ 4,9.
    assert (profile.value_area_low, profile.value_area_high) == (11.0, 13.0)


def test_tpo_groups_small_bars_into_30_minute_blocks() -> None:
    # Sáu nến 5m = một khối 30 phút: khối chỉ tính một lần cho mỗi hàng nó chạm.
    times = [DAY + timedelta(minutes=5 * i) for i in range(12)]
    highs = [11.0] * 6 + [13.0] * 6
    lows = [10.0] * 6 + [12.0] * 6
    profile = tpo_profile(times, highs, lows, timedelta(minutes=5), row_size=1.0)
    assert profile is not None
    assert profile.block_count == 2
    assert [(row.price_low, row.count) for row in profile.rows] == [
        (10.0, 1),
        (11.0, 0),
        (12.0, 1),
    ]


def test_tpo_rejects_bars_longer_than_block_and_handles_empty() -> None:
    with pytest.raises(ValueError, match="30 phút"):
        tpo_profile([DAY], [2.0], [1.0], timedelta(hours=1))
    assert tpo_profile([], [], [], timedelta(minutes=5)) is None


def test_naked_level_disappears_on_first_touch_after_arming() -> None:
    highs = [10.0, 10.5, 11.0, 12.0, 11.8]
    lows = [9.0, 9.8, 10.2, 11.5, 11.1]
    # Mức 11.2 có hiệu lực từ nến 2: nến 2 (10.2–11.0) chưa chạm, nến 3 (11.5–12) chưa,
    # nến 4 (11.1–11.8) chạm.
    assert naked_levels([(11.2, 2), (13.0, 1), (9.5, 0)], highs, lows) == (4, None, 0)

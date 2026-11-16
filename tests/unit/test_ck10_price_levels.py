"""CK-10: bảng "Mức giá" gom từ payload chart, khoảng cách % tới giá hiện tại."""

from __future__ import annotations

from typing import Any

from trading_bot.application.market_levels import LEVELS_VERSION, price_levels

PAYLOAD: dict[str, Any] = {
    "levels": [
        {
            "key": "pdh",
            "label": "Đỉnh ngày trước (PDH)",
            "side": "high",
            "price": 105.0,
            "armed_time": 1000,
            "state": "armed",
        },
        {
            "key": "pdl",
            "label": "Đáy ngày trước (PDL)",
            "side": "low",
            "price": 95.0,
            "armed_time": 1000,
            "state": "rejected",
        },
    ],
    "profile_levels": [
        {"kind": "npoc_volume", "label": "nPOC 15/11", "price": 98.0, "start_time": 2000},
        {"kind": "vah", "label": "VAH ngày trước", "price": 102.0, "start_time": 2000},
    ],
    "ict": {
        "zones": [
            {
                "kind": "fvg",
                "label": "FVG tăng",
                "top": 99.5,
                "bottom": 99.0,
                "start_time": 3000,
                "state": "active",
            },
            {
                "kind": "order_block",
                "label": "OB giảm",
                "top": 108.0,
                "bottom": 107.0,
                "start_time": 3100,
                "state": "invalidated",
            },
        ],
        "equal_levels": [
            {
                "side": "high",
                "label": "Đỉnh bằng nhau (EQH)",
                "price": 104.0,
                "start_time": 4000,
                "swept": False,
            },
            {
                "side": "low",
                "label": "Đáy bằng nhau (EQL)",
                "price": 96.0,
                "start_time": 4100,
                "swept": True,
            },
        ],
    },
}


def test_levels_sorted_by_distance_with_live_items_only() -> None:
    levels = price_levels(PAYLOAD, current_price=100.0)
    assert LEVELS_VERSION == "ck10-levels-v1"
    labels = [level["label"] for level in levels]
    # Mức đã bị quét/mất hiệu lực không còn là mức tham chiếu.
    assert "Đáy ngày trước (PDL)" not in labels
    assert "OB giảm" not in labels
    assert "Đáy bằng nhau (EQL)" not in labels
    assert labels[0] == "FVG tăng"  # vùng 99–99,5 gần giá 100 nhất
    fvg = levels[0]
    assert fvg["price"] == 99.5  # cạnh gần giá hiện tại
    assert fvg["side"] == "below"
    assert round(fvg["distance_pct"], 2) == -0.5
    assert fvg["time"] == 3000
    pdh = next(level for level in levels if level["label"].endswith("(PDH)"))
    assert pdh["side"] == "above"
    assert round(pdh["distance_pct"], 2) == 5.0
    assert [level["group"] for level in levels if level["label"] == "nPOC 15/11"] == ["profile"]


def test_levels_without_price_keep_source_order_and_empty_payload() -> None:
    levels = price_levels(PAYLOAD, current_price=None)
    assert all(level["distance_pct"] is None for level in levels)
    assert price_levels({}, current_price=100.0) == []

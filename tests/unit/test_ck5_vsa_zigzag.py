"""CK-5: nhãn nến VSA theo phân vị và ZigZag dựng từ pivot đã xác nhận (streaming = batch)."""

from __future__ import annotations

import pytest

from trading_bot.domain.market.structure import SwingPoint
from trading_bot.domain.market.vsa import (
    VSA_FORMULA_VERSION,
    VSA_LABELS_VI,
    ZIGZAG_FORMULA_VERSION,
    vsa_labels,
    zigzag,
)

LOOKBACK = 20


def _baseline() -> tuple[list[float], list[float], list[float], list[float]]:
    """20 nến nền: đáy 100, độ rộng 1,00…1,95, đóng giữa nến, khối lượng 10…29."""
    highs = [101.0 + 0.05 * i for i in range(LOOKBACK)]
    lows = [100.0] * LOOKBACK
    closes = [(h + lo) / 2 for h, lo in zip(highs, lows, strict=True)]
    volumes = [10.0 + i for i in range(LOOKBACK)]
    return highs, lows, closes, volumes


def _label_for(high: float, low: float, close: float, volume: float) -> str | None:
    highs, lows, closes, volumes = _baseline()
    labels = vsa_labels(highs + [high], lows + [low], closes + [close], volumes + [volume])
    assert labels[:LOOKBACK] == (None,) * LOOKBACK  # chưa đủ 20 nến trước thì không gắn nhãn
    return labels[-1]


@pytest.mark.parametrize(
    ("bar", "expected"),
    [
        ((103.0, 100.4, 101.8, 50.0), "buying_climax"),  # đỉnh mới, rộng, volume cực lớn
        ((99.6, 97.0, 98.5, 50.0), "selling_climax"),  # đáy mới, rộng, volume cực lớn
        ((102.5, 101.0, 101.2, 30.0), "upthrust"),  # đỉnh mới nhưng đóng sát đáy nến
        ((101.5, 100.1, 100.9, 40.0), "stopping_volume"),  # nến giảm volume lớn, đóng nửa trên
        ((101.3, 100.8, 101.1, 15.0), "no_demand"),  # nến tăng hẹp, volume thấp hơn 2 nến trước
        ((100.8, 100.3, 100.5, 15.0), "no_supply"),  # nến giảm hẹp, volume thấp hơn 2 nến trước
        ((100.3, 99.7, 100.2, 15.0), "test"),  # xuyên đáy nến trước, hẹp, đóng cao, volume thấp
        ((101.4, 100.0, 100.7, 22.0), None),
    ],
)
def test_vsa_golden_labels(bar: tuple[float, float, float, float], expected: str | None) -> None:
    assert _label_for(*bar) == expected


def test_vsa_versions_and_vietnamese_labels() -> None:
    assert VSA_FORMULA_VERSION == "ck5-vsa-v1"
    assert ZIGZAG_FORMULA_VERSION == "ck5-zigzag-v1"
    assert VSA_LABELS_VI["no_demand"] == "Không có cầu"
    assert VSA_LABELS_VI["test"] == "Kiểm tra cung"


def _swing(kind: str, index: int, price: float, confirmed: int) -> SwingPoint:
    return SwingPoint(kind=kind, index=index, price=price, confirmed_index=confirmed, label=None)  # type: ignore[arg-type]


SWINGS = [
    _swing("high", 2, 110.0, 4),
    _swing("low", 5, 100.0, 7),
    _swing("high", 8, 103.0, 10),  # nhịp hồi 3 < ngưỡng 5 → bỏ
    _swing("low", 11, 98.0, 13),  # đáy thấp hơn thay đáy ứng viên 100
    _swing("high", 14, 112.0, 16),
]
ATR = [2.5] * 20


def test_zigzag_keeps_only_moves_above_threshold_and_marks_pending_leg() -> None:
    result = zigzag(SWINGS, ATR, min_move_atr=2.0)
    assert [(p.kind, p.index, p.price, p.final_index) for p in result.points] == [
        ("high", 2, 110.0, 7),
        ("low", 11, 98.0, 16),
    ]
    assert result.pending is not None
    assert (result.pending.kind, result.pending.index) == ("high", 14)


def test_zigzag_streaming_equals_batch_for_finalized_points() -> None:
    batch = zigzag(SWINGS, ATR, min_move_atr=2.0)
    for bar in range(20):
        known = [s for s in SWINGS if s.confirmed_index <= bar]
        streaming = zigzag(known, ATR, min_move_atr=2.0)
        assert streaming.points == tuple(p for p in batch.points if p.final_index <= bar)


def test_zigzag_percent_threshold() -> None:
    # 5% của 110 = 5,5 → nhịp 110 → 100 (10) đủ; 112 − 98 = 14 đủ.
    result = zigzag(SWINGS, ATR, min_move_atr=None, min_move_pct=5.0)
    assert [p.index for p in result.points] == [2, 11]
    with pytest.raises(ValueError, match="ngưỡng"):
        zigzag(SWINGS, ATR, min_move_atr=None, min_move_pct=None)

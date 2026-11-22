"""Bộ dữ liệu vàng tham chiếu độc lập (Golden Reference Test Vectors) cho FX-6.

Nguồn đối chiếu tài liệu thẩm quyền:
- Murphy, John J. (1999), Technical Analysis of the Financial Markets,
  New York Institute of Finance, tr. 134 (Bảng tính mẫu 10-day EMA).
- Wilder, J. Welles Jr. (1978), New Concepts in Technical Trading Systems,
  Trend Research, tr. 23, 35-38 (Bảng mẫu True Range và ATR).
"""

import math
from decimal import Decimal

import pytest

from trading_bot.domain.market.features import atr, ema, true_range, zscore, zscore_inclusive
from trading_bot.domain.strategy_card.anatomy import (
    bearish_wick_rejection_strength,
    body_ratio,
    bullish_wick_rejection_strength,
    close_location,
    lower_wick_ratio,
    normalized_candle_range,
    upper_wick_ratio,
)


def test_golden_ema_murphy_1999_p134() -> None:
    """Đối chiếu golden vector EMA 10 ngày từ Murphy (1999, trang 134).

    Dữ liệu 15 ngày:
    Ngày 1-10: 22.27, 22.19, 22.08, 22.17, 22.18, 22.13, 22.23, 22.43, 22.24, 22.29
    Ngày 11-15: 22.15, 22.39, 22.38, 22.61, 23.36

    SMA ban đầu (ngày 10) = 22.221
    Hệ số làm mượt alpha = 2 / (10 + 1) = 2/11
    """
    closes = [
        22.27,
        22.19,
        22.08,
        22.17,
        22.18,
        22.13,
        22.23,
        22.43,
        22.24,
        22.29,
        22.15,
        22.39,
        22.38,
        22.61,
        23.36,
    ]

    # Tính toán tham chiếu độc lập bằng số học thuần
    sma_10 = sum(closes[:10]) / 10.0
    assert sma_10 == pytest.approx(22.221, rel=1e-5)

    alpha = 2.0 / 11.0
    expected = [None] * 9 + [sma_10]
    cur = sma_10
    for price in closes[10:]:
        cur = price * alpha + cur * (1.0 - alpha)
        expected.append(cur)

    # Output của hàm ema dự án
    actual = ema(closes, 10)

    for i in range(len(closes)):
        if expected[i] is None:
            assert actual[i] is None
        else:
            assert actual[i] is not None
            assert actual[i] == pytest.approx(expected[i], abs=1e-6)

    # Xác nhận từng giá trị golden cụ thể
    # Ngày 11: 22.15 * (2/11) + 22.221 * (9/11) = 22.208091
    assert actual[10] == pytest.approx(22.2080909, abs=1e-5)
    # Ngày 15: 22.516356
    assert actual[14] == pytest.approx(22.5163557, abs=1e-5)


def test_golden_atr_wilder_1978_p35() -> None:
    """Đối chiếu golden vector ATR từ Wilder (1978, trang 35-38).

    Bảng kiểm tra 6 ngày:
    Day 1: High=48.70, Low=47.79, Close=48.16 -> TR1 = 48.70 - 47.79 = 0.91
    Day 2: High=49.35, Low=48.86, Close=49.10
           TR2 = max(49.35-48.86, |49.35-48.16|, |48.86-48.16|) = 1.19
    Day 3: High=49.92, Low=49.50, Close=49.65
           TR3 = max(49.92-49.50, |49.92-49.10|, |49.50-49.10|) = 0.82
    Day 4: High=50.19, Low=49.87, Close=50.05
           TR4 = max(50.19-49.87, |50.19-49.65|, |49.87-49.65|) = 0.54
    Day 5: High=50.85, Low=50.30, Close=50.50
           TR5 = max(50.85-50.30, |50.85-50.05|, |50.30-50.05|) = 0.80
    Day 6: High=50.70, Low=49.90, Close=50.10
           TR6 = max(50.70-49.90, |50.70-50.50|, |49.90-50.50|) = 0.80

    Chu kỳ N=3:
    ATR[2] (Day 3) = SMA(TR1, TR2, TR3) = (0.91 + 1.19 + 0.82) / 3 = 2.92 / 3 ≈ 0.973333
    ATR[3] (Day 4) = (ATR[2]*2 + TR4) / 3 = (0.973333*2 + 0.54) / 3 = 2.486667 / 3 ≈ 0.828889
    ATR[4] (Day 5) = (ATR[3]*2 + TR5) / 3 = (0.828889*2 + 0.80) / 3 = 2.457778 / 3 ≈ 0.819259
    ATR[5] (Day 6) = (ATR[4]*2 + TR6) / 3 = (0.819259*2 + 0.80) / 3 = 2.438519 / 3 ≈ 0.812840
    """
    highs = [48.70, 49.35, 49.92, 50.19, 50.85, 50.70]
    lows = [47.79, 48.86, 49.50, 49.87, 50.30, 49.90]
    closes = [48.16, 49.10, 49.65, 50.05, 50.50, 50.10]

    tr_actual = true_range(highs, lows, closes)
    tr_expected = [0.91, 1.19, 0.82, 0.54, 0.80, 0.80]
    for act, exp in zip(tr_actual, tr_expected, strict=True):
        assert act == pytest.approx(exp, abs=1e-5)

    atr_actual = atr(highs, lows, closes, 3)
    assert atr_actual[0] is None
    assert atr_actual[1] is None
    assert atr_actual[2] == pytest.approx(2.92 / 3.0, abs=1e-5)
    assert atr_actual[3] == pytest.approx(0.8288889, abs=1e-5)
    assert atr_actual[4] == pytest.approx(0.8192593, abs=1e-5)
    assert atr_actual[5] == pytest.approx(0.8128395, abs=1e-5)


def test_golden_zscore_exclusive_and_inclusive() -> None:
    """Đối chiếu golden vector Z-score exclusive vs inclusive trên 6 điểm dữ liệu.

    Dữ liệu: [10.0, 12.0, 14.0, 16.0, 18.0, 30.0], window = 4

    1. Exclusive (prior 4 samples):
       - index 4 (giá 18.0): history = [10, 12, 14, 16]
         mean = 13.0, variance = (9 + 1 + 1 + 9)/4 = 5.0, std = sqrt(5) ≈ 2.236068
         z = (18 - 13) / sqrt(5) = 5 / 2.236068 ≈ 2.236068
       - index 5 (giá 30.0): history = [12, 14, 16, 18]
         mean = 15.0, variance = 5.0, std = sqrt(5)
         z = (30 - 15) / sqrt(5) = 15 / 2.236068 ≈ 6.708204

    2. Inclusive (current 4 samples):
       - index 3 (giá 16.0): history = [10, 12, 14, 16]
         mean = 13.0, std = sqrt(5)
         z = (16 - 13) / sqrt(5) = 3 / 2.236068 ≈ 1.341641
       - index 4 (giá 18.0): history = [12, 14, 16, 18]
         mean = 15.0, std = sqrt(5)
         z = (18 - 15) / sqrt(5) = 3 / 2.236068 ≈ 1.341641
       - index 5 (giá 30.0): history = [14, 16, 18, 30]
         mean = (14 + 16 + 18 + 30)/4 = 78 / 4 = 19.5
         var = ((14-19.5)^2 + (16-19.5)^2 + (18-19.5)^2 + (30-19.5)^2)/4
             = (30.25 + 12.25 + 2.25 + 110.25)/4 = 155.0 / 4 = 38.75
         std = sqrt(38.75) ≈ 6.224950
         z = (30 - 19.5) / 6.224950 = 10.5 / 6.224950 ≈ 1.686759
    """
    samples = [10.0, 12.0, 14.0, 16.0, 18.0, 30.0]
    window = 4

    z_ex = zscore(samples, window, inclusive=False)
    assert z_ex[0] is None
    assert z_ex[1] is None
    assert z_ex[2] is None
    assert z_ex[3] is None
    assert z_ex[4] == pytest.approx(5.0 / math.sqrt(5.0), abs=1e-5)
    assert z_ex[5] == pytest.approx(15.0 / math.sqrt(5.0), abs=1e-5)

    z_in = zscore_inclusive(samples, window)
    assert z_in[0] is None
    assert z_in[1] is None
    assert z_in[2] is None
    assert z_in[3] == pytest.approx(3.0 / math.sqrt(5.0), abs=1e-5)
    assert z_in[4] == pytest.approx(3.0 / math.sqrt(5.0), abs=1e-5)
    assert z_in[5] == pytest.approx(10.5 / math.sqrt(38.75), abs=1e-5)


def test_golden_candle_anatomy_and_atr_normalization() -> None:
    """Đối chiếu golden vector hình học nến và chuẩn hóa ATR.

    Nến Pinbar/Hammer tăng: O=108, H=110, L=100, C=109, ATR=5.0
    - Range = 10
    - Lower wick = 108 - 100 = 8 -> ratio = 0.8
    - Upper wick = 110 - 109 = 1 -> ratio = 0.1
    - Body = 109 - 108 = 1 -> ratio = 0.1
    - Close location = (109 - 100)/10 = 0.9
    - Bullish rejection strength = 0.8 * 0.9 * 0.9 = 0.648
    - Normalized range = 10 / 5.0 = 2.0 ATR

    Nến Pinbar/Shooting Star giảm: O=102, H=110, L=100, C=101, ATR=5.0
    - Range = 10
    - Lower wick = 101 - 100 = 1 -> ratio = 0.1
    - Upper wick = 110 - 102 = 8 -> ratio = 0.8
    - Body = 102 - 101 = 1 -> ratio = 0.1
    - Close location = (101 - 100)/10 = 0.1
    - Bearish rejection strength = 0.8 * (1 - 0.1) * (1 - 0.1) = 0.648
    - Normalized range = 10 / 5.0 = 2.0 ATR
    """
    # 1. Bullish Hammer
    bo, bh, bl, bc = Decimal("108"), Decimal("110"), Decimal("100"), Decimal("109")
    assert lower_wick_ratio(bo, bh, bl, bc) == Decimal("0.8")
    assert upper_wick_ratio(bo, bh, bl, bc) == Decimal("0.1")
    assert body_ratio(bo, bh, bl, bc) == Decimal("0.1")
    assert close_location(bo, bh, bl, bc) == Decimal("0.9")
    assert bullish_wick_rejection_strength(bo, bh, bl, bc) == Decimal("0.648")
    assert normalized_candle_range(bh, bl, Decimal("5.0")) == Decimal("2")

    # 2. Bearish Shooting Star
    so, sh, sl, sc = Decimal("102"), Decimal("110"), Decimal("100"), Decimal("101")
    assert lower_wick_ratio(so, sh, sl, sc) == Decimal("0.1")
    assert upper_wick_ratio(so, sh, sl, sc) == Decimal("0.8")
    assert body_ratio(so, sh, sl, sc) == Decimal("0.1")
    assert close_location(so, sh, sl, sc) == Decimal("0.1")
    assert bearish_wick_rejection_strength(so, sh, sl, sc) == Decimal("0.648")
    assert normalized_candle_range(sh, sl, Decimal("5.0")) == Decimal("2")

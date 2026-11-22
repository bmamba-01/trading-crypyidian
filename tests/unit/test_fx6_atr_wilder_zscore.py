import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.domain.market.feature_snapshot import (
    FeatureCalculator,
    FeatureSettings,
    _formula_hash,
)
from trading_bot.domain.market.features import atr, true_range, zscore, zscore_inclusive
from trading_bot.domain.market.models import MarketSnapshot, OHLCVBar


def _make_bar(i: int, high: float, low: float, close: float, base_time: datetime) -> OHLCVBar:
    t = base_time + timedelta(hours=i)
    close_t = t + timedelta(hours=1)
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe="1h",
        open_time=t,
        close_time=close_t,
        event_at=close_t,
        received_at=close_t,
        available_at=close_t,
        open=Decimal(str((high + low) / 2)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal("100"),
        is_closed=True,
    )


def test_atr_wilder_1978_mathematical_definition() -> None:
    """Kiểm tra ATR Wilder (1978) chuẩn xác:
    - TR[0] = H0 - L0 (không có close trước)
    - TR[i] = max(Hi - Li, |Hi - Ci-1|, |Li - Ci-1|)
    - ATR[window-1] = SMA(TR[0..window-1])
    - ATR[i] = (ATR[i-1] * (window-1) + TR[i]) / window
    """
    highs = [10.0, 12.0, 11.0, 13.0, 15.0]
    lows = [8.0, 9.0, 8.5, 10.0, 11.0]
    closes = [9.0, 11.5, 9.0, 12.5, 14.0]

    tr = true_range(highs, lows, closes)
    # i=0: H-L = 10-8 = 2.0
    assert tr[0] == pytest.approx(2.0)
    # i=1: max(12-9, |12-9|, |9-9|) = 3.0
    assert tr[1] == pytest.approx(3.0)
    # i=2: max(11-8.5, |11-11.5|, |8.5-11.5|) = max(2.5, 0.5, 3.0) = 3.0
    assert tr[2] == pytest.approx(3.0)
    # i=3: max(13-10, |13-9|, |10-9|) = max(3.0, 4.0, 1.0) = 4.0
    assert tr[3] == pytest.approx(4.0)
    # i=4: max(15-11, |15-12.5|, |11-12.5|) = max(4.0, 2.5, 1.5) = 4.0
    assert tr[4] == pytest.approx(4.0)

    # ATR chu kỳ 3:
    # window=3 -> ATR[2] = (2.0 + 3.0 + 3.0) / 3 = 8.0 / 3 ≈ 2.6666667
    # ATR[3] = (ATR[2] * 2 + 4.0) / 3 = (5.3333333 + 4.0) / 3 = 9.3333333 / 3 ≈ 3.1111111
    # ATR[4] = (ATR[3] * 2 + 4.0) / 3 = (6.2222222 + 4.0) / 3 = 10.2222222 / 3 ≈ 3.4074074
    calculated_atr = atr(highs, lows, closes, 3)
    assert calculated_atr[0] is None
    assert calculated_atr[1] is None
    assert calculated_atr[2] == pytest.approx(8.0 / 3)
    assert calculated_atr[3] == pytest.approx((8.0 / 3 * 2 + 4.0) / 3)
    assert calculated_atr[4] == pytest.approx(((8.0 / 3 * 2 + 4.0) / 3 * 2 + 4.0) / 3)


def test_zscore_exclusive_vs_inclusive() -> None:
    """Kiểm tra hai biến thể z-score:
    1. Exclusive (prior_samples_excluding_current):
       - history = samples[i-window : i] (N mẫu trước, không gồm hiện tại)
       - z = (x[i] - mean(history)) / std(history)
       - Yêu cầu tối thiểu window + 1 mẫu để có kết quả đầu tiên tại index = window
    2. Inclusive (inclusive_current_sample):
       - history = samples[i-window+1 : i+1] (N mẫu gồm cả hiện tại)
       - z = (x[i] - mean(history)) / std(history)
       - Yêu cầu tối thiểu window mẫu để có kết quả đầu tiên tại index = window - 1
    """
    # 5 mẫu: [10.0, 20.0, 30.0, 40.0, 50.0]
    samples = [10.0, 20.0, 30.0, 40.0, 50.0]
    window = 3

    # Biến thể 1: exclusive (prior excluding current)
    z_ex = zscore(samples, window, inclusive=False)
    # Tại index 0, 1, 2: None (chưa đủ 3 mẫu trước)
    assert z_ex[0] is None
    assert z_ex[1] is None
    assert z_ex[2] is None
    # Tại index 3: history = [10, 20, 30], mean = 20.0
    # std = sqrt((100+0+100)/3) = sqrt(200/3) ≈ 8.1649658
    # x[3] = 40.0 -> z = (40 - 20) / sqrt(200/3) ≈ 2.4494897
    std_prior = math.sqrt(200.0 / 3)
    assert z_ex[3] == pytest.approx((40.0 - 20.0) / std_prior)
    # Tại index 4: history = [20, 30, 40], mean = 30.0, std = sqrt(200/3)
    # x[4] = 50.0 -> z = (50 - 30) / sqrt(200/3) ≈ 2.4494897
    assert z_ex[4] == pytest.approx((50.0 - 30.0) / std_prior)

    # Biến thể 2: inclusive (including current sample)
    z_in = zscore(samples, window, inclusive=True)
    assert zscore_inclusive(samples, window) == z_in
    # Tại index 0, 1: None (chưa đủ 3 mẫu)
    assert z_in[0] is None
    assert z_in[1] is None
    # Tại index 2: history = [10, 20, 30], mean = 20.0, std = sqrt(200/3)
    # x[2] = 30.0 -> z = (30 - 20) / sqrt(200/3) ≈ 1.22474487
    assert z_in[2] == pytest.approx((30.0 - 20.0) / std_prior)
    # Tại index 3: history = [20, 30, 40], mean = 30.0, std = sqrt(200/3)
    # x[3] = 40.0 -> z = (40 - 30) / sqrt(200/3) ≈ 1.22474487
    assert z_in[3] == pytest.approx((40.0 - 30.0) / std_prior)
    # Tại index 4: history = [30, 40, 50], mean = 40.0, std = sqrt(200/3)
    # x[4] = 50.0 -> z = (50 - 40) / sqrt(200/3) ≈ 1.22474487
    assert z_in[4] == pytest.approx((50.0 - 40.0) / std_prior)


def test_zscore_zero_variance_safe() -> None:
    """Phương sai bằng 0 (chuỗi đi ngang tuyệt đối) phải trả về None cho cả 2 biến thể."""
    flat = [100.0, 100.0, 100.0, 100.0]
    z_ex = zscore(flat, 3, inclusive=False)
    z_in = zscore(flat, 3, inclusive=True)
    assert z_ex[3] is None
    assert z_in[2] is None
    assert z_in[3] is None


def test_feature_settings_and_formula_hash_track_atr_and_zscore_params() -> None:
    """FeatureSettings và formula_hash phải theo dõi tường minh tham số ATR seed và zscore mode."""
    s1 = FeatureSettings(code_hash="c" * 64)
    assert s1.atr_seed_method == "wilder_first_period_sma"
    assert s1.zscore_mode == "prior_samples_excluding_current"

    # Đổi tham số dẫn đến formula_hash khác nhau
    s2 = FeatureSettings(code_hash="c" * 64, zscore_mode="inclusive_current_sample")
    hash1 = _formula_hash(s1)
    hash2 = _formula_hash(s2)
    assert hash1 != hash2


def test_feature_calculator_outputs_both_zscore_variants() -> None:
    """Snapshot tính toán chứa cả zscore_{period} (exclusive) và zscore_inclusive_{period}."""
    now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    bars = []
    for i in range(80):
        val = 100.0 + (i % 5) * 2.0 if i < 79 else 150.0
        bars.append(_make_bar(i, val + 1.0, val - 1.0, val, now - timedelta(hours=80)))

    settings = FeatureSettings(code_hash="c" * 64, zscore_period=20)
    calc = FeatureCalculator(settings)
    snapshot = calc.calculate(
        MarketSnapshot(as_of=now, bars=tuple(bars)),
        workspace_id=uuid4(),
        input_event_ids=[f"e-{i}" for i in range(len(bars))],
        symbol="BTCUSDT",
        timeframe="1h",
        computed_at=now,
    )

    assert "zscore_20" in snapshot.features
    assert "zscore_inclusive_20" in snapshot.features
    assert snapshot.features["zscore_20"] is not None
    assert snapshot.features["zscore_inclusive_20"] is not None
    # Giá trị của exclusive và inclusive khác nhau do tập mẫu chuẩn hóa khác nhau
    assert snapshot.features["zscore_20"] != snapshot.features["zscore_inclusive_20"]

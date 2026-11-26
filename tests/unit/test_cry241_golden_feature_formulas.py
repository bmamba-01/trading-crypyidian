"""Golden fixture tham chiếu công thức FeatureCalculator (CRY-241 / WP-02B/golden-ref).

Giá trị kỳ vọng bên dưới được tính tay từ định nghĩa công thức (không sinh từ code dự án)
để đối chiếu độc lập. Không đổi `formula_version` hay snapshot hash cũ.
"""

from __future__ import annotations

import math

import pytest

from trading_bot.domain.market.feature_snapshot import (
    FeatureSettings,
    MarketRegime,
    _regime_assessment,
)
from trading_bot.domain.market.features import (
    adx_dmi,
    atr,
    bollinger_bands,
    efficiency_ratio,
    ema,
    macd,
    realized_volatility,
    rsi,
    sma,
    true_range,
    vwap,
    zscore,
    zscore_inclusive,
)


def test_golden_sma_ema_seed_va_warmup() -> None:
    assert sma([1, 2, 3, 4, 5, 6], 3) == (None, None, 2.0, 3.0, 4.0, 5.0)
    # EMA alpha = 2/(3+1) = 0.5, seed = SMA(1,2,3) = 2 → 3, rồi 4.
    assert ema([1, 2, 3, 4, 5], 3) == (None, None, 2.0, 3.0, 4.0)
    # Warmup 5 nến mà chỉ có 4 nến thì không được đoán seed.
    assert ema([1, 2, 3, 4], 5) == (None, None, None, None)
    assert sma([1], 2) == (None,)


def test_golden_true_range_atr_va_adx_nen_phang() -> None:
    flat = [100.0] * 5
    assert true_range(flat, flat, flat) == (0.0,) * 5
    assert atr(flat, flat, flat, 2) == (None, 0.0, 0.0, 0.0, 0.0)
    plus_di, minus_di, dx, adx = adx_dmi(flat, flat, flat, 2)
    assert plus_di[2:] == (0.0, 0.0, 0.0)
    assert minus_di[2:] == (0.0, 0.0, 0.0)
    assert dx[2:] == (0.0, 0.0, 0.0)
    assert adx[3:] == (0.0, 0.0)


def test_golden_rsi_flat_va_tang_deu() -> None:
    # Mẫu số 0 ở cả hai chiều → RSI trung tính 50 (không phải 0 hay 100).
    assert rsi([100.0] * 5, 2) == (None, None, 50.0, 50.0, 50.0)
    # Loss = 0 → RSI 100.
    assert rsi([100.0, 101.0, 102.0, 103.0], 2) == (None, None, 100.0, 100.0)


def test_golden_er_bollinger_zscore_mau_so_khong() -> None:
    flat = [100.0] * 4
    assert efficiency_ratio(flat, 3) == (None, None, None, 0.0)
    middle, upper, lower = bollinger_bands(flat, 3)
    assert middle == (None, None, 100.0, 100.0)
    assert upper == (None, None, 100.0, 100.0)
    assert lower == (None, None, 100.0, 100.0)
    # Phương sai 0 → None thay vì chia cho 0 hay gán 0.
    assert zscore(flat, 2, inclusive=True) == (None,) * 4
    assert zscore(flat, 2) == (None,) * 4


def test_golden_vwap_va_reset_theo_session() -> None:
    # Nến đầu volume 0 → None; sau đó (20*1 + 30*3)/4 = 27.5.
    assert vwap([10.0, 20.0, 30.0], [0.0, 1.0, 3.0]) == (None, 20.0, 27.5)
    # Caller chia session: VWAP phiên mới bắt đầu từ 0, không nối phiên trước.
    assert vwap([10.0], [1.0]) == (10.0,)
    assert vwap([20.0], [1.0]) == (20.0,)


def test_golden_log_return_volatility_khong_annualize() -> None:
    prices = [100.0, 110.0, 100.0, 110.0]
    returns = [math.log(110 / 100), math.log(100 / 110), math.log(110 / 100)]
    expected = math.sqrt(sum(value * value for value in returns) / 3)
    result = realized_volatility(prices, 3)
    assert result[:3] == (None, None, None)
    assert result[3] == pytest.approx(expected, rel=0, abs=1e-12)


def test_du_lieu_khong_hop_le_bi_tu_choi_fail_closed() -> None:
    with pytest.raises(ValueError, match="hữu hạn"):
        sma([1.0, float("nan"), 3.0], 2)
    with pytest.raises(ValueError, match="thấp không được lớn hơn giá cao"):
        atr([100.0], [101.0], [100.0], 1)
    with pytest.raises(ValueError, match="lớn hơn 0"):
        realized_volatility([100.0, 0.0], 1)
    with pytest.raises(ValueError, match="không được âm"):
        vwap([10.0], [-1.0])


SETTINGS = FeatureSettings(code_hash="a" * 64)
HISTORY = [0.001] * 2161  # regime_history 90 ngày / 1h = 2160 mẫu quá khứ


def _features(*, adx: float | None, er: float | None, rv: float | None) -> dict[str, float | None]:
    return {
        "adx_14": adx,
        "efficiency_ratio_40": er,
        "realized_volatility_20": rv,
    }


def test_semantic_review_nhan_regime_khong_bao_huong() -> None:
    trend = _regime_assessment(
        _features(adx=25.0, er=0.4, rv=0.001),
        HISTORY,
        SETTINGS,
        data_usable=True,
    )
    assert trend.label is MarketRegime.TREND
    assert trend.reason_code == "ADX_TREND_THRESHOLD"
    assert "không cho biết hướng" in trend.reason_vi

    range_ = _regime_assessment(
        _features(adx=10.0, er=0.1, rv=0.001),
        HISTORY,
        SETTINGS,
        data_usable=True,
    )
    assert range_.label is MarketRegime.RANGE
    assert range_.reason_code == "ADX_ER_RANGE_THRESHOLDS"
    assert "không phải tín hiệu đảo chiều" in range_.reason_vi

    high_vol = _regime_assessment(
        _features(adx=30.0, er=0.4, rv=0.01),
        HISTORY,
        SETTINGS,
        data_usable=True,
    )
    assert high_vol.label is MarketRegime.HIGH_VOLATILITY
    assert high_vol.reason_code == "RV_ABOVE_PRIOR_QUANTILE"


def test_golden_zscore_inclusive_va_exclusive() -> None:
    # Inclusive: mỗi cửa sổ [1,2], [2,3], [3,4] có mean/step 1.5/0.5 → z = 1.
    inclusive = zscore([1.0, 2.0, 3.0, 4.0], 2, inclusive=True)
    assert inclusive == (None, 1.0, 1.0, 1.0)
    assert zscore_inclusive([1.0, 2.0, 3.0, 4.0], 2) == inclusive
    # Exclusive: dùng 2 mẫu trước đó, nên mẫu đầu tiên ở chỉ số 2.
    assert zscore([1.0, 2.0, 3.0, 4.0], 2) == (None, None, 3.0, 3.0)
    assert zscore([5.0, 5.0, 5.0], 2, inclusive=True) == (None, None, None)


def test_golden_macd_voi_nhien_va_warmup() -> None:
    closes = [10.0, 10.0, 10.0, 10.0, 20.0, 20.0, 20.0, 20.0, 20.0]
    line, signal, histogram = macd(closes, fast=3, slow=5, signal=2)
    # EMA nhanh seed 15, EMA chậm seed 12 tại nến 4 → MACD = 3.
    assert line[:4] == (None, None, None, None)
    assert line[4] == pytest.approx(3.0, rel=0, abs=1e-12)
    # Nến 5: fast 17.5, slow 44/3 → 17/6.
    assert line[5] == pytest.approx(17 / 6, rel=0, abs=1e-12)
    # Đường tín hiệu seed = SMA của 2 mẫu MACD đầu (3 và 17/6) → 35/12 tại chỉ số 5.
    assert signal[:5] == (None, None, None, None, None)
    assert signal[5] == pytest.approx(35 / 12, rel=0, abs=1e-12)
    assert signal[6] == pytest.approx(271 / 108, rel=0, abs=1e-12)
    # Histogram chỉ có giá trị khi cả MACD và tín hiệu cùng có giá trị (từ chỉ số 5).
    assert histogram[:5] == (None, None, None, None, None)
    assert histogram[5] == pytest.approx(-1 / 12, rel=0, abs=1e-12)
    assert histogram[6] == pytest.approx(-11 / 54, rel=0, abs=1e-12)
    with pytest.raises(ValueError, match="fast phải nhỏ hơn slow"):
        macd(closes, fast=5, slow=5, signal=2)


def test_semantic_review_fail_closed_khi_chua_du_du_lieu() -> None:
    blocked = _regime_assessment(
        _features(adx=25.0, er=0.4, rv=0.001),
        HISTORY,
        SETTINGS,
        data_usable=False,
    )
    assert blocked.label is MarketRegime.UNKNOWN
    assert blocked.reason_code == "DATA_QUALITY_BLOCKED"
    assert blocked.confidence == 0.0

    warmup = _regime_assessment(
        _features(adx=None, er=0.4, rv=0.001),
        HISTORY,
        SETTINGS,
        data_usable=True,
    )
    assert warmup.label is MarketRegime.UNKNOWN
    assert warmup.reason_code == "REGIME_WARMUP_INCOMPLETE"

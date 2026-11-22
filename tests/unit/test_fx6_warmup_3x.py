"""Unit tests cho FX-6A (CRY-445): EMA/RMA warm-up quy tắc >= 3x period và formula_hash.

Kiểm tra:
1. Quy tắc warm-up >= 3x period cho EMA (ema_20 cần >= 60, ema_80 cần >= 240, ema_200 cần >= 600).
2. Quy tắc warm-up cho RMA/ATR (atr_14 cần >= 42 nến).
3. Quy tắc warm-up cho RSI (rsi_14 cần >= 43 nến).
4. Quy tắc warm-up cho ADX (adx_14 cần >= (3+1)*14 = 56 nến).
5. formula_hash thay đổi khi warmup_multiplier hoặc warmup_rule thay đổi.
6. Validation ràng buộc warmup_multiplier > 0 và warmup_rule không rỗng.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.domain.market.feature_snapshot import (
    FeatureCalculator,
    FeatureSettings,
    _formula_hash,
    _warmup_by_feature,
)
from trading_bot.domain.market.models import MarketSnapshot, OHLCVBar

WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
INTERVAL = timedelta(hours=1)


def make_test_bar(index: int, base_time: datetime, close_num: float = 100.0) -> OHLCVBar:
    open_time = base_time + index * INTERVAL
    close_time = open_time + INTERVAL
    c = Decimal(str(close_num))
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe="1h",
        open_time=open_time,
        close_time=close_time,
        event_at=close_time,
        received_at=close_time,
        available_at=close_time,
        open=c,
        high=c + Decimal("1.0"),
        low=c - Decimal("1.0"),
        close=c,
        volume=Decimal("10.0"),
        quote_volume=Decimal("1000.0"),
        is_closed=True,
        source="TEST",
        checksum=f"chk-{index}",
    )


def test_warmup_multiplier_validation() -> None:
    """Xác nhận warmup_multiplier phải > 0 và warmup_rule không được để trống."""
    with pytest.raises(ValueError, match="Hệ số warm-up phải lớn hơn 0"):
        FeatureSettings(code_hash="a" * 64, warmup_multiplier=0)

    with pytest.raises(ValueError, match="Hệ số warm-up phải lớn hơn 0"):
        FeatureSettings(code_hash="a" * 64, warmup_multiplier=-1)

    with pytest.raises(ValueError, match="Quy tắc warm-up không được để trống"):
        FeatureSettings(code_hash="a" * 64, warmup_rule="   ")


def test_warmup_requirements_enforce_3x_period_rule() -> None:
    """Xác nhận _warmup_by_feature tuân thủ chặt chẽ quy tắc >= 3x period cho EMA và RMA."""
    settings = FeatureSettings(
        code_hash="a" * 64,
        sma_period=20,
        ema_periods=(20, 80, 200),
        atr_period=14,
        rsi_period=14,
        adx_period=14,
        warmup_multiplier=3,
        warmup_rule="3x_period",
    )

    # Dưới 3x period của ema_20 (cần 60 nến)
    warmup_59 = _warmup_by_feature(settings, bar_count=59)
    assert warmup_59["sma_20"] is True  # SMA chỉ cần 20 nến
    assert warmup_59["ema_20"] is False  # 59 < 3 * 20 = 60
    assert warmup_59["atr_14"] is True  # 59 >= 3 * 14 = 42

    # Đạt đúng 60 nến cho ema_20
    warmup_60 = _warmup_by_feature(settings, bar_count=60)
    assert warmup_60["ema_20"] is True
    assert warmup_60["ema_80"] is False  # 60 < 3 * 80 = 240
    assert warmup_60["ema_200"] is False  # 60 < 3 * 200 = 600

    # Kiểm tra RMA / ATR (14 * 3 = 42)
    warmup_41 = _warmup_by_feature(settings, bar_count=41)
    assert warmup_41["atr_14"] is False
    warmup_42 = _warmup_by_feature(settings, bar_count=42)
    assert warmup_42["atr_14"] is True

    # Kiểm tra RSI (14 * 3 + 1 = 43)
    assert _warmup_by_feature(settings, bar_count=42)["rsi_14"] is False
    assert _warmup_by_feature(settings, bar_count=43)["rsi_14"] is True

    # Kiểm tra ADX ((3 + 1) * 14 = 56)
    assert _warmup_by_feature(settings, bar_count=55)["adx_14"] is False
    assert _warmup_by_feature(settings, bar_count=56)["adx_14"] is True

    # Kiểm tra EMA 80 (80 * 3 = 240)
    assert _warmup_by_feature(settings, bar_count=239)["ema_80"] is False
    assert _warmup_by_feature(settings, bar_count=240)["ema_80"] is True

    # Kiểm tra EMA 200 (200 * 3 = 600)
    assert _warmup_by_feature(settings, bar_count=599)["ema_200"] is False
    assert _warmup_by_feature(settings, bar_count=600)["ema_200"] is True


def test_formula_hash_tracks_warmup_multiplier_and_rule() -> None:
    """Xác nhận formula_hash đổi khi đổi warmup_multiplier hoặc warmup_rule."""
    base_settings = FeatureSettings(
        code_hash="a" * 64, warmup_multiplier=3, warmup_rule="3x_period"
    )
    alt_mult_settings = FeatureSettings(
        code_hash="a" * 64, warmup_multiplier=4, warmup_rule="3x_period"
    )
    alt_rule_settings = FeatureSettings(
        code_hash="a" * 64, warmup_multiplier=3, warmup_rule="4x_period"
    )

    h_base = _formula_hash(base_settings)
    h_mult = _formula_hash(alt_mult_settings)
    h_rule = _formula_hash(alt_rule_settings)

    assert h_base != h_mult, "formula_hash phải thay đổi khi warmup_multiplier thay đổi!"
    assert h_base != h_rule, "formula_hash phải thay đổi khi warmup_rule thay đổi!"


def test_calculator_applies_3x_warmup_to_snapshot_usability() -> None:
    """Xác nhận FeatureCalculator chỉ đánh dấu snapshot.is_usable=True khi toàn bộ warmup đạt 3x."""
    settings = FeatureSettings(
        code_hash="a" * 64,
        sma_period=5,
        ema_periods=(5,),  # cần 3 * 5 = 15 nến
        atr_period=5,      # cần 3 * 5 = 15 nến
        rsi_period=5,      # cần 3 * 5 + 1 = 16 nến
        adx_period=5,      # cần (3 + 1) * 5 = 20 nến
        bollinger_period=5,
        realized_volatility_period=5,
        efficiency_ratio_period=5,
        zscore_period=5,
        warmup_multiplier=3,
    )
    calculator = FeatureCalculator(settings)
    base_t = datetime(2026, 11, 20, 0, 0, 0, tzinfo=UTC)

    # 1. 15 nến: ADX chưa đủ warmup (cần 20 nến)
    bars_15 = tuple(make_test_bar(i, base_t, 100.0 + i) for i in range(15))
    snap_15 = calculator.calculate(
        MarketSnapshot(as_of=bars_15[-1].available_at, bars=bars_15),
        workspace_id=WORKSPACE_ID,
        input_event_ids=tuple(f"ev-{i}" for i in range(15)),
        symbol="BTCUSDT",
        timeframe="1h",
        computed_at=bars_15[-1].available_at,
    )
    assert not snap_15.warmup_complete
    assert snap_15.warmup["ema_5"] is True
    assert snap_15.warmup["atr_5"] is True
    assert snap_15.warmup["adx_5"] is False
    assert not snap_15.is_usable
    assert "WARMUP_INCOMPLETE" in snap_15.quality_flags

    # 2. 20 nến: Toàn bộ đạt warmup >= 3x
    bars_20 = tuple(make_test_bar(i, base_t, 100.0 + i) for i in range(20))
    snap_20 = calculator.calculate(
        MarketSnapshot(as_of=bars_20[-1].available_at, bars=bars_20),
        workspace_id=WORKSPACE_ID,
        input_event_ids=tuple(f"ev-{i}" for i in range(20)),
        symbol="BTCUSDT",
        timeframe="1h",
        computed_at=bars_20[-1].available_at,
    )
    assert snap_20.warmup_complete
    assert snap_20.warmup["adx_5"] is True
    assert snap_20.is_usable
    assert "WARMUP_INCOMPLETE" not in snap_20.quality_flags

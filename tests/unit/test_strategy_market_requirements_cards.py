"""SIG-2 / G4: Strategy Card sản phẩm có yêu cầu dữ liệu để producer và runtime đánh giá."""

from __future__ import annotations

from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.application.strategy_market_requirements import (
    CARD_VOL_BREAKOUT_RELEASE,
    entry_timeframe_for_release,
    feature_requirements_for_release,
)


def test_product_card_release_id_matches_registry_and_has_requirements() -> None:
    registry = strategy_registry()
    release = registry.get_release(CARD_VOL_BREAKOUT_RELEASE)
    assert release is not None
    assert entry_timeframe_for_release(CARD_VOL_BREAKOUT_RELEASE) == "1h"
    requirements = feature_requirements_for_release(CARD_VOL_BREAKOUT_RELEASE)
    # Chuỗi đầu tiên của snapshot là khung vào lệnh: engine L0–L6 đọc `series[0]`.
    assert next(iter(requirements)) == "1h"
    assert requirements["1h"] == ("atr_14",)


def test_short_timeframes_are_supported_with_pinned_settings() -> None:
    from datetime import timedelta

    from trading_bot.application.strategy_market_requirements import feature_settings_for

    for timeframe, minutes in (("1m", 1), ("3m", 3), ("5m", 5), ("30m", 30)):
        settings = feature_settings_for(timeframe)
        assert settings.expected_interval == timedelta(minutes=minutes)
        assert settings.max_event_age == timedelta(minutes=2 * minutes)


def test_unsupported_timeframe_and_release_errors_carry_reason_codes() -> None:
    import pytest

    from trading_bot.application.strategy_market_requirements import (
        MarketRequirementError,
        feature_settings_for,
    )

    with pytest.raises(MarketRequirementError) as timeframe:
        feature_settings_for("10m")
    assert timeframe.value.code == "TIMEFRAME_UNSUPPORTED"
    assert "10m" in timeframe.value.message
    assert str(timeframe.value).startswith("TIMEFRAME_UNSUPPORTED:")
    with pytest.raises(MarketRequirementError) as release:
        feature_requirements_for_release("h99-v1")
    assert release.value.code == "RELEASE_UNSUPPORTED"
    # Vẫn là ValueError để mọi nơi đang bắt ValueError giữ nguyên hành vi.
    assert isinstance(release.value, ValueError)


def test_requirement_limits_guard_load_per_release() -> None:
    import pytest

    from trading_bot.application.strategy_market_requirements import (
        FEATURES_BY_RELEASE,
        MAX_FEATURES_PER_TIMEFRAME,
        MAX_TIMEFRAMES_PER_RELEASE,
        MarketRequirementError,
        validate_requirements,
    )

    validate_requirements(FEATURES_BY_RELEASE)  # bảng hiện có hợp lệ
    too_many_frames = {"x-v1": dict.fromkeys(("1m", "3m", "5m", "15m"), ("atr_14",))}
    with pytest.raises(MarketRequirementError) as frames:
        validate_requirements(too_many_frames)
    assert frames.value.code == "TOO_MANY_TIMEFRAMES"
    assert MAX_TIMEFRAMES_PER_RELEASE == 3
    too_many_features = {
        "x-v1": {"5m": tuple(f"f{i}" for i in range(MAX_FEATURES_PER_TIMEFRAME + 1))}
    }
    with pytest.raises(MarketRequirementError) as features:
        validate_requirements(too_many_features)
    assert features.value.code == "TOO_MANY_FEATURES"
    with pytest.raises(MarketRequirementError) as unknown:
        validate_requirements({"x-v1": {"10m": ("atr_14",)}})
    assert unknown.value.code == "TIMEFRAME_UNSUPPORTED"

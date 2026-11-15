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

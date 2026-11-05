"""Hợp đồng timeframe/feature dùng chung giữa signal producer và replay."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType

from trading_bot.domain.market.feature_snapshot import FeatureSettings

FEATURE_CODE_HASH = hashlib.sha256(b"crypidian-market-signal-features-v1").hexdigest()
TIMEFRAME_INTERVALS = MappingProxyType(
    {
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
    }
)
FEATURES_BY_RELEASE: Mapping[str, Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "h01-v1": MappingProxyType(
            {"1h": ("ema_20", "ema_80", "adx_14", "atr_14"), "4h": ("ema_200",)}
        ),
        "h03-v1": MappingProxyType({"1h": ("atr_14",)}),
        "h06-v1": MappingProxyType(
            {
                "15m": (
                    "adx_14",
                    "efficiency_ratio_40",
                    "zscore_20",
                    "rsi_14",
                    "atr_14",
                )
            }
        ),
    }
)
ENTRY_TIMEFRAME_BY_RELEASE = MappingProxyType(
    {
        "h01-v1": "1h",
        "h03-v1": "1h",
        "h06-v1": "15m",
    }
)


def feature_requirements_for_release(release_id: str) -> Mapping[str, tuple[str, ...]]:
    """Trả đúng feature/timeframe đã khai báo cho built-in strategy release."""
    try:
        return FEATURES_BY_RELEASE[release_id]
    except KeyError as error:
        raise ValueError("Strategy release chưa được hỗ trợ bởi market snapshot.") from error


def entry_timeframe_for_release(release_id: str) -> str | None:
    """Trả timeframe phát quyết định; release chưa biết không được tự suy luận."""
    return ENTRY_TIMEFRAME_BY_RELEASE.get(release_id)


def feature_settings_for(timeframe: str) -> FeatureSettings:
    """Trả settings feature đã ghim theo timeframe được hỗ trợ."""
    try:
        interval = TIMEFRAME_INTERVALS[timeframe]
    except KeyError as error:
        raise ValueError("Timeframe chưa được hỗ trợ bởi market snapshot.") from error
    return FeatureSettings(
        code_hash=FEATURE_CODE_HASH,
        expected_interval=interval,
        max_event_age=interval * 2,
    )

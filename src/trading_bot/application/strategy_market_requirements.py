"""Hợp đồng timeframe/feature dùng chung giữa signal producer và replay.

SIG-2 (16/11/2026): mở khung ngắn 1m/3m/5m/30m cho release scalping; mỗi release tối đa
``MAX_TIMEFRAMES_PER_RELEASE`` khung và ``MAX_FEATURES_PER_TIMEFRAME`` feature mỗi khung để kiểm
soát tải tính toán. Lỗi mang mã lý do ổn định (``MarketRequirementError.code``) kèm câu tiếng Việt.
Khung "10m" không phải interval của Binance nên không hỗ trợ (không resample để tạo).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import timedelta
from types import MappingProxyType

from trading_bot.domain.market.feature_snapshot import FeatureSettings

# Release ID của thẻ `SC-BINANCE-USDM-VOL-BREAKOUT-001` v1.0.0 (strategy_card_release_id).
CARD_VOL_BREAKOUT_RELEASE = "sc-binance-usdm-vol-breakout-001-v1.0.0"
FEATURE_CODE_HASH = hashlib.sha256(b"crypidian-market-signal-features-v1").hexdigest()
MAX_TIMEFRAMES_PER_RELEASE = 3
MAX_FEATURES_PER_TIMEFRAME = 8
TIMEFRAME_INTERVALS = MappingProxyType(
    {
        "1m": timedelta(minutes=1),
        "3m": timedelta(minutes=3),
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "30m": timedelta(minutes=30),
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
        "h03-v2": MappingProxyType({"1h": ("atr_14",)}),
        # H03 v3 (15/11/2026): mỗi khung vào lệnh một release (domain/strategy/rules_v3.py).
        "h03-v3-15m": MappingProxyType({"15m": ("atr_14",)}),
        "h03-v3-1h": MappingProxyType({"1h": ("atr_14",)}),
        "h03-v3-4h": MappingProxyType({"4h": ("atr_14",)}),
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
        "h06-v2": MappingProxyType(
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
        # SIG-2 / G4: Strategy Card sản phẩm (application/strategy_catalog.py). Engine L0–L6 đánh
        # giá trên chuỗi đầu tiên của snapshot và ưu tiên ATR đã quản trị cùng timeframe.
        CARD_VOL_BREAKOUT_RELEASE: MappingProxyType({"1h": ("atr_14",)}),
        # SIG-4: họ scalping M5 (domain/strategy/scalping_rules.py); H20 cần thêm bối cảnh M15.
        "h18-v1": MappingProxyType({"5m": ("atr_14",)}),
        "h19-v1": MappingProxyType({"5m": ("atr_14", "adx_14", "efficiency_ratio_40")}),
        "h20-v1": MappingProxyType({"5m": ("atr_14",), "15m": ("atr_14",)}),
        # 6 rule định lượng mở rộng (expanded_rules.py)
        "h02-v1": MappingProxyType({"4h": ("atr_14", "realized_volatility_20")}),
        "h04-v1": MappingProxyType(
            {
                "15m": (
                    "atr_14",
                    "ema_20",
                    "sma_20",
                    "bollinger_upper_20",
                    "bollinger_lower_20",
                    "realized_volatility_20",
                )
            }
        ),
        "h05-v1": MappingProxyType(
            {
                "1h": ("atr_14", "ema_20", "ema_80", "rsi_14", "adx_14"),
                "4h": ("ema_200",),
            }
        ),
        "h07-v1": MappingProxyType(
            {"5m": ("atr_14", "adx_14", "efficiency_ratio_40", "session_vwap")}
        ),
        "h21-v1": MappingProxyType(
            {
                "1h": ("atr_14", "adx_14", "plus_di_14", "minus_di_14"),
                "4h": ("ema_200",),
            }
        ),
        "h22-v1": MappingProxyType({"15m": ("atr_14", "adx_14"), "1h": ("ema_80", "atr_14")}),
        # 7 Strategy Cards L0–L6 kích hoạt mới/nâng cấp (cards 002..008)
        "sc-binance-usdm-mean-revert-002-v1.2.1": MappingProxyType(
            {"15m": ("atr_14", "ema_20", "zscore_20")}
        ),
        "sc-binance-usdm-tsmom-003-v1.0.0": MappingProxyType(
            {"4h": ("atr_14", "realized_volatility_20")}
        ),
        "sc-binance-usdm-bbkc-squeeze-004-v1.0.0": MappingProxyType(
            {
                "15m": (
                    "atr_14",
                    "ema_20",
                    "sma_20",
                    "bollinger_upper_20",
                    "bollinger_lower_20",
                    "realized_volatility_20",
                )
            }
        ),
        "sc-binance-usdm-trend-pullback-005-v1.0.0": MappingProxyType(
            {
                "1h": ("atr_14", "ema_20", "ema_80", "rsi_14", "adx_14"),
                "4h": ("ema_200",),
            }
        ),
        "sc-binance-usdm-vwap-revert-006-v1.0.0": MappingProxyType(
            {"15m": ("atr_14", "adx_14", "efficiency_ratio_40", "session_vwap")}
        ),
        "sc-binance-usdm-macd-adx-007-v1.0.0": MappingProxyType(
            {
                "1h": ("atr_14", "adx_14", "plus_di_14", "minus_di_14"),
                "4h": ("ema_200",),
            }
        ),
        "sc-binance-usdm-taker-flow-008-v1.0.0": MappingProxyType(
            {"15m": ("atr_14", "adx_14"), "1h": ("ema_80", "atr_14")}
        ),
    }
)
ENTRY_TIMEFRAME_BY_RELEASE = MappingProxyType(
    {
        "h01-v1": "1h",
        "h03-v1": "1h",
        "h06-v1": "15m",
        "h03-v2": "1h",
        "h03-v3-15m": "15m",
        "h03-v3-1h": "1h",
        "h03-v3-4h": "4h",
        "h06-v2": "15m",
        CARD_VOL_BREAKOUT_RELEASE: "1h",
        "h18-v1": "5m",
        "h19-v1": "5m",
        "h20-v1": "5m",
        "h02-v1": "4h",
        "h04-v1": "15m",
        "h05-v1": "1h",
        "h07-v1": "5m",
        "h21-v1": "1h",
        "h22-v1": "15m",
        "sc-binance-usdm-mean-revert-002-v1.2.1": "15m",
        "sc-binance-usdm-tsmom-003-v1.0.0": "4h",
        "sc-binance-usdm-bbkc-squeeze-004-v1.0.0": "15m",
        "sc-binance-usdm-trend-pullback-005-v1.0.0": "1h",
        "sc-binance-usdm-vwap-revert-006-v1.0.0": "15m",
        "sc-binance-usdm-macd-adx-007-v1.0.0": "1h",
        "sc-binance-usdm-taker-flow-008-v1.0.0": "15m",
    }
)


class MarketRequirementError(ValueError):
    """Lỗi hợp đồng dữ liệu thị trường có mã lý do ổn định; vẫn là ``ValueError``."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def validate_requirements(table: Mapping[str, Mapping[str, tuple[str, ...]]]) -> None:
    """Kiểm bảng yêu cầu: khung được hỗ trợ, không quá số khung/feature cho phép mỗi release."""
    for release_id, frames in table.items():
        if len(frames) > MAX_TIMEFRAMES_PER_RELEASE:
            raise MarketRequirementError(
                "TOO_MANY_TIMEFRAMES",
                f"Release {release_id} khai báo {len(frames)} khung, tối đa "
                f"{MAX_TIMEFRAMES_PER_RELEASE}.",
            )
        for timeframe, features in frames.items():
            if timeframe not in TIMEFRAME_INTERVALS:
                raise MarketRequirementError(
                    "TIMEFRAME_UNSUPPORTED",
                    f"Khung {timeframe} của release {release_id} chưa được hỗ trợ.",
                )
            if len(features) > MAX_FEATURES_PER_TIMEFRAME:
                raise MarketRequirementError(
                    "TOO_MANY_FEATURES",
                    f"Release {release_id} khung {timeframe} có {len(features)} feature, tối đa "
                    f"{MAX_FEATURES_PER_TIMEFRAME}.",
                )


def feature_requirements_for_release(release_id: str) -> Mapping[str, tuple[str, ...]]:
    """Trả đúng feature/timeframe đã khai báo cho built-in strategy release."""
    try:
        return FEATURES_BY_RELEASE[release_id]
    except KeyError as error:
        raise MarketRequirementError(
            "RELEASE_UNSUPPORTED",
            f"Strategy release chưa được hỗ trợ bởi market snapshot: {release_id}.",
        ) from error


def entry_timeframe_for_release(release_id: str) -> str | None:
    """Trả timeframe phát quyết định; release chưa biết không được tự suy luận."""
    return ENTRY_TIMEFRAME_BY_RELEASE.get(release_id)


def feature_settings_for(
    timeframe: str,
    *,
    extra_features: Sequence[str] = (),
) -> FeatureSettings:
    """Trả settings feature đã ghim theo timeframe được hỗ trợ."""
    try:
        interval = TIMEFRAME_INTERVALS[timeframe]
    except KeyError as error:
        raise MarketRequirementError(
            "TIMEFRAME_UNSUPPORTED",
            f"Timeframe chưa được hỗ trợ bởi market snapshot: {timeframe} "
            f"(hỗ trợ: {', '.join(TIMEFRAME_INTERVALS)}).",
        ) from error
    return FeatureSettings(
        code_hash=FEATURE_CODE_HASH,
        expected_interval=interval,
        max_event_age=interval * 2,
        extra_features=tuple(extra_features),
    )


validate_requirements(FEATURES_BY_RELEASE)

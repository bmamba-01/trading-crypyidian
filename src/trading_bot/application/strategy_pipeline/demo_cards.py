"""Các Strategy Card dựng sẵn theo kiến trúc L0–L6.

Định nghĩa nằm ở tầng application để cả trang quản trị lẫn catalog release của runtime dùng
chung một nguồn; đổi đồ thị của thẻ là đổi hash canonical và vì thế đổi hash release.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading_bot.domain.strategy_card.models import (
    CompatibilityMap,
    CompositionLayer,
    DataType,
    ExecutionProfile,
    GraphEdge,
    GraphNode,
    NodeExecutionRole,
    ParameterBound,
    TypedPort,
)
from trading_bot.domain.strategy_card.schema import StrategyCard


def build_default_demo_cards() -> list[StrategyCard]:
    """Tạo các Strategy Card mẫu theo chuẩn kiến trúc L0-L6 QUANTSMART."""
    port_series = TypedPort("ohlcv_in", DataType.PRICE_SERIES, "Chuỗi nến chuẩn hóa")
    port_lower_wick = TypedPort("lower_wick_ratio", DataType.NUMERIC_SCALAR, "Tỷ lệ râu dưới")
    port_upper_wick = TypedPort("upper_wick_ratio", DataType.NUMERIC_SCALAR, "Tỷ lệ râu trên")
    port_body = TypedPort("body_ratio", DataType.NUMERIC_SCALAR, "Tỷ lệ thân nến")
    port_ema = TypedPort("ema_trend", DataType.NUMERIC_SCALAR, "Chỉ báo EMA lọc xu hướng")
    port_atr = TypedPort("atr_volatility", DataType.NUMERIC_SCALAR, "Chỉ báo ATR biến động")
    port_gate = TypedPort("regime_pass", DataType.BOOLEAN_FLAG, "Bối cảnh thị trường cho phép")
    port_trigger = TypedPort("trigger_signal", DataType.BOOLEAN_FLAG, "Ngòi nổ breakout")
    port_zone = TypedPort("candidate_order", DataType.CATEGORICAL_STATE, "Tọa độ vào lệnh và SL/TP")

    # Chiến lược 1: Donchian Trend & Volatility Breakout (Orthogonal & Clean)
    card1 = StrategyCard(
        card_id="SC-BINANCE-USDM-VOL-BREAKOUT-001",
        name="Donchian Trend & Volatility Breakout",
        version="1.0.0",
        author="Crypidian Core Quant Team",
        created_at=datetime(2026, 11, 5, 8, 30, tzinfo=UTC),
        layers=[
            CompositionLayer.L0_DATA_HYGIENE,
            CompositionLayer.L1_CANDLE_ANATOMY,
            CompositionLayer.L2_INDICATORS,
            CompositionLayer.L4_CONTEXT_GATE,
            CompositionLayer.L5_STRATEGY_TRIGGER,
            CompositionLayer.L6_ZONES,
        ],
        nodes=[
            GraphNode(
                "l0_hygiene",
                CompositionLayer.L0_DATA_HYGIENE,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "data_hygiene_check",
                {},
                (),
                (port_series,),
            ),
            GraphNode(
                "l1_wick",
                CompositionLayer.L1_CANDLE_ANATOMY,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "lower_wick_ratio",
                {},
                (port_series,),
                (port_lower_wick,),
            ),
            GraphNode(
                "l2_atr",
                CompositionLayer.L2_INDICATORS,
                NodeExecutionRole.STATEFUL_WINDOW,
                "average_true_range",
                {"period": 14},
                (port_series,),
                (port_atr,),
            ),
            GraphNode(
                "l4_gate",
                CompositionLayer.L4_CONTEXT_GATE,
                NodeExecutionRole.GATE_FILTER,
                "volatility_regime_gate",
                {"min_atr": 0.005},
                (port_atr,),
                (port_gate,),
            ),
            GraphNode(
                "l5_trigger",
                CompositionLayer.L5_STRATEGY_TRIGGER,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "breakout_candle_trigger",
                {},
                (port_gate, port_lower_wick),
                (port_trigger,),
            ),
            GraphNode(
                "l6_zones",
                CompositionLayer.L6_ZONES,
                NodeExecutionRole.TERMINAL_ZONE,
                "execution_zone_builder",
                {"risk_reward": 2.0},
                (port_trigger,),
                (port_zone,),
            ),
        ],
        edges=[
            GraphEdge("l0_hygiene", "ohlcv_in", "l1_wick", "ohlcv_in"),
            GraphEdge("l0_hygiene", "ohlcv_in", "l2_atr", "ohlcv_in"),
            GraphEdge("l2_atr", "atr_volatility", "l4_gate", "atr_volatility"),
            GraphEdge("l4_gate", "regime_pass", "l5_trigger", "regime_pass"),
            GraphEdge("l1_wick", "lower_wick_ratio", "l5_trigger", "lower_wick_ratio"),
            GraphEdge("l5_trigger", "trigger_signal", "l6_zones", "trigger_signal"),
        ],
        parameters=[
            ParameterBound("atr_period", 7.0, 30.0, 1.0, 14.0),
            ParameterBound("min_atr", 0.001, 0.05, 0.001, 0.005),
        ],
        compatibility=CompatibilityMap(
            target_venues=("binance_usdm",),
            target_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
            timeframes=("15m", "1h", "4h"),
        ),
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"),
            assumed_slippage_bps=Decimal("2.5"),
        ),
    )

    # Chiến lược 2: Adaptive Mean Reversion (Wick Rejection & EMA Filter)
    card2 = StrategyCard(
        card_id="SC-BINANCE-USDM-MEAN-REVERT-002",
        name="Adaptive Wick Rejection & Mean Reversion",
        version="1.2.0",
        author="Crypidian Core Quant Team",
        created_at=datetime(2026, 11, 5, 9, 15, tzinfo=UTC),
        layers=[
            CompositionLayer.L0_DATA_HYGIENE,
            CompositionLayer.L1_CANDLE_ANATOMY,
            CompositionLayer.L2_INDICATORS,
            CompositionLayer.L3_ALGORITHMS,
            CompositionLayer.L4_CONTEXT_GATE,
            CompositionLayer.L5_STRATEGY_TRIGGER,
            CompositionLayer.L6_ZONES,
        ],
        nodes=[
            GraphNode(
                "l0_clean",
                CompositionLayer.L0_DATA_HYGIENE,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "data_hygiene_check",
                {},
                (),
                (port_series,),
            ),
            GraphNode(
                "l1_body",
                CompositionLayer.L1_CANDLE_ANATOMY,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "body_ratio",
                {},
                (port_series,),
                (port_body,),
            ),
            GraphNode(
                "l1_upper_wick",
                CompositionLayer.L1_CANDLE_ANATOMY,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "upper_wick_ratio",
                {},
                (port_series,),
                (port_upper_wick,),
            ),
            GraphNode(
                "l2_ema",
                CompositionLayer.L2_INDICATORS,
                NodeExecutionRole.STATEFUL_WINDOW,
                "exponential_moving_average",
                {"period": 50},
                (port_series,),
                (port_ema,),
            ),
            GraphNode(
                "l3_zscore",
                CompositionLayer.L3_ALGORITHMS,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "zscore_normalizer",
                {},
                (port_ema,),
                (port_ema,),
            ),
            GraphNode(
                "l4_trend_gate",
                CompositionLayer.L4_CONTEXT_GATE,
                NodeExecutionRole.GATE_FILTER,
                "trend_alignment_gate",
                {},
                (port_ema,),
                (port_gate,),
            ),
            GraphNode(
                "l5_revert_trigger",
                CompositionLayer.L5_STRATEGY_TRIGGER,
                NodeExecutionRole.STATELESS_TRANSFORM,
                "wick_reversal_trigger",
                {},
                (port_gate, port_upper_wick),
                (port_trigger,),
            ),
            GraphNode(
                "l6_revert_zones",
                CompositionLayer.L6_ZONES,
                NodeExecutionRole.TERMINAL_ZONE,
                "mean_reversion_zone_builder",
                {},
                (port_trigger,),
                (port_zone,),
            ),
        ],
        edges=[
            GraphEdge("l0_clean", "ohlcv_in", "l1_body", "ohlcv_in"),
            GraphEdge("l0_clean", "ohlcv_in", "l1_upper_wick", "ohlcv_in"),
            GraphEdge("l0_clean", "ohlcv_in", "l2_ema", "ohlcv_in"),
            GraphEdge("l2_ema", "ema_trend", "l3_zscore", "ema_trend"),
            GraphEdge("l3_zscore", "ema_trend", "l4_trend_gate", "ema_trend"),
            GraphEdge("l4_trend_gate", "regime_pass", "l5_revert_trigger", "regime_pass"),
            GraphEdge("l1_upper_wick", "upper_wick_ratio", "l5_revert_trigger", "upper_wick_ratio"),
            GraphEdge("l5_revert_trigger", "trigger_signal", "l6_revert_zones", "trigger_signal"),
        ],
        parameters=[
            ParameterBound("ema_period", 20.0, 100.0, 5.0, 50.0),
        ],
        compatibility=CompatibilityMap(
            target_venues=("binance_usdm",),
            target_symbols=("BTCUSDT", "ETHUSDT"),
            timeframes=("5m", "15m"),
        ),
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"),
            assumed_slippage_bps=Decimal("2.0"),
        ),
    )

    return [card1, card2]

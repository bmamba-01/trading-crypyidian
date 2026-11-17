"""Bộ dựng 7 thẻ Strategy Card mở rộng L0–L6 (sửa Card 002 & thêm Cards 003–008).

Tất cả thẻ đều bảo đảm:
- Đồ thị có hướng phi chu trình (DAG) hợp lệ qua `DeadNodeDetector` (`dead_nodes = []`).
- Mỗi nút L1/L2/L3 đo đạc một chiều thông tin trực giao thực sự (giải phẫu nến, biến động,
  động lượng, sức mạnh xu hướng, hoặc dòng lệnh chủ động taker).
- Các thông số đều khớp hợp đồng `StrategyPipelineEngine`, `MicroscopeReplayService`,
  `StrategyCardStrategy` và `strategy_market_requirements.py`.
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

PORT_SERIES = TypedPort(
    name="ohlcv_in",
    data_type=DataType.PRICE_SERIES,
    description="Chuỗi nến chuẩn hóa",
)
PORT_SCALAR = TypedPort(
    name="val",
    data_type=DataType.NUMERIC_SCALAR,
    description="Đại lượng định lượng",
)
PORT_FLAG = TypedPort(
    name="flag",
    data_type=DataType.BOOLEAN_FLAG,
    description="Cờ kiểm định điều kiện",
)
PORT_ORDER = TypedPort(
    name="candidate_order",
    data_type=DataType.CATEGORICAL_STATE,
    description="Tọa độ vào lệnh và SL/TP",
)

ALL_SEVEN_LAYERS: list[CompositionLayer] = [
    CompositionLayer.L0_DATA_HYGIENE,
    CompositionLayer.L1_CANDLE_ANATOMY,
    CompositionLayer.L2_INDICATORS,
    CompositionLayer.L3_ALGORITHMS,
    CompositionLayer.L4_CONTEXT_GATE,
    CompositionLayer.L5_STRATEGY_TRIGGER,
    CompositionLayer.L6_ZONES,
]


def _make_card(
    *,
    card_id: str,
    name: str,
    version: str,
    created_at: datetime,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    parameters: list[ParameterBound],
    timeframes: tuple[str, ...],
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT"),
    slippage_bps: str = "2.0",
) -> StrategyCard:
    return StrategyCard(
        card_id=card_id,
        name=name,
        version=version,
        author="Crypidian Core Quant Team",
        created_at=created_at,
        layers=ALL_SEVEN_LAYERS,
        nodes=nodes,
        edges=edges,
        parameters=parameters,
        compatibility=CompatibilityMap(
            target_venues=("binance_usdm",),
            target_symbols=symbols,
            timeframes=timeframes,
        ),
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"),
            assumed_slippage_bps=Decimal(slippage_bps),
        ),
    )


def _card_mean_revert_002() -> StrategyCard:
    nodes = [
        GraphNode(
            "l0_clean",
            CompositionLayer.L0_DATA_HYGIENE,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "data_hygiene_check",
            outputs=(PORT_SERIES,),
        ),
        GraphNode(
            "l1_body",
            CompositionLayer.L1_CANDLE_ANATOMY,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "body_ratio",
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l1_location",
            CompositionLayer.L1_CANDLE_ANATOMY,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "close_location",
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_ema",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "exponential_moving_average",
            parameters={"period": 20},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_atr",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "average_true_range",
            parameters={"period": 14},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l3_zscore",
            CompositionLayer.L3_ALGORITHMS,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "zscore_normalizer",
            parameters={"window": 20},
            inputs=(PORT_SCALAR,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l4_trend_gate",
            CompositionLayer.L4_CONTEXT_GATE,
            NodeExecutionRole.GATE_FILTER,
            "trend_alignment_gate",
            parameters={"trend": "BIDIRECTIONAL", "min_atr": 0.005},
            inputs=(PORT_SCALAR,),
            outputs=(PORT_FLAG,),
        ),
        GraphNode(
            "l5_revert_trigger",
            CompositionLayer.L5_STRATEGY_TRIGGER,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "wick_reversal_trigger",
            parameters={"z_threshold": 1.5, "min_wick": 0.45, "max_body": 0.45},
            inputs=(PORT_FLAG, PORT_SCALAR),
            outputs=(PORT_FLAG,),
        ),
        GraphNode(
            "l6_revert_zones",
            CompositionLayer.L6_ZONES,
            NodeExecutionRole.TERMINAL_ZONE,
            "mean_reversion_zone_builder",
            parameters={"sl_multiplier": 1.5, "rr_ratio": 2.0},
            inputs=(PORT_FLAG,),
            outputs=(PORT_ORDER,),
        ),
    ]
    edges = [
        GraphEdge("l0_clean", "ohlcv_in", "l1_body", "ohlcv_in"),
        GraphEdge("l0_clean", "ohlcv_in", "l1_location", "ohlcv_in"),
        GraphEdge("l0_clean", "ohlcv_in", "l2_ema", "ohlcv_in"),
        GraphEdge("l0_clean", "ohlcv_in", "l2_atr", "ohlcv_in"),
        GraphEdge("l2_ema", "val", "l3_zscore", "val"),
        GraphEdge("l1_body", "val", "l5_revert_trigger", "val"),
        GraphEdge("l1_location", "val", "l5_revert_trigger", "val"),
        GraphEdge("l3_zscore", "val", "l4_trend_gate", "val"),
        GraphEdge("l2_atr", "val", "l4_trend_gate", "val"),
        GraphEdge("l4_trend_gate", "flag", "l5_revert_trigger", "flag"),
        GraphEdge("l3_zscore", "val", "l5_revert_trigger", "val"),
        GraphEdge("l5_revert_trigger", "flag", "l6_revert_zones", "flag"),
    ]
    return _make_card(
        card_id="SC-BINANCE-USDM-MEAN-REVERT-002",
        name="Adaptive Wick Rejection & Mean Reversion",
        version="1.2.1",
        created_at=datetime(2026, 11, 16, 9, 0, tzinfo=UTC),
        nodes=nodes,
        edges=edges,
        parameters=[
            ParameterBound(
                "ema_period", min_value=10.0, max_value=100.0, step=5.0, default_value=20.0
            ),
            ParameterBound(
                "z_threshold", min_value=1.0, max_value=3.5, step=0.25, default_value=1.5
            ),
        ],
        timeframes=("15m", "5m"),
        symbols=("BTCUSDT", "ETHUSDT"),
    )


def _card_tsmom_003() -> StrategyCard:
    nodes = [
        GraphNode(
            "l0_hygiene",
            CompositionLayer.L0_DATA_HYGIENE,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "data_hygiene_check",
            outputs=(PORT_SERIES,),
        ),
        GraphNode(
            "l1_location",
            CompositionLayer.L1_CANDLE_ANATOMY,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "close_location",
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_rv20",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "realized_volatility",
            parameters={"period": 20, "timeframe": "4h"},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_atr",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "average_true_range",
            parameters={"period": 14, "timeframe": "4h"},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l3_tsmom",
            CompositionLayer.L3_ALGORITHMS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "tsmom_composite",
            parameters={"horizons": [42, 126, 252]},
            inputs=(PORT_SCALAR,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l4_rv_gate",
            CompositionLayer.L4_CONTEXT_GATE,
            NodeExecutionRole.GATE_FILTER,
            "rv_regime_gate",
            parameters={"min_rv": 0.002, "max_rv": 0.08, "min_atr": 0.005},
            inputs=(PORT_SCALAR,),
            outputs=(PORT_FLAG,),
        ),
        GraphNode(
            "l5_tsmom_trigger",
            CompositionLayer.L5_STRATEGY_TRIGGER,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "tsmom_threshold_trigger",
            parameters={"threshold": 0.33},
            inputs=(PORT_FLAG, PORT_SCALAR),
            outputs=(PORT_FLAG,),
        ),
        GraphNode(
            "l6_zones",
            CompositionLayer.L6_ZONES,
            NodeExecutionRole.TERMINAL_ZONE,
            "atr_zone_calculator",
            parameters={"sl_multiplier": 3.0, "rr_ratio": 2.0},
            inputs=(PORT_FLAG,),
            outputs=(PORT_ORDER,),
        ),
    ]
    edges = [
        GraphEdge("l0_hygiene", "ohlcv_in", "l1_location", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_rv20", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_atr", "ohlcv_in"),
        GraphEdge("l1_location", "val", "l3_tsmom", "val"),
        GraphEdge("l2_rv20", "val", "l4_rv_gate", "val"),
        GraphEdge("l2_atr", "val", "l4_rv_gate", "val"),
        GraphEdge("l4_rv_gate", "flag", "l5_tsmom_trigger", "flag"),
        GraphEdge("l3_tsmom", "val", "l5_tsmom_trigger", "val"),
        GraphEdge("l5_tsmom_trigger", "flag", "l6_zones", "flag"),
    ]
    return _make_card(
        card_id="SC-BINANCE-USDM-TSMOM-003",
        name="Multi-Horizon Time-Series Momentum & Volatility Scaling",
        version="1.0.0",
        created_at=datetime(2026, 11, 16, 9, 5, tzinfo=UTC),
        nodes=nodes,
        edges=edges,
        parameters=[
            ParameterBound(
                "min_rv", min_value=0.001, max_value=0.01, step=0.001, default_value=0.002
            ),
            ParameterBound("max_rv", min_value=0.03, max_value=0.15, step=0.01, default_value=0.08),
        ],
        timeframes=("4h",),
    )


def _card_bbkc_squeeze_004() -> StrategyCard:
    nodes = [
        GraphNode(
            "l0_hygiene",
            CompositionLayer.L0_DATA_HYGIENE,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "data_hygiene_check",
            outputs=(PORT_SERIES,),
        ),
        GraphNode(
            "l1_body",
            CompositionLayer.L1_CANDLE_ANATOMY,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "body_ratio",
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_bb_width",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "bollinger_bandwidth",
            parameters={"period": 20},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_atr",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "average_true_range",
            parameters={"period": 14},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l3_squeeze",
            CompositionLayer.L3_ALGORITHMS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "squeeze_release_score",
            parameters={"lookback": 20, "volume_multiplier": 1.5},
            inputs=(PORT_SCALAR,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l4_squeeze_gate",
            CompositionLayer.L4_CONTEXT_GATE,
            NodeExecutionRole.GATE_FILTER,
            "squeeze_regime_gate",
            parameters={"min_atr": 0.005},
            inputs=(PORT_SCALAR,),
            outputs=(PORT_FLAG,),
        ),
        GraphNode(
            "l5_squeeze_trigger",
            CompositionLayer.L5_STRATEGY_TRIGGER,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "squeeze_breakout_trigger",
            parameters={"threshold": 0.5},
            inputs=(PORT_FLAG, PORT_SCALAR),
            outputs=(PORT_FLAG,),
        ),
        GraphNode(
            "l6_zones",
            CompositionLayer.L6_ZONES,
            NodeExecutionRole.TERMINAL_ZONE,
            "atr_zone_calculator",
            parameters={"sl_multiplier": 1.5, "rr_ratio": 2.0},
            inputs=(PORT_FLAG,),
            outputs=(PORT_ORDER,),
        ),
    ]
    edges = [
        GraphEdge("l0_hygiene", "ohlcv_in", "l1_body", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_bb_width", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_atr", "ohlcv_in"),
        GraphEdge("l1_body", "val", "l3_squeeze", "val"),
        GraphEdge("l2_bb_width", "val", "l3_squeeze", "val"),
        GraphEdge("l2_atr", "val", "l4_squeeze_gate", "val"),
        GraphEdge("l3_squeeze", "val", "l4_squeeze_gate", "val"),
        GraphEdge("l4_squeeze_gate", "flag", "l5_squeeze_trigger", "flag"),
        GraphEdge("l3_squeeze", "val", "l5_squeeze_trigger", "val"),
        GraphEdge("l5_squeeze_trigger", "flag", "l6_zones", "flag"),
    ]
    return _make_card(
        card_id="SC-BINANCE-USDM-BBKC-SQUEEZE-004",
        name="Bollinger–Keltner Volatility Squeeze Breakout",
        version="1.0.0",
        created_at=datetime(2026, 11, 16, 9, 10, tzinfo=UTC),
        nodes=nodes,
        edges=edges,
        parameters=[
            ParameterBound(
                "volume_multiplier", min_value=1.1, max_value=3.0, step=0.1, default_value=1.5
            ),
            ParameterBound(
                "sl_multiplier", min_value=1.0, max_value=3.0, step=0.25, default_value=1.5
            ),
        ],
        timeframes=("15m",),
    )


def _card_trend_pullback_005() -> StrategyCard:
    nodes = [
        GraphNode(
            "l0_hygiene",
            CompositionLayer.L0_DATA_HYGIENE,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "data_hygiene_check",
            outputs=(PORT_SERIES,),
        ),
        GraphNode(
            "l1_lower_wick",
            CompositionLayer.L1_CANDLE_ANATOMY,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "lower_wick_ratio",
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_ema20",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "exponential_moving_average",
            parameters={"period": 20},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_rsi14",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "rsi_oscillator",
            parameters={"period": 14},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_adx14",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "adx_trend_strength",
            parameters={"period": 14},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_atr",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "average_true_range",
            parameters={"period": 14},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l3_pullback",
            CompositionLayer.L3_ALGORITHMS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "pullback_alignment_score",
            parameters={"rsi_low": 40.0, "rsi_high": 60.0},
            inputs=(PORT_SCALAR,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l4_trend_gate",
            CompositionLayer.L4_CONTEXT_GATE,
            NodeExecutionRole.GATE_FILTER,
            "adx_regime_gate",
            parameters={"min_adx": 18.0, "min_atr": 0.005},
            inputs=(PORT_SCALAR,),
            outputs=(PORT_FLAG,),
        ),
        GraphNode(
            "l5_pullback_trigger",
            CompositionLayer.L5_STRATEGY_TRIGGER,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "pullback_continuation_trigger",
            parameters={"threshold": 0.5},
            inputs=(PORT_FLAG, PORT_SCALAR),
            outputs=(PORT_FLAG,),
        ),
        GraphNode(
            "l6_zones",
            CompositionLayer.L6_ZONES,
            NodeExecutionRole.TERMINAL_ZONE,
            "atr_zone_calculator",
            parameters={"sl_multiplier": 2.0, "rr_ratio": 1.5},
            inputs=(PORT_FLAG,),
            outputs=(PORT_ORDER,),
        ),
    ]
    edges = [
        GraphEdge("l0_hygiene", "ohlcv_in", "l1_lower_wick", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_ema20", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_rsi14", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_adx14", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_atr", "ohlcv_in"),
        GraphEdge("l1_lower_wick", "val", "l3_pullback", "val"),
        GraphEdge("l2_ema20", "val", "l3_pullback", "val"),
        GraphEdge("l2_rsi14", "val", "l3_pullback", "val"),
        GraphEdge("l2_adx14", "val", "l4_trend_gate", "val"),
        GraphEdge("l2_atr", "val", "l4_trend_gate", "val"),
        GraphEdge("l4_trend_gate", "flag", "l5_pullback_trigger", "flag"),
        GraphEdge("l3_pullback", "val", "l5_pullback_trigger", "val"),
        GraphEdge("l5_pullback_trigger", "flag", "l6_zones", "flag"),
    ]
    return _make_card(
        card_id="SC-BINANCE-USDM-TREND-PULLBACK-005",
        name="EMA Trend Pullback & RSI Regime Continuation",
        version="1.0.0",
        created_at=datetime(2026, 11, 16, 9, 15, tzinfo=UTC),
        nodes=nodes,
        edges=edges,
        parameters=[
            ParameterBound("min_adx", min_value=14.0, max_value=30.0, step=1.0, default_value=18.0),
            ParameterBound("rr_ratio", min_value=1.2, max_value=3.0, step=0.1, default_value=1.5),
        ],
        timeframes=("1h", "4h"),
    )


def _card_vwap_revert_006() -> StrategyCard:
    nodes = [
        GraphNode(
            "l0_hygiene",
            CompositionLayer.L0_DATA_HYGIENE,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "data_hygiene_check",
            outputs=(PORT_SERIES,),
        ),
        GraphNode(
            "l1_location",
            CompositionLayer.L1_CANDLE_ANATOMY,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "close_location",
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_vwap",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "session_vwap",
            parameters={},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_adx14",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "adx_trend_strength",
            parameters={"period": 14},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_atr",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "average_true_range",
            parameters={"period": 14},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l3_vwap_dev",
            CompositionLayer.L3_ALGORITHMS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "vwap_atr_deviation",
            parameters={},
            inputs=(PORT_SCALAR,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l4_range_gate",
            CompositionLayer.L4_CONTEXT_GATE,
            NodeExecutionRole.GATE_FILTER,
            "adx_regime_gate",
            parameters={"max_adx": 20.0, "min_atr": 0.005},
            inputs=(PORT_SCALAR,),
            outputs=(PORT_FLAG,),
        ),
        GraphNode(
            "l5_vwap_trigger",
            CompositionLayer.L5_STRATEGY_TRIGGER,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "vwap_reversion_trigger",
            parameters={"dev_threshold": 1.75},
            inputs=(PORT_FLAG, PORT_SCALAR),
            outputs=(PORT_FLAG,),
        ),
        GraphNode(
            "l6_zones",
            CompositionLayer.L6_ZONES,
            NodeExecutionRole.TERMINAL_ZONE,
            "atr_zone_calculator",
            parameters={"sl_multiplier": 1.5, "rr_ratio": 1.5},
            inputs=(PORT_FLAG,),
            outputs=(PORT_ORDER,),
        ),
    ]
    edges = [
        GraphEdge("l0_hygiene", "ohlcv_in", "l1_location", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_vwap", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_adx14", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_atr", "ohlcv_in"),
        GraphEdge("l2_vwap", "val", "l3_vwap_dev", "val"),
        GraphEdge("l2_atr", "val", "l3_vwap_dev", "val"),
        GraphEdge("l2_adx14", "val", "l4_range_gate", "val"),
        GraphEdge("l2_atr", "val", "l4_range_gate", "val"),
        GraphEdge("l4_range_gate", "flag", "l5_vwap_trigger", "flag"),
        GraphEdge("l1_location", "val", "l5_vwap_trigger", "val"),
        GraphEdge("l3_vwap_dev", "val", "l5_vwap_trigger", "val"),
        GraphEdge("l5_vwap_trigger", "flag", "l6_zones", "flag"),
    ]
    return _make_card(
        card_id="SC-BINANCE-USDM-VWAP-REVERT-006",
        name="Intraday Session VWAP & ATR Deviation Reversion",
        version="1.0.0",
        created_at=datetime(2026, 11, 16, 9, 20, tzinfo=UTC),
        nodes=nodes,
        edges=edges,
        parameters=[
            ParameterBound(
                "dev_threshold", min_value=1.25, max_value=3.0, step=0.25, default_value=1.75
            ),
            ParameterBound("max_adx", min_value=15.0, max_value=28.0, step=1.0, default_value=20.0),
        ],
        timeframes=("15m", "5m"),
        symbols=("BTCUSDT", "ETHUSDT"),
    )


def _card_macd_adx_007() -> StrategyCard:
    nodes = [
        GraphNode(
            "l0_hygiene",
            CompositionLayer.L0_DATA_HYGIENE,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "data_hygiene_check",
            outputs=(PORT_SERIES,),
        ),
        GraphNode(
            "l1_body",
            CompositionLayer.L1_CANDLE_ANATOMY,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "body_ratio",
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_macd",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "macd_histogram",
            parameters={"fast": 12, "slow": 26, "signal": 9},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_adx14",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "adx_trend_strength",
            parameters={"period": 14},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_atr",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "average_true_range",
            parameters={"period": 14},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l3_macd_score",
            CompositionLayer.L3_ALGORITHMS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "macd_momentum_score",
            parameters={},
            inputs=(PORT_SCALAR,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l4_adx_gate",
            CompositionLayer.L4_CONTEXT_GATE,
            NodeExecutionRole.GATE_FILTER,
            "adx_regime_gate",
            parameters={"min_adx": 22.0, "min_atr": 0.005},
            inputs=(PORT_SCALAR,),
            outputs=(PORT_FLAG,),
        ),
        GraphNode(
            "l5_macd_trigger",
            CompositionLayer.L5_STRATEGY_TRIGGER,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "macd_adx_trigger",
            parameters={"threshold": 0.5},
            inputs=(PORT_FLAG, PORT_SCALAR),
            outputs=(PORT_FLAG,),
        ),
        GraphNode(
            "l6_zones",
            CompositionLayer.L6_ZONES,
            NodeExecutionRole.TERMINAL_ZONE,
            "atr_zone_calculator",
            parameters={"sl_multiplier": 2.0, "rr_ratio": 2.0},
            inputs=(PORT_FLAG,),
            outputs=(PORT_ORDER,),
        ),
    ]
    edges = [
        GraphEdge("l0_hygiene", "ohlcv_in", "l1_body", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_macd", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_adx14", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_atr", "ohlcv_in"),
        GraphEdge("l1_body", "val", "l3_macd_score", "val"),
        GraphEdge("l2_macd", "val", "l3_macd_score", "val"),
        GraphEdge("l2_adx14", "val", "l4_adx_gate", "val"),
        GraphEdge("l2_atr", "val", "l4_adx_gate", "val"),
        GraphEdge("l4_adx_gate", "flag", "l5_macd_trigger", "flag"),
        GraphEdge("l3_macd_score", "val", "l5_macd_trigger", "val"),
        GraphEdge("l5_macd_trigger", "flag", "l6_zones", "flag"),
    ]
    return _make_card(
        card_id="SC-BINANCE-USDM-MACD-ADX-007",
        name="MACD Histogram + ADX Regime Confirmation Momentum",
        version="1.0.0",
        created_at=datetime(2026, 11, 16, 9, 25, tzinfo=UTC),
        nodes=nodes,
        edges=edges,
        parameters=[
            ParameterBound("min_adx", min_value=18.0, max_value=35.0, step=1.0, default_value=22.0),
        ],
        timeframes=("1h", "4h"),
    )


def _card_taker_flow_008() -> StrategyCard:
    nodes = [
        GraphNode(
            "l0_hygiene",
            CompositionLayer.L0_DATA_HYGIENE,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "data_hygiene_check",
            outputs=(PORT_SERIES,),
        ),
        GraphNode(
            "l1_location",
            CompositionLayer.L1_CANDLE_ANATOMY,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "close_location",
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_taker_flow",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "taker_flow_imbalance",
            parameters={"window": 3},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_adx14",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "adx_trend_strength",
            parameters={"period": 14},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l2_atr",
            CompositionLayer.L2_INDICATORS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "average_true_range",
            parameters={"period": 14},
            inputs=(PORT_SERIES,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l3_flow_breakout",
            CompositionLayer.L3_ALGORITHMS,
            NodeExecutionRole.STATEFUL_WINDOW,
            "taker_flow_score",
            parameters={"lookback": 20, "min_volume_multiple": 1.3},
            inputs=(PORT_SCALAR,),
            outputs=(PORT_SCALAR,),
        ),
        GraphNode(
            "l4_flow_gate",
            CompositionLayer.L4_CONTEXT_GATE,
            NodeExecutionRole.GATE_FILTER,
            "adx_regime_gate",
            parameters={"min_adx": 18.0, "min_atr": 0.005},
            inputs=(PORT_SCALAR,),
            outputs=(PORT_FLAG,),
        ),
        GraphNode(
            "l5_flow_trigger",
            CompositionLayer.L5_STRATEGY_TRIGGER,
            NodeExecutionRole.STATELESS_TRANSFORM,
            "taker_flow_breakout_trigger",
            parameters={"threshold": 0.25},
            inputs=(PORT_FLAG, PORT_SCALAR),
            outputs=(PORT_FLAG,),
        ),
        GraphNode(
            "l6_zones",
            CompositionLayer.L6_ZONES,
            NodeExecutionRole.TERMINAL_ZONE,
            "atr_zone_calculator",
            parameters={"sl_multiplier": 2.0, "rr_ratio": 2.0},
            inputs=(PORT_FLAG,),
            outputs=(PORT_ORDER,),
        ),
    ]
    edges = [
        GraphEdge("l0_hygiene", "ohlcv_in", "l1_location", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_taker_flow", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_adx14", "ohlcv_in"),
        GraphEdge("l0_hygiene", "ohlcv_in", "l2_atr", "ohlcv_in"),
        GraphEdge("l1_location", "val", "l3_flow_breakout", "val"),
        GraphEdge("l2_taker_flow", "val", "l3_flow_breakout", "val"),
        GraphEdge("l2_adx14", "val", "l4_flow_gate", "val"),
        GraphEdge("l2_atr", "val", "l4_flow_gate", "val"),
        GraphEdge("l4_flow_gate", "flag", "l5_flow_trigger", "flag"),
        GraphEdge("l3_flow_breakout", "val", "l5_flow_trigger", "val"),
        GraphEdge("l5_flow_trigger", "flag", "l6_zones", "flag"),
    ]
    return _make_card(
        card_id="SC-BINANCE-USDM-TAKER-FLOW-008",
        name="Taker Order-Flow Imbalance & Liquidity Breakout",
        version="1.0.0",
        created_at=datetime(2026, 11, 16, 9, 30, tzinfo=UTC),
        nodes=nodes,
        edges=edges,
        parameters=[
            ParameterBound(
                "min_taker_imbalance", min_value=0.1, max_value=0.6, step=0.05, default_value=0.25
            ),
            ParameterBound(
                "min_volume_multiple", min_value=1.1, max_value=2.5, step=0.1, default_value=1.3
            ),
        ],
        timeframes=("15m", "1h"),
    )


def build_expanded_strategy_cards() -> list[StrategyCard]:
    """Trả danh sách 7 Strategy Card mở rộng từ 002 đến 008."""
    return [
        _card_mean_revert_002(),
        _card_tsmom_003(),
        _card_bbkc_squeeze_004(),
        _card_trend_pullback_005(),
        _card_vwap_revert_006(),
        _card_macd_adx_007(),
        _card_taker_flow_008(),
    ]

"""Bảng đăng ký operation tường minh cho Strategy Card L0–L6 (SC-1).

Mọi node trong đồ thị Strategy Card phải khai báo một ``operation`` đã đăng ký
tại đúng tầng ``CompositionLayer`` tương ứng. Bộ kiểm định đồ thị
(``DeadNodeDetector``) và động cơ thực thi (``StrategyPipelineEngine``) cùng
tra cứu bảng này để từ chối operation lạ thay vì âm thầm bỏ qua hay dùng
nhánh dự phòng.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from trading_bot.domain.strategy_card.models import CompositionLayer


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """Đặc tả một operation hợp lệ trong đồ thị Strategy Card."""

    name: str
    layer: CompositionLayer
    canonical: str
    description_vi: str


_RAW_SPECS: tuple[OperationSpec, ...] = (
    # --- L0: Data Hygiene ---
    OperationSpec(
        "data_hygiene_check",
        CompositionLayer.L0_DATA_HYGIENE,
        "data_hygiene_check",
        "Kiểm tra độ sạch nến OHLCV.",
    ),
    OperationSpec(
        "sanitize_ohlcv",
        CompositionLayer.L0_DATA_HYGIENE,
        "data_hygiene_check",
        "Làm sạch nến OHLCV.",
    ),
    OperationSpec(
        "ohlcv_sanitizer",
        CompositionLayer.L0_DATA_HYGIENE,
        "data_hygiene_check",
        "Làm sạch chuỗi nến OHLCV.",
    ),
    OperationSpec(
        "sanitizer",
        CompositionLayer.L0_DATA_HYGIENE,
        "data_hygiene_check",
        "Bộ lọc độ sạch dữ liệu L0.",
    ),
    # --- L1: Candle Anatomy ---
    OperationSpec(
        "lower_wick_ratio",
        CompositionLayer.L1_CANDLE_ANATOMY,
        "lower_wick_ratio",
        "Tỷ lệ râu dưới trên biên độ nến.",
    ),
    OperationSpec(
        "upper_wick_ratio",
        CompositionLayer.L1_CANDLE_ANATOMY,
        "upper_wick_ratio",
        "Tỷ lệ râu trên trên biên độ nến.",
    ),
    OperationSpec(
        "body_ratio",
        CompositionLayer.L1_CANDLE_ANATOMY,
        "body_ratio",
        "Tỷ lệ thân nến trên biên độ nến.",
    ),
    OperationSpec(
        "close_location",
        CompositionLayer.L1_CANDLE_ANATOMY,
        "close_location",
        "Vị trí giá đóng cửa trong biên độ nến.",
    ),
    OperationSpec(
        "wick_rejection_strength",
        CompositionLayer.L1_CANDLE_ANATOMY,
        "wick_rejection_strength",
        "Độ mạnh từ chối giá bằng râu nến (bullish).",
    ),
    OperationSpec(
        "wick_rejection",
        CompositionLayer.L1_CANDLE_ANATOMY,
        "wick_rejection_strength",
        "Độ mạnh từ chối giá bằng râu nến (bullish).",
    ),
    OperationSpec(
        "bullish_wick_rejection_strength",
        CompositionLayer.L1_CANDLE_ANATOMY,
        "wick_rejection_strength",
        "Độ mạnh từ chối giá bằng râu dưới (bullish).",
    ),
    OperationSpec(
        "bearish_wick_rejection_strength",
        CompositionLayer.L1_CANDLE_ANATOMY,
        "bearish_wick_rejection_strength",
        "Độ mạnh từ chối giá bằng râu trên (bearish).",
    ),
    OperationSpec(
        "normalized_candle_range",
        CompositionLayer.L1_CANDLE_ANATOMY,
        "normalized_candle_range",
        "Biên độ nến chuẩn hóa theo ATR.",
    ),
    # --- L2: Indicators ---
    OperationSpec(
        "average_true_range",
        CompositionLayer.L2_INDICATORS,
        "atr",
        "Chỉ báo biên độ dao động thực trung bình (ATR).",
    ),
    OperationSpec("atr", CompositionLayer.L2_INDICATORS, "atr", "Chỉ báo ATR."),
    OperationSpec("atr_calculator", CompositionLayer.L2_INDICATORS, "atr", "Tính toán ATR."),
    OperationSpec(
        "true_range", CompositionLayer.L2_INDICATORS, "atr", "Biên độ thực (True Range / ATR)."
    ),
    OperationSpec(
        "exponential_moving_average",
        CompositionLayer.L2_INDICATORS,
        "ema",
        "Trung bình trượt hàm mũ (EMA).",
    ),
    OperationSpec("ema", CompositionLayer.L2_INDICATORS, "ema", "Trung bình trượt hàm mũ (EMA)."),
    OperationSpec(
        "simple_moving_average",
        CompositionLayer.L2_INDICATORS,
        "sma",
        "Trung bình trượt đơn giản (SMA).",
    ),
    OperationSpec("sma", CompositionLayer.L2_INDICATORS, "sma", "Trung bình trượt đơn giản (SMA)."),
    OperationSpec(
        "rsi_oscillator", CompositionLayer.L2_INDICATORS, "rsi", "Chỉ báo sức mạnh tương đối (RSI)."
    ),
    OperationSpec("rsi", CompositionLayer.L2_INDICATORS, "rsi", "Chỉ báo RSI."),
    OperationSpec(
        "adx_trend_strength",
        CompositionLayer.L2_INDICATORS,
        "adx",
        "Chỉ báo độ mạnh xu hướng (ADX).",
    ),
    OperationSpec("adx", CompositionLayer.L2_INDICATORS, "adx", "Chỉ báo ADX."),
    OperationSpec(
        "realized_volatility",
        CompositionLayer.L2_INDICATORS,
        "realized_volatility",
        "Độ biến động thực tế (RV).",
    ),
    OperationSpec(
        "rv", CompositionLayer.L2_INDICATORS, "realized_volatility", "Độ biến động thực tế (RV)."
    ),
    OperationSpec(
        "bollinger_bandwidth",
        CompositionLayer.L2_INDICATORS,
        "bollinger_bandwidth",
        "Độ rộng dải Bollinger chuẩn hóa.",
    ),
    OperationSpec(
        "bb_width", CompositionLayer.L2_INDICATORS, "bollinger_bandwidth", "Độ rộng dải Bollinger."
    ),
    OperationSpec(
        "session_vwap",
        CompositionLayer.L2_INDICATORS,
        "vwap",
        "Giá trung bình theo khối lượng phiên UTC (Session VWAP).",
    ),
    OperationSpec(
        "vwap", CompositionLayer.L2_INDICATORS, "vwap", "Giá trung bình theo khối lượng (VWAP)."
    ),
    OperationSpec(
        "macd_histogram",
        CompositionLayer.L2_INDICATORS,
        "macd",
        "Biểu đồ cột phân kỳ hội tụ trung bình động (MACD Histogram).",
    ),
    OperationSpec("macd", CompositionLayer.L2_INDICATORS, "macd", "Chỉ báo MACD."),
    OperationSpec(
        "taker_flow_imbalance",
        CompositionLayer.L2_INDICATORS,
        "taker_flow",
        "Mất cân bằng dòng lệnh chủ động (Taker Flow).",
    ),
    OperationSpec(
        "taker_flow",
        CompositionLayer.L2_INDICATORS,
        "taker_flow",
        "Dòng lệnh chủ động (Taker Flow).",
    ),
    # --- L3: Algorithms ---
    OperationSpec(
        "zscore_normalizer", CompositionLayer.L3_ALGORITHMS, "zscore", "Chuẩn hóa Z-Score."
    ),
    OperationSpec("zscore", CompositionLayer.L3_ALGORITHMS, "zscore", "Chuẩn hóa Z-Score."),
    OperationSpec(
        "tsmom_composite",
        CompositionLayer.L3_ALGORITHMS,
        "tsmom",
        "Động lượng chuỗi thời gian đa chân trời (TSMOM).",
    ),
    OperationSpec(
        "tsmom", CompositionLayer.L3_ALGORITHMS, "tsmom", "Động lượng chuỗi thời gian (TSMOM)."
    ),
    OperationSpec(
        "squeeze_release_score",
        CompositionLayer.L3_ALGORITHMS,
        "squeeze",
        "Điểm bùng nổ sau nén Bollinger–Keltner.",
    ),
    OperationSpec(
        "squeeze_release", CompositionLayer.L3_ALGORITHMS, "squeeze", "Điểm bùng nổ nén."
    ),
    OperationSpec(
        "squeeze", CompositionLayer.L3_ALGORITHMS, "squeeze", "Điểm nén Bollinger–Keltner."
    ),
    OperationSpec(
        "pullback_alignment_score",
        CompositionLayer.L3_ALGORITHMS,
        "pullback",
        "Điểm đồng thuận nhịp hồi về EMA.",
    ),
    OperationSpec(
        "pullback", CompositionLayer.L3_ALGORITHMS, "pullback", "Điểm nhịp hồi xu hướng."
    ),
    OperationSpec(
        "vwap_atr_deviation",
        CompositionLayer.L3_ALGORITHMS,
        "vwap_dev",
        "Độ lệch giá khỏi VWAP chuẩn hóa theo ATR.",
    ),
    OperationSpec("vwap_dev", CompositionLayer.L3_ALGORITHMS, "vwap_dev", "Độ lệch VWAP theo ATR."),
    OperationSpec(
        "macd_momentum_score",
        CompositionLayer.L3_ALGORITHMS,
        "macd_momentum",
        "Điểm động lượng chuyển pha MACD.",
    ),
    OperationSpec(
        "macd_momentum", CompositionLayer.L3_ALGORITHMS, "macd_momentum", "Điểm động lượng MACD."
    ),
    OperationSpec(
        "taker_flow_score",
        CompositionLayer.L3_ALGORITHMS,
        "taker_flow_score",
        "Điểm phá ngưỡng xác nhận bằng Taker Flow.",
    ),
    OperationSpec(
        "score_normalizer",
        CompositionLayer.L3_ALGORITHMS,
        "score_normalizer",
        "Tổng hợp chuẩn hóa điểm L3.",
    ),
    OperationSpec(
        "composite_score",
        CompositionLayer.L3_ALGORITHMS,
        "score_normalizer",
        "Tổng hợp điểm số L3.",
    ),
    # --- L4: Context Gate ---
    OperationSpec(
        "volatility_regime_gate",
        CompositionLayer.L4_CONTEXT_GATE,
        "volatility_gate",
        "Cổng kiểm tra biến động ATR / ATR%.",
    ),
    OperationSpec(
        "volatility_gate", CompositionLayer.L4_CONTEXT_GATE, "volatility_gate", "Cổng biến động."
    ),
    OperationSpec("atr_gate", CompositionLayer.L4_CONTEXT_GATE, "volatility_gate", "Cổng ATR."),
    OperationSpec(
        "rv_regime_gate",
        CompositionLayer.L4_CONTEXT_GATE,
        "rv_regime_gate",
        "Cổng chế độ biến động thực tế (RV).",
    ),
    OperationSpec(
        "realized_volatility_gate", CompositionLayer.L4_CONTEXT_GATE, "rv_regime_gate", "Cổng RV."
    ),
    OperationSpec(
        "adx_regime_gate",
        CompositionLayer.L4_CONTEXT_GATE,
        "adx_regime_gate",
        "Cổng chế độ xu hướng theo ADX.",
    ),
    OperationSpec("adx_gate", CompositionLayer.L4_CONTEXT_GATE, "adx_regime_gate", "Cổng ADX."),
    OperationSpec(
        "squeeze_regime_gate",
        CompositionLayer.L4_CONTEXT_GATE,
        "squeeze_regime_gate",
        "Cổng bối cảnh nén/ATR.",
    ),
    OperationSpec(
        "trend_alignment_gate",
        CompositionLayer.L4_CONTEXT_GATE,
        "trend_gate",
        "Cổng đồng thuận xu hướng theo EMA.",
    ),
    OperationSpec("trend_gate", CompositionLayer.L4_CONTEXT_GATE, "trend_gate", "Cổng xu hướng."),
    OperationSpec("ema_gate", CompositionLayer.L4_CONTEXT_GATE, "trend_gate", "Cổng EMA."),
    # --- L5: Strategy Trigger ---
    OperationSpec(
        "wick_reversal_trigger",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        "wick_reversal_trigger",
        "Ngòi nổ đảo chiều râu nến kèm Z-Score.",
    ),
    OperationSpec(
        "vwap_reversion_trigger",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        "vwap_reversion_trigger",
        "Ngòi nổ hồi về VWAP.",
    ),
    OperationSpec(
        "tsmom_threshold_trigger",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        "tsmom_threshold_trigger",
        "Ngòi nổ ngưỡng TSMOM.",
    ),
    OperationSpec(
        "squeeze_breakout_trigger",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        "squeeze_breakout_trigger",
        "Ngòi nổ phá vỡ nén.",
    ),
    OperationSpec(
        "pullback_continuation_trigger",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        "pullback_continuation_trigger",
        "Ngòi nổ tiếp diễn nhịp hồi.",
    ),
    OperationSpec(
        "macd_adx_trigger",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        "macd_adx_trigger",
        "Ngòi nổ MACD + ADX.",
    ),
    OperationSpec(
        "taker_flow_breakout_trigger",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        "taker_flow_breakout_trigger",
        "Ngòi nổ phá ngưỡng Taker Flow.",
    ),
    OperationSpec(
        "breakout_candle_trigger",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        "breakout_candle_trigger",
        "Ngòi nổ nến phá ngưỡng.",
    ),
    OperationSpec(
        "rejection_breakout_trigger",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        "threshold_trigger",
        "Ngòi nổ từ chối / phá ngưỡng.",
    ),
    OperationSpec(
        "rejection_trigger",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        "threshold_trigger",
        "Ngòi nổ từ chối theo ngưỡng.",
    ),
    OperationSpec(
        "threshold_trigger",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        "threshold_trigger",
        "Ngòi nổ vượt ngưỡng.",
    ),
    OperationSpec(
        "threshold",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        "threshold_trigger",
        "Ngòi nổ vượt ngưỡng.",
    ),
    OperationSpec(
        "trigger", CompositionLayer.L5_STRATEGY_TRIGGER, "threshold_trigger", "Ngòi nổ kích hoạt."
    ),
    OperationSpec(
        "crossover_trigger",
        CompositionLayer.L5_STRATEGY_TRIGGER,
        "crossover_trigger",
        "Ngòi nổ giao cắt hai đường chỉ báo.",
    ),
    OperationSpec(
        "crossover", CompositionLayer.L5_STRATEGY_TRIGGER, "crossover_trigger", "Ngòi nổ giao cắt."
    ),
    # --- L6: Execution Zones ---
    OperationSpec(
        "execution_zone_builder",
        CompositionLayer.L6_ZONES,
        "execution_zone_builder",
        "Dựng vùng giá vào/stop/mục tiêu theo ATR.",
    ),
    OperationSpec(
        "mean_reversion_zone_builder",
        CompositionLayer.L6_ZONES,
        "execution_zone_builder",
        "Dựng vùng giá hồi quy trung bình theo ATR.",
    ),
    OperationSpec(
        "atr_zone_calculator",
        CompositionLayer.L6_ZONES,
        "execution_zone_builder",
        "Tính vùng giá theo ATR.",
    ),
    OperationSpec(
        "atr_zones", CompositionLayer.L6_ZONES, "execution_zone_builder", "Tính vùng giá theo ATR."
    ),
    OperationSpec(
        "zone_builder", CompositionLayer.L6_ZONES, "execution_zone_builder", "Bộ dựng vùng giá L6."
    ),
)

OPERATION_REGISTRY: Mapping[str, OperationSpec] = MappingProxyType(
    {spec.name: spec for spec in _RAW_SPECS}
)


def resolve_operation(
    operation_name: str,
    layer: CompositionLayer | str | None = None,
) -> OperationSpec | None:
    """Tra cứu operation trong bảng đăng ký; trả ``None`` nếu chưa đăng ký hoặc sai tầng."""
    key = operation_name.strip().lower()
    spec = OPERATION_REGISTRY.get(key)
    if spec is None:
        return None
    if layer is not None:
        layer_val = layer.value if isinstance(layer, CompositionLayer) else str(layer)
        # Chấp nhận tên chuẩn hoặc bí danh L2_INDICATOR / L6_EXECUTION_ZONE trong test cũ.
        normalized = layer_val.upper()
        if spec.layer.value != layer_val and not normalized.startswith(spec.layer.value[:2]):
            return None
    return spec


def is_registered_operation(
    operation_name: str,
    layer: CompositionLayer | str | None = None,
) -> bool:
    """Kiểm tra ``operation_name`` đã đăng ký tường minh (và đúng tầng nếu truyền ``layer``)."""
    return resolve_operation(operation_name, layer=layer) is not None

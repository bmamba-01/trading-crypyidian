"""Unit tests cho Verification Engine của Strategy Card (WP-SC.2).

Bao gồm:
- AntiConsensusAnalyzer và calculate_correlation_matrix (kiểm tra ảo giác đồng thuận).
- DeadNodeDetector (phát hiện nút mồ côi, ngõ cụt logic và chu trình).
- CoordinateLabEngine (quét lưới tham số, phát hiện điểm kỳ dị và độ nhạy gradient).
"""

from __future__ import annotations

import math

import pytest

from trading_bot.domain.strategy_card.geometry_lab import CoordinateLabEngine
from trading_bot.domain.strategy_card.graph_verifier import DeadNodeDetector
from trading_bot.domain.strategy_card.orthogonality import (
    AntiConsensusAnalyzer,
    calculate_correlation_matrix,
)

# ============================================================================
# 1. Tests cho calculate_correlation_matrix & AntiConsensusAnalyzer
# ============================================================================


def test_calculate_correlation_matrix_with_perfect_and_inverse_correlation() -> None:
    """Kiểm tra ma trận tương quan Pearson với chuỗi hoàn hảo và nghịch biến."""
    series = {
        "x": [1.0, 2.0, 3.0, 4.0, 5.0],
        "y": [2.0, 4.0, 6.0, 8.0, 10.0],  # y = 2x => r = 1.0
        "z": [5.0, 4.0, 3.0, 2.0, 1.0],  # z = -x + 6 => r = -1.0
    }
    matrix = calculate_correlation_matrix(series)

    assert pytest.approx(matrix[("x", "y")], rel=1e-5) == 1.0
    assert pytest.approx(matrix[("y", "x")], rel=1e-5) == 1.0
    assert pytest.approx(matrix[("x", "z")], rel=1e-5) == -1.0
    assert pytest.approx(matrix[("x", "x")], rel=1e-5) == 1.0


def test_calculate_correlation_matrix_validates_equal_lengths() -> None:
    """Kiểm tra ngoại lệ khi các chuỗi có độ dài không bằng nhau."""
    series = {
        "a": [1.0, 2.0, 3.0],
        "b": [1.0, 2.0],
    }
    with pytest.raises(ValueError, match="cùng độ dài"):
        calculate_correlation_matrix(series)


def test_anti_consensus_analyzer_detects_high_correlation_signals() -> None:
    """Kiểm tra phát hiện ảo giác đồng thuận giữa RSI và Stochastic (r >= 0.85)."""
    # Tạo 2 chuỗi có tương quan rất cao (~0.95+)
    rsi_series = [30.0, 35.0, 42.0, 50.0, 58.0, 65.0, 72.0, 80.0]
    stoch_series = [28.0, 34.0, 40.0, 52.0, 60.0, 63.0, 75.0, 82.0]
    atr_series = [1.2, 1.1, 1.5, 2.0, 1.8, 1.3, 1.4, 1.2]  # Không tương quan với RSI/Stoch

    outputs = {
        "rsi_node": rsi_series,
        "stoch_node": stoch_series,
        "atr_node": atr_series,
    }

    analyzer = AntiConsensusAnalyzer()
    warnings = analyzer.analyze_signals(outputs, threshold=0.85)

    # Phải có cảnh báo giữa rsi_node và stoch_node
    assert len(warnings) >= 1
    found_rsi_stoch_warning = any(
        "rsi_node" in w and "stoch_node" in w and "ẢO GIÁC ĐỒNG THUẬN" in w for w in warnings
    )
    assert found_rsi_stoch_warning

    # Không được có cảnh báo giữa rsi_node và atr_node
    assert not any("rsi_node" in w and "atr_node" in w for w in warnings)


def test_anti_consensus_analyzer_orthogonal_signals_no_warnings() -> None:
    """Kiểm tra khi các chỉ báo trực giao (không tương quan) thì không xuất cảnh báo."""
    # Hai chuỗi trực giao/ít tương quan
    series_a = [10.0, 20.0, 10.0, 20.0, 10.0, 20.0]
    series_b = [5.0, 5.0, 15.0, 15.0, 5.0, 5.0]

    analyzer = AntiConsensusAnalyzer()
    report = analyzer.check_orthogonality({"ind_a": series_a, "ind_b": series_b}, threshold=0.85)

    assert report.is_orthogonal is True
    assert len(report.warnings) == 0
    assert len(report.high_correlation_pairs) == 0


# ============================================================================
# 2. Tests cho DeadNodeDetector
# ============================================================================


def test_dead_node_detector_identifies_unwired_orphan_node() -> None:
    """Đồ thị có 1 node L2 không nối tới L3/L4/L5/L6 -> phát hiện chính xác nút mồ côi."""
    nodes = [
        {"node_id": "clean_candle", "layer": "L0_DATA_HYGIENE", "operator_name": "sanitizer"},
        {"node_id": "ema_20", "layer": "L2_INDICATOR", "operator_name": "ema"},
        {"node_id": "orphan_rsi", "layer": "L2_INDICATOR", "operator_name": "rsi"},  # Mồ côi!
        {"node_id": "trend_filter", "layer": "L4_CONTEXT_GATE", "operator_name": "trend_gate"},
        {"node_id": "breakout_trigger", "layer": "L5_STRATEGY_TRIGGER", "operator_name": "trigger"},
        {"node_id": "risk_zones", "layer": "L6_EXECUTION_ZONE", "operator_name": "zone_builder"},
    ]
    edges = [
        {"from_node": "clean_candle", "to_node": "ema_20"},
        {"from_node": "clean_candle", "to_node": "orphan_rsi"},
        {"from_node": "ema_20", "to_node": "trend_filter"},
        {"from_node": "trend_filter", "to_node": "breakout_trigger"},
        {"from_node": "breakout_trigger", "to_node": "risk_zones"},
    ]

    detector = DeadNodeDetector()
    warnings = detector.detect_dead_nodes(nodes, edges)

    assert any("orphan_rsi" in w and "Dead Node" in w for w in warnings)
    # Các node nằm trên đường truyền đến L6 không được bị cảnh báo dead node
    assert not any("ema_20" in w for w in warnings)
    assert not any("risk_zones" in w for w in warnings)


def test_dead_node_detector_valid_connected_graph_has_no_dead_nodes() -> None:
    """Đồ thị chuẩn nối liền từ L0 tới L6 -> không có dead node nào."""
    nodes = [
        {"node_id": "l0_feed", "layer": "L0_DATA_HYGIENE"},
        {"node_id": "l1_wick", "layer": "L1_ANATOMY_FEATURE"},
        {"node_id": "l2_sma", "layer": "L2_INDICATOR"},
        {"node_id": "l4_gate", "layer": "L4_CONTEXT_GATE"},
        {"node_id": "l5_trig", "layer": "L5_STRATEGY_TRIGGER"},
        {"node_id": "l6_zone", "layer": "L6_EXECUTION_ZONE"},
    ]
    edges = [
        {"from_node": "l0_feed", "to_node": "l1_wick"},
        {"from_node": "l0_feed", "to_node": "l2_sma"},
        {"from_node": "l1_wick", "to_node": "l4_gate"},
        {"from_node": "l2_sma", "to_node": "l4_gate"},
        {"from_node": "l4_gate", "to_node": "l5_trig"},
        {"from_node": "l5_trig", "to_node": "l6_zone"},
    ]

    detector = DeadNodeDetector()
    warnings = detector.detect_dead_nodes(nodes, edges)

    assert len(warnings) == 0


def test_dead_node_detector_detects_logic_dead_ends() -> None:
    """Phát hiện cổng logic có giá trị cố định triệt tiêu (AND False hoặc OR True)."""
    nodes = [
        {"node_id": "l0_feed", "layer": "L0_DATA_HYGIENE"},
        {
            "node_id": "logic_and_node",
            "layer": "L4_CONTEXT_GATE",
            "operator_name": "AND",
            "parameters": {"constant_operand": False},  # Triệt tiêu! Luôn luôn False
            "input_ports": ["in_signal"],
        },
        {"node_id": "l6_zone", "layer": "L6_EXECUTION_ZONE"},
    ]
    edges = [
        {"from_node": "l0_feed", "to_node": "logic_and_node"},
        {"from_node": "logic_and_node", "to_node": "l6_zone"},
    ]

    detector = DeadNodeDetector()
    warnings = detector.detect_dead_nodes(nodes, edges)

    assert any("NGÕ CỤT LOGIC" in w and "logic_and_node" in w for w in warnings)


def test_dead_node_detector_detects_cycles() -> None:
    """Phát hiện chu trình vòng lặp trong đồ thị."""
    nodes = [
        {"node_id": "n1", "layer": "L2_INDICATOR"},
        {"node_id": "n2", "layer": "L2_INDICATOR"},
        {"node_id": "n3", "layer": "L6_EXECUTION_ZONE"},
    ]
    edges = [
        {"from_node": "n1", "to_node": "n2"},
        {"from_node": "n2", "to_node": "n1"},  # Vòng lặp n1 <-> n2
        {"from_node": "n2", "to_node": "n3"},
    ]

    detector = DeadNodeDetector()
    warnings = detector.detect_dead_nodes(nodes, edges)

    assert any("CHU TRÌNH" in w for w in warnings)


# ============================================================================
# 3. Tests cho CoordinateLabEngine
# ============================================================================


def test_coordinate_lab_sweep_parameter_grid() -> None:
    """Kiểm tra quét lưới không gian tham số và tính ma trận bề mặt 2D."""

    # Hàm ví dụ: f(lookback, mult) = lookback * mult
    def sample_formula(lookback: float, mult: float) -> float:
        return lookback * mult

    bounds = {
        "lookback": (10.0, 30.0, 3),  # 10.0, 20.0, 30.0
        "mult": (1.0, 3.0, 3),  # 1.0, 2.0, 3.0
    }

    engine = CoordinateLabEngine()
    result = engine.sweep_parameter_grid(sample_formula, bounds)

    assert result["param_names"] == ["lookback", "mult"]
    assert len(result["evaluated_points"]) == 9
    assert result["grid_axes"]["lookback"] == [10.0, 20.0, 30.0]
    assert result["grid_axes"]["mult"] == [1.0, 2.0, 3.0]

    # Kiểm tra surface_matrix 3x3
    surface = result["surface_matrix"]
    assert surface is not None
    assert len(surface) == 3
    assert len(surface[0]) == 3
    assert surface[0][0] == 10.0 * 1.0
    assert surface[2][2] == 30.0 * 3.0


def test_coordinate_lab_detects_division_by_zero_singularity() -> None:
    """Thử công thức có mẫu số (High - Low): khi High = Low, phát hiện điểm kỳ dị chia cho 0."""

    def candle_range_formula(high: float, low: float, close: float) -> float:
        return (close - low) / (high - low)

    test_cases = [
        {"high": 105.0, "low": 95.0, "close": 100.0},  # Bình thường: 5 / 10 = 0.5
        {"high": 100.0, "low": 100.0, "close": 100.0},  # Nến phẳng: High = Low -> Chia cho 0!
        {"high": 110.0, "low": 100.0, "close": 105.0},  # Bình thường
    ]

    engine = CoordinateLabEngine()
    singularities = engine.detect_singularities(candle_range_formula, test_cases)

    assert len(singularities) == 1
    singularity = singularities[0]
    assert singularity["singularity_type"] == "ZERO_DIVISION"
    assert singularity["params"] == {"high": 100.0, "low": 100.0, "close": 100.0}
    assert "safe_default" in singularity


def test_coordinate_lab_gradient_sensitivity_smooth_vs_fragile() -> None:
    """Đo gradient sensitivity của một hàm trơn (nhẹ) và một hàm răng cưa (độ nhạy cực cao)."""

    # Hàm trơn: f(x) = 2.0 * x  => df/dx = 2.0
    def smooth_fn(x: float) -> float:
        return 2.0 * x

    # Hàm dốc dựng đứng (Knife-edge / Fragile): f(x) = 100000.0 * x  => df/dx = 100000.0
    def fragile_fn(x: float) -> float:
        return 100_000.0 * x

    engine = CoordinateLabEngine()

    smooth_grad = engine.calculate_gradient_sensitivity(smooth_fn, {"x": 10.0})
    assert pytest.approx(smooth_grad["x"], rel=1e-3) == 2.0

    fragile_grad = engine.calculate_gradient_sensitivity(fragile_fn, {"x": 10.0})
    assert pytest.approx(fragile_grad["x"], rel=1e-3) == 100_000.0

    # Hàm phân tích tính mỏng manh (Knife-edge fragility evaluation)
    smooth_eval = engine.evaluate_fragility(smooth_fn, {"x": 10.0}, gradient_threshold=1000.0)
    assert smooth_eval["is_fragile"] is False
    assert len(smooth_eval["warnings"]) == 0

    fragile_eval = engine.evaluate_fragility(fragile_fn, {"x": 10.0}, gradient_threshold=1000.0)
    assert fragile_eval["is_fragile"] is True
    assert len(fragile_eval["warnings"]) >= 1
    assert "BỀ MẶT NHẠY CẢM NGUY HIỂM" in fragile_eval["warnings"][0]


# ============================================================================
# 4. Tests bổ sung cho Dataclass Models & Edge Cases
# ============================================================================


def test_dead_node_detector_with_graph_node_and_edge_dataclasses() -> None:
    """Kiểm tra DeadNodeDetector hoạt động chuẩn mực với GraphNode và GraphEdge dataclasses."""
    from trading_bot.domain.strategy_card.models import (
        CompositionLayer,
        DataType,
        GraphEdge,
        GraphNode,
        NodeExecutionRole,
        TypedPort,
    )

    port_in = TypedPort(name="in", data_type=DataType.PRICE_SERIES)
    port_out = TypedPort(name="out", data_type=DataType.NUMERIC_SCALAR)

    nodes = [
        GraphNode(
            node_id="l0_data",
            layer=CompositionLayer.L0_DATA_HYGIENE,
            role=NodeExecutionRole.STATELESS_TRANSFORM,
            operation="sanitize",
            outputs=(port_out,),
        ),
        GraphNode(
            node_id="l2_connected",
            layer=CompositionLayer.L2_INDICATORS,
            role=NodeExecutionRole.STATEFUL_WINDOW,
            operation="ema",
            inputs=(port_in,),
            outputs=(port_out,),
        ),
        GraphNode(
            node_id="l2_orphan",
            layer=CompositionLayer.L2_INDICATORS,
            role=NodeExecutionRole.STATEFUL_WINDOW,
            operation="atr",
            inputs=(port_in,),
            outputs=(port_out,),
        ),
        GraphNode(
            node_id="l6_exit",
            layer=CompositionLayer.L6_ZONES,
            role=NodeExecutionRole.TERMINAL_ZONE,
            operation="zones",
            inputs=(port_in,),
        ),
    ]

    edges = [
        GraphEdge(
            from_node="l0_data",
            from_port="out",
            to_node="l2_connected",
            to_port="in",
        ),
        GraphEdge(
            from_node="l0_data",
            from_port="out",
            to_node="l2_orphan",
            to_port="in",
        ),
        GraphEdge(
            from_node="l2_connected",
            from_port="out",
            to_node="l6_exit",
            to_port="in",
        ),
    ]

    detector = DeadNodeDetector()
    report = detector.verify(nodes, edges)

    assert report.is_valid is False
    assert "l2_orphan" in report.dead_nodes
    assert "l2_connected" not in report.dead_nodes
    assert "l6_exit" not in report.dead_nodes


def test_anti_consensus_analyzer_constant_and_empty_series() -> None:
    """Kiểm tra xử lý chuỗi rỗng và chuỗi hằng số (variance = 0) không bị lỗi chia cho 0."""
    assert calculate_correlation_matrix({}) == {}

    # Chuỗi hằng số
    series_constant = {
        "flat_line": [5.0, 5.0, 5.0, 5.0],
        "varying": [1.0, 2.0, 3.0, 4.0],
    }
    matrix = calculate_correlation_matrix(series_constant)
    assert matrix[("flat_line", "varying")] == 0.0
    assert matrix[("flat_line", "flat_line")] == 1.0


def test_coordinate_lab_nan_inf_math_domain_singularities() -> None:
    """Kiểm tra phát hiện NaN, vô cực và lỗi miền giá trị toán học (Domain error)."""

    def formula_with_singularities(a: float, b: float) -> float:
        if a < 0:
            return math.sqrt(a)  # Gây ValueError (math domain error)
        if b == 0:
            return float("inf")  # Trả về vô cực
        return a / b

    test_cases = [
        {"a": -4.0, "b": 2.0},  # ValueError: math domain error
        {"a": 10.0, "b": 0.0},  # float("inf")
    ]

    engine = CoordinateLabEngine()
    singularities = engine.detect_singularities(formula_with_singularities, test_cases)

    assert len(singularities) == 2
    types = [s["singularity_type"] for s in singularities]
    assert "MATH_DOMAIN_ERROR" in types
    assert "INFINITY_VALUE" in types

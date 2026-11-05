"""Gói domain Strategy Card (WP-SC) lấy cảm hứng từ QUANTSMART.

Bao gồm:
- models: Enums, TypedPort, GraphNode, GraphEdge, ParameterBound, CompatibilityMap.
- anatomy: Các hàm giải phẫu nến (lower_wick, upper_wick, body_ratio, close_location).
- schema: StrategyCard Pydantic v2 model và compute_canonical_hash.
- orthogonality: Phân tích trực giao và chống ảo giác đồng thuận (Anti-Consensus).
- graph_verifier: Thẩm định đồ thị, phát hiện nút chết (Dead Node), ngõ cụt logic.
- geometry_lab: Phòng thí nghiệm toán học tọa độ, quét lưới tham số và độ dốc gradient.
"""

from __future__ import annotations

from trading_bot.domain.strategy_card.anatomy import (
    body_ratio,
    close_location,
    lower_wick_ratio,
    upper_wick_ratio,
    wick_rejection_strength,
)
from trading_bot.domain.strategy_card.geometry_lab import CoordinateLabEngine
from trading_bot.domain.strategy_card.graph_verifier import (
    DeadNodeDetector,
    GraphVerificationReport,
)
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
from trading_bot.domain.strategy_card.orthogonality import (
    AntiConsensusAnalyzer,
    OrthogonalityReport,
    calculate_correlation_matrix,
)
from trading_bot.domain.strategy_card.schema import (
    StrategyCard,
    compute_canonical_hash,
)

__all__ = [
    "AntiConsensusAnalyzer",
    "CompatibilityMap",
    "CompositionLayer",
    "CoordinateLabEngine",
    "DataType",
    "DeadNodeDetector",
    "ExecutionProfile",
    "GraphEdge",
    "GraphNode",
    "GraphVerificationReport",
    "NodeExecutionRole",
    "OrthogonalityReport",
    "ParameterBound",
    "StrategyCard",
    "TypedPort",
    "body_ratio",
    "calculate_correlation_matrix",
    "close_location",
    "compute_canonical_hash",
    "lower_wick_ratio",
    "upper_wick_ratio",
    "wick_rejection_strength",
]

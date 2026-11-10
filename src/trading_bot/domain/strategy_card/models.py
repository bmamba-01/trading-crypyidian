"""Mô hình miền (Domain Models) cho Strategy Card theo kiến trúc QUANTSMART.

Các thành phần cốt lõi:
- CompositionLayer: Phân tầng 7 lớp chuẩn mực (L0 đến L6).
- DataType: Kiểu dữ liệu luân chuyển qua các cổng kết nối.
- NodeExecutionRole: Vai trò thực thi của node trong đồ thị.
- TypedPort, GraphNode, GraphEdge: Cấu trúc đồ thị tính toán có hướng.
- ParameterBound, CompatibilityMap, ExecutionProfile: Metadata tham số và môi trường thực thi.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


class CompositionLayer(StrEnum):
    """Phân tầng 7 lớp chuẩn mực L0 đến L6 cho luồng xử lý định lượng."""

    L0_DATA_HYGIENE = "L0_DATA_HYGIENE"
    L1_CANDLE_ANATOMY = "L1_CANDLE_ANATOMY"
    L2_INDICATORS = "L2_INDICATORS"
    L3_ALGORITHMS = "L3_ALGORITHMS"
    L4_CONTEXT_GATE = "L4_CONTEXT_GATE"
    L5_STRATEGY_TRIGGER = "L5_STRATEGY_TRIGGER"
    L6_ZONES = "L6_ZONES"


class DataType(StrEnum):
    """Kiểu dữ liệu định kiểu cho các cổng kết nối (Typed Ports)."""

    PRICE_SERIES = "PRICE_SERIES"
    NUMERIC_SCALAR = "NUMERIC_SCALAR"
    BOOLEAN_FLAG = "BOOLEAN_FLAG"
    CATEGORICAL_STATE = "CATEGORICAL_STATE"


class NodeExecutionRole(StrEnum):
    """Vai trò thực thi của nút trong đồ thị."""

    STATELESS_TRANSFORM = "STATELESS_TRANSFORM"
    STATEFUL_WINDOW = "STATEFUL_WINDOW"
    GATE_FILTER = "GATE_FILTER"
    TERMINAL_ZONE = "TERMINAL_ZONE"


@dataclass(frozen=True)
class TypedPort:
    """Cổng kết nối định kiểu dữ liệu vào hoặc ra của một node."""

    name: str
    data_type: DataType
    description: str = ""


@dataclass(frozen=True)
class GraphNode:
    """Khối xử lý nguyên tử trong đồ thị chiến lược."""

    node_id: str
    layer: CompositionLayer
    role: NodeExecutionRole
    operation: str
    parameters: dict[str, Any] = field(default_factory=dict)
    inputs: tuple[TypedPort, ...] = ()
    outputs: tuple[TypedPort, ...] = ()


@dataclass(frozen=True)
class GraphEdge:
    """Đường truyền dữ liệu giữa cổng ra của node trước tới cổng vào của node sau."""

    from_node: str
    from_port: str
    to_node: str
    to_port: str


@dataclass(frozen=True)
class ParameterBound:
    """Miền giá trị hợp lệ của tham số phục vụ Coordinate Lab và Hyperopt."""

    name: str
    min_value: float
    max_value: float
    step: float
    default_value: float


@dataclass(frozen=True)
class CompatibilityMap:
    """Bản đồ tương thích dữ liệu và môi trường đã kiểm nghiệm."""

    target_venues: tuple[str, ...] = ()
    target_symbols: tuple[str, ...] = ()
    timeframes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionProfile:
    """Hồ sơ giả định thực thi kèm theo chiến lược."""

    assumed_taker_fee: Decimal
    assumed_slippage_bps: Decimal

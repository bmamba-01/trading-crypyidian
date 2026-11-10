"""Schema định nghĩa khai báo cho Strategy Card (Pydantic v2) và Canonical Hashing.

Cung cấp:
- StrategyCard: Pydantic v2 model xác thực toàn vẹn cấu trúc và đồ thị DAG.
- compute_canonical_hash: Tạo mã băm SHA-256 bất biến thứ tự dữ liệu phục vụ audit/provenance.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading_bot.domain.strategy_card.models import (
    CompatibilityMap,
    CompositionLayer,
    ExecutionProfile,
    GraphEdge,
    GraphNode,
    ParameterBound,
)


class StrategyCard(BaseModel):
    """Bản kê chiến lược đóng gói toàn diện dạng khai báo (Declarative Strategy Card)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    card_id: str = Field(..., description="Mã định danh duy nhất của chiến lược")
    name: str = Field(..., description="Tên mô tả chiến lược")
    version: str = Field(..., description="Phiên bản ngữ nghĩa SemVer (ví dụ 1.0.0)")
    author: str = Field(..., description="Tác giả hoặc đơn vị phát triển")
    created_at: datetime = Field(..., description="Thời điểm tạo chiến lược")
    layers: list[CompositionLayer] = Field(
        default_factory=list, description="Danh sách các phân tầng L0-L6 tham gia"
    )
    nodes: list[GraphNode] = Field(
        default_factory=list, description="Danh sách các khối xử lý nguyên tử"
    )
    edges: list[GraphEdge] = Field(
        default_factory=list, description="Danh sách các đường truyền dữ liệu có hướng"
    )
    parameters: list[ParameterBound] = Field(
        default_factory=list, description="Không gian tham số và biên độ hợp lệ"
    )
    compatibility: CompatibilityMap = Field(
        default_factory=CompatibilityMap,
        description="Bản đồ tương thích sàn, cặp tiền, khung thời gian",
    )
    execution_profile: ExecutionProfile = Field(
        ..., description="Giả định hồ sơ thực thi (phí taker, trượt giá)"
    )

    @model_validator(mode="after")
    def validate_graph_structure_and_dag(self) -> StrategyCard:
        """Xác thực cấu trúc đồ thị tính toán và đảm bảo tính chất DAG (không có chu trình)."""
        node_ids: set[str] = set()
        for node in self.nodes:
            if node.node_id in node_ids:
                raise ValueError(
                    f"Phát hiện trùng lặp node_id '{node.node_id}' trong danh sách nodes."
                )
            node_ids.add(node.node_id)

        # 1. Kiểm tra tính tồn tại của các node tham chiếu trong edges
        for edge in self.edges:
            if edge.from_node not in node_ids:
                raise ValueError(
                    f"Cạnh tham chiếu đến node nguồn '{edge.from_node}' "
                    "không tồn tại trong danh sách nodes."
                )
            if edge.to_node not in node_ids:
                raise ValueError(
                    f"Cạnh tham chiếu đến node đích '{edge.to_node}' "
                    "không tồn tại trong danh sách nodes."
                )

        # 2. Kiểm tra chu trình (Cycle detection) bằng thuật toán Kahn
        adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
        in_degree: dict[str, int] = dict.fromkeys(node_ids, 0)

        for edge in self.edges:
            if edge.from_node == edge.to_node:
                raise ValueError(
                    f"Phát hiện chu trình tự trỏ (Self-loop) tại node '{edge.from_node}'."
                )
            adj[edge.from_node].append(edge.to_node)
            in_degree[edge.to_node] += 1

        queue: deque[str] = deque([nid for nid, deg in in_degree.items() if deg == 0])
        visited_count = 0

        while queue:
            curr = queue.popleft()
            visited_count += 1
            for neighbor in adj[curr]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited_count < len(node_ids):
            raise ValueError(
                "Đồ thị chiến lược chứa chu trình lặp (Cyclic dependency detected). "
                "Cấu trúc chiến lược phải là đồ thị có hướng không chu trình (DAG)."
            )

        return self

    @property
    def canonical_hash(self) -> str:
        """Trả về mã băm SHA-256 chuẩn hóa xác thực của bản kê chiến lược."""
        return compute_canonical_hash(self)


def compute_canonical_hash(card: StrategyCard) -> str:
    """Tính toán mã băm SHA-256 bất biến xác thực (Canonical Content Hash) cho StrategyCard.

    Chuẩn hóa cấu trúc thành canonical dictionary:
    - Sắp xếp có thứ tự xác định cho các danh sách (nodes, edges, parameters v.v.).
    - Chuẩn hóa kiểu dữ liệu datetime sang ISO-8601, Decimal sang chuỗi str chuẩn xác.
    - Tuần tự hóa JSON với sort_keys=True và loại bỏ khoảng trắng dư thừa (compact separators).
    - Đảm bảo tính tất định: Hai bản kê cùng cấu trúc dù khác thứ tự khai báo ban đầu
      vẫn sinh ra mã băm SHA-256 64 ký tự trùng khớp tuyệt đối.
    """
    # 1. Chuẩn hóa layers theo thứ tự bảng chữ cái
    sorted_layers = sorted(
        layer.value if hasattr(layer, "value") else str(layer) for layer in card.layers
    )

    # 2. Chuẩn hóa nodes theo node_id
    sorted_nodes: list[dict[str, Any]] = []
    for node in sorted(card.nodes, key=lambda n: n.node_id):
        sorted_inputs = [
            {
                "name": p.name,
                "data_type": (
                    p.data_type.value if hasattr(p.data_type, "value") else str(p.data_type)
                ),
                "description": p.description,
            }
            for p in sorted(node.inputs, key=lambda p: p.name)
        ]
        sorted_outputs = [
            {
                "name": p.name,
                "data_type": (
                    p.data_type.value if hasattr(p.data_type, "value") else str(p.data_type)
                ),
                "description": p.description,
            }
            for p in sorted(node.outputs, key=lambda p: p.name)
        ]
        sorted_nodes.append(
            {
                "node_id": node.node_id,
                "layer": node.layer.value if hasattr(node.layer, "value") else str(node.layer),
                "role": node.role.value if hasattr(node.role, "value") else str(node.role),
                "operation": node.operation,
                "parameters": node.parameters,
                "inputs": sorted_inputs,
                "outputs": sorted_outputs,
            }
        )

    # 3. Chuẩn hóa edges theo bộ 4 thuộc tính (from_node, from_port, to_node, to_port)
    sorted_edges = [
        {
            "from_node": e.from_node,
            "from_port": e.from_port,
            "to_node": e.to_node,
            "to_port": e.to_port,
        }
        for e in sorted(
            card.edges,
            key=lambda e: (e.from_node, e.from_port, e.to_node, e.to_port),
        )
    ]

    # 4. Chuẩn hóa parameters theo name
    sorted_parameters = [
        {
            "name": p.name,
            "min_value": float(p.min_value),
            "max_value": float(p.max_value),
            "step": float(p.step),
            "default_value": float(p.default_value),
        }
        for p in sorted(card.parameters, key=lambda p: p.name)
    ]

    # 5. Chuẩn hóa compatibility
    sorted_compatibility = {
        "target_venues": sorted(card.compatibility.target_venues),
        "target_symbols": sorted(card.compatibility.target_symbols),
        "timeframes": sorted(card.compatibility.timeframes),
    }

    # 6. Chuẩn hóa execution_profile
    execution_profile_dict = {
        "assumed_taker_fee": str(card.execution_profile.assumed_taker_fee),
        "assumed_slippage_bps": str(card.execution_profile.assumed_slippage_bps),
    }

    # 7. Xây dựng canonical dictionary
    canonical_dict: dict[str, Any] = {
        "card_id": card.card_id,
        "name": card.name,
        "version": card.version,
        "author": card.author,
        "created_at": card.created_at.isoformat()
        if isinstance(card.created_at, datetime)
        else str(card.created_at),
        "layers": sorted_layers,
        "nodes": sorted_nodes,
        "edges": sorted_edges,
        "parameters": sorted_parameters,
        "compatibility": sorted_compatibility,
        "execution_profile": execution_profile_dict,
    }

    canonical_json_bytes = json.dumps(
        canonical_dict,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    return hashlib.sha256(canonical_json_bytes).hexdigest()

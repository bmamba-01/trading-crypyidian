"""Bộ thẩm định đồ thị chiến lược và phát hiện nút chết (Dead Node & Anomaly Detector).

Module này kiểm tra tính toàn vẹn của đồ thị có hướng Strategy Card:
- Duyệt ngược từ L6 về L0 phát hiện các node mồ côi (Unwired / Dead Nodes).
- Phát hiện các ngõ cụt logic (Logic dead ends: AND False, OR True, thiếu cổng vào).
- Phát hiện chu trình (Cycle detection) để bảo đảm đồ thị là DAG thuần túy.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


def _extract_field(obj: Any, field_name: str, default: Any = None) -> Any:
    """Trích xuất giá trị trường từ đối tượng Pydantic, dataclass hoặc từ điển (dict)."""
    if hasattr(obj, field_name):
        return getattr(obj, field_name)
    if isinstance(obj, dict):
        return obj.get(field_name, default)
    return default


@dataclass(frozen=True)
class GraphVerificationReport:
    """Báo cáo thẩm định tính toàn vẹn cấu trúc của đồ thị Strategy Card."""

    is_valid: bool
    dead_nodes: list[str] = field(default_factory=list)
    cycles_detected: list[str] = field(default_factory=list)
    logic_anomalies: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DeadNodeDetector:
    """Bộ phát hiện nút chết, chu trình vòng lặp và ngõ cụt logic trong đồ thị chiến lược."""

    def detect_dead_nodes(
        self,
        nodes: Sequence[Any],
        edges: Sequence[Any],
    ) -> list[str]:
        """Phát hiện các node mồ côi và bất thường trong đồ thị, trả về danh sách cảnh báo.

        Args:
            nodes: Danh sách các node trong đồ thị (dict hoặc model).
            edges: Danh sách các edge kết nối giữa các node (dict hoặc model).

        Returns:
            Danh sách các chuỗi cảnh báo phát hiện được.
        """
        report = self.verify(nodes, edges)
        return report.warnings

    def verify(
        self,
        nodes: Sequence[Any],
        edges: Sequence[Any],
    ) -> GraphVerificationReport:
        """Thẩm định toàn diện cấu trúc đồ thị, phân tích nút chết, ngõ cụt logic và chu trình.

        Args:
            nodes: Danh sách các node.
            edges: Danh sách các cạnh kết nối.

        Returns:
            Đối tượng GraphVerificationReport chứa thông tin chi tiết.
        """
        warnings: list[str] = []
        dead_nodes: list[str] = []
        cycles_detected: list[str] = []
        logic_anomalies: list[str] = []

        node_map: dict[str, Any] = {}
        forward_adj: dict[str, list[str]] = {}
        reverse_adj: dict[str, list[str]] = {}
        in_degree: dict[str, int] = {}

        for n in nodes:
            nid = _extract_field(n, "node_id", "")
            if not nid:
                continue
            node_map[nid] = n
            forward_adj[nid] = []
            reverse_adj[nid] = []
            in_degree[nid] = 0

        for e in edges:
            u = _extract_field(e, "from_node", "")
            v = _extract_field(e, "to_node", "")
            if u in forward_adj and v in reverse_adj:
                forward_adj[u].append(v)
                reverse_adj[v].append(u)
                in_degree[v] = in_degree.get(v, 0) + 1

        # 1. Phát hiện chu trình (Cycle Detection) bằng DFS 3 trạng thái
        state: dict[str, int] = dict.fromkeys(node_map, 0)

        def dfs_cycle(curr: str, path: list[str]) -> bool:
            state[curr] = 1
            path.append(curr)
            for neighbor in forward_adj.get(curr, []):
                if state[neighbor] == 1:
                    cycle_str = " -> ".join(path + [neighbor])
                    cycles_detected.append(cycle_str)
                    warnings.append(
                        f"CẢNH BÁO CHU TRÌNH ĐỒ THỊ: Phát hiện chu trình vòng lặp tại "
                        f"node '{curr}'. Đồ thị Strategy Card phải là DAG thuần túy ({cycle_str})."
                    )
                    return True
                if state[neighbor] == 0 and dfs_cycle(neighbor, path):
                    return True
            path.pop()
            state[curr] = 2
            return False

        for nid in node_map:
            if state[nid] == 0:
                dfs_cycle(nid, [])

        # 2. Phát hiện nút mồ côi (Dead / Unwired Nodes) bằng duyệt ngược từ tầng L6 về L0
        l6_nodes: list[str] = []
        for nid, n in node_map.items():
            layer = str(_extract_field(n, "layer", "")).upper()
            if "L6" in layer or layer.startswith("L6_"):
                l6_nodes.append(nid)

        reachable_to_l6: set[str] = set(l6_nodes)
        queue: deque[str] = deque(l6_nodes)

        while queue:
            curr = queue.popleft()
            for pred in reverse_adj.get(curr, []):
                if pred not in reachable_to_l6:
                    reachable_to_l6.add(pred)
                    queue.append(pred)

        for nid, n in node_map.items():
            if nid not in reachable_to_l6:
                layer = _extract_field(n, "layer", "UNKNOWN")
                dead_nodes.append(nid)
                warnings.append(
                    f"CẢNH BÁO NÚT MỒ CÔI (Dead Node): Node '{nid}' thuộc tầng '{layer}' "
                    f"không có đường dẫn truyền dữ liệu tới tầng quyết định L6."
                )

        # 3. Phát hiện ngõ cụt logic (Logic Dead Ends / Constant Trivialization)
        for nid, n in node_map.items():
            op_name = str(
                _extract_field(n, "operation", "") or _extract_field(n, "operator_name", "")
            ).upper()
            params = _extract_field(n, "parameters", {}) or {}
            input_ports = (
                _extract_field(n, "inputs", []) or _extract_field(n, "input_ports", []) or []
            )

            # Kiểm tra toán tử AND bị triệt tiêu bởi False
            if op_name in {"AND", "LOGICAL_AND"}:
                constant_val = params.get("constant_operand")
                if constant_val is False or params.get("constant_value") is False:
                    logic_anomalies.append(nid)
                    warnings.append(
                        f"CẢNH BÁO NGÕ CỤT LOGIC: Node cổng logic '{nid}' có đầu vào "
                        f"cố định giá trị False gây triệt tiêu toàn bộ nhánh điều kiện "
                        f"(condition AND False)."
                    )

            # Kiểm tra toán tử OR bị vô hiệu hóa bởi True
            if op_name in {"OR", "LOGICAL_OR"}:
                constant_val = params.get("constant_operand")
                if constant_val is True or params.get("constant_value") is True:
                    logic_anomalies.append(nid)
                    warnings.append(
                        f"CẢNH BÁO NGÕ CỤT LOGIC: Node cổng logic '{nid}' có đầu vào "
                        f"cố định giá trị True gây vô hiệu hóa kiểm tra điều kiện "
                        f"(condition OR True)."
                    )

            # Kiểm tra cổng logic thiếu kết nối đầu vào
            if op_name in {"AND", "OR", "LOGICAL_AND", "LOGICAL_OR"} and len(input_ports) > 0:
                in_count = in_degree.get(nid, 0)
                if in_count < len(input_ports):
                    logic_anomalies.append(nid)
                    warnings.append(
                        f"CẢNH BÁO NGÕ CỤT LOGIC: Node cổng logic '{nid}' bị thiếu kết nối "
                        f"đầu vào (yêu cầu {len(input_ports)}, thực tế nhận {in_count})."
                    )

        is_valid = len(warnings) == 0
        return GraphVerificationReport(
            is_valid=is_valid,
            dead_nodes=dead_nodes,
            cycles_detected=cycles_detected,
            logic_anomalies=logic_anomalies,
            warnings=warnings,
        )

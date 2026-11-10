"""Bộ đánh giá kích hoạt chiến lược (Strategy Trigger) tại tầng L5 theo QUANTSMART.

Tầng L5 đóng vai trò ngòi nổ hành động (Action Trigger):
- Chỉ được phép đánh giá khi Tầng L4 Context Gate đã vượt qua (PASS).
- Phân tích tín hiệu xác nhận từ các đặc trưng giải phẫu nến L1, chỉ báo L2 hoặc thuật toán L3.
- Phát tín hiệu đề xuất hướng vào lệnh (LONG_ENTRY, SHORT_ENTRY) kèm điểm số tin cậy (trigger_score)
  và danh sách các node bằng chứng (evidence_nodes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from trading_bot.application.strategy_pipeline.context_gate import (
    ContextGateResult,
    ContextGateStatus,
)

if TYPE_CHECKING:
    from trading_bot.domain.market.models import OHLCVBar
    from trading_bot.domain.strategy_card.models import GraphEdge, GraphNode


class TriggerSignal(StrEnum):
    """Tín hiệu kích hoạt chiến lược ở tầng L5."""

    LONG_ENTRY = "LONG_ENTRY"
    SHORT_ENTRY = "SHORT_ENTRY"
    NO_SIGNAL = "NO_SIGNAL"


@dataclass(frozen=True)
class TriggerEvaluationResult:
    """Kết quả đánh giá ngòi nổ kích hoạt tại tầng L5."""

    signal: TriggerSignal
    trigger_score: Decimal = Decimal("0")
    evidence_nodes: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class TriggerEvaluator:
    """Bộ đánh giá ngòi nổ kích hoạt tầng L5 (Strategy Trigger)."""

    def evaluate(
        self,
        trigger_nodes: list[GraphNode],
        node_outputs: dict[str, Any],
        gate_result: ContextGateResult,
        latest_bar: OHLCVBar | None = None,
        edges: list[GraphEdge] | tuple[GraphEdge, ...] | None = None,
    ) -> TriggerEvaluationResult:
        """Đánh giá các điều kiện kích hoạt chiến lược.

        CẢNH BÁO BẢO VỆ PHÒNG THỦ: Hàm này nghiêm cấm thực thi nếu ContextGate
        chưa được xác nhận PASS từ tầng L4.
        """
        if gate_result.status != ContextGateStatus.PASS:
            raise PermissionError(
                "BẢO VỆ AN TOÀN VI PHẠM: TriggerEvaluator chỉ được phép đánh giá "
                f"khi ContextGate có trạng thái PASS (hiện tại: {gate_result.status})."
            )

        for node in trigger_nodes:
            op = node.operation.lower()
            params = node.parameters

            if "rejection" in op or "breakout" in op or "threshold" in op:
                threshold = Decimal(str(params.get("threshold", "0.5")))
                direction = str(params.get("direction", "LONG")).upper()

                # Xác định các node nguồn cung cấp dữ liệu số cho node trigger này
                source_candidates: list[str] = []
                if edges:
                    for edge in edges:
                        if (
                            edge.to_node == node.node_id
                            and edge.from_node in node_outputs
                            and edge.to_port not in {"flag", "gate", "price"}
                        ):
                            source_candidates.append(edge.from_node)

                if not source_candidates:
                    # Fallback lọc theo tên node mang đặc trưng tín hiệu
                    source_candidates = [
                        k
                        for k in node_outputs
                        if not any(
                            excluded in k.lower()
                            for excluded in ("atr", "price", "cleaner", "regime", "gate")
                        )
                    ]

                candidate_val: Decimal | None = None
                for src_id in source_candidates:
                    val = node_outputs.get(src_id)
                    if isinstance(val, (int, float, Decimal)):
                        candidate_val = Decimal(str(val))
                        break

                if candidate_val is not None and candidate_val >= threshold:
                    signal = (
                        TriggerSignal.LONG_ENTRY
                        if direction == "LONG"
                        else TriggerSignal.SHORT_ENTRY
                    )
                    return TriggerEvaluationResult(
                        signal=signal,
                        trigger_score=candidate_val,
                        evidence_nodes=(node.node_id,),
                        metadata={
                            "matched_node": node.node_id,
                            "threshold": float(threshold),
                            "trigger_val": float(candidate_val),
                        },
                    )

            elif "crossover" in op:
                direction = str(params.get("direction", "LONG")).upper()
                signal = (
                    TriggerSignal.LONG_ENTRY if direction == "LONG" else TriggerSignal.SHORT_ENTRY
                )
                return TriggerEvaluationResult(
                    signal=signal,
                    trigger_score=Decimal("1.0"),
                    evidence_nodes=(node.node_id,),
                )

        return TriggerEvaluationResult(
            signal=TriggerSignal.NO_SIGNAL,
            trigger_score=Decimal("0"),
            evidence_nodes=(),
        )

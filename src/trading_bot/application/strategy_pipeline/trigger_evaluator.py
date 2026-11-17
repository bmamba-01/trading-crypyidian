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
from trading_bot.domain.strategy_card.anatomy import (
    body_ratio,
    lower_wick_ratio,
    upper_wick_ratio,
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

            if "wick_reversal" in op:
                z_threshold = Decimal(str(params.get("z_threshold", "1.5")))
                min_wick = Decimal(str(params.get("min_wick", "0.45")))
                max_body = Decimal(str(params.get("max_body", "0.45")))
                z_val = self._find_decimal(node_outputs, ("zscore", "l3_zscore"))
                body_val = self._find_decimal(node_outputs, ("l1_body", "body"))
                lower_val = self._find_decimal(node_outputs, ("l1_lower_wick", "lower_wick"))
                upper_val = self._find_decimal(node_outputs, ("l1_upper_wick", "upper_wick"))
                if latest_bar is not None:
                    if body_val is None:
                        body_val = body_ratio(
                            latest_bar.open, latest_bar.high, latest_bar.low, latest_bar.close
                        )
                    if lower_val is None:
                        lower_val = lower_wick_ratio(
                            latest_bar.open, latest_bar.high, latest_bar.low, latest_bar.close
                        )
                    if upper_val is None:
                        upper_val = upper_wick_ratio(
                            latest_bar.open, latest_bar.high, latest_bar.low, latest_bar.close
                        )

                if z_val is not None and body_val is not None and body_val <= max_body:
                    if z_val <= -z_threshold and lower_val is not None and lower_val >= min_wick:
                        return TriggerEvaluationResult(
                            signal=TriggerSignal.LONG_ENTRY,
                            trigger_score=min(Decimal("1.0"), abs(z_val) / Decimal("3.0")),
                            evidence_nodes=(node.node_id,),
                            metadata={
                                "matched_node": node.node_id,
                                "zscore": float(z_val),
                                "lower_wick_ratio": float(lower_val),
                            },
                        )
                    if z_val >= z_threshold and upper_val is not None and upper_val >= min_wick:
                        return TriggerEvaluationResult(
                            signal=TriggerSignal.SHORT_ENTRY,
                            trigger_score=min(Decimal("1.0"), abs(z_val) / Decimal("3.0")),
                            evidence_nodes=(node.node_id,),
                            metadata={
                                "matched_node": node.node_id,
                                "zscore": float(z_val),
                                "upper_wick_ratio": float(upper_val),
                            },
                        )

            elif "vwap_reversion" in op:
                dev_threshold = Decimal(str(params.get("dev_threshold", "1.75")))
                dev_val = self._find_decimal(node_outputs, ("vwap_dev", "l3_vwap_dev"))
                loc_val = self._find_decimal(node_outputs, ("l1_location", "location"))
                if dev_val is not None:
                    if dev_val <= -dev_threshold and (loc_val is None or loc_val >= Decimal("0.5")):
                        return TriggerEvaluationResult(
                            signal=TriggerSignal.LONG_ENTRY,
                            trigger_score=min(Decimal("1.0"), abs(dev_val) / Decimal("3.0")),
                            evidence_nodes=(node.node_id,),
                            metadata={"matched_node": node.node_id, "vwap_dev": float(dev_val)},
                        )
                    if dev_val >= dev_threshold and (loc_val is None or loc_val <= Decimal("0.5")):
                        return TriggerEvaluationResult(
                            signal=TriggerSignal.SHORT_ENTRY,
                            trigger_score=min(Decimal("1.0"), abs(dev_val) / Decimal("3.0")),
                            evidence_nodes=(node.node_id,),
                            metadata={"matched_node": node.node_id, "vwap_dev": float(dev_val)},
                        )

            elif any(
                name in op
                for name in ("tsmom", "squeeze_breakout", "pullback", "macd_adx", "taker_flow")
            ):
                threshold = Decimal(str(params.get("threshold", "0.25")))
                score_val = self._find_connected_decimal(node.node_id, node_outputs, edges)
                if score_val is not None:
                    if score_val >= threshold:
                        return TriggerEvaluationResult(
                            signal=TriggerSignal.LONG_ENTRY,
                            trigger_score=abs(score_val),
                            evidence_nodes=(node.node_id,),
                            metadata={
                                "matched_node": node.node_id,
                                "threshold": float(threshold),
                                "trigger_val": float(score_val),
                            },
                        )
                    if score_val <= -threshold:
                        return TriggerEvaluationResult(
                            signal=TriggerSignal.SHORT_ENTRY,
                            trigger_score=abs(score_val),
                            evidence_nodes=(node.node_id,),
                            metadata={
                                "matched_node": node.node_id,
                                "threshold": float(threshold),
                                "trigger_val": float(score_val),
                            },
                        )

            elif "rejection" in op or "breakout" in op or "threshold" in op:
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

    def _find_decimal(
        self, node_outputs: dict[str, Any], substrings: tuple[str, ...]
    ) -> Decimal | None:
        for key, val in node_outputs.items():
            lowered = key.lower()
            if any(sub in lowered for sub in substrings) and isinstance(val, (int, float, Decimal)):
                return Decimal(str(val))
        return None

    def _find_connected_decimal(
        self,
        node_id: str,
        node_outputs: dict[str, Any],
        edges: list[GraphEdge] | tuple[GraphEdge, ...] | None,
    ) -> Decimal | None:
        if edges:
            # Ưu tiên lấy kết quả từ tầng L3 đã kết nối vào L5 trước
            connected = [
                edge.from_node
                for edge in edges
                if edge.to_node == node_id
                and edge.from_node in node_outputs
                and edge.to_port not in {"flag", "gate", "price"}
            ]
            for preferred_prefix in ("l3", "l2", "l1"):
                for src_id in connected:
                    if src_id.lower().startswith(preferred_prefix):
                        val = node_outputs.get(src_id)
                        if isinstance(val, (int, float, Decimal)):
                            return Decimal(str(val))
        return self._find_decimal(
            node_outputs, ("l3_", "tsmom", "squeeze", "pullback", "macd", "flow")
        )

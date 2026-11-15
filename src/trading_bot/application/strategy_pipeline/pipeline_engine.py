"""Động cơ thực thi quy trình chiến lược L0->L6 (Strategy Pipeline Runtime Engine).

Nguyên lý thiết kế chuẩn mực theo QUANTSMART:
- Duyệt và thực thi đồ thị có hướng không chu trình (DAG) theo tuần tự topo 7 tầng L0 đến L6.
- Tầng L0: Data Hygiene làm sạch dữ liệu nến, phát hiện nến hỏng (High < Low, v.v.).
- Tầng L1: Candle Anatomy đo đạc đặc trưng hình học nến chuẩn xác.
- Tầng L2: Indicators tính toán các chỉ báo phái sinh (ATR, EMA, v.v.).
- Tầng L3: Algorithms tổng hợp điểm số lượng hóa.
- Tầng L4: Context Gate thẩm định bối cảnh thị trường.
  NẾU L4 BLOCK -> NGẮT MẠCH TỨC THÌ (SHORT-CIRCUIT).
- Tầng L5: Strategy Trigger ngòi nổ hành động (CHỈ CHẠY KHI L4 PASS).
- Tầng L6: Execution Zones thiết lập vùng giá và đóng gói thành StrategyCandidate.

BẢO ĐẢM BẤT BIẾN AN TOÀN: Động cơ chỉ sinh StrategyCandidate, tuyệt đối không tạo lệnh sàn.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from trading_bot.application.strategy_pipeline.context_gate import (
    ContextGateEvaluator,
    ContextGateResult,
    ContextGateStatus,
)
from trading_bot.application.strategy_pipeline.trigger_evaluator import (
    TriggerEvaluationResult,
    TriggerEvaluator,
    TriggerSignal,
)
from trading_bot.application.strategy_pipeline.zone_builder import (
    ExecutionZones,
    ZoneBuilder,
)
from trading_bot.domain.strategy_card.anatomy import (
    body_ratio,
    close_location,
    lower_wick_ratio,
    upper_wick_ratio,
    wick_rejection_strength,
)
from trading_bot.domain.strategy_card.models import CompositionLayer
from trading_bot.domain.strategy_card.schema import StrategyCard, compute_canonical_hash

if TYPE_CHECKING:
    from trading_bot.domain.market.models import OHLCVBar
    from trading_bot.domain.strategy.contracts import DecisionSnapshot


# Phiên bản ngữ nghĩa của engine; đổi cách tính một tầng thì tăng giá trị này để hash release
# của mọi Strategy Card đổi theo.
PIPELINE_ENGINE_VERSION = "l0-l6-v2"
# Nguồn ATR khi không có feature đã quản trị lẫn đủ lịch sử nến: chỉ là biên độ của một nến.
ATR_SOURCE_SINGLE_BAR = "single_bar_range"


def compute_candidate_id(
    strategy_card_id: str,
    content_hash: str,
    symbol: str,
    direction: str,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    risk_reward_ratio: Decimal,
    signal_at: datetime,
) -> str:
    """Tạo mã băm SHA-256 tất định định danh duy nhất đề xuất chiến lược StrategyCandidate."""
    payload = {
        "strategy_card_id": strategy_card_id,
        "content_hash": content_hash,
        "symbol": symbol,
        "direction": direction.upper(),
        "entry_price": str(entry_price),
        "stop_loss": str(stop_loss),
        "take_profit": str(take_profit),
        "risk_reward_ratio": str(risk_reward_ratio),
        "signal_at": signal_at.isoformat(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StrategyCandidate:
    """Đề xuất chiến lược thô được sinh ra từ tầng L6 Execution Zones."""

    candidate_id: str
    strategy_card_id: str
    content_hash: str
    symbol: str
    direction: str  # "LONG" hoặc "SHORT"
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    risk_reward_ratio: Decimal
    confidence: Decimal
    signal_at: datetime
    evidence_nodes: tuple[str, ...] = ()
    zones: ExecutionZones | None = None
    reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineExecutionResult:
    """Kết quả thực thi quy trình Strategy Pipeline qua các tầng L0 -> L6."""

    status: str  # "SUCCESS", "DATA_HYGIENE_FAILED", "CONTEXT_GATE_BLOCKED", "NO_TRIGGER", "ERROR"
    candidate: StrategyCandidate | None = None
    gate_result: ContextGateResult | None = None
    trigger_result: TriggerEvaluationResult | None = None
    zones: ExecutionZones | None = None
    node_values: dict[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()


class StrategyPipelineEngine:
    """Động cơ thực thi tuần tự pipeline L0 -> L6 cho Strategy Card."""

    def __init__(
        self,
        context_gate_evaluator: ContextGateEvaluator | None = None,
        trigger_evaluator: TriggerEvaluator | None = None,
        zone_builder: ZoneBuilder | None = None,
    ) -> None:
        self.context_gate_evaluator = context_gate_evaluator or ContextGateEvaluator()
        self.trigger_evaluator = trigger_evaluator or TriggerEvaluator()
        self.zone_builder = zone_builder or ZoneBuilder()

    def execute(
        self,
        card: StrategyCard,
        snapshot: DecisionSnapshot,
    ) -> PipelineExecutionResult:
        """Thực thi toàn diện đồ thị tính toán L0 -> L6 từ DecisionSnapshot."""
        # 1. Trích xuất nến mới nhất từ snapshot
        if not snapshot.series or not snapshot.series[0].bars:
            return PipelineExecutionResult(
                status="DATA_HYGIENE_FAILED",
                reasons=("DATA_HYGIENE_FAILED: Không có chuỗi nến nào trong DecisionSnapshot.",),
            )

        bar = snapshot.series[0].bars[-1]

        # -------------------------------------------------------------------
        # TẦNG L0: DATA HYGIENE CHECK
        # -------------------------------------------------------------------
        hygiene_failed = self._check_data_hygiene(bar)
        if hygiene_failed:
            return PipelineExecutionResult(
                status="DATA_HYGIENE_FAILED",
                reasons=(f"DATA_HYGIENE_FAILED: {hygiene_failed}",),
            )

        node_values: dict[str, Any] = {}

        # -------------------------------------------------------------------
        # TẦNG L1: CANDLE ANATOMY PRIMITIVES
        # -------------------------------------------------------------------
        l1_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L1_CANDLE_ANATOMY]
        for node in l1_nodes:
            val = self._compute_l1_node(node.operation, bar)
            node_values[node.node_id] = val

        # -------------------------------------------------------------------
        # TẦNG L2: INDICATORS
        # -------------------------------------------------------------------
        l2_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L2_INDICATORS]
        indicator_sources: dict[str, str] = {}
        indicator_feature_ids: list[str] = []
        for node in l2_nodes:
            val, source, feature_id = self._compute_l2_node(node, bar, snapshot)
            node_values[node.node_id] = val
            indicator_sources[node.node_id] = source
            if feature_id is not None:
                indicator_feature_ids.append(feature_id)

        # -------------------------------------------------------------------
        # TẦNG L3: ALGORITHMS / QUANTITATIVE COMPOSITES
        # -------------------------------------------------------------------
        l3_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L3_ALGORITHMS]
        for node in l3_nodes:
            val = self._compute_l3_node(node, node_values)
            node_values[node.node_id] = val

        # -------------------------------------------------------------------
        # TẦNG L4: CONTEXT GATE EVALUATION (SHORT-CIRCUIT NẾU BLOCK)
        # -------------------------------------------------------------------
        l4_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L4_CONTEXT_GATE]
        gate_result = self.context_gate_evaluator.evaluate_gates(
            gate_nodes=l4_nodes,
            node_outputs=node_values,
            snapshot=snapshot,
            latest_bar=bar,
        )

        if gate_result.status == ContextGateStatus.BLOCK:
            # Ngắt mạch tức thì (Short-circuit): L5 và L6 không được thực thi!
            return PipelineExecutionResult(
                status="CONTEXT_GATE_BLOCKED",
                gate_result=gate_result,
                reasons=gate_result.reasons,
                node_values=node_values,
            )

        # -------------------------------------------------------------------
        # TẦNG L5: STRATEGY TRIGGER (CHỈ CHẠY KHI L4 LÀ PASS)
        # -------------------------------------------------------------------
        l5_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L5_STRATEGY_TRIGGER]
        trigger_result = self.trigger_evaluator.evaluate(
            trigger_nodes=l5_nodes,
            node_outputs=node_values,
            gate_result=gate_result,
            latest_bar=bar,
            edges=card.edges,
        )

        if trigger_result.signal == TriggerSignal.NO_SIGNAL:
            return PipelineExecutionResult(
                status="NO_TRIGGER",
                gate_result=gate_result,
                trigger_result=trigger_result,
                reasons=("NO_TRIGGER: Không có tín hiệu ngòi nổ kích hoạt nào thỏa mãn.",),
                node_values=node_values,
            )

        # -------------------------------------------------------------------
        # TẦNG L6: EXECUTION ZONES & CANDIDATE PACKAGING
        # -------------------------------------------------------------------
        l6_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L6_ZONES]
        sl_multiplier = Decimal("2.0")
        rr_ratio = Decimal("2.0")

        if l6_nodes:
            params = l6_nodes[0].parameters
            # Chấp nhận cả tên tham số của thẻ đã phát hành (`risk_reward`, `stop_atr_multiple`).
            for key in ("sl_multiplier", "stop_atr_multiple"):
                if key in params:
                    sl_multiplier = Decimal(str(params[key]))
                    break
            for key in ("rr_ratio", "risk_reward"):
                if key in params:
                    rr_ratio = Decimal(str(params[key]))
                    break

        # Lấy ATR từ node L2 nếu có, hoặc tính trực tiếp biên độ nến
        current_atr = Decimal(str(node_values.get("l2_atr", str(bar.high - bar.low))))
        direction_str = "LONG" if trigger_result.signal == TriggerSignal.LONG_ENTRY else "SHORT"

        zones = self.zone_builder.build_zones(
            direction=direction_str,
            current_price=bar.close,
            atr=current_atr,
            sl_multiplier=sl_multiplier,
            rr_ratio=rr_ratio,
        )

        content_hash = compute_canonical_hash(card)
        candidate_id = compute_candidate_id(
            strategy_card_id=card.card_id,
            content_hash=content_hash,
            symbol=snapshot.symbol,
            direction=direction_str,
            entry_price=zones.entry_price,
            stop_loss=zones.stop_loss_price,
            take_profit=zones.take_profit_price,
            risk_reward_ratio=zones.risk_reward_ratio,
            signal_at=bar.close_time,
        )

        candidate = StrategyCandidate(
            candidate_id=candidate_id,
            strategy_card_id=card.card_id,
            content_hash=content_hash,
            symbol=snapshot.symbol,
            direction=direction_str,
            entry_price=zones.entry_price,
            stop_loss=zones.stop_loss_price,
            take_profit=zones.take_profit_price,
            risk_reward_ratio=zones.risk_reward_ratio,
            confidence=(
                trigger_result.trigger_score
                if trigger_result.trigger_score > Decimal("0")
                else Decimal("1.0")
            ),
            signal_at=bar.close_time,
            evidence_nodes=trigger_result.evidence_nodes,
            zones=zones,
            reasons=(),
            # ATR dùng dựng vùng giá và điểm ngòi nổ được ghi kèm để cầu nối sang TradePlanner
            # và bằng chứng vào lệnh dùng đúng giá trị pipeline đã tính, không tự suy lại.
            metadata={
                "atr": str(current_atr),
                "gate_metrics": gate_result.metrics,
                "trigger_score": str(trigger_result.trigger_score),
                "indicator_sources": indicator_sources,
                "indicator_feature_ids": tuple(indicator_feature_ids),
            },
        )

        return PipelineExecutionResult(
            status="SUCCESS",
            candidate=candidate,
            gate_result=gate_result,
            trigger_result=trigger_result,
            zones=zones,
            node_values=node_values,
        )

    def _check_data_hygiene(self, bar: OHLCVBar) -> str | None:
        """Kiểm tra độ sạch của nến L0: High >= Low, High >= max(O,C), Low <= min(O,C)."""
        if bar.high < bar.low:
            return f"Nến vi phạm High < Low (High={bar.high}, Low={bar.low})"
        if bar.high < max(bar.open, bar.close):
            return f"Nến vi phạm High < max(Open, Close) (High={bar.high})"
        if bar.low > min(bar.open, bar.close):
            return f"Nến vi phạm Low > min(Open, Close) (Low={bar.low})"
        if bar.open <= Decimal("0") or bar.close <= Decimal("0"):
            return "Giá Open/Close phải là số dương"
        return None

    def _compute_l1_node(self, operation: str, bar: OHLCVBar) -> Decimal:
        """Tính toán khối nguyên tử giải phẫu nến L1."""
        op = operation.lower()
        if "lower_wick" in op:
            return lower_wick_ratio(bar.open, bar.high, bar.low, bar.close)
        if "upper_wick" in op:
            return upper_wick_ratio(bar.open, bar.high, bar.low, bar.close)
        if "body" in op:
            return body_ratio(bar.open, bar.high, bar.low, bar.close)
        if "location" in op:
            return close_location(bar.open, bar.high, bar.low, bar.close)
        # Mặc định là wick_rejection_strength
        return wick_rejection_strength(bar.open, bar.high, bar.low, bar.close)

    def _compute_l2_node(
        self,
        node: Any,
        bar: OHLCVBar,
        snapshot: DecisionSnapshot,
    ) -> tuple[Decimal, str, str | None]:
        """Tính chỉ báo L2; trả (giá trị, nguồn, feature_id đã dùng nếu có).

        ATR ưu tiên feature đã quản trị trong snapshot (`atr_<period>` cùng timeframe), sau đó
        tới trung bình true range của `period` nến gần nhất. Chỉ khi thiếu cả hai mới dùng biên
        độ một nến và gắn nguồn `single_bar_range` để nơi gọi quyết định có chấp nhận hay không.
        """
        op = node.operation.lower()
        if "atr" in op or "true_range" in op:
            period = int(node.parameters.get("period", 14))
            timeframe = snapshot.series[0].timeframe
            name = f"atr_{period}"
            for feature in snapshot.features:
                if (
                    feature.name == name
                    and feature.timeframe == timeframe
                    and feature.value is not None
                    and feature.value > 0
                ):
                    return Decimal(str(feature.value)), f"feature:{name}", feature.feature_id
            bars = snapshot.series[0].bars
            if len(bars) > period:
                window = bars[-(period + 1) :]
                ranges = [
                    max(
                        current.high - current.low,
                        abs(current.high - previous.close),
                        abs(current.low - previous.close),
                    )
                    for previous, current in zip(window, window[1:], strict=False)
                ]
                return sum(ranges, Decimal("0")) / Decimal(len(ranges)), f"bars:{period}", None
            return bar.high - bar.low, ATR_SOURCE_SINGLE_BAR, None
        return bar.close, "last_close", None

    def _compute_l3_node(
        self,
        node: Any,
        node_values: dict[str, Any],
    ) -> Decimal:
        """Tính toán điểm số tổng hợp L3."""
        # Gom các giá trị đầu vào từ L1/L2
        scores = [v for v in node_values.values() if isinstance(v, Decimal)]
        if scores:
            return sum(scores) / Decimal(len(scores))
        return Decimal("0.0")

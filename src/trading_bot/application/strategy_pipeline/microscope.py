"""Dịch vụ Kính hiển vi soi vết lịch sử (Microscope Replay Workspace).

Cung cấp khả năng mổ xẻ, ghi vết chi tiết (trace) đầu vào/đầu ra của từng node
ở tất cả 7 phân tầng L0→L6 cho Strategy Card theo mô hình QUANTSMART.
Hỗ trợ kiểm tra nến tức thời, phát hiện ngắt mạch (Short-Circuit) và tính toán
vùng thực thi (Execution Zones).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from trading_bot.application.strategy_pipeline.context_gate import (
    ContextGateResult,
    ContextGateStatus,
)
from trading_bot.application.strategy_pipeline.pipeline_engine import (
    StrategyPipelineEngine,
    compute_candidate_id,
)
from trading_bot.application.strategy_pipeline.trigger_evaluator import (
    TriggerEvaluationResult,
    TriggerSignal,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    MarketSeries,
    SeriesQuality,
)
from trading_bot.domain.strategy_card.anatomy import upper_wick_ratio
from trading_bot.domain.strategy_card.models import (
    CompositionLayer,
    GraphEdge,
    GraphNode,
    NodeExecutionRole,
)
from trading_bot.domain.strategy_card.schema import StrategyCard, compute_canonical_hash


@dataclass(frozen=True)
class LayerTrace:
    """Bản ghi vết chi tiết của từng nút (node) trong phân tầng L0→L6."""

    layer: CompositionLayer
    node_id: str
    operation: str
    inputs: dict[str, Any]
    output: Any
    status: str  # "SUCCESS", "FAILED", "BLOCK", "PASS", "TRIGGERED", "NO_SIGNAL", "SKIPPED"

    def to_dict(self) -> dict[str, Any]:
        """Chuyển đổi thành từ điển JSON-serializable."""
        layer_val = self.layer.value if hasattr(self.layer, "value") else str(self.layer)
        # Chuyển đổi an toàn các giá trị Decimal và complex trong inputs/output
        clean_inputs = _sanitize_for_json(self.inputs)
        clean_output = _sanitize_for_json(self.output)
        return {
            "layer": layer_val,
            "node_id": self.node_id,
            "operation": self.operation,
            "inputs": clean_inputs,
            "output": clean_output,
            "status": self.status,
        }


@dataclass(frozen=True)
class MicroscopeTrace:
    """Kết quả kiểm định toàn diện kính hiển vi cho một nến qua 7 phân tầng."""

    card_id: str
    card_name: str
    content_hash: str
    timestamp: str
    ohlcv: dict[str, Decimal]
    layer_traces: list[LayerTrace]
    is_short_circuited: bool
    short_circuit_reason: str | None
    context_gate_passed: bool
    trigger_signal: str
    zones: dict[str, Decimal] | None
    candidate_id: str | None

    def to_dict(self) -> dict[str, Any]:
        """Chuyển đổi thành từ điển JSON-serializable an toàn phục vụ API."""
        return {
            "card_id": self.card_id,
            "card_name": self.card_name,
            "content_hash": self.content_hash,
            "timestamp": self.timestamp,
            "ohlcv": {k: float(v) for k, v in self.ohlcv.items()},
            "layer_traces": [trace.to_dict() for trace in self.layer_traces],
            "is_short_circuited": self.is_short_circuited,
            "short_circuit_reason": self.short_circuit_reason,
            "context_gate_passed": self.context_gate_passed,
            "trigger_signal": self.trigger_signal,
            "zones": (
                {k: float(v) for k, v in self.zones.items()} if self.zones is not None else None
            ),
            "candidate_id": self.candidate_id,
        }


@dataclass(frozen=True)
class PresetCandle:
    """Mẫu nến điển hình định sẵn phục vụ kiểm thử và mô phỏng trực quan."""

    id: str
    title: str
    description: str
    ohlcv: dict[str, Decimal]
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Chuyển đổi thành dict phục vụ giao diện Web."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "ohlcv": {k: float(v) for k, v in self.ohlcv.items()},
            "context": _sanitize_for_json(self.context),
        }


def _sanitize_for_json(val: Any) -> Any:
    """Chuyển đổi Decimal, Tuple, Dataclass thành kiểu dữ liệu JSON chuẩn."""
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, dict):
        return {k: _sanitize_for_json(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_sanitize_for_json(v) for v in val]
    if hasattr(val, "value"):
        return val.value
    return val


class MicroscopeReplayService:
    """Dịch vụ soi kính hiển vi quy trình chiến lược (Microscope Replay Service)."""

    def __init__(self, engine: StrategyPipelineEngine | None = None) -> None:
        self._engine = engine or StrategyPipelineEngine()

    def get_presets(self) -> list[PresetCandle]:
        """Cung cấp các mẫu nến điển hình có sẵn (Presets)."""
        return [
            PresetCandle(
                id="preset-hammer-long",
                title="Mẫu 1 — Nến Hammer đảo chiều tăng (L4 PASS, L5 LONG, L6 Zones đầy đủ)",
                description=(
                    "Nến búa tăng giá có râu dưới dài mạnh mẽ, giá đóng cửa gần đỉnh nến. "
                    "Biên độ biến động ATR = 12.0 vượt qua ngưỡng lọc bối cảnh (L4 PASS). "
                    "Ngòi nổ bắt đáy kích hoạt tín hiệu LONG (L5 LONG), thiết lập đầy đủ "
                    "tọa độ Entry, Stop Loss bảo vệ và Take Profit mục tiêu với R:R 2.0 (L6 Zones)."
                ),
                ohlcv={
                    "open": Decimal("100.0"),
                    "high": Decimal("102.0"),
                    "low": Decimal("90.0"),
                    "close": Decimal("101.0"),
                    "volume": Decimal("150.0"),
                },
                context={"direction": "LONG", "min_atr": Decimal("5.0")},
            ),
            PresetCandle(
                id="preset-low-vol-block",
                title="Mẫu 2 — Nến biến động thấp (L4 BLOCK do ATR < 15, Ngắt mạch tức thì)",
                description=(
                    "Nến đi ngang có biên độ dao động cực hẹp (High - Low = 2.0). Cổng bối cảnh "
                    "L4 Context Gate phát hiện ATR 2.0 < 15.0 (hoặc dưới ngưỡng an toàn) nên "
                    "lập tức kích hoạt ngắt mạch khẩn cấp (Short-Circuit), chặn đứng việc đánh "
                    "giá ngòi nổ L5 Trigger và không sinh lệnh nhằm bảo vệ vốn "
                    "khỏi thị trường chop."
                ),
                ohlcv={
                    "open": Decimal("100.0"),
                    "high": Decimal("102.0"),
                    "low": Decimal("100.0"),
                    "close": Decimal("101.0"),
                    "volume": Decimal("10.0"),
                },
                context={"min_atr": Decimal("15.0")},
            ),
            PresetCandle(
                id="preset-shooting-star-short",
                title="Mẫu 3 — Nến Shooting Star đảo chiều giảm (L4 PASS, L5 SHORT, L6 Zones)",
                description=(
                    "Nến sao băng từ chối đỉnh với râu trên dài vượt trội "
                    "(upper wick ratio > 0.8), phe bán chiếm ưu thế tại đỉnh. "
                    "Cổng bối cảnh L4 PASS với ATR = 22.0. Ngòi nổ kích hoạt tín hiệu bán khống "
                    "(L5 SHORT_ENTRY), thiết lập cấu trúc rủi ro hình học với Stop Loss đặt "
                    "trên đỉnh và Take Profit nằm phía dưới điểm vào lệnh."
                ),
                ohlcv={
                    "open": Decimal("100.0"),
                    "high": Decimal("120.0"),
                    "low": Decimal("98.0"),
                    "close": Decimal("101.0"),
                    "volume": Decimal("200.0"),
                },
                context={"direction": "SHORT", "min_atr": Decimal("5.0")},
            ),
        ]

    def inspect_bar(
        self,
        card: StrategyCard,
        ohlcv: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> MicroscopeTrace:
        """Chạy nến qua Strategy Pipeline và ghi vết chi tiết 7 phân tầng L0→L6."""
        ctx = context or {}

        # 1. Chuẩn hóa giá trị nến
        open_val = Decimal(str(ohlcv.get("open", "100.0")))
        high_val = Decimal(str(ohlcv.get("high", "100.0")))
        low_val = Decimal(str(ohlcv.get("low", "100.0")))
        close_val = Decimal(str(ohlcv.get("close", "100.0")))
        volume_val = Decimal(str(ohlcv.get("volume", "100.0")))

        clean_ohlcv = {
            "open": open_val,
            "high": high_val,
            "low": low_val,
            "close": close_val,
            "volume": volume_val,
        }

        # Thời gian
        now_utc = datetime.now(UTC)
        timestamp_str = str(ctx.get("timestamp", now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")))
        symbol = (
            card.compatibility.target_symbols[0] if card.compatibility.target_symbols else "BTCUSDT"
        )
        timeframe = card.compatibility.timeframes[0] if card.compatibility.timeframes else "15m"
        content_hash = compute_canonical_hash(card)
        layer_traces: list[LayerTrace] = []
        node_values: dict[str, Any] = {}

        # -------------------------------------------------------------------
        # TẦNG L0: KIỂM TRA ĐỘ SẠCH DỮ LIỆU NẾN (DATA HYGIENE THÔ TRƯỚC)
        # -------------------------------------------------------------------
        l0_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L0_DATA_HYGIENE]
        l0_node = (
            l0_nodes[0]
            if l0_nodes
            else GraphNode(
                node_id="l0_hygiene",
                layer=CompositionLayer.L0_DATA_HYGIENE,
                role=NodeExecutionRole.STATELESS_TRANSFORM,
                operation="data_hygiene_check",
            )
        )

        hygiene_error: str | None = None
        if high_val < low_val:
            hygiene_error = f"Nến vi phạm High < Low (High={high_val}, Low={low_val})"
        elif high_val < max(open_val, close_val):
            hygiene_error = f"Nến vi phạm High < max(Open, Close) (High={high_val})"
        elif low_val > min(open_val, close_val):
            hygiene_error = f"Nến vi phạm Low > min(Open, Close) (Low={low_val})"
        elif (
            open_val <= Decimal("0")
            or close_val <= Decimal("0")
            or high_val <= Decimal("0")
            or low_val <= Decimal("0")
        ):
            hygiene_error = "Giá Open/High/Low/Close phải là số dương"

        if hygiene_error:
            # Ngắt mạch ngay tại L0
            layer_traces.append(
                LayerTrace(
                    layer=CompositionLayer.L0_DATA_HYGIENE,
                    node_id=l0_node.node_id,
                    operation=l0_node.operation,
                    inputs=clean_ohlcv,
                    output=hygiene_error,
                    status="FAILED",
                )
            )

            # Các tầng còn lại L1->L6 đều bị SKIPPED
            for node in card.nodes:
                if node.layer != CompositionLayer.L0_DATA_HYGIENE:
                    layer_traces.append(
                        LayerTrace(
                            layer=node.layer,
                            node_id=node.node_id,
                            operation=node.operation,
                            inputs={},
                            output=None,
                            status="SKIPPED",
                        )
                    )

            return MicroscopeTrace(
                card_id=card.card_id,
                card_name=card.name,
                content_hash=content_hash,
                timestamp=timestamp_str,
                ohlcv=clean_ohlcv,
                layer_traces=layer_traces,
                is_short_circuited=True,
                short_circuit_reason=f"Ngắt mạch tại Tầng L0 Data Hygiene: {hygiene_error}",
                context_gate_passed=False,
                trigger_signal="NONE",
                zones=None,
                candidate_id=None,
            )

        bar = OHLCVBar(
            symbol=symbol,
            timeframe=timeframe,
            open_time=now_utc - timedelta(minutes=15),
            close_time=now_utc,
            event_at=now_utc,
            received_at=now_utc,
            available_at=now_utc,
            open=open_val,
            high=high_val,
            low=low_val,
            close=close_val,
            volume=volume_val,
            is_closed=True,
        )

        layer_traces.append(
            LayerTrace(
                layer=CompositionLayer.L0_DATA_HYGIENE,
                node_id=l0_node.node_id,
                operation=l0_node.operation,
                inputs=clean_ohlcv,
                output="OK: Dữ liệu nến sạch sẽ, thỏa mãn tính nhân quả và hình học giá.",
                status="SUCCESS",
            )
        )

        # -------------------------------------------------------------------
        # TẦNG L1: KHỐI NGUYÊN TỬ GIẢI PHẪU NẾN (CANDLE ANATOMY)
        # -------------------------------------------------------------------
        target_direction = str(ctx.get("direction", "LONG")).upper()
        l1_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L1_CANDLE_ANATOMY]
        has_upper_node = any(
            "upper" in n.node_id.lower() or "upper" in n.operation.lower() for n in l1_nodes
        )
        for node in l1_nodes:
            if (
                target_direction == "SHORT"
                and not has_upper_node
                and "lower_wick" in node.operation.lower()
            ):
                val = upper_wick_ratio(bar.open, bar.high, bar.low, bar.close)
            else:
                val = self._engine._compute_l1_node(node.operation, bar)
            node_values[node.node_id] = val
            layer_traces.append(
                LayerTrace(
                    layer=node.layer,
                    node_id=node.node_id,
                    operation=node.operation,
                    inputs={
                        "open": open_val,
                        "high": high_val,
                        "low": low_val,
                        "close": close_val,
                    },
                    output=val,
                    status="SUCCESS",
                )
            )

        # -------------------------------------------------------------------
        # TẦNG L2: CHỈ BÁO KỸ THUẬT (INDICATORS)
        # -------------------------------------------------------------------
        l2_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L2_INDICATORS]
        for node in l2_nodes:
            val = self._engine._compute_l2_node(node, bar, node_values)
            node_values[node.node_id] = val
            layer_traces.append(
                LayerTrace(
                    layer=node.layer,
                    node_id=node.node_id,
                    operation=node.operation,
                    inputs={"bar_high": high_val, "bar_low": low_val, "bar_close": close_val},
                    output=val,
                    status="SUCCESS",
                )
            )

        # -------------------------------------------------------------------
        # TẦNG L3: THUẬT TOÁN ĐIỂM SỐ LƯỢNG HÓA (ALGORITHMS)
        # -------------------------------------------------------------------
        l3_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L3_ALGORITHMS]
        for node in l3_nodes:
            val = self._engine._compute_l3_node(node, node_values)
            node_values[node.node_id] = val
            layer_traces.append(
                LayerTrace(
                    layer=node.layer,
                    node_id=node.node_id,
                    operation=node.operation,
                    inputs={"node_values_in": list(node_values.keys())},
                    output=val,
                    status="SUCCESS",
                )
            )

        # -------------------------------------------------------------------
        # TẦNG L4: CỔNG BỐI CẢNH THỊ TRƯỜNG (CONTEXT GATE & SHORT-CIRCUIT)
        # -------------------------------------------------------------------
        l4_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L4_CONTEXT_GATE]
        series = MarketSeries(
            timeframe=timeframe,
            bars=(bar,),
            quality=SeriesQuality.READY,
            expected_interval=timedelta(minutes=15),
            max_event_age=timedelta(minutes=30),
            backfill_complete=True,
        )
        snapshot = DecisionSnapshot(
            symbol=symbol,
            decision_time=bar.close_time,
            snapshot_id=f"microscope-{int(bar.close_time.timestamp())}",
            series=(series,),
            features=(),
            position_is_flat=True,
            macro_blackout=bool(ctx.get("macro_blackout", False)),
        )

        # Hỗ trợ ghi đè tham số gate từ context (ví dụ min_atr)
        gate_nodes_to_eval = []
        for node in l4_nodes:
            params = dict(node.parameters)
            if "min_atr" in ctx:
                params["min_atr"] = ctx["min_atr"]
            if "max_atr" in ctx:
                params["max_atr"] = ctx["max_atr"]
            gate_nodes_to_eval.append(
                GraphNode(
                    node_id=node.node_id,
                    layer=node.layer,
                    role=node.role,
                    operation=node.operation,
                    parameters=params,
                    inputs=node.inputs,
                    outputs=node.outputs,
                )
            )

        gate_result: ContextGateResult = self._engine.context_gate_evaluator.evaluate_gates(
            gate_nodes=gate_nodes_to_eval,
            node_outputs=node_values,
            snapshot=snapshot,
            latest_bar=bar,
        )

        if gate_result.status == ContextGateStatus.BLOCK:
            # Ghi nhận trạng thái BLOCK cho các gate node
            reasons_str = "; ".join(gate_result.reasons)
            for node in l4_nodes:
                layer_traces.append(
                    LayerTrace(
                        layer=node.layer,
                        node_id=node.node_id,
                        operation=node.operation,
                        inputs=gate_result.metrics,
                        output=f"BLOCK: {reasons_str}",
                        status="BLOCK",
                    )
                )

            # NGẮT MẠCH TỨC THÌ (SHORT-CIRCUIT): L5 và L6 tuyệt đối SKIPPED
            for node in card.nodes:
                if node.layer in {
                    CompositionLayer.L5_STRATEGY_TRIGGER,
                    CompositionLayer.L6_ZONES,
                }:
                    layer_traces.append(
                        LayerTrace(
                            layer=node.layer,
                            node_id=node.node_id,
                            operation=node.operation,
                            inputs={},
                            output=None,
                            status="SKIPPED",
                        )
                    )

            return MicroscopeTrace(
                card_id=card.card_id,
                card_name=card.name,
                content_hash=content_hash,
                timestamp=timestamp_str,
                ohlcv=clean_ohlcv,
                layer_traces=layer_traces,
                is_short_circuited=True,
                short_circuit_reason=f"Ngắt mạch tại Tầng L4 Context Gate: {reasons_str}",
                context_gate_passed=False,
                trigger_signal="NONE",
                zones=None,
                candidate_id=None,
            )

        # L4 PASS
        for node in l4_nodes:
            layer_traces.append(
                LayerTrace(
                    layer=node.layer,
                    node_id=node.node_id,
                    operation=node.operation,
                    inputs=gate_result.metrics,
                    output="PASS: Bối cảnh thị trường cho phép kích hoạt chiến lược.",
                    status="PASS",
                )
            )

        # -------------------------------------------------------------------
        # TẦNG L5: NGÒI NỔ KÍCH HOẠT (STRATEGY TRIGGER) - CHỈ CHẠY KHI L4 PASS
        # -------------------------------------------------------------------
        l5_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L5_STRATEGY_TRIGGER]
        trigger_nodes_to_eval = []

        for node in l5_nodes:
            params = dict(node.parameters)
            if "direction" in ctx:
                params["direction"] = target_direction
            trigger_nodes_to_eval.append(
                GraphNode(
                    node_id=node.node_id,
                    layer=node.layer,
                    role=node.role,
                    operation=node.operation,
                    parameters=params,
                    inputs=node.inputs,
                    outputs=node.outputs,
                )
            )

        # Bổ sung edge thông minh nếu direction là SHORT và có node L1 upper wick
        eval_edges = list(card.edges)
        if target_direction == "SHORT":
            upper_wick_nodes = [
                n.node_id
                for n in l1_nodes
                if "upper" in n.operation.lower() or "upper" in n.node_id.lower()
            ]
            if upper_wick_nodes:
                new_edges = []
                for edge in eval_edges:
                    # Nếu cạnh nối từ lower wick sang trigger, thay bằng upper wick
                    if "upper" not in edge.from_node.lower() and any(
                        w in edge.from_node.lower() for w in ("wick", "lower")
                    ):
                        new_edges.append(
                            GraphEdge(
                                from_node=upper_wick_nodes[0],
                                from_port=edge.from_port,
                                to_node=edge.to_node,
                                to_port=edge.to_port,
                            )
                        )
                    else:
                        new_edges.append(edge)
                eval_edges = new_edges

        trigger_result: TriggerEvaluationResult = self._engine.trigger_evaluator.evaluate(
            trigger_nodes=trigger_nodes_to_eval,
            node_outputs=node_values,
            gate_result=gate_result,
            latest_bar=bar,
            edges=eval_edges,
        )

        if trigger_result.signal == TriggerSignal.NO_SIGNAL:
            for node in l5_nodes:
                layer_traces.append(
                    LayerTrace(
                        layer=node.layer,
                        node_id=node.node_id,
                        operation=node.operation,
                        inputs={"node_outputs": node_values},
                        output="NO_SIGNAL: Không có tín hiệu ngòi nổ nào thỏa mãn.",
                        status="NO_SIGNAL",
                    )
                )

            # L6 SKIPPED
            for node in card.nodes:
                if node.layer == CompositionLayer.L6_ZONES:
                    layer_traces.append(
                        LayerTrace(
                            layer=node.layer,
                            node_id=node.node_id,
                            operation=node.operation,
                            inputs={},
                            output="Bỏ qua do không có tín hiệu kích hoạt từ Tầng L5.",
                            status="SKIPPED",
                        )
                    )

            return MicroscopeTrace(
                card_id=card.card_id,
                card_name=card.name,
                content_hash=content_hash,
                timestamp=timestamp_str,
                ohlcv=clean_ohlcv,
                layer_traces=layer_traces,
                is_short_circuited=False,
                short_circuit_reason=None,
                context_gate_passed=True,
                trigger_signal="NO_SIGNAL",
                zones=None,
                candidate_id=None,
            )

        # Trigger kích hoạt thành công
        for node in l5_nodes:
            layer_traces.append(
                LayerTrace(
                    layer=node.layer,
                    node_id=node.node_id,
                    operation=node.operation,
                    inputs=trigger_result.metadata,
                    output=(
                        f"{trigger_result.signal.value} (Score: {trigger_result.trigger_score:.4f})"
                    ),
                    status="TRIGGERED",
                )
            )

        # -------------------------------------------------------------------
        # TẦNG L6: VÙNG GIÁ THỰC THI (EXECUTION ZONES)
        # -------------------------------------------------------------------
        l6_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L6_ZONES]
        sl_multiplier = Decimal("2.0")
        rr_ratio = Decimal("2.0")

        if l6_nodes:
            params = l6_nodes[0].parameters
            if "sl_multiplier" in params:
                sl_multiplier = Decimal(str(params["sl_multiplier"]))
            if "rr_ratio" in params:
                rr_ratio = Decimal(str(params["rr_ratio"]))

        # Lấy ATR từ node L2 hoặc tính biên độ nến
        current_atr = Decimal(str(node_values.get("l2_atr", str(bar.high - bar.low))))
        direction_str = "LONG" if trigger_result.signal == TriggerSignal.LONG_ENTRY else "SHORT"

        zones = self._engine.zone_builder.build_zones(
            direction=direction_str,
            current_price=bar.close,
            atr=current_atr,
            sl_multiplier=sl_multiplier,
            rr_ratio=rr_ratio,
        )

        candidate_id = compute_candidate_id(
            strategy_card_id=card.card_id,
            content_hash=content_hash,
            symbol=symbol,
            direction=direction_str,
            entry_price=zones.entry_price,
            stop_loss=zones.stop_loss_price,
            take_profit=zones.take_profit_price,
            risk_reward_ratio=zones.risk_reward_ratio,
            signal_at=bar.close_time,
        )

        zones_dict = {
            "entry_price": zones.entry_price,
            "stop_loss": zones.stop_loss_price,
            "take_profit": zones.take_profit_price,
            "risk_reward_ratio": zones.risk_reward_ratio,
        }

        for node in l6_nodes:
            layer_traces.append(
                LayerTrace(
                    layer=node.layer,
                    node_id=node.node_id,
                    operation=node.operation,
                    inputs={
                        "direction": direction_str,
                        "current_price": bar.close,
                        "atr": current_atr,
                        "sl_multiplier": sl_multiplier,
                        "rr_ratio": rr_ratio,
                    },
                    output=zones_dict,
                    status="SUCCESS",
                )
            )

        return MicroscopeTrace(
            card_id=card.card_id,
            card_name=card.name,
            content_hash=content_hash,
            timestamp=timestamp_str,
            ohlcv=clean_ohlcv,
            layer_traces=layer_traces,
            is_short_circuited=False,
            short_circuit_reason=None,
            context_gate_passed=True,
            trigger_signal=trigger_result.signal.value,
            zones=zones_dict,
            candidate_id=candidate_id,
        )

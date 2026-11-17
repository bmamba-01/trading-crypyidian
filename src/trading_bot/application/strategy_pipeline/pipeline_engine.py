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
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median
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
from trading_bot.domain.market.features import (
    adx_dmi,
    atr,
    bollinger_bands,
    ema,
    macd,
    realized_volatility,
    rsi,
    sma,
    vwap,
    zscore,
)
from trading_bot.domain.strategy.contracts import FeatureQuality
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
PIPELINE_ENGINE_VERSION = "l0-l6-v3"
# Nguồn ATR khi không có feature đã quản trị lẫn đủ lịch sử nến: chỉ là biên độ của một nến.
ATR_SOURCE_SINGLE_BAR = "single_bar_range"
# Nguồn chỉ báo khi chưa đủ nến lịch sử warmup hoặc thiếu feature trên snapshot.
WARMUP_SOURCE_INSUFFICIENT = "insufficient_history"
WARMUP_SOURCES = frozenset({ATR_SOURCE_SINGLE_BAR, WARMUP_SOURCE_INSUFFICIENT})


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
    indicator_sources: dict[str, str] = field(default_factory=dict)


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
            val, source, feature_id = self._compute_l3_node_with_source(
                node, node_values, bar=bar, snapshot=snapshot
            )
            node_values[node.node_id] = val
            indicator_sources[node.node_id] = source
            if feature_id is not None:
                indicator_feature_ids.append(feature_id)

        # -------------------------------------------------------------------
        # TẦNG L4: CONTEXT GATE EVALUATION (SHORT-CIRCUIT NẾU BLOCK)
        # -------------------------------------------------------------------
        l4_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L4_CONTEXT_GATE]
        gate_result = self.context_gate_evaluator.evaluate_gates(
            gate_nodes=l4_nodes,
            node_outputs=node_values,
            snapshot=snapshot,
            latest_bar=bar,
            indicator_sources=indicator_sources,
        )

        if gate_result.status == ContextGateStatus.BLOCK:
            # Ngắt mạch tức thì (Short-circuit): L5 và L6 không được thực thi!
            return PipelineExecutionResult(
                status="CONTEXT_GATE_BLOCKED",
                gate_result=gate_result,
                reasons=gate_result.reasons,
                node_values=node_values,
                indicator_sources=indicator_sources,
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
                indicator_sources=indicator_sources,
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

        # Lấy ATR từ node L2 nếu có, hoặc qua managed feature / biên độ nến
        current_atr = self._resolve_atr_value(node_values, bar, snapshot)
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
            indicator_sources=indicator_sources,
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

    def _snapshot_feature(
        self, snapshot: DecisionSnapshot, name: str, timeframe: str
    ) -> tuple[Decimal, str, str] | None:
        for feature in snapshot.features:
            if (
                feature.name == name
                and feature.timeframe == timeframe
                and feature.quality == FeatureQuality.READY
                and feature.value is not None
                and math.isfinite(feature.value)
            ):
                return Decimal(str(feature.value)), f"feature:{name}", feature.feature_id
        return None

    def _resolve_atr_value(
        self,
        node_values: dict[str, Any],
        bar: OHLCVBar,
        snapshot: DecisionSnapshot,
    ) -> Decimal:
        for key, val in node_values.items():
            if "atr" in key.lower() and isinstance(val, (int, float, Decimal)):
                dval = Decimal(str(val))
                if dval > Decimal("0"):
                    return dval
        if snapshot.series:
            tf = snapshot.series[0].timeframe
            found = self._snapshot_feature(snapshot, "atr_14", tf)
            if found is not None and found[0] > Decimal("0"):
                return found[0]
        return bar.high - bar.low

    def _compute_l2_node(
        self,
        node: Any,
        bar: OHLCVBar,
        snapshot: DecisionSnapshot,
    ) -> tuple[Decimal, str, str | None]:
        """Tính chỉ báo L2 point-in-time; trả (giá trị, nguồn, feature_id đã dùng nếu có)."""
        op = node.operation.lower()
        params = node.parameters
        timeframe = str(params.get("timeframe", snapshot.series[0].timeframe))
        series = snapshot.get_series(timeframe) or snapshot.series[0]
        bars = series.bars

        if "atr" in op or "true_range" in op:
            period = int(params.get("period", 14))
            name = f"atr_{period}"
            found = self._snapshot_feature(snapshot, name, timeframe)
            if found is not None and found[0] > Decimal("0"):
                return found
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

        if "ema" in op or "exponential_moving_average" in op:
            period = int(params.get("period", 20))
            found = self._snapshot_feature(snapshot, f"ema_{period}", timeframe)
            if found is not None:
                return found
            if len(bars) >= period:
                val = ema([float(b.close) for b in bars], period)[-1]
                if val is not None:
                    return Decimal(str(round(val, 6))), f"bars:ema_{period}", None
            return bar.close, WARMUP_SOURCE_INSUFFICIENT, None

        if "sma" in op or "simple_moving_average" in op:
            period = int(params.get("period", 20))
            found = self._snapshot_feature(snapshot, f"sma_{period}", timeframe)
            if found is not None:
                return found
            if len(bars) >= period:
                val = sma([float(b.close) for b in bars], period)[-1]
                if val is not None:
                    return Decimal(str(round(val, 6))), f"bars:sma_{period}", None
            return bar.close, WARMUP_SOURCE_INSUFFICIENT, None

        if "rsi" in op:
            period = int(params.get("period", 14))
            found = self._snapshot_feature(snapshot, f"rsi_{period}", timeframe)
            if found is not None:
                return found
            if len(bars) > period:
                val = rsi([float(b.close) for b in bars], period)[-1]
                if val is not None:
                    return Decimal(str(round(val, 4))), f"bars:rsi_{period}", None
            return Decimal("50"), WARMUP_SOURCE_INSUFFICIENT, None

        if "adx" in op:
            period = int(params.get("period", 14))
            found = self._snapshot_feature(snapshot, f"adx_{period}", timeframe)
            if found is not None:
                return found
            if len(bars) >= 2 * period:
                _, _, _, adx_s = adx_dmi(
                    [float(b.high) for b in bars],
                    [float(b.low) for b in bars],
                    [float(b.close) for b in bars],
                    period,
                )
                val = adx_s[-1]
                if val is not None:
                    return Decimal(str(round(val, 4))), f"bars:adx_{period}", None
            return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None

        if "realized_volatility" in op or "rv" in op:
            period = int(params.get("period", 20))
            found = self._snapshot_feature(snapshot, f"realized_volatility_{period}", timeframe)
            if found is not None:
                return found
            if len(bars) > period:
                val = realized_volatility([float(b.close) for b in bars], period)[-1]
                if val is not None:
                    return Decimal(str(round(val, 6))), f"bars:rv_{period}", None
            return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None

        if "bollinger_bandwidth" in op or "bb_width" in op:
            period = int(params.get("period", 20))
            upper_f = self._snapshot_feature(snapshot, f"bollinger_upper_{period}", timeframe)
            lower_f = self._snapshot_feature(snapshot, f"bollinger_lower_{period}", timeframe)
            mid_f = self._snapshot_feature(snapshot, f"sma_{period}", timeframe)
            if upper_f and lower_f and mid_f and mid_f[0] > 0:
                bw = (upper_f[0] - lower_f[0]) / mid_f[0]
                return bw, f"feature:bollinger_bw_{period}", upper_f[2]
            if len(bars) >= period:
                mid, up, low = bollinger_bands([float(b.close) for b in bars], period=period)
                u, m, lw = up[-1], mid[-1], low[-1]
                if u is not None and m is not None and lw is not None and m > 0:
                    return Decimal(str(round((u - lw) / m, 6))), f"bars:bbw_{period}", None
            return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None

        if "vwap" in op:
            found = self._snapshot_feature(snapshot, "session_vwap", timeframe)
            if found is not None:
                return found
            utc_day = bar.open_time.astimezone(UTC).date()
            day_bars = [b for b in bars if b.open_time.astimezone(UTC).date() == utc_day]
            if len(day_bars) >= 8:
                prices = [float((b.high + b.low + b.close) / Decimal("3")) for b in day_bars]
                vols = [float(b.volume) for b in day_bars]
                val = vwap(prices, vols)[-1]
                if val is not None:
                    return Decimal(str(round(val, 6))), "bars:session_vwap", None
            return bar.close, WARMUP_SOURCE_INSUFFICIENT, None

        if "macd" in op:
            fast = int(params.get("fast", 12))
            slow = int(params.get("slow", 26))
            sig = int(params.get("signal", 9))
            if len(bars) >= slow + sig:
                _, _, hist = macd([float(b.close) for b in bars], fast=fast, slow=slow, signal=sig)
                val = hist[-1]
                if val is not None:
                    return Decimal(str(round(val, 6))), "bars:macd_hist", None
            return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None

        if "taker_flow" in op or "taker_imbalance" in op:
            tf_window = int(params.get("window", 3))
            if len(bars) < tf_window:
                return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None
            recent = bars[-tf_window:]
            if any(
                b.taker_buy_base_volume is None
                or b.volume <= 0
                or b.taker_buy_base_volume < 0
                or b.taker_buy_base_volume > b.volume
                for b in recent
            ):
                return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None
            ti_list = [
                (2.0 * float(b.taker_buy_base_volume) - float(b.volume)) / float(b.volume)
                for b in recent
                if b.taker_buy_base_volume is not None
            ]
            return (
                Decimal(str(round(sum(ti_list) / len(ti_list), 6))),
                f"bars:taker_flow_{tf_window}",
                None,
            )

        return bar.close, "last_close", None

    def _compute_l3_node(
        self,
        node: Any,
        node_values: dict[str, Any],
    ) -> Decimal:
        """Hàm tương thích cũ cho các nơi chỉ truyền `(node, node_values)`."""
        val, _, _ = self._compute_l3_node_with_source(node, node_values)
        return val

    def _compute_l3_node_with_source(
        self,
        node: Any,
        node_values: dict[str, Any],
        *,
        bar: OHLCVBar | None = None,
        snapshot: DecisionSnapshot | None = None,
    ) -> tuple[Decimal, str, str | None]:
        """Tính điểm tổng hợp L3 point-in-time kèm nguồn dữ liệu/warmup."""
        op = node.operation.lower()
        params = node.parameters
        timeframe = snapshot.series[0].timeframe if (snapshot and snapshot.series) else "15m"
        bars = snapshot.series[0].bars if (snapshot and snapshot.series) else ()

        if "zscore" in op:
            window = int(params.get("window", 20))
            if snapshot is not None:
                found = self._snapshot_feature(snapshot, f"zscore_{window}", timeframe)
                if found is not None:
                    return found
            if len(bars) > window:
                z_val = zscore([float(b.close) for b in bars], window)[-1]
                if z_val is not None and math.isfinite(z_val):
                    return Decimal(str(round(z_val, 6))), f"bars:zscore_{window}", None
            return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None

        if "tsmom" in op:
            horizons = tuple(int(h) for h in params.get("horizons", (42, 126, 252)))
            if len(bars) <= max(horizons):
                return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None
            c_cur = float(bars[-1].close)
            signs = [
                1
                if c_cur > float(bars[-1 - h].close)
                else -1
                if c_cur < float(bars[-1 - h].close)
                else 0
                for h in horizons
            ]
            score = sum(signs) / len(signs)
            pos = sum(1 for s in signs if s > 0)
            neg = sum(1 for s in signs if s < 0)
            if pos >= 2 or neg >= 2:
                return Decimal(str(round(score, 4))), "bars:tsmom", None
            return Decimal("0"), "bars:tsmom", None

        if "squeeze" in op:
            lookback = int(params.get("lookback", 20))
            vol_mult = float(params.get("volume_multiplier", 1.5))
            if len(bars) < 60:
                return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None
            closes = [float(b.close) for b in bars]
            highs = [float(b.high) for b in bars]
            lows = [float(b.low) for b in bars]
            bb_m, bb_u, bb_l = bollinger_bands(closes, period=lookback)
            ema20 = ema(closes, lookback)
            atr14 = atr(highs, lows, closes, 14)
            cur, prev = len(bars) - 1, len(bars) - 2
            if any(s[idx] is None for s in (bb_u, bb_m, bb_l, ema20, atr14) for idx in (cur, prev)):
                return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None
            u_c, m_c, l_c = bb_u[cur], bb_m[cur], bb_l[cur]
            u_p, m_p, l_p = bb_u[prev], bb_m[prev], bb_l[prev]
            e_p, a_p = ema20[prev], atr14[prev]
            assert None not in (u_c, m_c, l_c, u_p, m_p, l_p, e_p, a_p)
            bw_c = (u_c - l_c) / m_c  # type: ignore[operator]
            bw_p = (u_p - l_p) / m_p  # type: ignore[operator]
            kc_in = (u_p < e_p + 1.5 * a_p) and (l_p > e_p - 1.5 * a_p)  # type: ignore[operator]
            bw_hist = [
                (bb_u[i] - bb_l[i]) / bb_m[i]  # type: ignore[operator]
                for i in range(max(0, prev - 240), prev)
                if bb_u[i] is not None and bb_m[i] and bb_l[i] is not None
            ]
            bw_hist.sort()
            pct25 = bw_hist[int(0.25 * (len(bw_hist) - 1))] if bw_hist else 0.0
            in_squeeze = kc_in or (bool(bw_hist) and bw_p <= pct25)
            prior_vols = [float(b.volume) for b in bars[-1 - lookback : -1]]
            med_vol = median(prior_vols)
            vol_ok = med_vol > 0 and float(bars[-1].volume) > vol_mult * med_vol
            hh = max(float(b.high) for b in bars[-1 - lookback : -1])
            ll = min(float(b.low) for b in bars[-1 - lookback : -1])
            c_cur = float(bars[-1].close)
            if in_squeeze and bw_c > bw_p and vol_ok:
                if c_cur > hh and c_cur > u_c:  # type: ignore[operator]
                    return Decimal("1.0"), "bars:squeeze_release", None
                if c_cur < ll and c_cur < l_c:  # type: ignore[operator]
                    return Decimal("-1.0"), "bars:squeeze_release", None
            return Decimal("0"), "bars:squeeze_release", None

        if "pullback" in op:
            if len(bars) < 83 or snapshot is None or bar is None:
                return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None
            closes = [float(b.close) for b in bars]
            e20_s = ema(closes, 20)
            e80_s = ema(closes, 80)
            rsi_s = rsi(closes, 14)
            if e20_s[-1] is None or e80_s[-1] is None or e80_s[-4] is None or rsi_s[-1] is None:
                return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None
            htf_ok_long, htf_ok_short = True, True
            htf_series = snapshot.get_series("4h")
            if htf_series is not None and htf_series.bars:
                e200 = self._snapshot_feature(snapshot, "ema_200", "4h")
                if e200 is not None:
                    c4 = float(htf_series.bars[-1].close)
                    htf_ok_long = c4 > float(e200[0])
                    htf_ok_short = c4 < float(e200[0])
            rsi_low = float(params.get("rsi_low", 40.0))
            rsi_high = float(params.get("rsi_high", 60.0))
            in_rsi = rsi_low <= rsi_s[-1] <= rsi_high
            e20, e80, slope = e20_s[-1], e80_s[-1], e80_s[-1] - e80_s[-4]
            c_val, o_val, h_val, l_val = (
                float(bar.close),
                float(bar.open),
                float(bar.high),
                float(bar.low),
            )
            if (
                htf_ok_long
                and in_rsi
                and e20 > e80
                and slope > 0
                and l_val <= e20 < c_val
                and c_val > o_val
            ):
                return Decimal("1.0"), "bars:pullback_alignment", None
            if (
                htf_ok_short
                and in_rsi
                and e20 < e80
                and slope < 0
                and h_val >= e20 > c_val
                and c_val < o_val
            ):
                return Decimal("-1.0"), "bars:pullback_alignment", None
            return Decimal("0"), "bars:pullback_alignment", None

        if "vwap" in op:
            if bar is None:
                return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None
            utc_day = bar.open_time.astimezone(UTC).date()
            day_bars = [b for b in bars if b.open_time.astimezone(UTC).date() == utc_day]
            if len(day_bars) < 8:
                return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None
            vwap_val: Decimal | None = None
            for k, v in node_values.items():
                if "vwap" in k.lower() and isinstance(v, (int, float, Decimal)):
                    vwap_val = Decimal(str(v))
                    break
            atr_val = (
                self._resolve_atr_value(node_values, bar, snapshot)
                if snapshot
                else (bar.high - bar.low)
            )
            if vwap_val is None or atr_val <= 0:
                return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None
            dev = (bar.close - vwap_val) / atr_val
            return dev.quantize(Decimal("0.0001")), "bars:vwap_dev", None

        if "macd" in op:
            if len(bars) < 35 or snapshot is None:
                return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None
            closes = [float(b.close) for b in bars]
            m_line, _, hist = macd(closes, 12, 26, 9)
            if hist[-1] is None or hist[-2] is None or m_line[-1] is None or m_line[-2] is None:
                return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None
            htf_ok_long, htf_ok_short = True, True
            htf_series = snapshot.get_series("4h")
            if htf_series is not None and htf_series.bars:
                e200 = self._snapshot_feature(snapshot, "ema_200", "4h")
                if e200 is not None:
                    c4 = float(htf_series.bars[-1].close)
                    htf_ok_long = c4 > float(e200[0])
                    htf_ok_short = c4 < float(e200[0])
            pdi = self._snapshot_feature(snapshot, "plus_di_14", timeframe)
            mdi = self._snapshot_feature(snapshot, "minus_di_14", timeframe)
            di_long = True if (pdi is None or mdi is None) else (pdi[0] > mdi[0])
            di_short = True if (pdi is None or mdi is None) else (mdi[0] > pdi[0])
            if htf_ok_long and di_long and hist[-2] <= 0 < hist[-1] and m_line[-1] > m_line[-2]:
                return Decimal("1.0"), "bars:macd_momentum", None
            if htf_ok_short and di_short and hist[-2] >= 0 > hist[-1] and m_line[-1] < m_line[-2]:
                return Decimal("-1.0"), "bars:macd_momentum", None
            return Decimal("0"), "bars:macd_momentum", None

        if "taker_flow" in op:
            lookback = int(params.get("lookback", 20))
            min_mult = float(params.get("min_volume_multiple", 1.3))
            if len(bars) < lookback + 1 or snapshot is None or bar is None:
                return Decimal("0"), WARMUP_SOURCE_INSUFFICIENT, None
            recent = bars[-3:]
            if any(
                b.taker_buy_base_volume is None
                or b.volume <= 0
                or b.taker_buy_base_volume < 0
                or b.taker_buy_base_volume > b.volume
                for b in recent
            ):
                return Decimal("0"), "taker_flow_unavailable", None
            ti_vals = [
                (2.0 * float(b.taker_buy_base_volume) - float(b.volume)) / float(b.volume)
                for b in recent
                if b.taker_buy_base_volume is not None
            ]
            ti_cur = ti_vals[-1]
            ti_avg3 = sum(ti_vals) / len(ti_vals)
            prior_med = median([float(b.volume) for b in bars[-1 - lookback : -1]])
            if prior_med <= 0 or float(bar.volume) < min_mult * prior_med:
                return Decimal("0"), "bars:taker_flow", None
            hh = max(float(b.high) for b in bars[-1 - lookback : -1])
            ll = min(float(b.low) for b in bars[-1 - lookback : -1])
            htf_ok_long, htf_ok_short = True, True
            htf_series = snapshot.get_series("1h")
            if htf_series is not None and htf_series.bars:
                htf_e80 = self._snapshot_feature(snapshot, "ema_80", "1h")
                if htf_e80 is not None:
                    c1h = float(htf_series.bars[-1].close)
                    htf_ok_long = c1h > float(htf_e80[0])
                    htf_ok_short = c1h < float(htf_e80[0])
            c_val = float(bar.close)
            if htf_ok_long and c_val > hh and ti_cur > 0 and ti_avg3 >= 0.12:
                return Decimal(str(round(ti_cur, 4))), "bars:taker_flow", None
            if htf_ok_short and c_val < ll and ti_cur < 0 and ti_avg3 <= -0.12:
                return Decimal(str(round(ti_cur, 4))), "bars:taker_flow", None
            return Decimal("0"), "bars:taker_flow", None

        # Mặc định cho score_normalizer hoặc node L3 tổng hợp
        scores = [v for v in node_values.values() if isinstance(v, Decimal)]
        if scores:
            return sum(scores) / Decimal(len(scores)), "composite", None
        return Decimal("0.0"), "composite", None

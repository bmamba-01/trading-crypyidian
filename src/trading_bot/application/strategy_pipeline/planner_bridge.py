"""Cầu nối chuyển đổi giữa Strategy Pipeline L0-L6 và TradePlanner Domain Contracts.

Module này đóng vai trò Adapter đảm bảo tính tương thích tuyệt đối giữa:
1. Đề xuất từ đồ thị QUANTSMART L0->L6 (StrategyPipelineEngine -> PipelineCandidate).
2. Hợp đồng dữ liệu đầu vào của TradePlanner (contracts.StrategyCandidate + ExitRecipe).

BẢO ĐẢM NGUYÊN TẮC QUANTSMART:
- Không cho phép bypass thẩm định rủi ro. Mọi candidate L6 đều phải đóng gói thành
  StrategyCandidate hợp lệ với ExitRecipe chuẩn để TradePlanner thẩm định vốn và kiểm tra.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from trading_bot.domain.strategy.contracts import (
    CandidateTarget,
    InvalidationRule,
)
from trading_bot.domain.strategy.contracts import StrategyCandidate as ContractCandidate
from trading_bot.domain.strategy.recipes import ExitRecipe
from trading_bot.domain.strategy_card.models import CompositionLayer

if TYPE_CHECKING:
    from trading_bot.application.strategy_pipeline.pipeline_engine import (
        StrategyCandidate as PipelineCandidate,
    )
    from trading_bot.domain.strategy.contracts import DecisionSnapshot
    from trading_bot.domain.strategy_card.schema import StrategyCard


class StrategyPipelinePlannerBridge:
    """Bộ chuyển đổi kết quả từ StrategyPipelineEngine sang hợp đồng cho TradePlanner."""

    @staticmethod
    def to_contract_candidate(
        pipeline_candidate: PipelineCandidate,
        card: StrategyCard,
        snapshot: DecisionSnapshot,
        settings_version_id: str = "v1.0.0",
        evidence_bar_keys: tuple[str, ...] | None = None,
        macro_evidence_ids: tuple[str, ...] = (),
        valid_duration: timedelta = timedelta(minutes=15),
    ) -> ContractCandidate:
        """Chuyển đổi PipelineCandidate thành ContractCandidate hoàn chỉnh cho TradePlanner."""
        # 1. Xác định ATR và bội số cho ExitRecipe
        raw_atr = pipeline_candidate.metadata.get("atr")
        try:
            atr = Decimal(str(raw_atr))
        except InvalidOperation as error:
            raise ValueError("PipelineCandidate thiếu ATR hợp lệ trong metadata.") from error
        if not atr.is_finite() or atr <= 0:
            raise ValueError("PipelineCandidate thiếu ATR dương trong metadata.")
        atr_val = float(atr)

        stop_distance = abs(pipeline_candidate.entry_price - pipeline_candidate.stop_loss)
        target_distance = abs(pipeline_candidate.take_profit - pipeline_candidate.entry_price)
        if stop_distance <= 0 or target_distance <= 0:
            raise ValueError("Vùng giá L6 phải có khoảng cách stop và mục tiêu dương.")
        # Bội số ATR lấy trực tiếp từ vùng giá L6 để TradePlanner dựng lại đúng SL/TP của thẻ.
        sl_multiplier = float(stop_distance / atr)
        tp_multiplier = float(target_distance / atr)

        # Đọc max_hold_bars và candidate_validity_minutes từ node L6 của thẻ nếu có.
        max_hold_bars = 12
        effective_validity = valid_duration
        l6_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L6_ZONES]
        if l6_nodes:
            l6_params = l6_nodes[0].parameters
            if "max_hold_bars" in l6_params:
                max_hold_bars = max(1, int(l6_params["max_hold_bars"]))
            if "candidate_validity_minutes" in l6_params:
                effective_validity = timedelta(
                    minutes=max(1, int(l6_params["candidate_validity_minutes"]))
                )

        recipe = ExitRecipe(
            recipe_id=f"recipe_{card.card_id[:8]}",
            stop_atr_multiple=sl_multiplier,
            take_profit_atr_multiple=tp_multiplier,
            max_hold_bars=max_hold_bars,
        )

        # 2. Xác định các mốc thời gian hiệu lực (evaluated_at tại replay là signal_at + 1µs)
        signal_at = pipeline_candidate.signal_at
        eligible_after = signal_at + timedelta(microseconds=1)
        expires_at = signal_at + effective_validity

        # 3. Bar keys bằng chứng
        if evidence_bar_keys is None:
            # Sinh natural bar key mặc định từ nến cuối
            tf = snapshot.series[0].timeframe
            ts = int(signal_at.timestamp())
            bar_key = f"{snapshot.symbol}_{tf}_{ts}"
            evidence_bar_keys = (bar_key,)

        # 4. Danh sách evidence features
        evidence_features = pipeline_candidate.evidence_nodes
        if not evidence_features:
            evidence_features = (f"{card.card_id}_node_L5",)

        direction_contract = "long" if pipeline_candidate.direction.upper() == "LONG" else "short"

        # 5. Bảo toàn vùng vào, R:R, điểm, điều kiện mất hiệu lực và metadata có cấu trúc (G5).
        entry_zone_low, entry_zone_high = _resolve_entry_zone(pipeline_candidate)
        card_score = _resolve_card_score(pipeline_candidate)
        targets = (
            CandidateTarget(
                index=1,
                price=pipeline_candidate.take_profit,
                fraction=Decimal("1"),
                r_multiple=pipeline_candidate.risk_reward_ratio,
                label="Mục tiêu L6",
            ),
        )
        invalidation = (
            InvalidationRule(
                kind="STOP_LOSS",
                detail_vi=f"Giá chạm SL {_fmt_vi(pipeline_candidate.stop_loss)} (dừng lỗ L6).",
                price=pipeline_candidate.stop_loss,
            ),
            InvalidationRule(
                kind="EXPIRY",
                detail_vi="Tín hiệu hết hạn nếu chưa vào lệnh trước mốc hết hạn.",
                expires_at=expires_at,
            ),
            InvalidationRule(
                kind="MAX_HOLD_BARS",
                detail_vi=f"Thoát theo thời gian sau tối đa {max_hold_bars} nến.",
                bars=max_hold_bars,
            ),
        )
        card_metadata = _structured_card_metadata(pipeline_candidate, card, atr, card_score)

        return ContractCandidate(
            strategy_id=card.card_id,
            strategy_release_id=card.version,
            release_content_hash=pipeline_candidate.content_hash,
            settings_version_id=settings_version_id,
            snapshot_id=snapshot.snapshot_id,
            symbol=snapshot.symbol,
            direction=direction_contract,
            reference_price=pipeline_candidate.entry_price,
            atr_value=atr_val,
            signal_at=signal_at,
            eligible_after=eligible_after,
            expires_at=expires_at,
            recipe=recipe,
            evidence_feature_ids=evidence_features,
            evidence_bar_keys=evidence_bar_keys,
            macro_evidence_ids=macro_evidence_ids,
            geometry_version="quantsmart_l0_l6",
            reason_code="QUANTSMART_L6_ZONE_CONFIRMED",
            detail_vi=_detail_vi(pipeline_candidate, card, atr),
            entry_zone_low=entry_zone_low,
            entry_zone_high=entry_zone_high,
            stop_price=pipeline_candidate.stop_loss,
            targets=targets,
            setup_id=f"{card.card_id}:v{card.version}",
            invalidation=invalidation,
            confidence_tier="uncalibrated",
            risk_reward_ratio=pipeline_candidate.risk_reward_ratio,
            card_score=card_score,
            card_metadata=card_metadata,
        )


def _fmt_vi(value: Decimal) -> str:
    """Định dạng số Decimal gọn kiểu Việt Nam cho chuỗi mô tả."""
    text = format(value.quantize(Decimal("0.0001")).normalize(), "f")
    return text.replace(".", ",")


def _resolve_entry_zone(pipeline_candidate: PipelineCandidate) -> tuple[Decimal, Decimal]:
    """Lấy vùng vào từ metadata L6 nếu có; mặc định thu về đúng ``entry_price``."""
    raw_low = pipeline_candidate.metadata.get("entry_zone_low")
    raw_high = pipeline_candidate.metadata.get("entry_zone_high")
    if raw_low is not None and raw_high is not None:
        try:
            low = Decimal(str(raw_low))
            high = Decimal(str(raw_high))
        except InvalidOperation:
            return pipeline_candidate.entry_price, pipeline_candidate.entry_price
        if low.is_finite() and high.is_finite() and low > 0 and high > 0:
            low_val, high_val = sorted((low, high))
            if pipeline_candidate.direction.upper() == "LONG":
                if (
                    pipeline_candidate.stop_loss < low_val
                    and high_val < pipeline_candidate.take_profit
                ):
                    return low_val, high_val
            elif (
                pipeline_candidate.take_profit < low_val and high_val < pipeline_candidate.stop_loss
            ):
                return low_val, high_val
    return pipeline_candidate.entry_price, pipeline_candidate.entry_price


def _resolve_card_score(pipeline_candidate: PipelineCandidate) -> Decimal:
    """Đọc điểm kích hoạt L5 (fallback về ``confidence`` thô của pipeline)."""
    raw_score = pipeline_candidate.metadata.get("trigger_score", pipeline_candidate.confidence)
    try:
        score = Decimal(str(raw_score))
        if score.is_finite():
            return score
    except InvalidOperation:
        pass
    return pipeline_candidate.confidence


def _structured_card_metadata(
    pipeline_candidate: PipelineCandidate,
    card: StrategyCard,
    atr: Decimal,
    card_score: Decimal,
) -> tuple[tuple[str, str], ...]:
    """Đóng gói metadata có cấu trúc, bất biến của Strategy Card cho thẻ tín hiệu và truy vết."""
    operations = {node.node_id: node.operation for node in card.nodes}
    gates = [
        node.operation
        for node in card.nodes
        if node.layer.value.startswith("L4") and node.operation
    ]
    triggers = [operations.get(node_id, node_id) for node_id in pipeline_candidate.evidence_nodes]
    items: dict[str, str] = {
        "card_id": card.card_id,
        "card_name": card.name,
        "card_version": card.version,
        "atr": str(atr),
        "confidence": str(pipeline_candidate.confidence),
        "trigger_score": str(card_score),
        "risk_reward_ratio": str(pipeline_candidate.risk_reward_ratio),
        "stop_loss": str(pipeline_candidate.stop_loss),
        "take_profit": str(pipeline_candidate.take_profit),
    }
    if gates:
        items["l4_gates"] = ",".join(gates)
    raw_warnings = pipeline_candidate.metadata.get("gate_warnings")
    if raw_warnings:
        items["l4_warnings"] = " | ".join(str(item) for item in raw_warnings)
    if triggers:
        items["l5_triggers"] = ",".join(triggers)
    return tuple(sorted(items.items()))


def _detail_vi(pipeline_candidate: PipelineCandidate, card: StrategyCard, atr: Decimal) -> str:
    """Diễn giải tín hiệu bằng đúng số liệu pipeline đã tính ở L4, L5 và L6."""

    def number(value: object) -> str:
        try:
            text = format(Decimal(str(value)).quantize(Decimal("0.0001")).normalize(), "f")
        except InvalidOperation:
            return str(value)
        return text.replace(".", ",")

    operations = {node.node_id: node.operation for node in card.nodes}
    gates = [
        node.operation
        for node in card.nodes
        if node.layer.value.startswith("L4") and node.operation
    ]
    triggers = ", ".join(
        operations.get(node_id, node_id) for node_id in pipeline_candidate.evidence_nodes
    )
    parts = [f"Strategy Card '{card.name}'"]
    parts.append(
        f"L4 {', '.join(gates)} ĐẠT (ATR {number(atr)})"
        if gates
        else f"L4 không có cổng (ATR {number(atr)})"
    )
    score = pipeline_candidate.metadata.get("trigger_score", pipeline_candidate.confidence)
    parts.append(f"L5 {triggers or 'ngòi nổ'} điểm {number(score)}")
    parts.append(
        f"L6 vào {number(pipeline_candidate.entry_price)}, "
        f"stop {number(pipeline_candidate.stop_loss)}, "
        f"mục tiêu {number(pipeline_candidate.take_profit)}, "
        f"R:R {number(pipeline_candidate.risk_reward_ratio)}"
    )
    return "; ".join(parts) + "."

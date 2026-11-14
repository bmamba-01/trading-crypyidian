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

from trading_bot.domain.strategy.contracts import StrategyCandidate as ContractCandidate
from trading_bot.domain.strategy.recipes import ExitRecipe

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

        recipe = ExitRecipe(
            recipe_id=f"recipe_{card.card_id[:8]}",
            stop_atr_multiple=sl_multiplier,
            take_profit_atr_multiple=tp_multiplier,
            max_hold_bars=12,
        )

        # 2. Xác định các mốc thời gian hiệu lực
        signal_at = pipeline_candidate.signal_at
        eligible_after = signal_at + timedelta(seconds=1)
        expires_at = signal_at + valid_duration

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
        )


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

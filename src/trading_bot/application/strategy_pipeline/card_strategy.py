"""Adapter cho phép một Strategy Card L0–L6 chạy sau hợp đồng `Strategy` của runtime.

Runtime (dispatcher fan-out và PaperDecisionCycle) chỉ biết hợp đồng `Strategy.evaluate`. Adapter
này chạy `StrategyPipelineEngine` trên `DecisionSnapshot`, rồi dùng `StrategyPipelinePlannerBridge`
để đóng gói candidate. Nó chỉ sinh `StrategyCandidate`/`NoTrade`; plan, thẩm định rủi ro và lệnh
PAPER vẫn do TradePlanner, RiskEngine và gateway quyết định.

Adapter CHƯA được đăng ký trong `StrategyRegistry` mặc định: Strategy Card chỉ vào đường quyết
định khi một registry có chủ đích đưa release của thẻ vào (hiện là bộ seed ledger kiểm thử).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from trading_bot.application.strategy_pipeline.pipeline_engine import StrategyPipelineEngine
from trading_bot.application.strategy_pipeline.planner_bridge import StrategyPipelinePlannerBridge
from trading_bot.domain.strategy.contracts import NoTrade, StrategyCandidate
from trading_bot.domain.strategy.rules import release_content_hash
from trading_bot.domain.strategy_card.schema import StrategyCard, compute_canonical_hash

if TYPE_CHECKING:
    from trading_bot.domain.strategy.contracts import DecisionSnapshot, StrategySelection

# Candidate của thẻ hết hạn sau khoảng này nếu chưa được lập kế hoạch và đặt lệnh PAPER.
DEFAULT_CANDIDATE_VALIDITY = timedelta(minutes=15)

_NO_TRADE_DETAILS_VI = {
    "DATA_HYGIENE_FAILED": "Nến không qua kiểm tra độ sạch dữ liệu L0 của Strategy Card.",
    "CONTEXT_GATE_BLOCKED": "Cổng bối cảnh L4 của Strategy Card chặn tín hiệu.",
    "NO_TRIGGER": "Ngòi nổ L5 của Strategy Card không kích hoạt.",
}


def strategy_card_release_id(card: StrategyCard) -> str:
    """Release ID ổn định của một phiên bản thẻ, ví dụ `sc-binance-usdm-vol-breakout-001-v1.0.0`."""
    return f"{card.card_id.lower()}-v{card.version}"


@dataclass(frozen=True)
class StrategyCardStrategy:
    """Một phiên bản Strategy Card đóng vai rule chiến lược có release và hash riêng."""

    card: StrategyCard
    candidate_validity: timedelta = DEFAULT_CANDIDATE_VALIDITY
    engine: StrategyPipelineEngine = field(default_factory=StrategyPipelineEngine, compare=False)

    @property
    def strategy_id(self) -> str:
        return self.card.card_id

    @property
    def release_id(self) -> str:
        return strategy_card_release_id(self.card)

    @property
    def recipe_manifest(self) -> dict[str, Any]:
        """Nội dung thẻ quyết định hash release: đổi đồ thị L0–L6 là đổi release hash."""
        return {
            "card_id": self.card.card_id,
            "card_version": self.card.version,
            "card_canonical_hash": compute_canonical_hash(self.card),
        }

    @property
    def content_hash(self) -> str:
        return release_content_hash(self)

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        """Chạy L0→L6 trên snapshot; chỉ trả candidate khi L4 cho qua và L5 kích hoạt."""
        result = self.engine.execute(self.card, snapshot)
        if result.status != "SUCCESS" or result.candidate is None:
            return NoTrade(
                strategy_id=self.strategy_id,
                code=f"STRATEGY_CARD_{result.status}",
                detail_vi=_NO_TRADE_DETAILS_VI.get(
                    result.status, "Strategy Card không sinh tín hiệu trên snapshot này."
                ),
                gate_details=tuple(result.reasons),
            )
        candidate = StrategyPipelinePlannerBridge.to_contract_candidate(
            result.candidate,
            self.card,
            snapshot,
            settings_version_id=settings.settings_version_id,
            macro_evidence_ids=snapshot.macro_evidence_ids,
            valid_duration=self.candidate_validity,
        )
        return replace(
            candidate,
            strategy_release_id=self.release_id,
            release_content_hash=self.content_hash,
        )

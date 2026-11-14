"""Adapter cho phép một Strategy Card L0–L6 chạy sau hợp đồng `Strategy` của runtime.

Runtime (dispatcher fan-out và PaperDecisionCycle) chỉ biết hợp đồng `Strategy.evaluate`. Adapter
này chạy `StrategyPipelineEngine` trên `DecisionSnapshot`, rồi dùng `StrategyPipelinePlannerBridge`
để đóng gói candidate. Nó chỉ sinh `StrategyCandidate`/`NoTrade`; plan, thẩm định rủi ro và lệnh
PAPER vẫn do TradePlanner, RiskEngine và gateway quyết định.

Release của thẻ chỉ vào đường quyết định qua `application.strategy_catalog.strategy_registry()`,
nơi liệt kê tường minh các thẻ được phép; registry không tham số chỉ có rule dựng sẵn H0x.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from trading_bot.application.strategy_pipeline.pipeline_engine import (
    ATR_SOURCE_SINGLE_BAR,
    PIPELINE_ENGINE_VERSION,
    StrategyPipelineEngine,
)
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


@dataclass(eq=False)
class StrategyCardStrategy:
    """Một phiên bản Strategy Card đóng vai rule chiến lược có release và hash riêng."""

    card: StrategyCard
    candidate_validity: timedelta = DEFAULT_CANDIDATE_VALIDITY
    engine: StrategyPipelineEngine = field(default_factory=StrategyPipelineEngine, compare=False)
    # Hai định danh là thuộc tính thường để khớp hợp đồng `Strategy`; suy ra từ thẻ khi khởi tạo.
    strategy_id: str = field(init=False)
    release_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.strategy_id = self.card.card_id
        self.release_id = strategy_card_release_id(self.card)

    @property
    def recipe_manifest(self) -> dict[str, Any]:
        """Nội dung thẻ quyết định hash release: đổi đồ thị L0–L6 là đổi release hash."""
        return {
            "card_id": self.card.card_id,
            "card_version": self.card.version,
            "card_canonical_hash": compute_canonical_hash(self.card),
            "pipeline_engine_version": PIPELINE_ENGINE_VERSION,
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
        sources = result.candidate.metadata.get("indicator_sources", {})
        if ATR_SOURCE_SINGLE_BAR in sources.values():
            # Đường quyết định không chấp nhận ATR suy từ một nến: cần feature đã quản trị hoặc
            # đủ lịch sử nến theo chu kỳ thẻ khai báo.
            return NoTrade(
                strategy_id=self.strategy_id,
                code="STRATEGY_CARD_INDICATOR_WARMUP",
                detail_vi="Chưa đủ feature hoặc lịch sử nến để tính chỉ báo của Strategy Card.",
                missing_features=tuple(
                    sorted(
                        node for node, source in sources.items() if source == ATR_SOURCE_SINGLE_BAR
                    )
                ),
            )
        candidate = StrategyPipelinePlannerBridge.to_contract_candidate(
            result.candidate,
            self.card,
            snapshot,
            settings_version_id=settings.settings_version_id,
            macro_evidence_ids=snapshot.macro_evidence_ids,
            valid_duration=self.candidate_validity,
        )
        feature_ids = tuple(result.candidate.metadata.get("indicator_feature_ids", ()))
        return replace(
            candidate,
            strategy_release_id=self.release_id,
            release_content_hash=self.content_hash,
            # Bằng chứng gồm node ngòi nổ và các feature đã quản trị mà pipeline thật sự dùng.
            evidence_feature_ids=tuple(sorted({*candidate.evidence_feature_ids, *feature_ids})),
        )

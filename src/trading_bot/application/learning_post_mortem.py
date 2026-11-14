"""Dịch vụ Application hậu kiểm giao dịch (Trade Post-Mortem) và ghi nhãn ca cần rà soát.

Dịch vụ nhận diện các ca sai lệch (Deviant trades) và giữ nhãn do người rà soát gắn. Nhãn chỉ
nằm trong bộ nhớ tiến trình và CHƯA phải dữ liệu huấn luyện: một `DatasetSample` của WP06 cần
snapshot đặc trưng, cửa sổ outcome, hash nhãn và quyền nguồn có lineage thật, nên dịch vụ này
không tự dựng mẫu huấn luyện từ nhãn thủ công.
"""
# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from trading_bot.domain.portfolio.post_mortem import (
    TradePostMortem,
    evaluate_trade_post_mortem,
)


@dataclass(frozen=True, slots=True)
class RetrainingSampleRecord:
    """Nhãn ca giao dịch cần rà soát; chưa phải mẫu huấn luyện và chưa được lưu bền."""

    sample_id: UUID
    trade_id: UUID
    bot_id: UUID
    symbol: str
    original_signal: str
    counterfactual_action: str
    penalty_weight: str | None
    deviation_reason: str
    feature_snapshot_id: str | None
    created_at: datetime
    ingested_by: str = "SYSTEM_POST_MORTEM"

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_id": str(self.sample_id),
            "trade_id": str(self.trade_id),
            "bot_id": str(self.bot_id),
            "symbol": self.symbol,
            "original_signal": self.original_signal,
            "counterfactual_action": self.counterfactual_action,
            "penalty_weight": self.penalty_weight,
            "deviation_reason": self.deviation_reason,
            "feature_snapshot_id": self.feature_snapshot_id,
            "created_at": self.created_at.isoformat(),
            "ingested_by": self.ingested_by,
        }


class LearningPostMortemService:
    """Quản lý việc mổ xẻ lệnh và thu thập các ca sai lệch cho AI Retraining."""

    def __init__(self) -> None:
        self._retraining_ledger: dict[UUID, RetrainingSampleRecord] = {}

    def analyze_trade(
        self,
        trade_dict: Mapping[str, Any],
    ) -> TradePostMortem:
        """Thực hiện mổ xẻ và phân loại một giao dịch."""
        return evaluate_trade_post_mortem(trade_dict)

    def ingest_trade_for_ai_retraining(
        self,
        trade_dict: Mapping[str, Any],
        *,
        override_reason: str | None = None,
        ingested_by: str = "ADMIN_OPERATOR",
    ) -> RetrainingSampleRecord:
        """Ghi nhãn một ca giao dịch (thường là sai lệch/thua lỗ) để người rà soát xem xét."""
        post_mortem = self.analyze_trade(trade_dict)
        trade_id = post_mortem.trade_id
        bot_id = post_mortem.bot_id
        symbol = post_mortem.symbol

        deviation_reason = (
            override_reason
            or post_mortem.deviation_reason
            or f"Giao dịch bị gắn nhãn thủ công bởi {ingested_by} để huấn luyện phản thực nghiệm."
        )

        sample = RetrainingSampleRecord(
            sample_id=uuid4(),
            trade_id=trade_id,
            bot_id=bot_id,
            symbol=symbol,
            original_signal=post_mortem.side,
            counterfactual_action=post_mortem.recommended_counterfactual_action,
            penalty_weight=(
                format(post_mortem.ai_penalty_weight, "f")
                if post_mortem.ai_penalty_weight is not None
                else None
            ),
            deviation_reason=deviation_reason,
            feature_snapshot_id=trade_dict.get("lineage", {}).get("feature_snapshot_id"),
            created_at=datetime.now(UTC),
            ingested_by=ingested_by,
        )
        self._retraining_ledger[sample.sample_id] = sample
        return sample

    def list_ingested_samples(self) -> list[RetrainingSampleRecord]:
        """Danh sách các mẫu phản thực nghiệm đã ghi nhận."""
        return list(self._retraining_ledger.values())

    def get_sample_by_trade_id(self, trade_id: UUID) -> RetrainingSampleRecord | None:
        """Tra cứu mẫu phạt theo ID của trade."""
        for sample in self._retraining_ledger.values():
            if sample.trade_id == trade_id:
                return sample
        return None

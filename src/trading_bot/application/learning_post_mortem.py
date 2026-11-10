"""Dịch vụ Application phân tích sau giao dịch (Trade Post-Mortem) và tiếp nhận dữ liệu huấn luyện AI (Retraining Ingestion).

Dịch vụ này nhận diện các ca giao dịch sai lệch (Deviant trades), phân tích nguyên nhân
đa tầng L4-L6 và đóng gói các mẫu phản thực nghiệm (Counterfactual negative samples)
làm dữ liệu phạt cho các chu kỳ huấn luyện và tinh chỉnh Scorer AI tiếp theo.
"""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from trading_bot.domain.learning.dataset import DatasetSample, DatasetSplit
from trading_bot.domain.portfolio.post_mortem import (
    TradePostMortem,
    evaluate_trade_post_mortem,
)


@dataclass(frozen=True, slots=True)
class RetrainingSampleRecord:
    """Bản ghi mẫu dữ liệu phản thực nghiệm gửi sang bộ phận huấn luyện AI."""

    sample_id: UUID
    trade_id: UUID
    bot_id: UUID
    symbol: str
    original_signal: str
    counterfactual_action: str
    penalty_weight: str
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
        """Đóng gói ca giao dịch (đặc biệt là các ca sai lệch/thua lỗ) vào tập dữ liệu phạt của AI."""
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
            penalty_weight=str(post_mortem.ai_penalty_weight),
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

    def export_dataset_samples(
        self,
        *,
        workspace_id: UUID,
        split: DatasetSplit = DatasetSplit.TRAIN,
    ) -> list[DatasetSample]:
        """Chuyển đổi toàn bộ các mẫu phạt phản thực nghiệm thành DatasetSample.

        Gán nhãn phản thực nghiệm (counterfactual_action = ABSTAIN -> label_value = 0)
        để đưa vào tập huấn luyện của Fold, dạy cho mô hình AI cách phạt và tránh các
        điểm vào lệnh bẫy thanh khoản / trượt giá cao.
        """
        samples: list[DatasetSample] = []
        for record in self._retraining_ledger.values():
            sample_hash = hashlib.sha256(str(record.sample_id).encode("utf-8")).hexdigest()
            feature_hash = (
                hashlib.sha256(record.feature_snapshot_id.encode("utf-8")).hexdigest()
                if record.feature_snapshot_id
                else sample_hash
            )
            outcome_hash = hashlib.sha256(str(record.trade_id).encode("utf-8")).hexdigest()
            label_hash = hashlib.sha256(record.counterfactual_action.encode("utf-8")).hexdigest()

            sample = DatasetSample(
                sample_id=str(record.sample_id),
                decision_at=record.created_at,
                feature_available_at=record.created_at,
                outcome_start=record.created_at,
                outcome_end=record.created_at + timedelta(seconds=60),
                feature_snapshot_hash=feature_hash,
                outcome_id=str(record.trade_id),
                outcome_hash=outcome_hash,
                label_hash=label_hash,
                label_value=0,  # 0 đại diện cho ABSTAIN (phạt đứng ngoài)
                label_version="counterfactual_negative_v1",
                split=split,
                source_dependent=False,
                source_rights_hashes=(),
                source_revision_refs=(),
                source_references=(),
            )
            samples.append(sample)
        return samples

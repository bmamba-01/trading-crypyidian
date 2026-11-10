"""Candidate split immutable, privé với payload chỉ cho builder."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey
from trading_bot.application.learning_fold_plan import build_manifest_fold_plan
from trading_bot.application.learning_split_candidate import (
    SPLIT_POLICY_VERSION,
    LearningSplitCandidate,
    LearningSplitCandidateConfig,
)
from trading_bot.domain.learning.dataset import DatasetManifest
from trading_bot.domain.learning.evaluation import TemporalWindow, WalkForwardSplit


class LearningSplitCandidateRecord(UuidPrimaryKey, CreatedAt, Base):
    """Canonical manifest/folds chỉ builder đọc; admin chỉ nhận preview column."""

    __tablename__ = "learning_split_candidate"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "trial_record_id"],
            ["backtest_trial.workspace_id", "backtest_trial.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "proof_record_id"],
            ["candidate_universe_proof.workspace_id", "candidate_universe_proof.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "trial_record_id",
            "manifest_hash",
            "fold_plan_hash",
            name="uq_learning_split_candidate_scope",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_learning_split_candidate_workspace_id"),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND proof_hash ~ '^[0-9a-f]{64}$' AND "
            "fold_plan_hash ~ '^[0-9a-f]{64}$'",
            name="learning_split_candidate_hashes_valid",
        ),
        CheckConstraint(
            "split_policy_version = 'wp06-walk-forward-v1'",
            name="learning_split_candidate_policy_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(preview) = 'object'",
            name="learning_split_candidate_preview_object",
        ),
        CheckConstraint(
            "length(canonical_manifest) > 0",
            name="learning_split_candidate_manifest_present",
        ),
        CheckConstraint(
            "length(canonical_configuration) > 0",
            name="learning_split_candidate_configuration_present",
        ),
        Index("ix_learning_split_candidate_workspace_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trial_record_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    proof_record_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    proof_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    split_policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluation_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    preview: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_manifest: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_configuration: Mapped[str] = mapped_column(Text, nullable=False)

    @classmethod
    def from_domain(cls, candidate: LearningSplitCandidate) -> LearningSplitCandidateRecord:
        """Lưu candidate typed sau khi fold-plan hash đã được dựng lại."""
        return cls(
            id=candidate.candidate_id,
            workspace_id=candidate.workspace_id,
            trial_record_id=candidate.trial_record_id,
            proof_record_id=candidate.proof_record_id,
            proof_hash=candidate.proof_hash,
            manifest_hash=candidate.manifest.content_hash,
            split_policy_version=candidate.manifest.split_policy_version,
            fold_plan_hash=candidate.fold_plan.content_hash,
            evaluation_cutoff=candidate.config.evaluation_cutoff,
            preview=json.loads(candidate.preview_json),
            canonical_manifest=candidate.manifest.canonical_json,
            canonical_configuration=candidate.canonical_configuration,
        )

    def to_domain(self) -> LearningSplitCandidate:
        """Xác minh lại bytes private và toàn bộ fold plan trước khi seal."""
        manifest = DatasetManifest.from_canonical_json(
            self.canonical_manifest,
            expected_content_hash=self.manifest_hash,
        )
        if self.split_policy_version != SPLIT_POLICY_VERSION:
            raise ValueError("Split policy candidate không được hỗ trợ.")
        payload = json.loads(self.canonical_configuration)
        if type(payload) is not dict or set(payload) != {
            "evaluation_cutoff",
            "folds",
            "holdout_start",
            "split_policy_version",
        }:
            raise ValueError("Cấu hình candidate có schema không hợp lệ.")
        if payload["split_policy_version"] != SPLIT_POLICY_VERSION:
            raise ValueError("Cấu hình candidate sai split policy.")
        folds_raw = payload["folds"]
        if type(folds_raw) is not list:
            raise ValueError("Fold candidate cần là danh sách.")
        folds = tuple(_fold_from_json(item) for item in folds_raw)
        cutoff = _timestamp(payload["evaluation_cutoff"])
        config = LearningSplitCandidateConfig(
            holdout_start=_timestamp(payload["holdout_start"]),
            evaluation_cutoff=cutoff,
            folds=folds,
        )
        plan = build_manifest_fold_plan(
            manifest,
            folds=folds,
            expected_split_policy_version=SPLIT_POLICY_VERSION,
            evaluation_cutoff=cutoff,
        )
        candidate = LearningSplitCandidate(
            candidate_id=self.id,
            workspace_id=self.workspace_id,
            trial_record_id=self.trial_record_id,
            proof_record_id=self.proof_record_id,
            proof_hash=self.proof_hash,
            manifest=manifest,
            fold_plan=plan,
            config=config,
            preview_json=json.dumps(
                self.preview, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
        if (
            self.fold_plan_hash != plan.content_hash
            or self.evaluation_cutoff != cutoff
            or self.canonical_configuration != candidate.canonical_configuration
        ):
            raise ValueError("Cột candidate không khớp canonical configuration/plan.")
        return candidate


def _fold_from_json(value: object) -> WalkForwardSplit:
    if type(value) is not dict or set(value) != {"train", "validation", "test"}:
        raise ValueError("Fold config cần train/validation/test chính xác.")
    return WalkForwardSplit(
        train=_window(value["train"]),
        validation=_window(value["validation"]),
        test=_window(value["test"]),
    )


def _window(value: object) -> TemporalWindow:
    if type(value) is not list or len(value) != 2:
        raise ValueError("Temporal window cần cặp ISO timestamps.")
    return TemporalWindow(_timestamp(value[0]), _timestamp(value[1]))


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Timestamp config cần chuỗi ISO-8601.")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamp config cần timezone.")
    return result


__all__ = ["LearningSplitCandidateRecord"]

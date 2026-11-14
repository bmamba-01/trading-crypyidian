"""Chuẩn hóa payload theo sample/fold và dựng batch trainer không có test/holdout."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from trading_bot.application.learning_fold_evaluation import (
    FoldOutcomeExpectation,
    FoldOutcomeVerificationBatch,
    FoldOutcomeVerificationRequest,
    _validate_plan,
    _validate_verification_batch,
)
from trading_bot.application.learning_fold_plan import ManifestFoldPlan
from trading_bot.domain.learning.approval import LearningSplitApproval
from trading_bot.domain.learning.dataset import DatasetManifest

FoldRole = Literal["train", "validation", "test"]


@dataclass(frozen=True, slots=True)
class NormalizedLearningSample:
    """Feature và label đã được đối chiếu với outcome entry authoritative."""

    sample_id: str
    outcome_id: str
    outcome_hash: str
    feature_snapshot_hash: str
    feature_schema_hash: str
    label_hash: str
    label_value: int
    features: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class NormalizedFoldAssignment:
    """Một sample được gán một vai trò trong đúng một fold."""

    fold_id: str
    sample_id: str
    role: FoldRole


@dataclass(frozen=True, slots=True)
class NormalizedLearningDataset:
    """Payload chuẩn hóa bất biến cùng membership theo fold."""

    workspace_id: UUID
    manifest_hash: str
    split_policy_version: str
    fold_plan_hash: str
    samples: tuple[NormalizedLearningSample, ...]
    assignments: tuple[NormalizedFoldAssignment, ...]
    content_hash: str


@dataclass(frozen=True, slots=True)
class TrainerFoldSample:
    """Một dòng được chọn cho batch; kiểu này không có giá trị role test."""

    sample_id: str
    role: Literal["train", "validation"]
    features: tuple[tuple[str, float], ...]
    label_value: int


@dataclass(frozen=True, slots=True)
class TrainerFoldBatch:
    """Batch gắn approval/fold, chỉ có TRAIN và VALIDATION."""

    workspace_id: UUID
    manifest_hash: str
    split_policy_version: str
    fold_plan_hash: str
    fold_id: str
    approval_hash: str
    samples: tuple[TrainerFoldSample, ...]
    batch_hash: str


def build_normalized_learning_dataset(
    *,
    manifest: DatasetManifest,
    fold_plan: ManifestFoldPlan,
    verified_batches: tuple[FoldOutcomeVerificationBatch, ...],
) -> NormalizedLearningDataset:
    """Tái sử dụng batch DB-verifier, giữ mỗi payload một lần và role theo từng fold."""
    canonical_plan = _validate_plan(manifest, fold_plan)
    if type(verified_batches) is not tuple or any(
        type(item) is not FoldOutcomeVerificationBatch for item in verified_batches
    ):
        raise ValueError("Cần tuple batch outcome đã qua DatabaseFoldOutcomeVerifier.")
    if any(sample.source_dependent for sample in manifest.samples):
        raise ValueError("Source-dependent sample cần cổng quyền TRAINING hiện hành.")
    batches_by_fold = {item.fold_id: item for item in verified_batches}
    if len(batches_by_fold) != len(verified_batches) or set(batches_by_fold) != {
        item.fold_id for item in canonical_plan.folds
    }:
        raise ValueError("Verifier batch phải bao phủ đúng một lần mọi fold trong plan.")

    samples_by_id = {sample.sample_id: sample for sample in manifest.samples}
    normalized_by_id: dict[str, NormalizedLearningSample] = {}
    assignments: list[NormalizedFoldAssignment] = []
    for fold in canonical_plan.folds:
        roles: dict[str, FoldRole] = {}
        role_assignments: tuple[tuple[FoldRole, tuple[str, ...]], ...] = (
            ("train", fold.train_sample_ids),
            ("validation", fold.validation_sample_ids),
            ("test", fold.test_sample_ids),
        )
        for role, sample_ids in role_assignments:
            for sample_id in sample_ids:
                if sample_id in roles:
                    raise ValueError("Fold plan lặp sample giữa các vai trò.")
                roles[sample_id] = role
        expectations = tuple(
            FoldOutcomeExpectation(
                sample_id=sample_id,
                role=role,
                outcome_id=samples_by_id[sample_id].outcome_id,
                outcome_hash=samples_by_id[sample_id].outcome_hash,
                outcome_start=samples_by_id[sample_id].outcome_start,
                outcome_end=samples_by_id[sample_id].outcome_end,
            )
            for sample_id, role in sorted(roles.items())
        )
        request = FoldOutcomeVerificationRequest(
            workspace_id=manifest.workspace_id,
            manifest_hash=manifest.content_hash,
            split_policy_version=manifest.split_policy_version,
            fold_plan=canonical_plan,
            fold_plan_hash=canonical_plan.content_hash,
            fold_id=fold.fold_id,
            expected_outcomes=expectations,
        )
        batch = batches_by_fold[fold.fold_id]
        trades_by_sample = _validate_verification_batch(request, batch)
        for sample_id, role in sorted(roles.items()):
            sample = samples_by_id[sample_id]
            trades = trades_by_sample[sample_id]
            if len(trades) != 1:
                raise ValueError(
                    "Outcome không có đúng một trade đã đóng, không được chuẩn hóa "
                    "thành sample huấn luyện."
                )
            trade = trades[0]
            if not trade.has_entry_provenance or trade.entry_features_hash is None:
                raise ValueError("Sample thiếu provenance feature tại entry.")
            if trade.entry_feature_schema_hash is None:
                raise ValueError("Sample thiếu hash schema feature tại entry.")
            if trade.entry_features_hash != sample.feature_snapshot_hash:
                raise ValueError("Feature payload không khớp feature snapshot trong manifest.")
            if trade.entry_symbol not in manifest.symbols:
                raise ValueError("Symbol của feature entry không thuộc manifest.")
            sign = (trade.net_pnl > 0) - (trade.net_pnl < 0)
            if sign != sample.label_value:
                raise ValueError("Label không khớp dấu net PnL của outcome authoritative.")
            normalized = NormalizedLearningSample(
                sample_id=sample_id,
                outcome_id=sample.outcome_id,
                outcome_hash=sample.outcome_hash,
                feature_snapshot_hash=sample.feature_snapshot_hash,
                feature_schema_hash=trade.entry_feature_schema_hash,
                label_hash=sample.label_hash,
                label_value=sample.label_value,
                features=trade.entry_features,
            )
            previous = normalized_by_id.setdefault(sample_id, normalized)
            if previous != normalized:
                raise ValueError("Payload authoritative của cùng sample khác nhau giữa các fold.")
            assignments.append(NormalizedFoldAssignment(fold.fold_id, sample_id, role))

    normalized_samples = tuple(normalized_by_id[key] for key in sorted(normalized_by_id))
    normalized_assignments = tuple(
        sorted(assignments, key=lambda item: (item.fold_id, item.sample_id))
    )
    dataset = NormalizedLearningDataset(
        workspace_id=manifest.workspace_id,
        manifest_hash=manifest.content_hash,
        split_policy_version=manifest.split_policy_version,
        fold_plan_hash=canonical_plan.content_hash,
        samples=normalized_samples,
        assignments=normalized_assignments,
        content_hash="",
    )
    return NormalizedLearningDataset(
        workspace_id=dataset.workspace_id,
        manifest_hash=dataset.manifest_hash,
        split_policy_version=dataset.split_policy_version,
        fold_plan_hash=dataset.fold_plan_hash,
        samples=dataset.samples,
        assignments=dataset.assignments,
        content_hash=normalized_learning_dataset_hash(dataset),
    )


def build_trainer_fold_batch(
    dataset: NormalizedLearningDataset,
    *,
    approval: LearningSplitApproval,
    fold_id: str,
) -> TrainerFoldBatch:
    """Tạo allowlist typed; không trả mẫu TEST, holdout hay fold khác."""
    if type(dataset) is not NormalizedLearningDataset:
        raise ValueError("dataset phải là NormalizedLearningDataset authoritative.")
    if type(approval) is not LearningSplitApproval:
        raise ValueError("Cần approval tường minh.")
    if (
        approval.workspace_id != dataset.workspace_id
        or approval.manifest_hash != dataset.manifest_hash
        or approval.split_policy_version != dataset.split_policy_version
        or approval.fold_plan_hash != dataset.fold_plan_hash
    ):
        raise ValueError("Approval không khớp dataset/fold plan.")
    if not isinstance(fold_id, str) or len(fold_id) != 64:
        raise LookupError("Fold không tồn tại trong dataset.")
    sample_by_id = {item.sample_id: item for item in dataset.samples}
    assignments = tuple(item for item in dataset.assignments if item.fold_id == fold_id)
    if not assignments:
        raise LookupError("Fold không tồn tại trong dataset.")
    allowed: list[TrainerFoldSample] = []
    for assignment in assignments:
        if assignment.role == "test":
            continue
        if assignment.role not in {"train", "validation"}:
            raise ValueError("Dataset chứa role ngoài allowlist trainer.")
        sample = sample_by_id.get(assignment.sample_id)
        if sample is None:
            raise ValueError("Fold assignment thiếu normalized payload.")
        allowed.append(
            TrainerFoldSample(
                sample_id=sample.sample_id,
                role=assignment.role,
                features=sample.features,
                label_value=sample.label_value,
            )
        )
    ordered = tuple(sorted(allowed, key=lambda item: (item.role, item.sample_id)))
    roles = {item.role for item in ordered}
    if not {"train", "validation"}.issubset(roles):
        raise ValueError("Fold trainer cần cả train và validation sample.")
    payload = {
        "workspace_id": str(dataset.workspace_id),
        "manifest_hash": dataset.manifest_hash,
        "split_policy_version": dataset.split_policy_version,
        "fold_plan_hash": dataset.fold_plan_hash,
        "fold_id": fold_id,
        "approval_hash": approval.content_hash,
        "samples": [
            {
                "sample_id": item.sample_id,
                "role": item.role,
                "features": item.features,
                "label_value": item.label_value,
            }
            for item in ordered
        ],
    }
    return TrainerFoldBatch(
        workspace_id=dataset.workspace_id,
        manifest_hash=dataset.manifest_hash,
        split_policy_version=dataset.split_policy_version,
        fold_plan_hash=dataset.fold_plan_hash,
        fold_id=fold_id,
        approval_hash=approval.content_hash,
        samples=ordered,
        batch_hash=_hash(payload),
    )


def _hash(payload: object) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalized_learning_dataset_hash(dataset: NormalizedLearningDataset) -> str:
    """Tính lại hash canonical từ payload và assignments đã chuẩn hoá."""
    if type(dataset) is not NormalizedLearningDataset:
        raise ValueError("dataset phải là NormalizedLearningDataset.")
    payload = {
        "workspace_id": str(dataset.workspace_id),
        "manifest_hash": dataset.manifest_hash,
        "split_policy_version": dataset.split_policy_version,
        "fold_plan_hash": dataset.fold_plan_hash,
        "samples": [
            {
                "sample_id": item.sample_id,
                "outcome_id": item.outcome_id,
                "outcome_hash": item.outcome_hash,
                "feature_snapshot_hash": item.feature_snapshot_hash,
                "feature_schema_hash": item.feature_schema_hash,
                "label_hash": item.label_hash,
                "label_value": item.label_value,
                "features": item.features,
            }
            for item in dataset.samples
        ],
        "assignments": [
            {"fold_id": item.fold_id, "sample_id": item.sample_id, "role": item.role}
            for item in dataset.assignments
        ],
    }
    return _hash(payload)


__all__ = [
    "NormalizedFoldAssignment",
    "NormalizedLearningDataset",
    "NormalizedLearningSample",
    "TrainerFoldBatch",
    "TrainerFoldSample",
    "build_normalized_learning_dataset",
    "build_trainer_fold_batch",
    "normalized_learning_dataset_hash",
]

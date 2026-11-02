"""Tạo kế hoạch fold từ manifest authoritative mà chưa chạy trainer/evaluator."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from trading_bot.application.learning_temporal_evaluation import (
    FoldSampleExclusion,
    select_walk_forward_samples,
)
from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSample, DatasetSplit
from trading_bot.domain.learning.evaluation import (
    SampleLineage,
    TemporalWindow,
    WalkForwardSplit,
)

SampleRole = Literal["train", "validation", "test"]


@dataclass(frozen=True, slots=True)
class ManifestFoldSelection:
    """Fold đã chọn sample ID và lưu exclusion có lý do."""

    fold_id: str
    split: WalkForwardSplit
    train_sample_ids: tuple[str, ...]
    validation_sample_ids: tuple[str, ...]
    test_sample_ids: tuple[str, ...]
    exclusions: tuple[FoldSampleExclusion, ...]

    @property
    def excluded_sample_ids(self) -> tuple[str, ...]:
        """ID bị loại theo thứ tự ổn định, không lặp."""
        return tuple(sorted({item.sample_id for item in self.exclusions}))


@dataclass(frozen=True, slots=True)
class ManifestFoldPlan:
    """Kế hoạch canonical gắn chặt fold với manifest và split policy."""

    manifest_hash: str
    split_policy_version: str
    evaluation_cutoff: datetime
    folds: tuple[ManifestFoldSelection, ...]
    content_hash: str


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
    return value.astimezone(UTC)


def _inside(decision_at: datetime, split_start: datetime, split_end: datetime) -> bool:
    return split_start <= decision_at < split_end


def _sample_role(sample: DatasetSample, fold: WalkForwardSplit) -> SampleRole | None:
    if _inside(sample.decision_at, fold.train.start, fold.train.end):
        return "train"
    if _inside(sample.decision_at, fold.validation.start, fold.validation.end):
        return "validation"
    if _inside(sample.decision_at, fold.test.start, fold.test.end):
        return "test"
    return None


def _overlapping_holdout_id(
    interval: TemporalWindow,
    *,
    holdout_starts: tuple[datetime, ...],
    holdout_prefix_max_ends: tuple[tuple[datetime, str], ...],
) -> str | None:
    index = bisect_left(holdout_starts, interval.end) - 1
    if index < 0:
        return None
    max_end, sample_id = holdout_prefix_max_ends[index]
    return sample_id if max_end > interval.start else None


def _expected_split(role: SampleRole) -> DatasetSplit:
    return {
        "train": DatasetSplit.TRAIN,
        "validation": DatasetSplit.VALIDATION,
        "test": DatasetSplit.TEST,
    }[role]


def _window_payload(window: TemporalWindow) -> list[str]:
    return [window.start.isoformat(), window.end.isoformat()]


def _fold_payload(fold: WalkForwardSplit) -> dict[str, object]:
    return {
        "train": _window_payload(fold.train),
        "validation": _window_payload(fold.validation),
        "test": _window_payload(fold.test),
    }


def _validate_folds(
    manifest: DatasetManifest,
    folds: tuple[WalkForwardSplit, ...],
    *,
    evaluation_cutoff: datetime,
) -> None:
    if not folds:
        raise ValueError("Cần ít nhất một fold.")
    seen: set[str] = set()
    previous: WalkForwardSplit | None = None
    for fold in folds:
        if type(fold) is not WalkForwardSplit:
            raise ValueError("Mọi fold phải là WalkForwardSplit.")
        key = json.dumps(_fold_payload(fold), sort_keys=True, separators=(",", ":"))
        if key in seen:
            raise ValueError("Fold bị lặp.")
        seen.add(key)
        if not (
            manifest.data_start <= fold.train.start
            and fold.train.end <= fold.validation.start
            and fold.validation.end <= fold.test.start
            and fold.test.end <= manifest.data_end
            and fold.test.end <= manifest.as_of
        ):
            raise ValueError("Fold không tương thích với thứ tự hoặc phạm vi manifest.")
        if evaluation_cutoff < fold.test.end:
            raise ValueError("evaluation_cutoff phải không trước cuối cửa sổ test.")
        if previous is not None and not (
            previous.train.start <= fold.train.start
            and previous.validation.start < fold.validation.start
            and previous.test.start < fold.test.start
            and previous.test.end <= fold.validation.start
        ):
            raise ValueError("Các fold phải theo thứ tự thời gian và không tái dùng cửa sổ test.")
        previous = fold


def build_manifest_fold_plan(
    manifest: DatasetManifest,
    *,
    folds: tuple[WalkForwardSplit, ...],
    expected_split_policy_version: str,
    evaluation_cutoff: datetime,
) -> ManifestFoldPlan:
    """Dựng kế hoạch fold từ sample authoritative và split policy đã pin.

    Hàm chỉ chọn thành viên cho đánh giá; không train model, không tính metric và
    không khẳng định manifest đã chứa toàn bộ các sample của truy vấn nguồn.
    V2 suy vai trò development theo từng fold; legacy giữ split cố định.
    Seal holdout không cấp quyền truy cập; mọi cửa sổ chạm holdout đều bị từ chối.
    """
    if type(manifest) is not DatasetManifest:
        raise ValueError("manifest phải là DatasetManifest authoritative.")
    if (
        not isinstance(expected_split_policy_version, str)
        or not expected_split_policy_version.strip()
    ):
        raise ValueError("expected_split_policy_version không được để trống.")
    if manifest.split_policy_version != expected_split_policy_version:
        raise ValueError("split policy của manifest không khớp phiên bản được yêu cầu.")
    if any(type(sample) is not DatasetSample for sample in manifest.samples):
        raise ValueError("Manifest thiếu sample lineage authoritative hợp lệ.")
    cutoff = _utc(evaluation_cutoff, "evaluation_cutoff")
    if cutoff > manifest.as_of:
        raise ValueError("evaluation_cutoff không được sau manifest.as_of.")
    normalized_folds = tuple(folds)
    _validate_folds(manifest, normalized_folds, evaluation_cutoff=cutoff)

    samples_by_id = {sample.sample_id: sample for sample in manifest.samples}
    if len(samples_by_id) != len(manifest.samples):
        raise ValueError("Manifest chứa sample lineage trùng ID.")
    lineages = tuple(
        SampleLineage(
            sample_id=sample.sample_id,
            decision_at=sample.decision_at,
            feature_available_at=sample.feature_available_at,
            label_interval=TemporalWindow(sample.outcome_start, sample.outcome_end),
        )
        for sample in manifest.samples
    )
    holdout_intervals = tuple(
        sorted(
            (
                sample.outcome_start,
                sample.outcome_end,
                sample.sample_id,
            )
            for sample in manifest.samples
            if sample.split is DatasetSplit.HOLDOUT
        )
    )
    holdout_starts = tuple(item[0] for item in holdout_intervals)
    holdout_prefix_max_ends_list: list[tuple[datetime, str]] = []
    max_holdout_end: datetime | None = None
    max_holdout_id = ""
    for _, holdout_end, holdout_id in holdout_intervals:
        if max_holdout_end is None or holdout_end > max_holdout_end:
            max_holdout_end = holdout_end
            max_holdout_id = holdout_id
        holdout_prefix_max_ends_list.append((max_holdout_end, max_holdout_id))
    holdout_prefix_max_ends = tuple(holdout_prefix_max_ends_list)

    planned: list[ManifestFoldSelection] = []
    for fold in normalized_folds:
        for lineage in lineages:
            sample = samples_by_id[lineage.sample_id]
            role = _sample_role(sample, fold)
            if manifest.manifest_version == "dataset-manifest-v2":
                if role is not None:
                    if sample.split is DatasetSplit.HOLDOUT:
                        raise ValueError(
                            f"Holdout {sample.sample_id} nằm trong cửa sổ fold {role}."
                        )
                    sample_interval = lineage.label_interval
                    overlapping_holdout_id = _overlapping_holdout_id(
                        sample_interval,
                        holdout_starts=holdout_starts,
                        holdout_prefix_max_ends=holdout_prefix_max_ends,
                    )
                    if overlapping_holdout_id is not None:
                        message = (
                            f"Holdout outcome interval {overlapping_holdout_id} giao với "
                            f"khoảng outcome của sample {sample.sample_id} trong "
                            f"cửa sổ fold {role}."
                        )
                        raise ValueError(message)
                continue
            if role is not None and sample.split is not _expected_split(role):
                raise ValueError(
                    f"Sample {sample.sample_id} split assignment không khớp vai trò {role}."
                )
        selection = select_walk_forward_samples(
            lineages,
            split=fold,
            evaluation_cutoff=cutoff,
        )
        if not (
            selection.train_sample_ids
            and selection.validation_sample_ids
            and selection.test_sample_ids
        ):
            raise ValueError("Mỗi fold phải còn sample train, validation và test đủ điều kiện.")
        payload = _fold_payload(fold)
        fold_id = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        planned.append(
            ManifestFoldSelection(
                fold_id=fold_id,
                split=fold,
                train_sample_ids=selection.train_sample_ids,
                validation_sample_ids=selection.validation_sample_ids,
                test_sample_ids=selection.test_sample_ids,
                exclusions=selection.exclusions,
            )
        )

    payload = {
        "manifest_hash": manifest.content_hash,
        "split_policy_version": manifest.split_policy_version,
        "evaluation_cutoff": cutoff.isoformat(),
        "folds": [
            {
                **_fold_payload(item.split),
                "fold_id": item.fold_id,
                "train_sample_ids": item.train_sample_ids,
                "validation_sample_ids": item.validation_sample_ids,
                "test_sample_ids": item.test_sample_ids,
                "exclusions": [
                    (excluded.role, excluded.sample_id, excluded.reasons)
                    for excluded in item.exclusions
                ],
            }
            for item in planned
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return ManifestFoldPlan(
        manifest_hash=manifest.content_hash,
        split_policy_version=manifest.split_policy_version,
        evaluation_cutoff=cutoff,
        folds=tuple(planned),
        content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )

"""Tạo batch huấn luyện đã được DB gateway ràng buộc vào attempt."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from trading_bot.adapters.database.models.learning_fold_sample_entities import (
    NormalizedLearningSampleRecord,
)
from trading_bot.domain.learning.accounting import LearningTrainingAttempt
from trading_bot.domain.learning.resources import (
    LearningResourcePlan,
    LearningResourceRequest,
    assess_learning_resource_request,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class AuthorizedTrainerSample:
    """Sample chỉ có vai trò train hoặc validation; không có kiểu test."""

    sample_id: str
    role: Literal["train", "validation"]
    strategy_release_id: str | None
    strategy_release_content_hash: str | None
    vector_schema_version: str | None
    vector_schema_hash: str | None
    vector_hash: str | None
    features: tuple[tuple[str, float], ...]
    label_value: int
    sample_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.sample_id, str) or not self.sample_id.strip():
            raise ValueError("sample_id trainer phải có giá trị.")
        if self.role not in {"train", "validation"}:
            raise ValueError("Trainer chỉ nhận role train hoặc validation.")
        lineage = (
            self.strategy_release_id,
            self.strategy_release_content_hash,
            self.vector_schema_version,
            self.vector_schema_hash,
            self.vector_hash,
        )
        if any(value is not None for value in lineage):
            if any(value is None for value in lineage):
                raise ValueError("Trainer lineage phải đầy đủ hoặc để trống hoàn toàn.")
            if (
                not isinstance(self.strategy_release_id, str)
                or not self.strategy_release_id.strip()
            ):
                raise ValueError("strategy_release_id trainer phải có giá trị authoritative.")
            if (
                not isinstance(self.vector_schema_version, str)
                or not self.vector_schema_version.strip()
            ):
                raise ValueError("vector_schema_version trainer phải có giá trị authoritative.")
            for name in (
                "strategy_release_content_hash",
                "vector_schema_hash",
                "vector_hash",
            ):
                value = getattr(self, name)
                if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                    raise ValueError(f"{name} trainer phải là SHA-256.")
        if type(self.label_value) is not int or self.label_value not in {-1, 0, 1}:
            raise ValueError("label trainer phải là -1, 0 hoặc 1.")
        if not isinstance(self.sample_hash, str) or _SHA256.fullmatch(self.sample_hash) is None:
            raise ValueError("sample_hash trainer phải là SHA-256.")
        if type(self.features) is not tuple or not self.features:
            raise ValueError("features trainer phải là tuple không rỗng.")
        names: list[str] = []
        for item in self.features:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("Feature trainer phải là cặp tên và giá trị.")
            name, value = item
            if (
                not isinstance(name, str)
                or not name.strip()
                or type(value) not in {int, float}
                or not math.isfinite(value)
            ):
                raise ValueError("Feature trainer có tên hoặc giá trị số không hợp lệ.")
            names.append(name)
        if names != sorted(set(names)):
            raise ValueError("Tên feature trainer phải duy nhất và đã sắp xếp.")


@dataclass(frozen=True, slots=True)
class AuthorizedTrainerBatch:
    """Batch được cấp một lần theo attempt, fold và approval bất biến."""

    attempt_id: UUID
    normalized_dataset_id: UUID
    workspace_id: UUID
    manifest_hash: str
    split_policy_version: str
    fold_plan_hash: str
    fold_id: str
    approval_hash: str
    seed: int
    trainer_version: str
    dependency_fingerprint: str
    feature_schema_hash: str
    attempt_content_hash: str
    resource_request: LearningResourceRequest
    resource_plan: LearningResourcePlan
    samples: tuple[AuthorizedTrainerSample, ...]
    batch_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if any(
            type(value) is not UUID
            for value in (
                self.attempt_id,
                self.normalized_dataset_id,
                self.workspace_id,
            )
        ):
            raise ValueError("Trainer batch cần UUID authoritative.")
        for name in (
            "manifest_hash",
            "fold_plan_hash",
            "approval_hash",
            "dependency_fingerprint",
            "feature_schema_hash",
            "attempt_content_hash",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{name} trainer batch phải là SHA-256.")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("seed trainer batch không hợp lệ.")
        if type(self.resource_request) is not LearningResourceRequest:
            raise ValueError("Trainer batch thiếu resource request authoritative.")
        if type(self.resource_plan) is not LearningResourcePlan:
            raise ValueError("Trainer batch thiếu resource plan authoritative.")
        if self.resource_plan != assess_learning_resource_request(self.resource_request):
            raise ValueError("Trainer resource plan không khớp request đã duyệt.")
        if type(self.samples) is not tuple or not self.samples:
            raise ValueError("Trainer batch phải có sample tuple không rỗng.")
        if any(type(sample) is not AuthorizedTrainerSample for sample in self.samples):
            raise ValueError("Trainer batch chứa sample không typed.")
        if len({sample.sample_id for sample in self.samples}) != len(self.samples):
            raise ValueError("Trainer batch chứa sample_id trùng.")
        if self.samples != tuple(
            sorted(self.samples, key=lambda item: (item.role, item.sample_id))
        ):
            raise ValueError("Trainer batch không có thứ tự canonical.")
        counts = {
            role: sum(sample.role == role for sample in self.samples)
            for role in ("train", "validation")
        }
        if counts != {
            "train": self.resource_request.training_samples,
            "validation": self.resource_request.validation_samples,
        }:
            raise ValueError("Trainer batch counts không khớp resource request.")
        feature_names = tuple(name for name, _ in self.samples[0].features)
        if any(
            tuple(name for name, _ in sample.features) != feature_names for sample in self.samples
        ):
            raise ValueError("Trainer batch feature schema không đồng nhất.")
        has_lineage = [sample.strategy_release_id is not None for sample in self.samples]
        if any(has_lineage) and not all(has_lineage):
            raise ValueError("TRAIN/VALIDATION có lineage đầy đủ hoặc bị giữ ngoài scorer.")
        if all(has_lineage):
            release_lineage = {
                (sample.strategy_release_id, sample.strategy_release_content_hash)
                for sample in self.samples
            }
            vector_schemas = {
                (sample.vector_schema_version, sample.vector_schema_hash) for sample in self.samples
            }
            if len(release_lineage) != 1 or len(vector_schemas) != 1:
                raise ValueError("TRAIN/VALIDATION phải cùng strategy release và vector schema.")
            if next(iter(vector_schemas))[1] != self.feature_schema_hash:
                raise ValueError("Vector schema hash không khớp feature profile đã duyệt.")
        object.__setattr__(self, "batch_hash", _trainer_batch_hash(self))


def _trainer_batch_hash(batch: AuthorizedTrainerBatch) -> str:
    payload = {
        "approval_hash": batch.approval_hash,
        "attempt_content_hash": batch.attempt_content_hash,
        "attempt_id": str(batch.attempt_id),
        "fold_id": batch.fold_id,
        "fold_plan_hash": batch.fold_plan_hash,
        "feature_schema_hash": batch.feature_schema_hash,
        "manifest_hash": batch.manifest_hash,
        "normalized_dataset_id": str(batch.normalized_dataset_id),
        "resource_request": {
            "feature_count": batch.resource_request.feature_count,
            "max_runtime_seconds": batch.resource_request.max_runtime_seconds,
            "max_steps": batch.resource_request.max_steps,
            "memory_limit_bytes": batch.resource_request.memory_limit_bytes,
            "training_samples": batch.resource_request.training_samples,
            "validation_samples": batch.resource_request.validation_samples,
        },
        "samples": [
            {
                "features": sample.features,
                "label_value": sample.label_value,
                "role": sample.role,
                "sample_hash": sample.sample_hash,
                "sample_id": sample.sample_id,
                "strategy_release_content_hash": sample.strategy_release_content_hash,
                "strategy_release_id": sample.strategy_release_id,
                "vector_hash": sample.vector_hash,
                "vector_schema_hash": sample.vector_schema_hash,
                "vector_schema_version": sample.vector_schema_version,
            }
            for sample in sorted(batch.samples, key=lambda item: (item.role, item.sample_id))
        ],
        "workspace_id": str(batch.workspace_id),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def authorized_training_batch_from_rows(
    *,
    attempt: LearningTrainingAttempt,
    normalized_dataset_id: UUID,
    rows: list[object] | tuple[object, ...],
    lineage_rows: list[object] | tuple[object, ...],
) -> AuthorizedTrainerBatch:
    """Xác minh metadata, membership, sample hash và count trước khi trả batch."""
    if type(attempt) is not LearningTrainingAttempt or type(normalized_dataset_id) is not UUID:
        raise ValueError("Cần attempt và normalized dataset UUID authoritative.")
    if attempt.resource_plan is None:
        raise ValueError("Attempt bị từ chối không được cấp dữ liệu huấn luyện.")
    if type(rows) not in {list, tuple} or not rows:
        raise ValueError("Gateway không trả batch sample.")
    if type(lineage_rows) not in {list, tuple}:
        raise ValueError("Gateway lineage phải là list/tuple.")

    lineage_by_id: dict[str, object] = {}
    for lineage in lineage_rows:
        lineage_sample_id = getattr(lineage, "sample_id", None)
        if (
            not isinstance(lineage_sample_id, str)
            or not lineage_sample_id.strip()
            or lineage_sample_id in lineage_by_id
        ):
            raise ValueError("Lineage gateway có sample_id rỗng hoặc trùng.")
        if any(
            getattr(lineage, field, None) != expected
            for field, expected in (
                ("attempt_id", attempt.id),
                ("workspace_id", attempt.workspace_id),
                ("normalized_dataset_id", normalized_dataset_id),
                ("manifest_hash", attempt.manifest_hash),
                ("split_policy_version", attempt.split_policy_version),
                ("fold_plan_hash", attempt.fold_plan_hash),
                ("approval_hash", attempt.approval_hash),
                ("fold_id", attempt.fold_id),
            )
        ):
            raise ValueError("Gateway lineage không khớp attempt/fold/approval.")
        lineage_by_id[lineage_sample_id] = lineage

    allowed_rows: list[AuthorizedTrainerSample] = []
    observed_ids: set[str] = set()
    schema_hash: str | None = None
    feature_names: tuple[str, ...] | None = None
    for row in rows:
        if row is None:
            raise ValueError("Gateway trả row không hợp lệ.")
        if any(
            getattr(row, field, None) != expected
            for field, expected in (
                ("attempt_id", attempt.id),
                ("workspace_id", attempt.workspace_id),
                ("normalized_dataset_id", normalized_dataset_id),
                ("manifest_hash", attempt.manifest_hash),
                ("split_policy_version", attempt.split_policy_version),
                ("fold_plan_hash", attempt.fold_plan_hash),
                ("approval_hash", attempt.approval_hash),
                ("fold_id", attempt.fold_id),
                ("seed", attempt.seed),
                ("trainer_version", attempt.trainer_version),
                ("dependency_fingerprint", attempt.dependency_fingerprint),
            )
        ):
            raise ValueError("Gateway trả attempt metadata không khớp.")

        role = getattr(row, "role", None)
        if role == "test":
            raise ValueError("Gateway trả role test; holdout bị cấm trong batch trainer.")
        if role not in {"train", "validation"}:
            raise ValueError("Gateway trả role ngoài allowlist train/validation.")
        sample_id = getattr(row, "sample_id", None)
        if not isinstance(sample_id, str) or not sample_id.strip() or sample_id in observed_ids:
            raise ValueError("sample_id rỗng hoặc trùng trong batch gateway.")
        observed_ids.add(sample_id)

        sample_record = NormalizedLearningSampleRecord(
            workspace_id=attempt.workspace_id,
            manifest_hash=attempt.manifest_hash,
            fold_plan_hash=attempt.fold_plan_hash,
            sample_id=sample_id,
            outcome_id=getattr(row, "outcome_id", None),
            outcome_hash=getattr(row, "outcome_hash", None),
            feature_snapshot_hash=getattr(row, "feature_snapshot_hash", None),
            feature_schema_hash=getattr(row, "feature_schema_hash", None),
            label_hash=getattr(row, "label_hash", None),
            label_value=getattr(row, "label_value", None),
            features=getattr(row, "features", None),
            sample_hash=getattr(row, "sample_hash", None),
        )
        sample = sample_record.to_domain()
        if len(sample.features) != attempt.resource_request.feature_count:
            raise ValueError("Số feature sample không khớp resource profile.")
        if schema_hash is None:
            schema_hash = sample.feature_schema_hash
            feature_names = tuple(name for name, _ in sample.features)
        elif schema_hash != sample.feature_schema_hash or feature_names != tuple(
            name for name, _ in sample.features
        ):
            raise ValueError("Feature schema giữa các sample không đồng nhất.")
        lineage = lineage_by_id.pop(sample_id, None)
        if lineage_rows and (lineage is None or getattr(lineage, "role", None) != role):
            raise ValueError("Gateway thiếu lineage khớp sample/role.")
        vector_schema_hash = (
            getattr(lineage, "vector_schema_hash", None) if lineage is not None else None
        )
        if lineage is not None and vector_schema_hash != sample.feature_schema_hash:
            raise ValueError("Lineage vector schema không khớp sample đã chuẩn hóa.")
        allowed_rows.append(
            AuthorizedTrainerSample(
                sample_id=sample.sample_id,
                role=role,
                strategy_release_id=(
                    getattr(lineage, "strategy_release_id", None) if lineage is not None else None
                ),
                strategy_release_content_hash=getattr(
                    lineage, "strategy_release_content_hash", None
                ),
                vector_schema_version=(
                    getattr(lineage, "vector_schema_version", None) if lineage is not None else None
                ),
                vector_schema_hash=vector_schema_hash,
                vector_hash=getattr(lineage, "vector_hash", None) if lineage is not None else None,
                features=sample.features,
                label_value=sample.label_value,
                sample_hash=sample_record.sample_hash,
            )
        )

    if lineage_by_id:
        raise ValueError("Gateway lineage trả sample ngoài TRAIN/VALIDATION batch.")

    ordered = tuple(sorted(allowed_rows, key=lambda item: (item.role, item.sample_id)))
    observed_counts = {
        role: sum(1 for sample in ordered if sample.role == role)
        for role in ("train", "validation")
    }
    expected_counts = {
        "train": attempt.resource_request.training_samples,
        "validation": attempt.resource_request.validation_samples,
    }
    if observed_counts != expected_counts:
        raise ValueError("train/validation count gateway không khớp profile authoritative.")
    if schema_hash is None:
        raise ValueError("Gateway thiếu feature schema hash.")

    return AuthorizedTrainerBatch(
        attempt_id=attempt.id,
        normalized_dataset_id=normalized_dataset_id,
        workspace_id=attempt.workspace_id,
        manifest_hash=attempt.manifest_hash,
        split_policy_version=attempt.split_policy_version,
        fold_plan_hash=attempt.fold_plan_hash,
        fold_id=attempt.fold_id,
        approval_hash=attempt.approval_hash,
        seed=attempt.seed,
        trainer_version=attempt.trainer_version,
        dependency_fingerprint=attempt.dependency_fingerprint,
        feature_schema_hash=schema_hash,
        attempt_content_hash=attempt.content_hash,
        resource_request=attempt.resource_request,
        resource_plan=attempt.resource_plan,
        samples=ordered,
    )


__all__ = [
    "AuthorizedTrainerBatch",
    "AuthorizedTrainerSample",
    "authorized_training_batch_from_rows",
]

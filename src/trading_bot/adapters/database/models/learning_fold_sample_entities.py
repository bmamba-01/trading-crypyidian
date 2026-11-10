"""Payload học máy chuẩn hóa và gán vai trò append-only theo fold."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey

if TYPE_CHECKING:
    from trading_bot.application.learning_fold_samples import (
        NormalizedFoldAssignment,
        NormalizedLearningDataset,
        NormalizedLearningSample,
    )


def _canonical_hash(payload: object) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class NormalizedLearningDatasetRecord(UuidPrimaryKey, CreatedAt, Base):
    """Header immutable khóa normalized payload với manifest, approval và fold plan."""

    __tablename__ = "learning_normalized_dataset"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "manifest_hash"],
            ["dataset_manifest.workspace_id", "dataset_manifest.manifest_hash"],
            ondelete="RESTRICT",
            name="fk_learning_normalized_dataset_manifest",
        ),
        UniqueConstraint(
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
            name="uq_learning_normalized_dataset_plan",
        ),
        UniqueConstraint(
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
            "id",
            name="uq_learning_normalized_dataset_identity",
        ),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND approval_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'",
            name="learning_normalized_dataset_hashes_valid",
        ),
        CheckConstraint(
            "length(split_policy_version) BETWEEN 1 AND 128",
            name="learning_normalized_dataset_policy_present",
        ),
        CheckConstraint(
            "sample_count > 0 AND assignment_count > 0",
            name="learning_normalized_dataset_counts_positive",
        ),
        Index("ix_learning_normalized_dataset_workspace_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    split_policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    assignment_count: Mapped[int] = mapped_column(Integer, nullable=False)

    @classmethod
    def from_domain(
        cls, dataset: NormalizedLearningDataset, *, approval_hash: str
    ) -> NormalizedLearningDatasetRecord:
        """Tạo header từ dataset đã được application chuẩn hóa và kiểm hash."""
        from trading_bot.application.learning_fold_samples import (
            NormalizedLearningDataset,
            normalized_learning_dataset_hash,
        )

        if type(dataset) is not NormalizedLearningDataset:
            raise ValueError("dataset phải là NormalizedLearningDataset.")
        if dataset.content_hash != normalized_learning_dataset_hash(dataset):
            raise ValueError("Hash normalized dataset không khớp payload.")
        return cls(
            workspace_id=dataset.workspace_id,
            manifest_hash=dataset.manifest_hash,
            split_policy_version=dataset.split_policy_version,
            fold_plan_hash=dataset.fold_plan_hash,
            approval_hash=approval_hash,
            content_hash=dataset.content_hash,
            sample_count=len(dataset.samples),
            assignment_count=len(dataset.assignments),
        )


class NormalizedLearningSampleRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một payload feature/label duy nhất cho sample, dùng chung qua các fold."""

    __tablename__ = "learning_normalized_sample"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "manifest_hash", "fold_plan_hash"],
            [
                "learning_normalized_dataset.workspace_id",
                "learning_normalized_dataset.manifest_hash",
                "learning_normalized_dataset.fold_plan_hash",
            ],
            ondelete="RESTRICT",
            name="fk_learning_normalized_sample_dataset",
        ),
        UniqueConstraint(
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
            "sample_id",
            name="uq_learning_normalized_sample_identity",
        ),
        UniqueConstraint(
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
            "sample_id",
            "id",
            name="uq_learning_normalized_sample_row_identity",
        ),
        CheckConstraint(
            "outcome_hash ~ '^[0-9a-f]{64}$' AND feature_snapshot_hash ~ '^[0-9a-f]{64}$' "
            "AND feature_schema_hash ~ '^[0-9a-f]{64}$' AND label_hash ~ '^[0-9a-f]{64}$' "
            "AND sample_hash ~ '^[0-9a-f]{64}$'",
            name="learning_normalized_sample_hashes_valid",
        ),
        CheckConstraint("label_value IN (-1, 0, 1)", name="learning_normalized_sample_label_valid"),
        CheckConstraint(
            "jsonb_typeof(features) = 'object' AND features <> '{}'::jsonb",
            name="learning_normalized_sample_features_valid",
        ),
        Index(
            "ix_learning_normalized_sample_outcome",
            "workspace_id",
            "outcome_id",
            "outcome_hash",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_id: Mapped[str] = mapped_column(Text, nullable=False)
    outcome_id: Mapped[str] = mapped_column(Text, nullable=False)
    outcome_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    label_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    label_value: Mapped[int] = mapped_column(Integer, nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    sample_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    @classmethod
    def from_domain(
        cls,
        dataset: NormalizedLearningDataset,
        sample: NormalizedLearningSample,
    ) -> NormalizedLearningSampleRecord:
        """Chuyển một feature/label payload sang hàng JSONB canonical."""
        from trading_bot.application.learning_fold_samples import (
            NormalizedLearningDataset,
            NormalizedLearningSample,
        )

        if (
            type(dataset) is not NormalizedLearningDataset
            or type(sample) is not NormalizedLearningSample
        ):
            raise ValueError("Cần normalized dataset và sample typed.")
        payload = _sample_payload(sample)
        return cls(
            workspace_id=dataset.workspace_id,
            manifest_hash=dataset.manifest_hash,
            fold_plan_hash=dataset.fold_plan_hash,
            sample_id=sample.sample_id,
            outcome_id=sample.outcome_id,
            outcome_hash=sample.outcome_hash,
            feature_snapshot_hash=sample.feature_snapshot_hash,
            feature_schema_hash=sample.feature_schema_hash,
            label_hash=sample.label_hash,
            label_value=sample.label_value,
            features=dict(sample.features),
            sample_hash=_canonical_hash(payload),
        )

    def to_domain(self) -> NormalizedLearningSample:
        """Kiểm lại JSONB và hash trước khi cấp sample cho trusted boundary."""
        from trading_bot.application.learning_fold_samples import NormalizedLearningSample

        if type(self.features) is not dict or any(
            type(name) is not str or type(value) not in {int, float}
            for name, value in self.features.items()
        ):
            raise ValueError("Features JSONB không phải object số hợp lệ.")
        sample = NormalizedLearningSample(
            sample_id=self.sample_id,
            outcome_id=self.outcome_id,
            outcome_hash=self.outcome_hash,
            feature_snapshot_hash=self.feature_snapshot_hash,
            feature_schema_hash=self.feature_schema_hash,
            label_hash=self.label_hash,
            label_value=self.label_value,
            features=tuple(sorted((name, float(value)) for name, value in self.features.items())),
        )
        if self.sample_hash != _canonical_hash(_sample_payload(sample)):
            raise ValueError("Sample JSONB không khớp sample_hash.")
        return sample


class NormalizedFoldAssignmentRecord(UuidPrimaryKey, CreatedAt, Base):
    """Membership TRAIN/VALIDATION/TEST của sample trong một fold cụ thể."""

    __tablename__ = "learning_fold_assignment"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "manifest_hash", "fold_plan_hash"],
            [
                "learning_normalized_dataset.workspace_id",
                "learning_normalized_dataset.manifest_hash",
                "learning_normalized_dataset.fold_plan_hash",
            ],
            ondelete="RESTRICT",
            name="fk_learning_fold_assignment_dataset",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "manifest_hash", "fold_plan_hash", "sample_id"],
            [
                "learning_normalized_sample.workspace_id",
                "learning_normalized_sample.manifest_hash",
                "learning_normalized_sample.fold_plan_hash",
                "learning_normalized_sample.sample_id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_fold_assignment_sample",
        ),
        UniqueConstraint(
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
            "fold_id",
            "sample_id",
            name="uq_learning_fold_assignment_role",
        ),
        CheckConstraint(
            "fold_id ~ '^[0-9a-f]{64}$'", name="learning_fold_assignment_fold_hash_valid"
        ),
        CheckConstraint(
            "role IN ('train', 'validation', 'test')", name="learning_fold_assignment_role_valid"
        ),
        Index(
            "ix_learning_fold_assignment_lookup",
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
            "fold_id",
            "role",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)

    @classmethod
    def from_domain(
        cls, dataset: NormalizedLearningDataset, assignment: NormalizedFoldAssignment
    ) -> NormalizedFoldAssignmentRecord:
        """Tạo hàng membership từ fold plan canonical."""
        from trading_bot.application.learning_fold_samples import (
            NormalizedFoldAssignment,
            NormalizedLearningDataset,
        )

        if (
            type(dataset) is not NormalizedLearningDataset
            or type(assignment) is not NormalizedFoldAssignment
        ):
            raise ValueError("Cần normalized dataset và fold assignment typed.")
        return cls(
            workspace_id=dataset.workspace_id,
            manifest_hash=dataset.manifest_hash,
            fold_plan_hash=dataset.fold_plan_hash,
            fold_id=assignment.fold_id,
            sample_id=assignment.sample_id,
            role=assignment.role,
        )


def _sample_payload(sample: NormalizedLearningSample) -> dict[str, object]:
    return {
        "sample_id": sample.sample_id,
        "outcome_id": sample.outcome_id,
        "outcome_hash": sample.outcome_hash,
        "feature_snapshot_hash": sample.feature_snapshot_hash,
        "feature_schema_hash": sample.feature_schema_hash,
        "label_hash": sample.label_hash,
        "label_value": sample.label_value,
        "features": sample.features,
    }


__all__ = [
    "NormalizedFoldAssignmentRecord",
    "NormalizedLearningDatasetRecord",
    "NormalizedLearningSampleRecord",
]

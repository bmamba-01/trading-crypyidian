"""Bảng manifest dataset bất biến cho huấn luyện và tái dựng lineage."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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
    from trading_bot.domain.learning.dataset import DatasetManifest


class DatasetManifestRecord(UuidPrimaryKey, CreatedAt, Base):
    """Manifest canonical append-only, lưu đầy đủ payload đã được verifier chấp thuận."""

    __tablename__ = "dataset_manifest"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        UniqueConstraint("workspace_id", "manifest_hash", name="uq_dataset_manifest_hash"),
        UniqueConstraint("workspace_id", "id", name="uq_dataset_manifest_workspace_id"),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND query_hash ~ '^[0-9a-f]{64}$'",
            name="dataset_manifest_hashes_valid",
        ),
        CheckConstraint("data_start < data_end", name="dataset_manifest_range_valid"),
        CheckConstraint(
            "sample_count >= 0 AND exclusion_count >= 0",
            name="dataset_manifest_counts_nonnegative",
        ),
        CheckConstraint("jsonb_typeof(manifest) = 'object'", name="dataset_manifest_object"),
        CheckConstraint(
            "length(feature_schema_version) > 0 AND length(feature_version) > 0 "
            "AND length(label_version) > 0 AND length(sample_schema_version) > 0 "
            "AND length(split_policy_version) > 0 AND length(retention_policy_ref) > 0",
            name="dataset_manifest_versions_present",
        ),
        Index("ix_dataset_manifest_workspace_created", "workspace_id", "created_at"),
        Index("ix_dataset_manifest_workspace_asof", "workspace_id", "as_of"),
        Index("ix_dataset_manifest_split_policy", "workspace_id", "split_policy_version"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=False)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_version: Mapped[str] = mapped_column(String(64), nullable=False)
    data_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    feature_schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    feature_version: Mapped[str] = mapped_column(String(128), nullable=False)
    label_version: Mapped[str] = mapped_column(String(128), nullable=False)
    sample_schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    split_policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    exclusion_count: Mapped[int] = mapped_column(Integer, nullable=False)
    holdout_sealed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retention_policy_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_manifest: Mapped[str] = mapped_column(Text, nullable=False)

    @classmethod
    def from_domain(cls, manifest: object) -> DatasetManifestRecord:
        """Lưu nguyên payload canonical; không tái diễn giải hash từ caller."""
        from trading_bot.domain.learning.dataset import DatasetManifest

        if type(manifest) is not DatasetManifest:
            raise ValueError("manifest phải là DatasetManifest.")
        return cls(
            workspace_id=manifest.workspace_id,
            manifest_hash=manifest.content_hash,
            query_hash=manifest.query_hash,
            manifest_version=manifest.manifest_version,
            data_start=manifest.data_start,
            data_end=manifest.data_end,
            as_of=manifest.as_of,
            feature_schema_version=manifest.feature_schema_version,
            feature_version=manifest.feature_version,
            label_version=manifest.label_version,
            sample_schema_version=manifest.sample_schema_version,
            split_policy_version=manifest.split_policy_version,
            sample_count=len(manifest.samples),
            exclusion_count=len(manifest.exclusions),
            holdout_sealed_at=manifest.holdout_sealed_at,
            retention_policy_ref=manifest.retention_policy_ref,
            manifest=json.loads(manifest.canonical_json),
            canonical_manifest=manifest.canonical_json,
        )

    def to_domain(self) -> DatasetManifest:
        """Tái dựng và xác minh nội dung trước khi cấp manifest cho trainer."""
        from trading_bot.domain.learning.dataset import DatasetManifest

        manifest = DatasetManifest.from_canonical_json(
            self.canonical_manifest,
            expected_content_hash=self.manifest_hash,
        )
        if self.manifest != json.loads(self.canonical_manifest):
            raise ValueError("JSONB manifest không khớp canonical bytes.")
        denormalized = (
            self.workspace_id == manifest.workspace_id,
            self.query_hash == manifest.query_hash,
            self.manifest_version == manifest.manifest_version,
            self.data_start == manifest.data_start,
            self.data_end == manifest.data_end,
            self.as_of == manifest.as_of,
            self.feature_schema_version == manifest.feature_schema_version,
            self.feature_version == manifest.feature_version,
            self.label_version == manifest.label_version,
            self.sample_schema_version == manifest.sample_schema_version,
            self.split_policy_version == manifest.split_policy_version,
            self.sample_count == len(manifest.samples),
            self.exclusion_count == len(manifest.exclusions),
            self.holdout_sealed_at == manifest.holdout_sealed_at,
            self.retention_policy_ref == manifest.retention_policy_ref,
        )
        if not all(denormalized):
            raise ValueError("Cột chỉ mục manifest không khớp canonical bytes.")
        return manifest

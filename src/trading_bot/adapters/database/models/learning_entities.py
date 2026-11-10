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
from trading_bot.domain.learning.approval import LearningSplitApproval
from trading_bot.domain.learning.dataset import DatasetSourceReference

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


class DatasetManifestSourceRefRecord(UuidPrimaryKey, CreatedAt, Base):
    """Chỉ mục bất biến để truy ngược nguồn tới sample/manifest theo workspace."""

    __tablename__ = "dataset_manifest_source_ref"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "manifest_id"],
            ["dataset_manifest.workspace_id", "dataset_manifest.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "manifest_id",
            "sample_id",
            "source_id",
            "item_id",
            "revision_id",
            "policy_id",
            "policy_sequence",
            "purpose",
            name="uq_dataset_manifest_source_ref",
        ),
        CheckConstraint(
            "source_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "item_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "revision_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "policy_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$'",
            name="dataset_manifest_source_ref_identifiers_valid",
        ),
        CheckConstraint("policy_sequence > 0", name="dataset_manifest_source_ref_sequence_valid"),
        CheckConstraint("purpose = 'training'", name="dataset_manifest_source_ref_purpose_valid"),
        CheckConstraint(
            "rights_hash ~ '^[0-9a-f]{64}$'", name="dataset_manifest_source_ref_hash_valid"
        ),
        Index(
            "ix_dataset_manifest_source_ref_reverse",
            "workspace_id",
            "source_id",
            "item_id",
            "revision_id",
            "policy_id",
            "policy_sequence",
            "purpose",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sample_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_id: Mapped[str] = mapped_column(String(160), nullable=False)
    policy_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    rights_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    @classmethod
    def from_reference(
        cls,
        *,
        workspace_id: UUID,
        manifest_id: UUID,
        sample_id: str,
        reference: DatasetSourceReference,
    ) -> DatasetManifestSourceRefRecord:
        """Tạo chỉ mục từ typed reference đã được domain validate."""
        if type(reference) is not DatasetSourceReference:
            raise ValueError("reference phải là DatasetSourceReference.")
        return cls(
            workspace_id=workspace_id,
            manifest_id=manifest_id,
            sample_id=sample_id,
            source_id=reference.source_id,
            item_id=reference.item_id,
            revision_id=reference.revision_id,
            policy_id=reference.policy_id,
            policy_sequence=reference.policy_sequence,
            purpose=reference.purpose.value,
            rights_hash=reference.rights_hash,
        )


class DatasetManifestEligibilityEventRecord(UuidPrimaryKey, CreatedAt, Base):
    """Sự kiện bất biến làm mất hiệu lực một sample có source lineage."""

    __tablename__ = "dataset_manifest_eligibility_event"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "manifest_id"],
            ["dataset_manifest.workspace_id", "dataset_manifest.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "outbox_event_id"],
            ["outbox_event.workspace_id", "outbox_event.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "outbox_event_id",
            "manifest_id",
            "sample_id",
            "source_id",
            "item_id",
            "revision_id",
            "policy_id",
            "policy_sequence",
            "purpose",
            name="uq_dataset_manifest_eligibility_event_identity",
        ),
        CheckConstraint(
            "source_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "item_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "revision_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "policy_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$'",
            name="dataset_manifest_eligibility_event_identifiers_valid",
        ),
        CheckConstraint("policy_sequence > 0", name="dataset_manifest_eligibility_sequence_valid"),
        CheckConstraint("purpose = 'training'", name="dataset_manifest_eligibility_purpose_valid"),
        CheckConstraint(
            "rights_hash ~ '^[0-9a-f]{64}$'",
            name="dataset_manifest_eligibility_hash_valid",
        ),
        CheckConstraint(
            "change_type IN ('policy_revision', 'policy_expired', 'item_revision', "
            "'item_tombstoned', 'item_takedown')",
            name="dataset_manifest_eligibility_change_type_valid",
        ),
        CheckConstraint(
            "reason_code IN ('source_policy_changed', 'source_policy_expired', "
            "'source_item_superseded', 'source_item_tombstoned', 'source_item_takedown')",
            name="dataset_manifest_eligibility_reason_valid",
        ),
        Index(
            "ix_dataset_manifest_eligibility_latest",
            "workspace_id",
            "manifest_id",
            "sample_id",
            "created_at",
        ),
        Index(
            "ix_dataset_manifest_eligibility_reference",
            "workspace_id",
            "source_id",
            "item_id",
            "revision_id",
            "policy_id",
            "policy_sequence",
            "purpose",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sample_id: Mapped[str] = mapped_column(Text, nullable=False)
    outbox_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_id: Mapped[str] = mapped_column(String(160), nullable=False)
    policy_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    rights_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LearningSplitApprovalRecord(UuidPrimaryKey, CreatedAt, Base):
    """Phê duyệt bất biến gắn với candidate manifest và fold plan cụ thể."""

    __tablename__ = "learning_split_approval"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "actor_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_learning_split_approval_actor_membership",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_split_approval_idempotency"
        ),
        UniqueConstraint(
            "workspace_id", "manifest_hash", name="uq_learning_split_approval_manifest"
        ),
        UniqueConstraint("workspace_id", "id", name="uq_learning_split_approval_workspace_id"),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND approval_hash ~ '^[0-9a-f]{64}$'",
            name="learning_split_approval_hashes_valid",
        ),
        CheckConstraint(
            "length(btrim(split_policy_version)) BETWEEN 1 AND 128",
            name="learning_split_approval_policy_present",
        ),
        CheckConstraint(
            "length(btrim(reason)) BETWEEN 12 AND 500",
            name="learning_split_approval_reason_valid",
        ),
        CheckConstraint(
            "mfa_verified_at <= approved_at AND "
            "approved_at - mfa_verified_at <= interval '15 minutes'",
            name="learning_split_approval_mfa_recent",
        ),
        CheckConstraint(
            "length(canonical_approval) > 0",
            name="learning_split_approval_canonical_present",
        ),
        Index("ix_learning_split_approval_workspace_approved", "workspace_id", "approved_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    split_policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mfa_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approval_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_approval: Mapped[str] = mapped_column(Text, nullable=False)

    @classmethod
    def from_domain(cls, approval: LearningSplitApproval) -> LearningSplitApprovalRecord:
        """Lưu binding canonical đã được Principal service dựng."""
        if type(approval) is not LearningSplitApproval:
            raise ValueError("approval phải là LearningSplitApproval.")
        return cls(
            workspace_id=approval.workspace_id,
            actor_membership_id=approval.actor_membership_id,
            idempotency_key=approval.idempotency_key,
            manifest_hash=approval.manifest_hash,
            split_policy_version=approval.split_policy_version,
            fold_plan_hash=approval.fold_plan_hash,
            reason=approval.reason.strip(),
            approved_at=approval.approved_at,
            mfa_verified_at=approval.mfa_verified_at,
            approval_hash=approval.content_hash,
            canonical_approval=approval.canonical_json,
        )

    def to_domain(self) -> LearningSplitApproval:
        """Tái dựng approval và kiểm lại hash/cột chỉ mục trước khi dùng."""
        payload = json.loads(self.canonical_approval)
        approval = LearningSplitApproval(
            workspace_id=UUID(payload["workspace_id"]),
            manifest_hash=payload["manifest_hash"],
            split_policy_version=payload["split_policy_version"],
            fold_plan_hash=payload["fold_plan_hash"],
            actor_membership_id=UUID(payload["actor_membership_id"]),
            idempotency_key=UUID(payload["idempotency_key"]),
            reason=payload["reason"],
            approved_at=datetime.fromisoformat(payload["approved_at"]),
            mfa_verified_at=datetime.fromisoformat(payload["mfa_verified_at"]),
        )
        if (
            approval.content_hash != self.approval_hash
            or approval.workspace_id != self.workspace_id
            or approval.actor_membership_id != self.actor_membership_id
            or approval.idempotency_key != self.idempotency_key
            or approval.manifest_hash != self.manifest_hash
            or approval.split_policy_version != self.split_policy_version
            or approval.fold_plan_hash != self.fold_plan_hash
            or approval.reason != self.reason
            or approval.approved_at != self.approved_at
            or approval.mfa_verified_at != self.mfa_verified_at
        ):
            raise ValueError("Approval canonical không khớp các cột đã chỉ mục.")
        return approval

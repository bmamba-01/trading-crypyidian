"""Biên nhận metadata về quyền TRAINING cho từng sample và child replay."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class LearningReplayTrainingReceiptRecord(UuidPrimaryKey, CreatedAt, Base):
    """Lưu đúng lineage và scope đã kiểm, không lưu payload hay bytes evidence."""

    __tablename__ = "learning_replay_training_receipt"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "manifest_hash", "fold_plan_hash", "normalized_dataset_id"],
            [
                "learning_normalized_dataset.workspace_id",
                "learning_normalized_dataset.manifest_hash",
                "learning_normalized_dataset.fold_plan_hash",
                "learning_normalized_dataset.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_replay_training_receipt_dataset",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "license_evidence_id"],
            [
                "source_license_evidence.workspace_id",
                "source_license_evidence.source_id",
                "source_license_evidence.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_replay_training_receipt_evidence",
        ),
        UniqueConstraint(
            "workspace_id",
            "access_kind",
            "access_id",
            "normalized_dataset_id",
            "sample_id",
            "child_dataset_hash",
            name="uq_learning_replay_training_receipt_access_child",
        ),
        CheckConstraint(
            "access_kind IN ('normalization', 'evaluation', 'training')",
            name="learning_replay_training_receipt_access_kind_valid",
        ),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND bundle_hash ~ '^[0-9a-f]{64}$' "
            "AND child_dataset_hash ~ '^[0-9a-f]{64}$' "
            "AND policy_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND current_policy_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND bar_identity_hash ~ '^[0-9a-f]{64}$' "
            "AND scope_hash ~ '^[0-9a-f]{64}$' "
            "AND evidence_digest ~ '^[0-9a-f]{64}$' "
            "AND license_scope_hash ~ '^[0-9a-f]{64}$'",
            name="learning_replay_training_receipt_hashes_valid",
        ),
        CheckConstraint(
            "purpose = 'training' AND policy_sequence > 0 "
            "AND data_start < data_end AND checked_at IS NOT NULL",
            name="learning_replay_training_receipt_scope_valid",
        ),
        CheckConstraint(
            "length(sample_id) BETWEEN 1 AND 128 "
            "AND length(source_id) BETWEEN 1 AND 128 "
            "AND length(provider) BETWEEN 1 AND 128 "
            "AND length(product) BETWEEN 1 AND 128 "
            "AND length(venue) BETWEEN 1 AND 128 "
            "AND length(contract) BETWEEN 1 AND 128 "
            "AND length(symbol) BETWEEN 1 AND 128 "
            "AND length(interval) BETWEEN 1 AND 16 "
            "AND length(policy_id) BETWEEN 1 AND 160",
            name="learning_replay_training_receipt_identifiers_valid",
        ),
        Index(
            "ix_learning_replay_training_receipt_manifest",
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
            "sample_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    access_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    access_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    normalized_dataset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_id: Mapped[str] = mapped_column(String(128), nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    child_dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    product: Mapped[str] = mapped_column(String(128), nullable=False)
    venue: Mapped[str] = mapped_column(String(128), nullable=False)
    contract: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(128), nullable=False)
    interval: Mapped[str] = mapped_column(String(16), nullable=False)
    data_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bar_identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    current_policy_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_id: Mapped[str] = mapped_column(String(160), nullable=False)
    policy_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    license_evidence_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    license_scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

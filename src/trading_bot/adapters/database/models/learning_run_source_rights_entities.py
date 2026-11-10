"""Biên nhận quyền TRAINING hiện hành theo lần evaluator hoặc trainer đọc dữ liệu."""

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


class LearningRunSourceRightsReceiptRecord(UuidPrimaryKey, CreatedAt, Base):
    """Lưu metadata rights đã kiểm dưới source lock, không lưu nội dung hay nhãn."""

    __tablename__ = "learning_run_source_rights_receipt"
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
            name="fk_learning_run_source_rights_dataset",
        ),
        UniqueConstraint(
            "workspace_id",
            "access_kind",
            "access_id",
            "sample_id",
            "source_id",
            "item_id",
            "revision_id",
            "policy_id",
            name="uq_learning_run_source_rights_access_reference",
        ),
        CheckConstraint(
            "access_kind IN ('evaluation', 'training')",
            name="learning_run_source_rights_access_kind_valid",
        ),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND rights_hash ~ '^[0-9a-f]{64}$'",
            name="learning_run_source_rights_hashes_valid",
        ),
        CheckConstraint(
            "purpose = 'training' AND policy_sequence > 0",
            name="learning_run_source_rights_purpose_valid",
        ),
        CheckConstraint(
            "length(sample_id) BETWEEN 1 AND 128 AND length(source_id) BETWEEN 1 AND 128 "
            "AND length(item_id) BETWEEN 1 AND 128 AND length(revision_id) BETWEEN 1 AND 128 "
            "AND length(policy_id) BETWEEN 1 AND 160",
            name="learning_run_source_rights_identifiers_valid",
        ),
        Index(
            "ix_learning_run_source_rights_manifest",
            "workspace_id",
            "manifest_hash",
            "checked_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    access_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    access_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    normalized_dataset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_id: Mapped[str] = mapped_column(String(160), nullable=False)
    policy_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    rights_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["LearningRunSourceRightsReceiptRecord"]

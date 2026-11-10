"""Biên nhận metadata cho lần builder kiểm quyền TRAINING của source reference."""

from datetime import datetime
from typing import TYPE_CHECKING
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
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey

if TYPE_CHECKING:
    from trading_bot.application.dataset_source_eligibility import DatasetSourceRightsEvidence


class LearningSourceTrainingReceiptRecord(UuidPrimaryKey, CreatedAt, Base):
    """Lưu lineage và DB check time, không chứa body/feature/label nguồn."""

    __tablename__ = "learning_source_training_receipt"
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
            name="fk_learning_source_training_receipt_dataset",
        ),
        UniqueConstraint(
            "workspace_id",
            "normalized_dataset_id",
            "sample_id",
            "source_id",
            "item_id",
            "revision_id",
            "policy_id",
            name="uq_learning_source_training_receipt_reference",
        ),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND rights_hash ~ '^[0-9a-f]{64}$' AND evidence_hash ~ '^[0-9a-f]{64}$'",
            name="learning_source_training_receipt_hashes_valid",
        ),
        CheckConstraint(
            "purpose = 'training' AND policy_sequence > 0",
            name="learning_source_training_receipt_scope_valid",
        ),
        CheckConstraint(
            "length(sample_id) BETWEEN 1 AND 128 AND length(source_id) BETWEEN 1 AND 128 "
            "AND length(item_id) BETWEEN 1 AND 128 AND length(revision_id) BETWEEN 1 AND 128 "
            "AND length(policy_id) BETWEEN 1 AND 160",
            name="learning_source_training_receipt_identifiers_valid",
        ),
        Index(
            "ix_learning_source_training_receipt_manifest",
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
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
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_evidence: Mapped[str] = mapped_column(Text, nullable=False)

    @classmethod
    def from_evidence(
        cls,
        evidence: DatasetSourceRightsEvidence,
        *,
        normalized_dataset_id: UUID,
        fold_plan_hash: str,
    ) -> LearningSourceTrainingReceiptRecord:
        """Tạo dòng kiểm toán từ evidence đã qua live-rights gate."""
        from trading_bot.application.dataset_source_eligibility import DatasetSourceRightsEvidence

        if type(evidence) is not DatasetSourceRightsEvidence:
            raise ValueError("evidence phải là DatasetSourceRightsEvidence.")
        if type(normalized_dataset_id) is not UUID:
            raise ValueError("normalized_dataset_id phải là UUID.")
        if (
            not isinstance(fold_plan_hash, str)
            or len(fold_plan_hash) != 64
            or any(character not in "0123456789abcdef" for character in fold_plan_hash)
        ):
            raise ValueError("fold_plan_hash phải là SHA-256 hex thường.")
        reference = evidence.reference
        return cls(
            workspace_id=evidence.workspace_id,
            normalized_dataset_id=normalized_dataset_id,
            manifest_hash=evidence.manifest_hash,
            fold_plan_hash=fold_plan_hash,
            sample_id=evidence.sample_id,
            source_id=reference.source_id,
            item_id=reference.item_id,
            revision_id=reference.revision_id,
            policy_id=reference.policy_id,
            policy_sequence=reference.policy_sequence,
            purpose=reference.purpose.value,
            rights_hash=reference.rights_hash,
            checked_at=evidence.checked_at,
            evidence_hash=evidence.evidence_hash,
            canonical_evidence=evidence.canonical_json,
        )

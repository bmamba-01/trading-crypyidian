"""Bảng registry, lịch sử trạng thái và active pointer cho artifact learning."""

from datetime import datetime
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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class LearningModelRegistryRecord(UuidPrimaryKey, CreatedAt, Base):
    """Artifact bất biến về lineage, kèm trạng thái hiện tại do event dẫn xuất."""

    __tablename__ = "learning_model_registry"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["artifact_id"],
            ["learning_training_artifact.id"],
            ondelete="RESTRICT",
            name="fk_learning_model_registry_artifact",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "created_by_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="RESTRICT",
            name="fk_learning_model_registry_creator",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "evaluation_report_id"],
            ["learning_evaluation_report.workspace_id", "learning_evaluation_report.id"],
            ondelete="RESTRICT",
            name="fk_learning_model_registry_evaluation",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_learning_model_registry_workspace_id"),
        UniqueConstraint("workspace_id", "artifact_id", name="uq_learning_model_registry_artifact"),
        CheckConstraint(
            "state IN ('draft', 'evaluated', 'rejected', 'paper_active', 'retired')",
            name="learning_model_registry_state_valid",
        ),
        CheckConstraint(
            "artifact_hash ~ '^[0-9a-f]{64}$' AND manifest_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_plan_hash ~ '^[0-9a-f]{64}$' AND approval_hash ~ '^[0-9a-f]{64}$'",
            name="learning_model_registry_hashes_valid",
        ),
        CheckConstraint(
            "(evaluation_report_id IS NULL AND evaluation_report_hash IS NULL) OR "
            "(evaluation_report_id IS NOT NULL AND "
            "evaluation_report_hash ~ '^[0-9a-f]{64}$')",
            name="learning_model_registry_evaluation_pair_valid",
        ),
        CheckConstraint("revision >= 1", name="learning_model_registry_revision_positive"),
        Index("ix_learning_model_registry_workspace_state", "workspace_id", "state", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluation_report_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    evaluation_report_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by_membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class LearningModelRegistryEventRecord(UuidPrimaryKey, CreatedAt, Base):
    """Lịch sử append-only cho mọi quyết định và lần đổi trạng thái registry."""

    __tablename__ = "learning_model_registry_event"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            ondelete="RESTRICT",
            name="fk_learning_model_registry_event_entry",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "actor_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="RESTRICT",
            name="fk_learning_model_registry_event_actor",
        ),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_model_registry_event_key"
        ),
        UniqueConstraint(
            "workspace_id", "registry_id", "sequence", name="uq_learning_model_registry_event_seq"
        ),
        UniqueConstraint(
            "workspace_id",
            "registry_id",
            "id",
            name="uq_learning_model_registry_event_exact_entry",
        ),
        CheckConstraint("sequence >= 1", name="learning_model_registry_event_sequence_positive"),
        CheckConstraint(
            "action IN ('register', 'evaluate', 'approve', 'reject', 'activate', "
            "'rollback', 'retire')",
            name="learning_model_registry_event_action_valid",
        ),
        CheckConstraint(
            "(from_state IS NULL OR from_state IN "
            "('draft', 'evaluated', 'rejected', 'paper_active', 'retired')) AND "
            "to_state IN ('draft', 'evaluated', 'rejected', 'paper_active', 'retired')",
            name="learning_model_registry_event_state_valid",
        ),
        CheckConstraint(
            "length(btrim(policy_version)) BETWEEN 1 AND 128 AND "
            "length(btrim(reason_code)) BETWEEN 1 AND 64 AND "
            "length(btrim(reason)) BETWEEN 12 AND 500",
            name="learning_model_registry_event_audit_valid",
        ),
        CheckConstraint(
            "(action IN ('approve', 'reject', 'activate', 'rollback', 'retire') AND "
            "mfa_verified_at IS NOT NULL) OR "
            "(action IN ('register', 'evaluate') AND mfa_verified_at IS NULL)",
            name="learning_model_registry_event_mfa_required",
        ),
        CheckConstraint(
            "(evaluation_report_id IS NULL AND evaluation_report_hash IS NULL) OR "
            "(evaluation_report_id IS NOT NULL AND "
            "evaluation_report_hash ~ '^[0-9a-f]{64}$')",
            name="learning_model_registry_event_evaluation_pair_valid",
        ),
        CheckConstraint(
            "(expected_revision IS NULL OR expected_revision >= 1) AND "
            "(expected_pointer_version IS NULL OR expected_pointer_version >= 0)",
            name="learning_model_registry_event_expected_versions_valid",
        ),
        Index("ix_learning_model_registry_event_workspace_time", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    registry_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_state: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mfa_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_pointer_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evaluation_report_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    evaluation_report_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class LearningModelActivePointerRecord(Base):
    """Một active pointer CAS cho mỗi workspace; không tự kết nối scorer."""

    __tablename__ = "learning_model_active_pointer"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            ondelete="RESTRICT",
            name="fk_learning_model_active_pointer_registry",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "updated_by_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="RESTRICT",
            name="fk_learning_model_active_pointer_actor",
        ),
        CheckConstraint("version >= 0", name="learning_model_active_pointer_version_nonnegative"),
        CheckConstraint(
            "(version = 0 AND registry_id IS NULL AND updated_by_membership_id IS NULL) OR "
            "(version > 0 AND registry_id IS NOT NULL AND updated_by_membership_id IS NOT NULL)",
            name="learning_model_active_pointer_initial_state_valid",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    registry_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_by_membership_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "LearningModelActivePointerRecord",
    "LearningModelRegistryEventRecord",
    "LearningModelRegistryRecord",
]

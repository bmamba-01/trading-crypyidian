"""Evidence tín hiệu dùng chung (workspace) và job/quyết định theo bot (bot-scoped)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey

_BOT_INSTANCE_SCOPE = (
    "bot_instance.workspace_id",
    "bot_instance.id",
    "bot_instance.trading_account_id",
)
# FK ghép cùng workspace + bot tới unique (workspace_id, bot_id, id) của bot_config_version.
_BOT_CONFIG_VERSION_SCOPE = (
    "bot_config_version.workspace_id",
    "bot_config_version.bot_id",
    "bot_config_version.id",
)


class SharedSignalEvidenceRecord(UuidPrimaryKey, CreatedAt, Base):
    """Evidence bất biến scope workspace; không có cột bot/account/quantity/lệnh."""

    __tablename__ = "shared_signal_evidence"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_shared_signal_evidence_workspace_id"),
        UniqueConstraint("workspace_id", "evidence_hash", name="uq_shared_signal_evidence_hash"),
        # Định danh observation không gồm mốc thời gian: retry chỉ khác timestamp bị chặn ở DB.
        UniqueConstraint("workspace_id", "content_hash", name="uq_shared_signal_evidence_content"),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            ondelete="CASCADE",
            name="fk_shared_signal_evidence_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "feature_snapshot_id"],
            ["feature_snapshot.workspace_id", "feature_snapshot.snapshot_id"],
            name="fk_shared_signal_evidence_feature_snapshot",
        ),
        CheckConstraint(
            "length(evidence_hash) = 64 AND length(content_hash) = 64", name="evidence_hash_sha256"
        ),
        CheckConstraint("length(feature_snapshot_id) = 64", name="evidence_feature_sha256"),
        CheckConstraint(
            "observed_at <= available_at AND available_at < validity_end",
            name="evidence_time_order",
        ),
        CheckConstraint(
            "length(trim(symbol)) > 0 AND length(trim(setup_type)) > 0",
            name="evidence_identity_nonempty",
        ),
        CheckConstraint(
            "jsonb_typeof(source_refs) = 'array' AND jsonb_array_length(source_refs) > 0",
            name="evidence_sources_nonempty",
        ),
        Index(
            "ix_shared_signal_evidence_asof",
            "workspace_id",
            "symbol",
            "setup_type",
            "observed_at",
            "available_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    setup_type: Mapped[str] = mapped_column(String(80), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    validity_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    feature_snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    model_release_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)


class BotDecisionJobRecord(UuidPrimaryKey, CreatedAt, Base):
    """Job quyết định bot-scoped ghim evidence, config version và thời điểm đánh giá."""

    __tablename__ = "bot_decision_job"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_bot_decision_job_workspace_id"),
        UniqueConstraint(
            "workspace_id", "bot_id", "account_id", "id", name="uq_bot_decision_job_scope_id"
        ),
        UniqueConstraint(
            "workspace_id",
            "bot_id",
            "evidence_id",
            "bot_config_version_id",
            name="uq_bot_decision_job_key",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            list(_BOT_INSTANCE_SCOPE),
            name="fk_bot_decision_job_bot_account",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "evidence_id"],
            ["shared_signal_evidence.workspace_id", "shared_signal_evidence.id"],
            name="fk_bot_decision_job_evidence",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "bot_config_version_id"],
            list(_BOT_CONFIG_VERSION_SCOPE),
            name="fk_bot_decision_job_bot_config_version",
        ),
        CheckConstraint("status IN ('PENDING', 'COMPLETED')", name="bot_decision_job_status_valid"),
        CheckConstraint(
            "(status = 'COMPLETED') = (completed_at IS NOT NULL)",
            name="bot_decision_job_completion_consistent",
        ),
        CheckConstraint(
            "length(evidence_hash) = 64 AND length(context_hash) = 64",
            name="bot_decision_job_hashes_sha256",
        ),
        CheckConstraint(
            "jsonb_typeof(context_payload) = 'object'", name="bot_decision_job_context_object"
        ),
        Index("ix_bot_decision_job_bot_scope", "workspace_id", "bot_id", "account_id"),
        Index("ix_bot_decision_job_pending", "workspace_id", "bot_id", "status", "evaluated_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evidence_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Chỉ version PUBLISHED hiệu lực mới được fan-out (provider); FK chặn config bot khác.
    bot_config_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    context_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BotDecisionRecord(UuidPrimaryKey, CreatedAt, Base):
    """Quyết định bất biến của một bot: candidate hoặc abstain/veto kèm lineage."""

    __tablename__ = "bot_decision"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_bot_decision_workspace_id"),
        UniqueConstraint(
            "workspace_id", "bot_id", "idempotency_key", name="uq_bot_decision_idempotency"
        ),
        UniqueConstraint("workspace_id", "bot_id", "job_id", name="uq_bot_decision_job"),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            list(_BOT_INSTANCE_SCOPE),
            name="fk_bot_decision_bot_account",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id", "job_id"],
            [
                "bot_decision_job.workspace_id",
                "bot_decision_job.bot_id",
                "bot_decision_job.account_id",
                "bot_decision_job.id",
            ],
            name="fk_bot_decision_job",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "evidence_id"],
            ["shared_signal_evidence.workspace_id", "shared_signal_evidence.id"],
            name="fk_bot_decision_evidence",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "bot_config_version_id"],
            list(_BOT_CONFIG_VERSION_SCOPE),
            name="fk_bot_decision_bot_config_version",
        ),
        CheckConstraint(
            "status IN ('CANDIDATE', 'ABSTAIN', 'VETO')", name="bot_decision_status_valid"
        ),
        CheckConstraint(
            "(status = 'CANDIDATE') = (candidate_id IS NOT NULL)",
            name="bot_decision_candidate_consistent",
        ),
        CheckConstraint(
            "length(evidence_hash) = 64 AND length(bot_config_hash) = 64 "
            "AND length(root_settings_hash) = 64 AND length(idempotency_key) = 64 "
            "AND length(decision_hash) = 64",
            name="bot_decision_hashes_sha256",
        ),
        CheckConstraint("length(trim(reason_code)) > 0", name="bot_decision_reason_nonempty"),
        Index("ix_bot_decision_bot_scope", "workspace_id", "bot_id", "account_id"),
        Index("ix_bot_decision_bot_time", "workspace_id", "bot_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evidence_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Chỉ version PUBLISHED hiệu lực mới được fan-out (provider); FK chặn config bot khác.
    bot_config_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    root_settings_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    root_settings_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_release_id: Mapped[str] = mapped_column(String(80), nullable=False)
    strategy_release_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_release_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

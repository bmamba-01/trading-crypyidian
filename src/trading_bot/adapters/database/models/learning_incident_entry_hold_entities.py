"""Bảng metadata append-only cho xác nhận chặn entry từ incident học máy."""

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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey
from trading_bot.domain.learning.incident_entry_hold import (
    LearningIncidentHoldAction,
    LearningIncidentHoldEvent,
)

_PYTHON_STRIP_WHITESPACE_CODEPOINTS = (
    9,
    10,
    11,
    12,
    13,
    28,
    29,
    30,
    31,
    32,
    133,
    160,
    5760,
    *range(8192, 8203),
    8232,
    8233,
    8239,
    8287,
    12288,
)
_WHITESPACE_TRIM_SQL = " || ".join(
    f"pg_catalog.chr({codepoint})" for codepoint in _PYTHON_STRIP_WHITESPACE_CODEPOINTS
)


class LearningIncidentHoldEventRecord(UuidPrimaryKey, CreatedAt, Base):
    """Lịch sử HOLD/RELEASE; không mang sample, vector hoặc quyền thực thi giao dịch."""

    __tablename__ = "learning_incident_entry_hold_event"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "proposal_id"],
            ["learning_retrain_proposal.workspace_id", "learning_retrain_proposal.id"],
            ondelete="RESTRICT",
            name="fk_learning_incident_proposal",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "assessment_id", "assessment_hash"],
            [
                "learning_drift_assessment.workspace_id",
                "learning_drift_assessment.id",
                "learning_drift_assessment.content_hash",
            ],
            ondelete="RESTRICT",
            name="fk_learning_incident_assessment",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "incumbent_registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            ondelete="RESTRICT",
            name="fk_learning_incident_registry",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "actor_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="RESTRICT",
            name="fk_learning_incident_actor",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "proposal_id", "previous_event_id", "previous_event_hash"],
            [
                "learning_incident_entry_hold_event.workspace_id",
                "learning_incident_entry_hold_event.proposal_id",
                "learning_incident_entry_hold_event.id",
                "learning_incident_entry_hold_event.event_hash",
            ],
            ondelete="RESTRICT",
            name="fk_learning_incident_predecessor",
        ),
        UniqueConstraint(
            "workspace_id", "proposal_id", "revision", name="uq_learning_incident_revision"
        ),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_incident_idempotency"
        ),
        UniqueConstraint(
            "workspace_id", "previous_event_id", name="uq_learning_incident_successor"
        ),
        UniqueConstraint(
            "workspace_id",
            "proposal_id",
            "id",
            "event_hash",
            name="uq_learning_incident_exact_event",
        ),
        CheckConstraint(
            "(action = 'HOLD' AND revision = 1 AND previous_event_id IS NULL AND "
            "previous_event_hash IS NULL) OR (action = 'RELEASE' AND revision = "
            "2 AND previous_event_id IS NOT NULL AND previous_event_hash IS NOT "
            "NULL AND previous_event_id <> id)",
            name="learning_incident_transition_valid",
        ),
        CheckConstraint(
            "proposal_hash ~ '^[0-9a-f]{64}$' AND assessment_hash ~ "
            "'^[0-9a-f]{64}$' AND incumbent_artifact_hash ~ '^[0-9a-f]{64}$' AND "
            "event_hash ~ '^[0-9a-f]{64}$' AND intent_hash ~ '^[0-9a-f]{64}$' "
            "AND (previous_event_hash IS NULL OR previous_event_hash ~ "
            "'^[0-9a-f]{64}$')",
            name="learning_incident_hashes_valid",
        ),
        CheckConstraint(
            "incumbent_registry_revision >= 1 AND active_pointer_version >= 1 "
            "AND deployment_revision >= 1",
            name="learning_incident_versions_valid",
        ),
        CheckConstraint(
            f"length(btrim(reason, ({_WHITESPACE_TRIM_SQL}))) BETWEEN 1 AND 500 "
            f"AND length(reason) <= 500 AND length(btrim(request_id, ({_WHITESPACE_TRIM_SQL}))) "
            "BETWEEN 1 AND 200 AND mfa_method = 'TOTP' AND mfa_verified_at <= created_at "
            "AND created_at - mfa_verified_at <= interval '15 minutes'",
            name="learning_incident_audit_valid",
        ),
        Index("ix_learning_incident_workspace_proposal", "workspace_id", "proposal_id", "revision"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    proposal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    assessment_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    assessment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_event_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    previous_event_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    incumbent_registry_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incumbent_registry_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    incumbent_artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    active_pointer_version: Mapped[int] = mapped_column(Integer, nullable=False)
    deployment_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mfa_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mfa_method: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    idempotency_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    request_id: Mapped[str] = mapped_column(String(200), nullable=False)
    intent_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_event: Mapped[str] = mapped_column(Text, nullable=False)
    event_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

    @classmethod
    def from_domain(cls, event: LearningIncidentHoldEvent) -> LearningIncidentHoldEventRecord:
        """Ghi cùng canonical bytes/hash đã được domain xác minh."""
        values = {name: getattr(event, name) for name in event.__dataclass_fields__}
        values["id"] = values.pop("event_id")
        values["action"] = event.action.value
        values["mfa_method"] = str(event.mfa_method)
        return cls(
            **values,
            intent_hash=event.intent_hash,
            event_hash=event.event_hash,
            canonical_event=event.canonical_json,
            event_payload=event.canonical_payload,
        )

    def to_domain(self) -> LearningIncidentHoldEvent:
        """Tái dựng và kiểm toàn bộ hash để không trả record đã sai canonical."""
        values = {
            name: getattr(self, "id" if name == "event_id" else name)
            for name in LearningIncidentHoldEvent.__dataclass_fields__
        }
        values["action"] = LearningIncidentHoldAction(self.action)
        event = LearningIncidentHoldEvent(**values)
        if (
            event.event_hash != self.event_hash
            or event.intent_hash != self.intent_hash
            or event.canonical_json != self.canonical_event
            or event.canonical_payload != self.event_payload
        ):
            raise ValueError("Event incident đã lưu không khớp canonical/hash.")
        return event

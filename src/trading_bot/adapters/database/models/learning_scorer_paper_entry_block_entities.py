"""ORM cho bản ghi chặn entry PAPER theo một quyết định của bot."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt
from trading_bot.domain.learning.paper_entry_block import LearningPaperEntryBlockEvent


class LearningPaperEntryBlockRecord(CreatedAt, Base):
    """Ledger bất biến, lưu dữ liệu cần để đối chiếu quyết định và quyền thao tác."""

    __tablename__ = "learning_scorer_paper_entry_block"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "bot_decision_id", name="uq_learning_paper_block_decision"
        ),
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_learning_paper_block_key"),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "bot_decision_id"],
            ["bot_decision.workspace_id", "bot_decision.bot_id", "bot_decision.id"],
            ondelete="RESTRICT",
            name="fk_learning_paper_block_decision",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "score_event_id"],
            ["learning_score_event.workspace_id", "learning_score_event.id"],
            ondelete="RESTRICT",
            name="fk_learning_paper_block_score_event",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "actor_user_id"],
            ["user_membership.workspace_id", "user_membership.user_id"],
            ondelete="RESTRICT",
            name="fk_learning_paper_block_actor",
        ),
        CheckConstraint(
            "'00000000-0000-0000-0000-000000000000'::uuid NOT IN ("
            "id, workspace_id, bot_id, bot_decision_id, score_event_id, deployment_event_id, "
            "registry_id, scorer_artifact_id, actor_user_id, idempotency_key)",
            name="nonzero_uuids",
        ),
        CheckConstraint(
            "bot_decision_hash ~ '^[0-9a-f]{64}$' AND candidate_id ~ '^[0-9a-f]{64}$' "
            "AND score_event_hash ~ '^[0-9a-f]{64}$' AND scorer_artifact_hash ~ '^[0-9a-f]{64}$'",
            name="hashes",
        ),
        CheckConstraint(
            "deployment_revision >= 1 AND registry_revision >= 1 AND active_pointer_version >= 0",
            name="revisions",
        ),
        CheckConstraint(
            r"reason = pg_catalog.btrim(reason, U&'\0009\000A\000D\0020\0085\00A0\1680"
            r"\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029"
            r"\202F\205F\3000') AND char_length(reason) BETWEEN 1 AND 500 AND "
            r"reason !~ '[\x01-\x08\x0B\x0C\x0E-\x1F]'",
            name="reason",
        ),
        CheckConstraint(
            "mfa_verified_at <= created_at AND "
            "created_at - mfa_verified_at <= interval '15 minutes'",
            name="mfa_window",
        ),
    )

    event_id: Mapped[UUID] = mapped_column("id", Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    bot_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    bot_decision_hash: Mapped[str] = mapped_column(String(64))
    candidate_id: Mapped[str] = mapped_column(String(64))
    score_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    score_event_hash: Mapped[str] = mapped_column(String(64))
    deployment_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    deployment_revision: Mapped[int] = mapped_column(Integer)
    registry_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    registry_revision: Mapped[int] = mapped_column(Integer)
    active_pointer_version: Mapped[int] = mapped_column(Integer)
    scorer_artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    scorer_artifact_hash: Mapped[str] = mapped_column(String(64))
    actor_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    mfa_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(500))
    idempotency_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))

    def to_domain(self) -> LearningPaperEntryBlockEvent:
        """Nạp dữ liệu audit qua cùng quy tắc xác thực với sự kiện mới."""
        return LearningPaperEntryBlockEvent(
            event_id=self.event_id,
            workspace_id=self.workspace_id,
            bot_id=self.bot_id,
            bot_decision_id=self.bot_decision_id,
            bot_decision_hash=self.bot_decision_hash,
            candidate_id=self.candidate_id,
            score_event_id=self.score_event_id,
            score_event_hash=self.score_event_hash,
            deployment_event_id=self.deployment_event_id,
            deployment_revision=self.deployment_revision,
            registry_id=self.registry_id,
            registry_revision=self.registry_revision,
            active_pointer_version=self.active_pointer_version,
            scorer_artifact_id=self.scorer_artifact_id,
            scorer_artifact_hash=self.scorer_artifact_hash,
            actor_user_id=self.actor_user_id,
            mfa_verified_at=self.mfa_verified_at,
            reason=self.reason,
            created_at=self.created_at,
            idempotency_key=self.idempotency_key,
        )

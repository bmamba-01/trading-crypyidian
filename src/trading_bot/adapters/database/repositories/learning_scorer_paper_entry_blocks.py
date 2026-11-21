"""Ghi nối tiếp bản ghi chặn entry PAPER trong transaction của dịch vụ gọi."""

from dataclasses import asdict, dataclass, replace
from uuid import UUID

from sqlalchemy import exists, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.learning_scorer_paper_entry_block_entities import (
    LearningPaperEntryBlockRecord,
)
from trading_bot.adapters.database.models.paper_entities import OrderIntentRecord
from trading_bot.domain.learning.paper_entry_block import LearningPaperEntryBlockEvent


class LearningPaperEntryBlockConflict(RuntimeError):
    """Quyết định đã có bản ghi chặn hoặc khóa idempotency đã dùng cho ý định khác."""


@dataclass(frozen=True, slots=True)
class LearningPaperEntryBlockAppendResult:
    event: LearningPaperEntryBlockEvent
    is_duplicate: bool


@dataclass(frozen=True, slots=True)
class PaperEntryBlockEvidence:
    """Metadata bất biến đã nối với một quyết định APPROVE/PAPER_GATE ở DB."""

    bot_id: UUID
    bot_decision_hash: str
    candidate_id: str
    score_event_id: UUID
    score_event_hash: str
    deployment_event_id: UUID
    deployment_revision: int
    registry_id: UUID
    registry_revision: int
    active_pointer_version: int
    scorer_artifact_id: UUID
    scorer_artifact_hash: str
    source_rights_fingerprint: str


class LearningPaperEntryBlockRepository:
    """Nhận workspace đã thiết lập RLS trong transaction hiện tại."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_decision(self, *, workspace_id: UUID, bot_decision_id: UUID) -> None:
        """Lấy khóa transaction theo BotDecision sau khóa workspace L1."""
        await self._session.execute(
            text(
                "SELECT pg_catalog.pg_advisory_xact_lock("
                "pg_catalog.hashtextextended(:decision_lock, 0))"
            ),
            {"decision_lock": f"paper-entry-decision:{workspace_id}:{bot_decision_id}"},
        )

    async def load_blockable_evidence(
        self, *, workspace_id: UUID, bot_decision_id: UUID
    ) -> PaperEntryBlockEvidence | None:
        """Chỉ đọc lineage đủ điều kiện qua hàm DB được cấp riêng cho Admin."""
        result = await self._session.execute(
            text(
                "SELECT * FROM public.wp06_read_paper_entry_block_source("
                "CAST(:workspace_id AS uuid), CAST(:decision_id AS uuid))"
            ),
            {"workspace_id": str(workspace_id), "decision_id": str(bot_decision_id)},
        )
        row = result.mappings().one_or_none()
        return PaperEntryBlockEvidence(**row) if row is not None else None

    async def has_paper_order_intent(
        self, *, workspace_id: UUID, bot_id: UUID, bot_decision_id: UUID
    ) -> bool:
        """Kiểm có OrderIntent PAPER gắn quyết định, không lấy nội dung hay trạng thái."""
        result = await self._session.execute(
            select(
                exists().where(
                    OrderIntentRecord.workspace_id == workspace_id,
                    OrderIntentRecord.bot_id == bot_id,
                    OrderIntentRecord.bot_decision_id == bot_decision_id,
                    OrderIntentRecord.mode == "PAPER",
                )
            )
        )
        return result.scalar_one()

    async def find_by_decision(
        self, *, workspace_id: UUID, bot_decision_id: UUID
    ) -> LearningPaperEntryBlockEvent | None:
        result = await self._session.execute(
            select(LearningPaperEntryBlockRecord).where(
                LearningPaperEntryBlockRecord.workspace_id == workspace_id,
                LearningPaperEntryBlockRecord.bot_decision_id == bot_decision_id,
            )
        )
        record = result.scalar_one_or_none()
        return record.to_domain() if record is not None else None

    async def find_by_idempotency_key(
        self, *, workspace_id: UUID, idempotency_key: UUID
    ) -> LearningPaperEntryBlockEvent | None:
        result = await self._session.execute(
            select(LearningPaperEntryBlockRecord).where(
                LearningPaperEntryBlockRecord.workspace_id == workspace_id,
                LearningPaperEntryBlockRecord.idempotency_key == idempotency_key,
            )
        )
        record = result.scalar_one_or_none()
        return record.to_domain() if record is not None else None

    async def is_blocked(self, *, workspace_id: UUID, bot_decision_id: UUID) -> bool:
        """Chỉ đọc khóa quyết định, phù hợp với quyền của kết nối runtime."""
        decision = select(LearningPaperEntryBlockRecord.bot_decision_id).where(
            LearningPaperEntryBlockRecord.workspace_id == workspace_id,
            LearningPaperEntryBlockRecord.bot_decision_id == bot_decision_id,
        )
        result = await self._session.execute(select(exists(decision)))
        return result.scalar_one()

    async def append_block(
        self, *, workspace_id: UUID, event: LearningPaperEntryBlockEvent
    ) -> LearningPaperEntryBlockAppendResult:
        """Thêm đúng một bản ghi hoặc trả lại bản ghi gốc khi thử lại cùng ý định."""
        if event.workspace_id != workspace_id:
            raise PermissionError("Sự kiện chặn không thuộc workspace của transaction.")
        statement = (
            insert(LearningPaperEntryBlockRecord)
            .values(**asdict(event))
            .on_conflict_do_nothing()
            .returning(LearningPaperEntryBlockRecord.event_id)
        )
        result = await self._session.execute(statement)
        if result.scalar_one_or_none() is not None:
            return LearningPaperEntryBlockAppendResult(event, False)

        existing = await self.find_by_idempotency_key(
            workspace_id=workspace_id, idempotency_key=event.idempotency_key
        )
        if existing is None:
            raise LearningPaperEntryBlockConflict("Quyết định đã có bản ghi chặn hoặc trùng ID.")
        # ID và thời điểm audit do server sinh có thể khác giữa các lần gửi cùng yêu cầu.
        if (
            replace(
                existing,
                event_id=event.event_id,
                created_at=event.created_at,
                mfa_verified_at=event.mfa_verified_at,
            )
            != event
        ):
            raise LearningPaperEntryBlockConflict("Khóa idempotency đã dùng cho ý định chặn khác.")
        return LearningPaperEntryBlockAppendResult(existing, True)

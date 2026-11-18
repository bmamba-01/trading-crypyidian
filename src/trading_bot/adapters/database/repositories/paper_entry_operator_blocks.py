"""Repository quản lý sổ cái chặn entry PAPER theo BotDecision (CRY-440)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.paper_entities import OrderIntentRecord
from trading_bot.adapters.database.models.paper_entry_block_entities import (
    PaperEntryOperatorBlockRecord,
)
from trading_bot.adapters.database.models.signal_entities import BotDecisionRecord


class OperatorEntryBlockError(Exception):
    """Lỗi khi chặn entry PAPER theo BotDecision."""

    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class PaperEntryOperatorBlockRepository:
    """Đọc/ghi append-only ``paper_entry_operator_block`` kèm khóa ``FOR UPDATE`` chống race."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_decision_and_check_blocked(
        self,
        *,
        workspace_id: UUID,
        bot_decision_id: UUID,
    ) -> PaperEntryOperatorBlockRecord | None:
        """Khóa giao dịch theo ``bot_decision_id`` rồi kiểm tra đã bị chặn chưa."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:decision_id, 0))"),
            {"decision_id": str(bot_decision_id)},
        )
        return await self.is_blocked(workspace_id=workspace_id, bot_decision_id=bot_decision_id)

    async def is_blocked(
        self,
        *,
        workspace_id: UUID,
        bot_decision_id: UUID,
    ) -> PaperEntryOperatorBlockRecord | None:
        """Kiểm tra quyết định đã có bản ghi chặn entry PAPER chưa."""
        return await self._session.scalar(
            select(PaperEntryOperatorBlockRecord)
            .where(
                PaperEntryOperatorBlockRecord.workspace_id == workspace_id,
                PaperEntryOperatorBlockRecord.bot_decision_id == bot_decision_id,
            )
            .limit(1)
        )

    async def list_recent(
        self,
        *,
        workspace_id: UUID,
        limit: int = 20,
    ) -> list[PaperEntryOperatorBlockRecord]:
        """Liệt kê các lần operator chặn entry gần nhất trong workspace."""
        rows = (
            await self._session.scalars(
                select(PaperEntryOperatorBlockRecord)
                .where(PaperEntryOperatorBlockRecord.workspace_id == workspace_id)
                .order_by(
                    PaperEntryOperatorBlockRecord.created_at.desc(),
                    PaperEntryOperatorBlockRecord.id.desc(),
                )
                .limit(max(1, min(limit, 100)))
            )
        ).all()
        return list(rows)

    async def block_decision(
        self,
        *,
        workspace_id: UUID,
        bot_decision_id: UUID,
        blocked_by: UUID,
        reason: str,
        mfa_verified_at: datetime,
        correlation_id: UUID,
    ) -> PaperEntryOperatorBlockRecord:
        """Khóa ``bot_decision`` ``FOR UPDATE`` và ghi bản ghi chặn trước khi có OrderIntent."""
        clean_reason = reason.strip()
        if not clean_reason:
            raise OperatorEntryBlockError(
                "REASON_REQUIRED",
                "Cần ghi rõ lý do chặn vào lệnh PAPER.",
                status_code=400,
            )

        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:decision_id, 0))"),
            {"decision_id": str(bot_decision_id)},
        )
        decision = await self._session.scalar(
            select(BotDecisionRecord).where(
                BotDecisionRecord.workspace_id == workspace_id,
                BotDecisionRecord.id == bot_decision_id,
            )
        )
        if decision is None:
            raise OperatorEntryBlockError(
                "DECISION_NOT_FOUND",
                "Không tìm thấy quyết định bot trong workspace.",
                status_code=404,
            )
        if decision.status != "CANDIDATE":
            raise OperatorEntryBlockError(
                "DECISION_NOT_CANDIDATE",
                "Chỉ chặn được quyết định ứng viên vào lệnh PAPER; không sửa VETO/ABSTAIN.",
                status_code=409,
            )
        if isinstance(decision.payload, dict) and decision.payload.get("scorer_mode") == "SHADOW":
            raise OperatorEntryBlockError(
                "SHADOW_NOT_APPLICABLE",
                "Không áp dụng chặn cho chế độ SHADOW; chỉ áp dụng cho PAPER.",
                status_code=409,
            )

        existing_intent_id = await self._session.scalar(
            select(OrderIntentRecord.id)
            .where(
                OrderIntentRecord.workspace_id == workspace_id,
                OrderIntentRecord.mode == "PAPER",
                OrderIntentRecord.bot_decision_id == bot_decision_id,
            )
            .limit(1)
        )
        if existing_intent_id is not None:
            raise OperatorEntryBlockError(
                "ENTRY_ALREADY_RECORDED",
                "Quyết định này đã tạo OrderIntent PAPER, không thể chặn thêm.",
                status_code=409,
            )

        existing_block = await self.is_blocked(
            workspace_id=workspace_id,
            bot_decision_id=bot_decision_id,
        )
        if existing_block is not None:
            raise OperatorEntryBlockError(
                "ALREADY_BLOCKED",
                "Quyết định này đã bị chặn vào lệnh PAPER trước đó.",
                status_code=409,
            )

        record = PaperEntryOperatorBlockRecord(
            workspace_id=workspace_id,
            bot_id=decision.bot_id,
            bot_decision_id=bot_decision_id,
            blocked_by=blocked_by,
            reason=clean_reason[:500],
            mfa_verified_at=mfa_verified_at,
            correlation_id=correlation_id,
        )
        self._session.add(record)
        await self._session.flush()
        return record


__all__ = [
    "OperatorEntryBlockError",
    "PaperEntryOperatorBlockRepository",
]

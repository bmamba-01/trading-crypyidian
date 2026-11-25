"""Adapter ghi nhận kết quả denial của preflight chỉ đọc vào PostgreSQL (OPS07F / Task 5).

Chỉ cập nhật trạng thái readiness của bot về `RECOVERY_REQUIRED` kèm mã lý do và đánh dấu
`activity_status = 'UNVERIFIED'`; tuyệt đối không xóa hay giải phóng `risk_reservation`.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.bot_runtime_entities import BotRuntimeStateRecord
from trading_bot.domain.execution.live_preflight import LivePreflightVerdict


class SqlAlchemyPreflightIncidentRecorder:
    """Lưu mã từ chối preflight vào `bot_runtime_state` mà không giải phóng reservation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_incident(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        verdict: LivePreflightVerdict,
    ) -> None:
        state = await self._session.scalar(
            select(BotRuntimeStateRecord).where(
                BotRuntimeStateRecord.workspace_id == workspace_id,
                BotRuntimeStateRecord.bot_id == bot_id,
                BotRuntimeStateRecord.account_id == account_id,
            )
        )
        if state is None:
            raise LookupError(
                f"Không tìm thấy bot_runtime_state cho workspace={workspace_id}, bot={bot_id}."
            )
        state.readiness = "RECOVERY_REQUIRED"
        state.reason_code = verdict.reason_code.value
        state.activity_status = "UNVERIFIED"
        state.activity_checked_at = verdict.checked_at
        await self._session.flush()

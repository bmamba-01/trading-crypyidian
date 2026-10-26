"""Trạng thái runtime theo bot (MB05.3); chỉ process giữ lease ghi, fence chạy trước mọi ghi."""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import BotRuntimeStateRecord
from trading_bot.adapters.database.repositories.bot_runtime_leases import (
    RuntimeFenceGuard,
    runtime_db_reason,
)
from trading_bot.domain.execution.runtime_control import (
    BotReadiness,
    RuntimeActivityStatus,
    RuntimeControlError,
    RuntimeFence,
)

# Chỉ mã máy đọc được; không lưu stack trace/secret/thông điệp tự do (WBS bảng dữ liệu).
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_UNSET = object()


def _safe_code(value: str | None) -> str | None:
    if value is not None and not _SAFE_CODE.fullmatch(value):
        raise RuntimeControlError("STATE_REASON_INVALID")
    return value


class BotRuntimeStateRepository:
    """Upsert một hàng state cho mỗi bot với fencing token hiện hành (trigger DB kiểm lại)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, *, workspace_id: UUID, bot_id: UUID) -> BotRuntimeStateRecord | None:
        return await self._session.scalar(
            select(BotRuntimeStateRecord).where(
                BotRuntimeStateRecord.workspace_id == workspace_id,
                BotRuntimeStateRecord.bot_id == bot_id,
            )
        )

    async def upsert(
        self,
        *,
        fence: RuntimeFence,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        readiness: BotReadiness,
        reason_code: str | None,
        heartbeat_at: datetime,
        applied_bot_control_revision: int | None = None,
        applied_workspace_control_revision: int | None = None,
        last_checkpoint_revision: int | None = None,
        activity_cursor: str | None = None,
        activity_checked_at: datetime | None = None,
        activity_status: RuntimeActivityStatus | None = None,
        fanout_exclusion_code: object = _UNSET,
    ) -> BotRuntimeStateRecord:
        """Ghi state sau khi fence xác nhận lease; giá trị None giữ nguyên cột tùy chọn."""
        _safe_code(reason_code)
        if fanout_exclusion_code is not _UNSET:
            _safe_code(fanout_exclusion_code if isinstance(fanout_exclusion_code, str) else None)
        if heartbeat_at.tzinfo is None or heartbeat_at.utcoffset() is None:
            raise RuntimeControlError("STATE_TIME_NOT_UTC")
        await RuntimeFenceGuard(self._session).require(
            workspace_id=workspace_id, bot_id=bot_id, account_id=account_id, fence=fence
        )
        record = await self._session.scalar(
            select(BotRuntimeStateRecord)
            .where(
                BotRuntimeStateRecord.workspace_id == workspace_id,
                BotRuntimeStateRecord.bot_id == bot_id,
            )
            .with_for_update()
        )
        if record is None:
            record = BotRuntimeStateRecord(
                id=uuid4(),
                workspace_id=workspace_id,
                bot_id=bot_id,
                account_id=account_id,
                applied_bot_control_revision=0,
                applied_workspace_control_revision=0,
            )
            self._session.add(record)
        elif record.account_id != account_id:
            raise RuntimeControlError("STATE_SCOPE_MISMATCH")
        record.readiness = BotReadiness(readiness).value
        record.reason_code = reason_code
        record.heartbeat_at = heartbeat_at
        record.fencing_token = int(fence.fencing_token)
        optional: dict[str, object] = {
            "applied_bot_control_revision": applied_bot_control_revision,
            "applied_workspace_control_revision": applied_workspace_control_revision,
            "last_checkpoint_revision": last_checkpoint_revision,
            "activity_cursor": activity_cursor,
            "activity_checked_at": activity_checked_at,
            "activity_status": (
                RuntimeActivityStatus(activity_status).value if activity_status else None
            ),
        }
        for column, value in optional.items():
            if value is not None:
                setattr(record, column, value)
        if fanout_exclusion_code is not _UNSET:
            record.fanout_exclusion_code = fanout_exclusion_code  # type: ignore[assignment]
        try:
            await self._session.flush()
        except DBAPIError as error:
            raise RuntimeControlError(runtime_db_reason(error, "STATE_WRITE_REJECTED")) from None
        return record

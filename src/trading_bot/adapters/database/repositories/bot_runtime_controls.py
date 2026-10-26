"""Control vận hành append-only: kill workspace, pause bot (MB05.3, ruling 21, 22, 25, 27).

Mỗi thay đổi INSERT một hàng revision kế tiếp; trạng thái hiện hành là revision lớn nhất; chưa
có hàng nghĩa là chưa từng kill/pause. Không có hàm UPDATE/DELETE. Mutation lấy L1
`workspace-settings:{ws}` trước khi đọc/ghi để tuần tự hóa với mọi command entry/reservation.
`read_runtime_control` là helper đọc lại control trong chính transaction entry/reservation
(bất biến ruling 27); MB-05 Part B nối helper này vào mọi đường entry/reservation.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    BotInstance,
    BotRuntimeControlRecord,
    WorkspaceRuntimeControlRecord,
)
from trading_bot.adapters.database.repositories.bot_runtime_leases import runtime_db_reason
from trading_bot.adapters.database.repositories.workspace_settings import (
    WorkspaceSettingsRepository,
)
from trading_bot.domain.execution.runtime_control import (
    BotControlResult,
    BotControlState,
    BotEntryState,
    ControlChangeKind,
    RuntimeControlError,
    RuntimeControlSnapshot,
    WorkspaceControlResult,
    WorkspaceControlState,
    WorkspaceKillState,
    plan_bot_control_change,
    plan_workspace_control_change,
)


def _validate_reason(reason_code: str, reason_text: str) -> None:
    if (
        not isinstance(reason_code, str)
        or not reason_code.strip()
        or len(reason_code) > 80
        or not isinstance(reason_text, str)
        or not reason_text.strip()
        or len(reason_text) > 500
    ):
        raise RuntimeControlError("CONTROL_REASON_REQUIRED")


async def _current_workspace(
    session: AsyncSession, workspace_id: UUID
) -> WorkspaceControlState | None:
    record = await session.scalar(
        select(WorkspaceRuntimeControlRecord)
        .where(WorkspaceRuntimeControlRecord.workspace_id == workspace_id)
        .order_by(WorkspaceRuntimeControlRecord.revision.desc())
        .limit(1)
    )
    if record is None:
        return None
    return WorkspaceControlState(int(record.revision), WorkspaceKillState(record.kill_state))


async def _current_bot(
    session: AsyncSession, workspace_id: UUID, bot_id: UUID
) -> BotControlState | None:
    record = await session.scalar(
        select(BotRuntimeControlRecord)
        .where(
            BotRuntimeControlRecord.workspace_id == workspace_id,
            BotRuntimeControlRecord.bot_id == bot_id,
        )
        .order_by(BotRuntimeControlRecord.revision.desc())
        .limit(1)
    )
    if record is None:
        return None
    return BotControlState(int(record.revision), BotEntryState(record.entry_state))


async def read_runtime_control(
    session: AsyncSession, *, workspace_id: UUID, bot_id: UUID
) -> RuntimeControlSnapshot:
    """Đọc control workspace + bot trong transaction hiện tại; lỗi DB/quyền thì fail-closed.

    Caller phải giữ L1 trước khi gọi và không được dùng giá trị đọc ở transaction trước.
    """
    try:
        workspace = await _current_workspace(session, workspace_id)
        bot = await _current_bot(session, workspace_id, bot_id)
    except SQLAlchemyError, ValueError:
        raise RuntimeControlError("RUNTIME_CONTROL_UNAVAILABLE") from None
    return RuntimeControlSnapshot(workspace=workspace, bot=bot)


class BotRuntimeControlRepository:
    """Ghi ý định control (admin-web); runtime chỉ có quyền SELECT trên bảng control."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def current_workspace(self, workspace_id: UUID) -> WorkspaceControlState | None:
        return await _current_workspace(self._session, workspace_id)

    async def current_bot(self, workspace_id: UUID, bot_id: UUID) -> BotControlState | None:
        return await _current_bot(self._session, workspace_id, bot_id)

    async def append_workspace_control(
        self,
        *,
        workspace_id: UUID,
        target: WorkspaceKillState,
        expected_revision: int | None,
        changed_by: UUID,
        reason_code: str,
        reason_text: str,
        correlation_id: UUID,
    ) -> WorkspaceControlResult:
        """Kill/release workspace; idempotent theo trạng thái đích (noop không thêm revision)."""
        _validate_reason(reason_code, reason_text)
        await WorkspaceSettingsRepository(self._session).lock_workspace(workspace_id)
        current = await _current_workspace(self._session, workspace_id)
        change = plan_workspace_control_change(
            current, WorkspaceKillState(target), expected_revision=expected_revision
        )
        if change.kind is ControlChangeKind.NOOP:
            return WorkspaceControlResult(
                workspace_id=workspace_id,
                kill_state=current.kill_state if current else WorkspaceKillState.ACTIVE,
                revision=change.next_revision,
                changed=False,
            )
        self._session.add(
            WorkspaceRuntimeControlRecord(
                id=uuid4(),
                workspace_id=workspace_id,
                revision=change.next_revision,
                kill_state=WorkspaceKillState(target).value,
                reason_code=reason_code,
                reason_text=reason_text,
                changed_by=changed_by,
                correlation_id=correlation_id,
            )
        )
        await self._flush()
        return WorkspaceControlResult(
            workspace_id=workspace_id,
            kill_state=WorkspaceKillState(target),
            revision=change.next_revision,
            changed=True,
        )

    async def append_bot_control(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        target: BotEntryState,
        expected_revision: int | None,
        changed_by: UUID,
        reason_code: str,
        reason_text: str,
        correlation_id: UUID,
    ) -> BotControlResult:
        """Pause/resume bot; resume bắt buộc `expected_revision` (domain kiểm)."""
        _validate_reason(reason_code, reason_text)
        await WorkspaceSettingsRepository(self._session).lock_workspace(workspace_id)
        current = await _current_bot(self._session, workspace_id, bot_id)
        change = plan_bot_control_change(
            current, BotEntryState(target), expected_revision=expected_revision
        )
        if change.kind is ControlChangeKind.NOOP:
            return BotControlResult(
                bot_id=bot_id,
                entry_state=current.entry_state if current else BotEntryState.ENABLED,
                revision=change.next_revision,
                changed=False,
            )
        account_id = await self._session.scalar(
            select(BotInstance.trading_account_id).where(
                BotInstance.workspace_id == workspace_id, BotInstance.id == bot_id
            )
        )
        if account_id is None:
            # Bot không thấy được (khác workspace/thiếu grant) trả cùng lỗi như không tồn tại.
            raise RuntimeControlError("BOT_NOT_FOUND")
        self._session.add(
            BotRuntimeControlRecord(
                id=uuid4(),
                workspace_id=workspace_id,
                bot_id=bot_id,
                account_id=account_id,
                revision=change.next_revision,
                entry_state=BotEntryState(target).value,
                reason_code=reason_code,
                reason_text=reason_text,
                changed_by=changed_by,
                correlation_id=correlation_id,
            )
        )
        await self._flush()
        return BotControlResult(
            bot_id=bot_id,
            entry_state=BotEntryState(target),
            revision=change.next_revision,
            changed=True,
        )

    async def _flush(self) -> None:
        try:
            await self._session.flush()
        except IntegrityError as error:
            # Unique (workspace, [bot,] revision) trùng: request song song đã ghi revision này.
            reason = runtime_db_reason(error, "CONTROL_REVISION_CONFLICT")
            raise RuntimeControlError(reason) from None
        except DBAPIError as error:
            raise RuntimeControlError(runtime_db_reason(error, "CONTROL_WRITE_REJECTED")) from None

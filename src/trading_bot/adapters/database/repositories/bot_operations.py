"""Đọc trạng thái vận hành bot và ghi disable account cho admin-web (MB-07a, ruling 31–34).

Mọi truy vấn chạy dưới RLS `crypidian_admin_web` của transaction hiện tại (membership ACTIVE với
role workspace-wide hoặc `bot_grant`), nên bot ngoài scope không xuất hiện. Quyền hành động vẫn
kiểm ở application (`BotGrantRepository.require`). `disable_account` chỉ được gọi sau
`BotLifecycleGuard.ensure_flat_for_disable` (đã giữ L1) trong cùng transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    BotInstance,
    BotRuntimeStateRecord,
    TradingAccount,
)
from trading_bot.adapters.database.repositories.bot_runtime_controls import (
    BotRuntimeControlRepository,
)
from trading_bot.domain.execution.runtime_control import BotEntryState, WorkspaceKillState


@dataclass(frozen=True, slots=True)
class BotIdentityRow:
    """Bot thấy được cùng account PAPER của nó (MB-01: bot mirror trạng thái account)."""

    bot_id: UUID
    name: str
    status: str
    account_id: UUID


@dataclass(frozen=True, slots=True)
class BotRuntimeRow:
    """Control bot hiện hành (chưa có hàng = ENABLED, revision 0) và readiness runtime."""

    entry_state: BotEntryState
    control_revision: int
    readiness: str | None
    readiness_reason_code: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceControlRow:
    kill_state: WorkspaceKillState
    revision: int


class BotOperationsRepository:
    """Read model vận hành + ghi `trading_account.status` (grant cột của migration MB-07a)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_bots(self, workspace_id: UUID) -> list[BotIdentityRow]:
        rows = await self._session.execute(
            select(BotInstance.id, BotInstance.name, BotInstance.status, TradingAccount.id)
            .join(
                TradingAccount,
                (TradingAccount.workspace_id == BotInstance.workspace_id)
                & (TradingAccount.id == BotInstance.trading_account_id),
            )
            .where(BotInstance.workspace_id == workspace_id)
            .order_by(BotInstance.name, BotInstance.id)
        )
        return [BotIdentityRow(row[0], row[1], row[2], row[3]) for row in rows.all()]

    async def get_bot(self, workspace_id: UUID, bot_id: UUID) -> BotIdentityRow | None:
        row = (
            await self._session.execute(
                select(BotInstance.id, BotInstance.name, BotInstance.status, TradingAccount.id)
                .join(
                    TradingAccount,
                    (TradingAccount.workspace_id == BotInstance.workspace_id)
                    & (TradingAccount.id == BotInstance.trading_account_id),
                )
                .where(BotInstance.workspace_id == workspace_id, BotInstance.id == bot_id)
            )
        ).first()
        return BotIdentityRow(row[0], row[1], row[2], row[3]) if row is not None else None

    async def bot_runtime(self, workspace_id: UUID, bot_id: UUID) -> BotRuntimeRow:
        control = await BotRuntimeControlRepository(self._session).current_bot(workspace_id, bot_id)
        state = (
            await self._session.execute(
                select(BotRuntimeStateRecord.readiness, BotRuntimeStateRecord.reason_code).where(
                    BotRuntimeStateRecord.workspace_id == workspace_id,
                    BotRuntimeStateRecord.bot_id == bot_id,
                )
            )
        ).first()
        return BotRuntimeRow(
            entry_state=control.entry_state if control is not None else BotEntryState.ENABLED,
            control_revision=control.revision if control is not None else 0,
            readiness=state[0] if state is not None else None,
            readiness_reason_code=state[1] if state is not None else None,
        )

    async def workspace_control(self, workspace_id: UUID) -> WorkspaceControlRow:
        control = await BotRuntimeControlRepository(self._session).current_workspace(workspace_id)
        if control is None:
            return WorkspaceControlRow(WorkspaceKillState.ACTIVE, 0)
        return WorkspaceControlRow(control.kill_state, control.revision)

    async def disable_account(self, workspace_id: UUID, account_id: UUID) -> bool:
        """Khóa account rồi đặt DISABLED; trigger MB-01 mirror sang `bot_instance`.

        Trả False khi account đã DISABLED (noop). Caller phải giữ L1 (guard phẳng) trước.
        """
        account = await self._session.scalar(
            select(TradingAccount)
            .where(TradingAccount.workspace_id == workspace_id, TradingAccount.id == account_id)
            .with_for_update()
        )
        if account is None:
            raise LookupError("BOT_NOT_FOUND")
        if account.status == "DISABLED":
            return False
        account.status = "DISABLED"
        account.updated_at = datetime.now(UTC)
        await self._session.flush()
        return True

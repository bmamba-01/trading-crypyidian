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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    BotInstance,
    BotRuntimeStateRecord,
    TradingAccount,
)
from trading_bot.adapters.database.repositories.account_activity_incidents import (
    AccountActivityIncidentRepository,
)
from trading_bot.adapters.database.repositories.bot_config_versions import (
    BotConfigVersionRepository,
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


@dataclass(frozen=True, slots=True)
class BotEnableSnapshot:
    account_status: str
    bot_status: str
    mode: str
    venue: str
    workspace_killed: bool
    readiness: str | None
    activity_status: str | None
    applied_bot_revision: int | None
    bot_revision: int
    applied_workspace_revision: int | None
    workspace_revision: int
    open_incident: bool
    published_config: bool


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

    async def enable_snapshot(self, workspace_id: UUID, bot_id: UUID) -> BotEnableSnapshot:
        """Khóa account sau L1 rồi chụp điều kiện enable trong transaction hiện tại."""
        account = await self._session.scalar(
            select(TradingAccount)
            .join(
                BotInstance,
                (BotInstance.workspace_id == TradingAccount.workspace_id)
                & (BotInstance.trading_account_id == TradingAccount.id),
            )
            .where(BotInstance.workspace_id == workspace_id, BotInstance.id == bot_id)
            .with_for_update(of=TradingAccount)
        )
        if account is None:
            raise LookupError("BOT_NOT_FOUND")
        bot = await self._session.scalar(
            select(BotInstance).where(
                BotInstance.workspace_id == workspace_id,
                BotInstance.id == bot_id,
                BotInstance.trading_account_id == account.id,
            )
        )
        if bot is None:
            raise LookupError("BOT_NOT_FOUND")
        controls = BotRuntimeControlRepository(self._session)
        workspace_control = await controls.current_workspace(workspace_id)
        bot_control = await controls.current_bot(workspace_id, bot_id)
        runtime = await self._session.scalar(
            select(BotRuntimeStateRecord).where(
                BotRuntimeStateRecord.workspace_id == workspace_id,
                BotRuntimeStateRecord.bot_id == bot_id,
                BotRuntimeStateRecord.account_id == account.id,
            )
        )
        incidents = await AccountActivityIncidentRepository(self._session).list_open(
            workspace_id=workspace_id, bot_id=bot_id
        )
        head = await BotConfigVersionRepository(self._session).get_head(
            workspace_id=workspace_id, bot_id=bot_id
        )
        return BotEnableSnapshot(
            account_status=account.status,
            bot_status=bot.status,
            mode=account.mode,
            venue=account.venue,
            workspace_killed=workspace_control is not None
            and workspace_control.kill_state is WorkspaceKillState.KILLED,
            readiness=runtime.readiness if runtime is not None else None,
            activity_status=runtime.activity_status if runtime is not None else None,
            applied_bot_revision=(
                runtime.applied_bot_control_revision if runtime is not None else None
            ),
            bot_revision=bot_control.revision if bot_control is not None else 0,
            applied_workspace_revision=(
                runtime.applied_workspace_control_revision if runtime is not None else None
            ),
            workspace_revision=workspace_control.revision if workspace_control is not None else 0,
            open_incident=bool(incidents),
            published_config=head is not None and head.status == "PUBLISHED",
        )

    async def enable_account(self, workspace_id: UUID, account_id: UUID) -> bool:
        """Đổi account DISABLED → ENABLED sau khi application đã khóa và kiểm tra điều kiện."""
        account = await self._session.scalar(
            select(TradingAccount)
            .where(TradingAccount.workspace_id == workspace_id, TradingAccount.id == account_id)
            .with_for_update()
        )
        if account is None:
            raise LookupError("BOT_NOT_FOUND")
        if account.status == "ENABLED":
            return False
        if account.status != "DISABLED":
            raise ValueError("ACCOUNT_STATUS_INVALID")
        await self._session.execute(
            select(func.set_config("app.bot_enable_authorized", "on", True))
        )
        account.status = "ENABLED"
        account.updated_at = datetime.now(UTC)
        await self._session.flush()
        return True

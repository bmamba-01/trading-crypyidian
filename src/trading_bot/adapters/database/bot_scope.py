"""Đặt scope bot transaction-local từ mapping account đã được DB xác thực."""

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.bot_entities import BotInstance


async def bind_account_bot(
    session: AsyncSession, *, workspace_id: UUID, account_id: UUID, allow_switch: bool = False
) -> UUID:
    """Resolve bot theo account; context hiện hữu không được âm thầm đổi account."""
    info = session.info
    if info.get("workspace_id") not in (None, workspace_id):
        raise ValueError("Workspace khác scope của transaction.")
    if info.get("bot_account_id") == account_id and info.get("workspace_id") == workspace_id:
        # Mapping đã xác thực; vẫn đặt lại GUC vì savepoint rollback có thể đã reset nó.
        cached = UUID(str(info["bot_id"]))
        await session.execute(
            text("SELECT set_config('app.bot_id', :bot_id, true)"), {"bot_id": str(cached)}
        )
        return cached
    bot = await session.scalar(
        select(BotInstance).where(
            BotInstance.workspace_id == workspace_id, BotInstance.trading_account_id == account_id
        )
    )
    if bot is None:
        raise ValueError("Account chưa có bot trong workspace hiện tại.")
    if info.get("bot_id") is not None and info["bot_id"] != bot.id and not allow_switch:
        raise ValueError("Account khác scope bot của transaction.")
    await session.execute(
        text("SELECT set_config('app.bot_id', :bot_id, true)"), {"bot_id": str(bot.id)}
    )
    info.update(workspace_id=workspace_id, bot_id=bot.id, bot_account_id=account_id)
    return bot.id

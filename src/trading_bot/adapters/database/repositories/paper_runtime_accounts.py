"""Liệt kê PAPER account chỉ trong tenant do runtime deployment cấp."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.models.entities import PaperAccountSeed, TradingAccount


class PaperRuntimeAccountRepository:
    """Truy vấn account PAPER đã bật trong workspace đã scope bởi UnitOfWork."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_enabled(self, *, workspace_id: UUID) -> list[TradingAccount]:
        """Chỉ liệt kê PAPER ENABLED có workspace bằng deployment identity."""
        records = await self._session.scalars(
            select(TradingAccount)
            .join(
                BotInstance,
                (BotInstance.workspace_id == TradingAccount.workspace_id)
                & (BotInstance.trading_account_id == TradingAccount.id),
            )
            .where(
                TradingAccount.workspace_id == workspace_id,
                TradingAccount.mode == "PAPER",
                TradingAccount.status == "ENABLED",
                BotInstance.status == "ENABLED",
            )
            .order_by(TradingAccount.id)
        )
        return list(records)

    async def load_seed(self, *, workspace_id: UUID, account_id: UUID) -> PaperAccountSeed | None:
        """Đọc seed theo khóa kép, không cho UUID account mở rộng tenant scope."""
        return await self._session.scalar(
            select(PaperAccountSeed).where(
                PaperAccountSeed.workspace_id == workspace_id,
                PaperAccountSeed.trading_account_id == account_id,
            )
        )

"""Truy vấn snapshot số dư PAPER đã hiện thực từ ledger."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.entities import PaperAccountSeed, TradingAccount
from trading_bot.adapters.database.models.paper_entities import TradeRecord


class PaperAccountStateRepository:
    """Đọc account, seed và trade trong phạm vi workspace/account rõ ràng."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(
        self, *, workspace_id: UUID, account_id: UUID
    ) -> tuple[TradingAccount | None, PaperAccountSeed | None, list[TradeRecord]]:
        """Đọc dữ liệu đầu vào; caller kiểm tra trạng thái và quy tắc equity."""
        account = await self._session.scalar(
            select(TradingAccount).where(
                TradingAccount.workspace_id == workspace_id,
                TradingAccount.id == account_id,
            )
        )
        if account is None:
            return None, None, []

        seed = await self._session.scalar(
            select(PaperAccountSeed).where(
                PaperAccountSeed.workspace_id == workspace_id,
                PaperAccountSeed.trading_account_id == account_id,
            )
        )
        result = await self._session.scalars(
            select(TradeRecord)
            .where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.account_id == account_id,
            )
            .order_by(TradeRecord.closed_at.asc().nullsfirst(), TradeRecord.id.asc())
        )
        return account, seed, list(result.all())

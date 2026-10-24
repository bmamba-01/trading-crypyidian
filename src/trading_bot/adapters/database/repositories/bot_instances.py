"""Ánh xạ bot/account tường minh, không lookup bằng UUID thiếu workspace."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import BotInstance, TradingAccount


class BotInstanceRepository:
    """Tạo identity trong transaction do caller quản lý."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_account(self, *, workspace_id: UUID, account_id: UUID) -> BotInstance | None:
        return await self._session.scalar(
            select(BotInstance).where(
                BotInstance.workspace_id == workspace_id,
                BotInstance.trading_account_id == account_id,
            )
        )

    async def create_for_account(self, *, account: TradingAccount) -> BotInstance:
        """Khóa account rồi resolve identity do trigger DB tạo cùng transaction INSERT account.

        Role ứng dụng không có quyền INSERT bot_instance (ruling 11), nên thiếu mapping là
        lỗi toàn vẹn cần điều tra, không tự tạo bot ở tầng ứng dụng.
        """
        locked = await self._session.scalar(
            select(TradingAccount)
            .where(
                TradingAccount.workspace_id == account.workspace_id, TradingAccount.id == account.id
            )
            .with_for_update()
        )
        if locked is None or locked.mode != "PAPER" or locked.status not in {"ENABLED", "DISABLED"}:
            raise ValueError("Account phải là PAPER hợp lệ trong workspace hiện tại.")
        existing = await self.get_for_account(
            workspace_id=account.workspace_id, account_id=account.id
        )
        if existing is None:
            raise ValueError("Account thiếu bot do trigger provision; kiểm tra migration MB-01.")
        return existing

    async def list_enabled_for_runtime(self, *, workspace_id: UUID) -> list[BotInstance]:
        records = await self._session.scalars(
            select(BotInstance)
            .join(
                TradingAccount,
                (TradingAccount.workspace_id == BotInstance.workspace_id)
                & (TradingAccount.id == BotInstance.trading_account_id),
            )
            .where(
                BotInstance.workspace_id == workspace_id,
                BotInstance.status == "ENABLED",
                TradingAccount.mode == "PAPER",
                TradingAccount.status == "ENABLED",
            )
            .order_by(BotInstance.id)
        )
        return list(records)

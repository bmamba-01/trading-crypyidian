from uuid import UUID

from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import TradingAccount, Workspace
from trading_bot.adapters.database.repositories.account_equity import AccountEquityRepository
from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
    BacktestDatasetManifestRepository,
)
from trading_bot.adapters.database.repositories.dataset_manifest import DatasetManifestRepository
from trading_bot.adapters.database.repositories.risk_conversion import RiskConversionRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.adapters.database.repositories.strategy_card_repository import (
    StrategyCardConflict,
    StrategyCardNotFoundError,
    StrategyCardRepository,
)

__all__ = [
    "AccountEquityRepository",
    "BacktestDatasetManifestRepository",
    "DatasetManifestRepository",
    "RiskConversionRepository",
    "SourceRegistryRepository",
    "StrategyCardConflict",
    "StrategyCardNotFoundError",
    "StrategyCardRepository",
    "WorkspaceRepository",
]


class WorkspaceRepository:
    """Truy vấn repository luôn giới hạn dữ liệu theo workspace được gọi."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def trading_accounts_statement(workspace_id: UUID) -> Select[TradingAccount]:
        return select(TradingAccount).where(TradingAccount.workspace_id == workspace_id)

    async def get_workspace(self, workspace_id: UUID) -> Workspace | None:
        statement = select(Workspace).where(Workspace.id == workspace_id)
        return await self._session.scalar(statement)

    async def list_trading_accounts(self, workspace_id: UUID) -> list[TradingAccount]:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        result = await self._session.execute(self.trading_accounts_statement(workspace_id))
        return list(result.scalars().all())

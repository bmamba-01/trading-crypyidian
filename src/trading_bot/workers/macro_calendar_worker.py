"""Store PostgreSQL cho collector lịch vĩ mô (AI-4) chạy trong process ``ingestion-worker``."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import MacroCalendarFetchRecord
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.source_use_gate import SourceUseGate


class _MacroStore:
    def __init__(self, session: AsyncSession, workspace_id: UUID) -> None:
        self._session = session
        self._workspace_id = workspace_id

    def gate(self, source_id: str) -> SourceUseGate:
        return SourceUseGate(
            workspace_id=self._workspace_id,
            source_id=source_id,
            repository=SourceRegistryRepository(self._session),
        )

    async def last_fetched_at(self, source_id: str) -> datetime | None:
        return await self._session.scalar(
            select(func.max(MacroCalendarFetchRecord.fetched_at)).where(
                MacroCalendarFetchRecord.workspace_id == self._workspace_id,
                MacroCalendarFetchRecord.source_id == source_id,
            )
        )

    async def insert(self, record: dict[str, Any]) -> None:
        self._session.add(MacroCalendarFetchRecord(**record))
        await self._session.flush()


def database_macro_store_scope(
    session_factory: Callable[[], AsyncSession], workspace_id: UUID
) -> Callable[[], Any]:
    """Mỗi scope một transaction gắn ``app.workspace_id`` (RLS)."""

    @asynccontextmanager
    async def scope() -> AsyncIterator[_MacroStore]:
        async with session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            yield _MacroStore(session, workspace_id)

    return scope


__all__ = ["database_macro_store_scope"]

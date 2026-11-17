"""Store PostgreSQL cho collector exchangeInfo (AI-4 lát 3) trong ``ingestion-worker``."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import ListingSnapshotRecord
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.source_use_gate import SourceUseGate


class _ListingsStore:
    def __init__(self, session: AsyncSession, workspace_id: UUID) -> None:
        self._session = session
        self._workspace_id = workspace_id

    def gate(self, source_id: str) -> SourceUseGate:
        return SourceUseGate(
            workspace_id=self._workspace_id,
            source_id=source_id,
            repository=SourceRegistryRepository(self._session),
        )

    async def last_check(self) -> tuple[datetime, str] | None:
        row = (
            await self._session.execute(
                select(ListingSnapshotRecord.fetched_at, ListingSnapshotRecord.entries_sha256)
                .where(ListingSnapshotRecord.workspace_id == self._workspace_id)
                .order_by(ListingSnapshotRecord.fetched_at.desc())
                .limit(1)
            )
        ).first()
        return (row[0], row[1]) if row else None

    async def insert(self, record: dict[str, Any]) -> None:
        self._session.add(ListingSnapshotRecord(**record))
        await self._session.flush()


def database_listings_store_scope(
    session_factory: Callable[[], AsyncSession], workspace_id: UUID
) -> Callable[[], Any]:
    """Mỗi scope một transaction gắn ``app.workspace_id`` (RLS)."""

    @asynccontextmanager
    async def scope() -> AsyncIterator[_ListingsStore]:
        async with session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            yield _ListingsStore(session, workspace_id)

    return scope


__all__ = ["database_listings_store_scope"]

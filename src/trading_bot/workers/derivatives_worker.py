"""Store PostgreSQL cho collector phái sinh Binance (AI-4 lát 2) trong ``ingestion-worker``."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import DerivativesSnapshotRecord
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.source_use_gate import SourceUseGate


class _DerivativesStore:
    def __init__(self, session: AsyncSession, workspace_id: UUID) -> None:
        self._session = session
        self._workspace_id = workspace_id

    def gate(self, source_id: str) -> SourceUseGate:
        return SourceUseGate(
            workspace_id=self._workspace_id,
            source_id=source_id,
            repository=SourceRegistryRepository(self._session),
        )

    async def last_collected_at(self) -> datetime | None:
        return await self._session.scalar(
            select(func.max(DerivativesSnapshotRecord.collected_at)).where(
                DerivativesSnapshotRecord.workspace_id == self._workspace_id
            )
        )

    async def insert(self, record: dict[str, Any]) -> None:
        stmt = (
            pg_insert(DerivativesSnapshotRecord)
            .values(id=record.get("id") or uuid4(), **record)
            .on_conflict_do_nothing(constraint="uq_derivatives_snapshot_point")
        )
        await self._session.execute(stmt)
        await self._session.flush()


def database_derivatives_store_scope(
    session_factory: Callable[[], AsyncSession], workspace_id: UUID
) -> Callable[[], Any]:
    """Mỗi scope một transaction gắn ``app.workspace_id`` (RLS)."""

    @asynccontextmanager
    async def scope() -> AsyncIterator[_DerivativesStore]:
        async with session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            yield _DerivativesStore(session, workspace_id)

    return scope


__all__ = ["database_derivatives_store_scope"]

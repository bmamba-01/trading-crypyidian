"""Worker SIG-0 chạy ``MarketBarCollector`` định kỳ trong process ``ingestion-worker``."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.binance_rest_backfill_rights import SourceRegistryBackfillRightsGate
from trading_bot.application.market_bar_collector import CollectorTick, MarketBarCollector

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CollectorSession:
    market: MarketDataRepository
    rights_gate: SourceRegistryBackfillRightsGate


def database_collector_scope(
    session_factory: Callable[[], AsyncSession], workspace_id: UUID
) -> Callable[[], AsyncIterator[_CollectorSession]]:
    """Mỗi luồng một transaction gắn ``app.workspace_id`` (RLS) với repository thật."""

    @asynccontextmanager
    async def scope() -> AsyncIterator[_CollectorSession]:
        async with session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            yield _CollectorSession(
                market=MarketDataRepository(session),
                rights_gate=SourceRegistryBackfillRightsGate(
                    workspace_id=workspace_id,
                    repository=SourceRegistryRepository(session),
                ),
            )

    return scope  # type: ignore[return-value]


class MarketBarCollectorWorker:
    """Lặp ``collect_once`` theo ``tick_seconds`` tới khi shutdown; giữ tick gần nhất cho health."""

    def __init__(
        self,
        *,
        collector: MarketBarCollector,
        tick_seconds: float = 30.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not 5 <= tick_seconds <= 600:
            raise ValueError("tick_seconds collector phải trong 5..600.")
        self._collector = collector
        self._tick_seconds = float(tick_seconds)
        self._clock = clock
        self.last_tick: CollectorTick | None = None
        self.last_tick_at: datetime | None = None

    async def run_once(self) -> CollectorTick:
        tick = await self._collector.collect_once(self._clock())
        self.last_tick = tick
        self.last_tick_at = self._clock()
        collected = sum(result.bars for result in tick.results)
        denied = sum(result.status == "rights_denied" for result in tick.results)
        failed = sum(result.status == "error" for result in tick.results)
        if collected or denied or failed:
            logger.info(
                "market collector: %d nến mới, %d luồng thiếu quyền, %d luồng lỗi",
                collected,
                denied,
                failed,
            )
        return tick

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - giữ worker sống, thử lại tick sau
                logger.warning("market collector tick thất bại: %s", type(error).__name__)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=self._tick_seconds)
            except TimeoutError:
                continue

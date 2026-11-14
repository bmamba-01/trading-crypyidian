"""Lắp worker ingest live với PostgreSQL, REST công khai và stream `/market` của Binance.

Composition này không nhận API key và không có cổng thực thi lệnh.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

import httpx2
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.binance_usdm.market_data import BinanceUsdMMarketDataAdapter
from trading_bot.adapters.binance_usdm.market_stream import BinanceUsdMKlineStream
from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.binance_rest_backfill import BackfillRightsGate
from trading_bot.application.binance_rest_backfill_rights import SourceRegistryBackfillRightsGate
from trading_bot.application.live_kline_ingestion import (
    LiveKlineIngestionService,
    LiveKlineRepository,
    LiveKlineSubscription,
    LiveKlineTransaction,
    TransactionFactory,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.workers.live_market_ingestion_worker import LiveMarketIngestionWorker


@dataclass(frozen=True, slots=True)
class _Transaction:
    market: LiveKlineRepository
    rights: BackfillRightsGate


class LiveMarketIngestionRuntime:
    """Giữ worker cùng các client mạng để lifespan đóng đúng thứ tự."""

    def __init__(
        self,
        *,
        worker: LiveMarketIngestionWorker,
        market_data: BinanceUsdMMarketDataAdapter,
        stream_client: httpx2.AsyncClient,
    ) -> None:
        self.worker = worker
        self._market_data = market_data
        self._stream_client = stream_client

    async def aclose(self) -> None:
        await self._stream_client.aclose()
        await self._market_data.aclose()


def workspace_transactions(
    sessions: async_sessionmaker[AsyncSession], *, workspace_id: UUID
) -> TransactionFactory:
    """Tạo factory transaction: kho market và rights gate dùng chung một Unit of Work."""

    @asynccontextmanager
    async def transactions() -> AsyncIterator[LiveKlineTransaction]:
        async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
            yield _Transaction(
                market=MarketDataRepository(uow.session),
                rights=SourceRegistryBackfillRightsGate(
                    workspace_id=workspace_id,
                    repository=SourceRegistryRepository(uow.session),
                ),
            )

    return transactions


def build_live_market_ingestion(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: UUID,
    subscriptions: Sequence[tuple[str, str]],
    warmup_bars: int,
) -> LiveMarketIngestionRuntime:
    """Dựng runtime mặc định cho ingestion-worker; chỉ gọi khi live ingest được bật."""
    typed = tuple(LiveKlineSubscription(symbol, interval) for symbol, interval in subscriptions)
    market_data = BinanceUsdMMarketDataAdapter()
    stream_client = httpx2.AsyncClient(
        timeout=httpx2.Timeout(10.0, connect=5.0),
        follow_redirects=False,
        trust_env=False,
        auth=None,
        headers={"User-Agent": "Crypidian/0.1"},
    )

    service = LiveKlineIngestionService(
        workspace_id=workspace_id,
        market_data=market_data,
        transactions=workspace_transactions(session_factory, workspace_id=workspace_id),
        warmup_bars=warmup_bars,
    )
    worker = LiveMarketIngestionWorker(
        service=service,
        stream_factory=lambda: BinanceUsdMKlineStream(
            subscriptions=tuple(subscriptions),
            client=stream_client,
        ),
        subscriptions=typed,
    )
    return LiveMarketIngestionRuntime(
        worker=worker, market_data=market_data, stream_client=stream_client
    )

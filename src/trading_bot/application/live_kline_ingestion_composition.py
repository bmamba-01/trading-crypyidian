"""Lắp worker ingest live với PostgreSQL, REST công khai và stream `/market` của Binance.

Composition này không nhận API key và không có cổng thực thi lệnh.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

import httpx2
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.binance_usdm.market_data import BinanceUsdMMarketDataAdapter
from trading_bot.adapters.binance_usdm.market_stream import (
    BinanceUsdMEventStream,
    BinanceUsdMKlineStream,
)
from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.binance_rest_backfill_rights import SourceRegistryBackfillRightsGate
from trading_bot.application.live_kline_ingestion import (
    LiveKlineIngestionService,
    LiveKlineSubscription,
)
from trading_bot.application.live_market_events import LiveMarketEventService
from trading_bot.application.outbox import UnitOfWork
from trading_bot.workers.live_market_ingestion_worker import (
    LiveMarketEventWorker,
    LiveMarketIngestionWorker,
    LiveQuoteWorker,
)

# Loại stream đi route /public (kết nối riêng), phần còn lại dùng route /market.
QUOTE_EVENT_KIND = "book_ticker"


@dataclass(frozen=True, slots=True)
class _Transaction:
    market: MarketDataRepository
    rights: SourceRegistryBackfillRightsGate


class LiveMarketIngestionRuntime:
    """Giữ các worker cùng client mạng để lifespan đóng đúng thứ tự."""

    def __init__(
        self,
        *,
        worker: LiveMarketIngestionWorker,
        market_data: BinanceUsdMMarketDataAdapter,
        stream_client: httpx2.AsyncClient,
        event_worker: LiveMarketEventWorker | None = None,
        quote_worker: LiveQuoteWorker | None = None,
    ) -> None:
        self.worker = worker
        self.event_worker = event_worker
        self.quote_worker = quote_worker
        self._market_data = market_data
        self._stream_client = stream_client

    async def aclose(self) -> None:
        await self._stream_client.aclose()
        await self._market_data.aclose()


def workspace_transactions(
    sessions: async_sessionmaker[AsyncSession], *, workspace_id: UUID
) -> Callable[[], AbstractAsyncContextManager[_Transaction]]:
    """Tạo factory transaction: kho market và rights gate dùng chung một Unit of Work."""

    @asynccontextmanager
    async def transactions() -> AsyncIterator[_Transaction]:
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
    events: Sequence[str] = (),
    mark_price_interval_seconds: int = 60,
    quote_interval_seconds: float = 2.0,
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
    transactions = workspace_transactions(session_factory, workspace_id=workspace_id)
    service = LiveKlineIngestionService(
        workspace_id=workspace_id,
        market_data=market_data,
        transactions=transactions,
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
    event_worker: LiveMarketEventWorker | None = None
    quote_worker: LiveQuoteWorker | None = None
    symbols = tuple(sorted({symbol for symbol, _ in subscriptions}))
    if QUOTE_EVENT_KIND in events:
        quote_worker = LiveQuoteWorker(
            service=LiveMarketEventService(workspace_id=workspace_id, transactions=transactions),
            stream_factory=lambda: BinanceUsdMEventStream(
                symbols=symbols, kinds=(QUOTE_EVENT_KIND,), client=stream_client
            ),
            symbols=symbols,
            interval_seconds=quote_interval_seconds,
        )
    kinds = tuple(kind for kind in events if kind != QUOTE_EVENT_KIND)
    if kinds:
        event_worker = LiveMarketEventWorker(
            service=LiveMarketEventService(workspace_id=workspace_id, transactions=transactions),
            stream_factory=lambda: BinanceUsdMEventStream(
                symbols=symbols, kinds=kinds, client=stream_client
            ),
            symbols=symbols,
            mark_price_interval_seconds=mark_price_interval_seconds,
        )
    return LiveMarketIngestionRuntime(
        worker=worker,
        market_data=market_data,
        stream_client=stream_client,
        event_worker=event_worker,
        quote_worker=quote_worker,
    )

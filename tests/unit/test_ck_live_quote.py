"""CK-1: giá mua/bán tốt nhất (bookTicker, route `/public`) → bảng giá trực tiếp."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx2
import pytest

from trading_bot.adapters.binance_usdm.market_data import MarketDataContractError
from trading_bot.adapters.binance_usdm.market_stream import (
    BinanceUsdMEventStream,
    build_event_stream_names,
    parse_market_event_message,
)
from trading_bot.application.live_market_events import LiveMarketEventService
from trading_bot.config.settings import AppSettings
from trading_bot.domain.market.stream_events import BookTickerEvent
from trading_bot.workers.live_market_ingestion_worker import LiveQuoteWorker

NOW = datetime(2026, 11, 15, 12, 0, 1, tzinfo=UTC)
WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
INSTRUMENT = UUID("c3b4a5d6-e7f8-4a9b-9c1d-2e3f4a5b6c7d")
MIGRATION = (
    Path(__file__).parents[2] / "migrations" / "versions" / "a7c9e1b3d5f6_ck1_market_live_quote.py"
)


def book_message(
    *, bid: str = "96000.10", ask: str = "96000.20", symbol: str = "BTCUSDT", u: int = 7
) -> str:
    return json.dumps(
        {
            "stream": f"{symbol.lower()}@bookTicker",
            "data": {
                "e": "bookTicker",
                "u": u,
                "E": 1794744000500,
                "T": 1794744000499,
                "s": symbol,
                "b": bid,
                "B": "1.250",
                "a": ask,
                "A": "0.875",
            },
        }
    )


def test_book_ticker_stream_names_and_public_route() -> None:
    assert build_event_stream_names(("BTCUSDT", "ETHUSDT"), ("book_ticker",)) == (
        "btcusdt@bookTicker",
        "ethusdt@bookTicker",
    )
    stream = BinanceUsdMEventStream(
        symbols=("BTCUSDT",), kinds=("book_ticker",), client=httpx2.AsyncClient()
    )
    # bookTicker chỉ có trên route /public (thông báo đổi WebSocket của Binance, 2026-03-05).
    assert stream.url == "wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker"
    market = BinanceUsdMEventStream(
        symbols=("BTCUSDT",), kinds=("mark_price",), client=httpx2.AsyncClient()
    )
    assert market.url.startswith("wss://fstream.binance.com/market/stream?")
    with pytest.raises(ValueError, match="route"):
        BinanceUsdMEventStream(
            symbols=("BTCUSDT",), kinds=("book_ticker", "mark_price"), client=httpx2.AsyncClient()
        )


def test_parse_book_ticker_keeps_decimals_and_causal_times() -> None:
    event = parse_market_event_message(book_message(), received_at=NOW)
    assert isinstance(event, BookTickerEvent)
    assert event.symbol == "BTCUSDT"
    assert (event.bid_price, event.ask_price) == (Decimal("96000.10"), Decimal("96000.20"))
    assert (event.bid_quantity, event.ask_quantity) == (Decimal("1.250"), Decimal("0.875"))
    assert event.update_id == 7
    assert event.spread == Decimal("0.10")
    assert event.mid == Decimal("96000.15")
    assert event.event_at <= event.received_at == NOW


@pytest.mark.parametrize(
    "message",
    [
        book_message(bid="96000.30", ask="96000.20"),  # sổ lệnh bị chéo
        book_message(bid="0", ask="1"),
        book_message(u=-1),
        book_message(symbol="ETHUSDT").replace("ethusdt@bookTicker", "btcusdt@bookTicker"),
    ],
)
def test_parse_book_ticker_rejects_broken_payloads(message: str) -> None:
    with pytest.raises(MarketDataContractError):
        parse_market_event_message(message, received_at=NOW)


class QuoteRepository:
    def __init__(self) -> None:
        self.upserts: list[tuple[UUID, UUID, str, BookTickerEvent]] = []

    async def get_instrument(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(id=INSTRUMENT)

    async def upsert_live_quote(
        self, *, workspace_id: UUID, instrument_id: UUID, source: str, event: BookTickerEvent
    ) -> bool:
        self.upserts.append((workspace_id, instrument_id, source, event))
        return True


class Rights:
    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow
        self.calls = 0

    async def authorize(self, **_kwargs: Any) -> None:
        self.calls += 1
        if not self.allow:
            raise PermissionError("denied")


def _service(repository: QuoteRepository, rights: Rights) -> LiveMarketEventService:
    @asynccontextmanager
    async def transactions() -> AsyncGenerator[Any]:
        yield SimpleNamespace(market=repository, rights=rights)

    return LiveMarketEventService(workspace_id=WORKSPACE, transactions=transactions)


@pytest.mark.asyncio
async def test_service_upserts_latest_quote_per_symbol_after_rights_check() -> None:
    repository, rights = QuoteRepository(), Rights()
    first = parse_market_event_message(book_message(u=1), received_at=NOW)
    second = parse_market_event_message(
        book_message(u=2, bid="96001.00", ask="96001.10"), received_at=NOW
    )

    written = await _service(repository, rights).upsert_quotes([first, second], now=NOW)

    assert written == 1
    assert rights.calls == 1
    (row,) = repository.upserts
    assert row[:3] == (WORKSPACE, INSTRUMENT, "BINANCE_USDM")
    assert row[3].update_id == 2


@pytest.mark.asyncio
async def test_service_refuses_quotes_without_rights() -> None:
    repository = QuoteRepository()
    event = parse_market_event_message(book_message(), received_at=NOW)
    with pytest.raises(PermissionError):
        await _service(repository, Rights(allow=False)).upsert_quotes([event], now=NOW)
    assert repository.upserts == []


class FakeQuoteStream:
    def __init__(self, events: list[BookTickerEvent]) -> None:
        self._events = events

    async def events(self) -> AsyncGenerator[BookTickerEvent]:
        for event in self._events:
            yield event
        raise ConnectionError("đóng")


class RecordingService:
    def __init__(self) -> None:
        self.batches: list[list[BookTickerEvent]] = []
        self.ready: list[tuple[str, ...]] = []

    async def ensure_ready(self, symbols: Any, *, now: datetime) -> None:
        self.ready.append(tuple(symbols))

    async def upsert_quotes(self, events: Any, *, now: datetime) -> int:
        self.batches.append(list(events))
        return len({event.symbol for event in events})


@pytest.mark.asyncio
async def test_quote_worker_throttles_writes_and_flushes_on_disconnect() -> None:
    ticks = iter([0.0, 0.1, 0.2, 2.5, 2.6, 2.7, 2.8])
    events = [
        parse_market_event_message(book_message(u=index), received_at=NOW) for index in range(1, 5)
    ]
    service = RecordingService()
    worker = LiveQuoteWorker(
        service=service,  # type: ignore[arg-type]
        stream_factory=lambda: FakeQuoteStream(events),
        symbols=("BTCUSDT",),
        now=lambda: NOW,
        interval_seconds=2.0,
        monotonic=lambda: next(ticks),
    )
    with pytest.raises(ConnectionError):
        await worker.run_session()

    assert service.ready == [("BTCUSDT",)]
    # Chỉ ghi giá mới nhất khi đủ nhịp 2 giây, và ghi nốt giá cuối khi kết nối rớt.
    assert [[event.update_id for event in batch] for batch in service.batches] == [[3], [4]]
    assert worker.quotes_written == 2


def test_settings_accept_book_ticker_and_bound_quote_interval() -> None:
    settings = AppSettings(
        app_env="test",
        process_role="ingestion-worker",
        ingestion_workspace_id=WORKSPACE,
        ingestion_live_symbols="BTCUSDT",
        ingestion_live_events="book_ticker,mark_price",
        ingestion_live_quote_interval_seconds=2,
    )
    assert settings.ingestion_live_event_kinds == ("mark_price", "book_ticker")
    with pytest.raises(ValueError, match="ingestion_live_quote_interval_seconds"):
        AppSettings(app_env="test", ingestion_live_quote_interval_seconds=0)


def test_migration_creates_rls_quote_table_and_read_grants() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "f1b3d5e7a9c2"' in source
    assert "market_live_quote" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    writer = "GRANT SELECT, INSERT, UPDATE ON TABLE market_live_quote TO crypidian_ingestion_worker"
    assert writer in source
    assert "GRANT SELECT ON TABLE market_live_quote TO crypidian_admin_web" in source
    assert "GRANT SELECT ON TABLE market_mark_price TO crypidian_admin_web" in source
    assert "REVOKE SELECT ON TABLE market_mark_price FROM crypidian_admin_web" in source


def test_quote_freshness_window_is_part_of_contract() -> None:
    event = parse_market_event_message(book_message(), received_at=NOW)
    assert NOW - event.event_at < timedelta(seconds=5)

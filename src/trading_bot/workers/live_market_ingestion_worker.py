"""Worker thu nến đóng thời gian thực: lấp bằng REST, nối stream, tự nối lại khi rớt.

Worker chỉ đọc market data công khai và ghi kho market; không có cổng thực thi lệnh.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from trading_bot.application.live_kline_ingestion import (
    CatchUpResult,
    LiveBarDisposition,
    LiveKlineSubscription,
)
from trading_bot.application.live_market_events import LiveMarketRecord
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.market.stream_events import (
    AggTradeEvent,
    BookTickerEvent,
    LiquidationEvent,
    MarketStreamEvent,
    MarkPriceEvent,
)
from trading_bot.domain.market.trade_flow import TradeFlowAggregator

logger = logging.getLogger(__name__)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class LiveKlineService(Protocol):
    """Phần service ingest mà worker cần."""

    async def catch_up(
        self, subscription: LiveKlineSubscription, *, now: datetime
    ) -> CatchUpResult: ...

    async def ingest_closed_bar(self, bar: OHLCVBar, *, now: datetime) -> LiveBarDisposition: ...


class KlineStream(Protocol):
    """Một phiên stream; kết thúc bằng exception khi kết nối rớt."""

    def bars(self) -> AsyncGenerator[OHLCVBar]: ...


class LiveMarketIngestionWorker:
    """Giữ kho market bám sát nến đóng mới nhất cho các subscription đã cấu hình."""

    def __init__(
        self,
        *,
        service: LiveKlineService,
        stream_factory: Callable[[], KlineStream],
        subscriptions: Sequence[LiveKlineSubscription],
        now: Callable[[], datetime] | None = None,
        reconnect_seconds: float = 5.0,
        catch_up_min_interval_seconds: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not subscriptions or len(set(subscriptions)) != len(subscriptions):
            raise ValueError("subscriptions phải khác rỗng và không trùng lặp.")
        if not 0 < reconnect_seconds <= 300:
            raise ValueError("reconnect_seconds phải nằm trong (0, 300].")
        if not 0 <= catch_up_min_interval_seconds <= 300:
            raise ValueError("catch_up_min_interval_seconds phải nằm trong [0, 300].")
        self._service = service
        self._stream_factory = stream_factory
        self._subscriptions = tuple(subscriptions)
        self._now = now or (lambda: datetime.now(UTC))
        self._reconnect_seconds = reconnect_seconds
        self._catch_up_min_interval = catch_up_min_interval_seconds
        self._monotonic = monotonic
        self._expected: dict[LiveKlineSubscription, datetime] = {}
        self._last_gap_catch_up: dict[LiveKlineSubscription, float] = {}
        self.is_healthy = False
        self.last_error_code: str | None = None
        self.last_bar_at: datetime | None = None
        self.bars_appended = 0

    async def run_session(self) -> None:
        """Lấp gap cho mọi subscription rồi đọc stream tới khi kết nối kết thúc."""
        self._last_gap_catch_up.clear()
        for subscription in self._subscriptions:
            await self._catch_up(subscription)
        self.is_healthy = True
        self.last_error_code = None
        async with contextlib.aclosing(self._stream_factory().bars()) as bars:
            async for bar in bars:
                await self._handle(bar)

    async def run(self, stopping: asyncio.Event) -> None:
        """Chạy tới khi lifespan dừng; mọi lỗi chỉ ghi mã an toàn rồi nối lại."""
        await _run_reconnecting(self, stopping, name="live-kline-session")

    async def _handle(self, bar: OHLCVBar) -> None:
        subscription = LiveKlineSubscription(bar.symbol, bar.timeframe)
        expected = self._expected.get(subscription)
        if expected is None:
            raise RuntimeError("Stream trả nến ngoài subscription của worker.")
        if bar.open_time < expected:
            return
        if bar.open_time > expected:
            # Đã lỡ sự kiện đóng của nến `expected`; chỉ REST mới lấy lại được.
            previous = self._last_gap_catch_up.get(subscription)
            current = self._monotonic()
            if previous is not None and current - previous < self._catch_up_min_interval:
                return
            self._last_gap_catch_up[subscription] = current
            expected = await self._catch_up(subscription)
        if not bar.is_closed or bar.open_time != expected:
            return
        disposition = await self._service.ingest_closed_bar(bar, now=self._now())
        if disposition is LiveBarDisposition.APPENDED:
            self._expected[subscription] = expected + subscription.duration
            self.bars_appended += 1
            self.last_bar_at = bar.open_time
        else:
            # Cursor trong DB lệch với trạng thái nhớ trong tiến trình; đồng bộ lại từ DB.
            await self._catch_up(subscription)

    async def _catch_up(self, subscription: LiveKlineSubscription) -> datetime:
        result = await self._service.catch_up(subscription, now=self._now())
        self._expected[subscription] = result.covered_until
        self.bars_appended += result.bars_appended
        return result.covered_until


class LiveMarketEventService(Protocol):
    """Phần service ghi sự kiện mà worker cần."""

    async def ensure_ready(self, symbols: Sequence[str], *, now: datetime) -> None: ...

    async def write(self, events: Sequence[LiveMarketRecord], *, now: datetime) -> int: ...


class MarketEventStream(Protocol):
    """Một phiên stream sự kiện; kết thúc bằng exception khi kết nối rớt."""

    def events(self) -> AsyncGenerator[MarketStreamEvent]: ...


class LiveMarketEventWorker:
    """Thu mark price (lấy mẫu), mọi lệnh thanh lý và dòng lệnh taker theo bucket."""

    def __init__(
        self,
        *,
        service: LiveMarketEventService,
        stream_factory: Callable[[], MarketEventStream],
        symbols: Sequence[str],
        now: Callable[[], datetime] | None = None,
        mark_price_interval_seconds: int = 60,
        flush_seconds: float = 1.0,
        reconnect_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("symbols phải khác rỗng và không trùng lặp.")
        if (
            type(mark_price_interval_seconds) is not int
            or not 1 <= mark_price_interval_seconds <= 3600
        ):
            raise ValueError("mark_price_interval_seconds phải nằm trong 1..3600.")
        if not 0 <= flush_seconds <= 60 or not 0 < reconnect_seconds <= 300:
            raise ValueError("flush_seconds hoặc reconnect_seconds không hợp lệ.")
        self._service = service
        self._stream_factory = stream_factory
        self._symbols = tuple(symbols)
        self._now = now or (lambda: datetime.now(UTC))
        self._mark_interval = timedelta(seconds=mark_price_interval_seconds)
        self._flush_seconds = flush_seconds
        self._reconnect_seconds = reconnect_seconds
        self._monotonic = monotonic
        self.is_healthy = False
        self.last_error_code: str | None = None
        self.last_event_at: datetime | None = None
        self.events_written = 0

    async def run_session(self) -> None:
        """Kiểm tra quyền/instrument, rồi đọc stream và ghi theo lô tới khi kết nối rớt."""
        await self._service.ensure_ready(self._symbols, now=self._now())
        self.is_healthy = True
        self.last_error_code = None
        aggregator = TradeFlowAggregator()
        sampled: dict[str, datetime] = {}
        buffer: list[LiveMarketRecord] = []
        last_write = self._monotonic()
        try:
            async with contextlib.aclosing(self._stream_factory().events()) as events:
                async for event in events:
                    if isinstance(event, AggTradeEvent):
                        buffer.extend(aggregator.add(event))
                    elif isinstance(event, MarkPriceEvent):
                        slot = _EPOCH + self._mark_interval * (
                            (event.event_at - _EPOCH) // self._mark_interval
                        )
                        if sampled.get(event.symbol) != slot:
                            sampled[event.symbol] = slot
                            buffer.append(event)
                        # Mark price tới đều mỗi giây nên dùng làm nhịp đóng bucket yên lặng.
                        buffer.extend(aggregator.flush(self._now()))
                    elif isinstance(event, LiquidationEvent):
                        buffer.append(event)
                    # BookTickerEvent đi đường LiveQuoteWorker riêng (route /public), bỏ qua ở đây.
                    if buffer and self._monotonic() - last_write >= self._flush_seconds:
                        await self._write(buffer)
                        last_write = self._monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Bucket đang mở bị cắt ngang: ghi lại dưới dạng một phần, không coi là đầy đủ.
            buffer.extend(aggregator.reset(self._now()))
            with contextlib.suppress(Exception):
                await self._write(buffer)
            raise

    async def run(self, stopping: asyncio.Event) -> None:
        """Chạy tới khi lifespan dừng; mọi lỗi chỉ ghi mã an toàn rồi nối lại."""
        await _run_reconnecting(self, stopping, name="live-market-event-session")

    async def _write(self, buffer: list[LiveMarketRecord]) -> None:
        batch = list(buffer)
        buffer.clear()
        self.events_written += await self._service.write(batch, now=self._now())
        self.last_event_at = self._now()


class LiveQuoteService(Protocol):
    """Ghi giá mua/bán mới nhất; trả số symbol đã cập nhật."""

    async def ensure_ready(self, symbols: Sequence[str], *, now: datetime) -> None: ...

    async def upsert_quotes(self, events: Sequence[BookTickerEvent], *, now: datetime) -> int: ...


class LiveQuoteWorker:
    """Đọc bookTicker (route `/public`), giữ giá mới nhất mỗi symbol và ghi theo nhịp.

    bookTicker cập nhật theo thời gian thực (rất dày với mã thanh khoản cao); worker chỉ ghi
    bản mới nhất mỗi ``interval_seconds`` để bảng giá trực tiếp không tạo tải ghi lớn.
    """

    def __init__(
        self,
        *,
        service: LiveQuoteService,
        stream_factory: Callable[[], MarketEventStream],
        symbols: Sequence[str],
        now: Callable[[], datetime] | None = None,
        interval_seconds: float = 2.0,
        reconnect_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("symbols phải khác rỗng và không trùng lặp.")
        if not 0.5 <= interval_seconds <= 60 or not 0 < reconnect_seconds <= 300:
            raise ValueError("interval_seconds hoặc reconnect_seconds không hợp lệ.")
        self._service = service
        self._stream_factory = stream_factory
        self._symbols = tuple(symbols)
        self._now = now or (lambda: datetime.now(UTC))
        self._interval = interval_seconds
        self._reconnect_seconds = reconnect_seconds
        self._monotonic = monotonic
        self.is_healthy = False
        self.last_error_code: str | None = None
        self.last_event_at: datetime | None = None
        self.quotes_written = 0

    async def run_session(self) -> None:
        await self._service.ensure_ready(self._symbols, now=self._now())
        self.is_healthy = True
        self.last_error_code = None
        pending: dict[str, BookTickerEvent] = {}
        last_write = self._monotonic()
        try:
            async with contextlib.aclosing(self._stream_factory().events()) as events:
                async for event in events:
                    if not isinstance(event, BookTickerEvent):
                        raise ValueError("Stream giá trực tiếp trả sự kiện không phải bookTicker.")
                    pending[event.symbol] = event
                    if self._monotonic() - last_write >= self._interval:
                        await self._write(pending)
                        last_write = self._monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Giữ giá cuối cùng đã nhận trước khi rớt kết nối.
            with contextlib.suppress(Exception):
                await self._write(pending)
            raise

    async def run(self, stopping: asyncio.Event) -> None:
        await _run_reconnecting(self, stopping, name="live-market-quote-session")

    async def _write(self, pending: dict[str, BookTickerEvent]) -> None:
        if not pending:
            return
        batch = list(pending.values())
        pending.clear()
        self.quotes_written += await self._service.upsert_quotes(batch, now=self._now())
        self.last_event_at = self._now()


async def _run_reconnecting(worker: Any, stopping: asyncio.Event, *, name: str) -> None:
    """Vòng lặp phiên dùng chung: lỗi nào cũng chỉ ghi mã an toàn rồi chờ nối lại."""
    while not stopping.is_set():
        session = asyncio.create_task(worker.run_session(), name=name)
        stop = asyncio.create_task(stopping.wait())
        await asyncio.wait({session, stop}, return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        if not session.done():
            session.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await session
            break
        error = session.exception()
        worker.is_healthy = False
        worker.last_error_code = _error_code(error)
        logger.warning("Live market ingestion gián đoạn; code=%s", worker.last_error_code)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=worker._reconnect_seconds)


def _error_code(error: BaseException | None) -> str:
    if error is None:
        return "LIVE_INGEST_STREAM_ENDED"
    if isinstance(error, PermissionError):
        return "LIVE_INGEST_RIGHTS_DENIED"
    return f"LIVE_INGEST_{type(error).__name__.upper()[:80]}"

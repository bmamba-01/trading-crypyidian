"""Worker thu nến đóng thời gian thực: lấp bằng REST, nối stream, tự nối lại khi rớt.

Worker chỉ đọc market data công khai và ghi kho market; không có cổng thực thi lệnh.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator, Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from trading_bot.application.live_kline_ingestion import (
    CatchUpResult,
    LiveBarDisposition,
    LiveKlineSubscription,
)
from trading_bot.domain.market.models import OHLCVBar

logger = logging.getLogger(__name__)


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
        while not stopping.is_set():
            session = asyncio.create_task(self.run_session(), name="live-kline-session")
            stop = asyncio.create_task(stopping.wait())
            await asyncio.wait({session, stop}, return_when=asyncio.FIRST_COMPLETED)
            stop.cancel()
            if not session.done():
                session.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await session
                break
            error = session.exception()
            self.is_healthy = False
            self.last_error_code = _error_code(error)
            logger.warning("Live market ingestion gián đoạn; code=%s", self.last_error_code)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=self._reconnect_seconds)

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


def _error_code(error: BaseException | None) -> str:
    if error is None:
        return "LIVE_INGEST_STREAM_ENDED"
    if isinstance(error, PermissionError):
        return "LIVE_INGEST_RIGHTS_DENIED"
    return f"LIVE_INGEST_{type(error).__name__.upper()[:80]}"

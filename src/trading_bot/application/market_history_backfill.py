"""Backfill lùi lịch sử nến Binance USDⓈ-M qua REST công khai (AI-5, CRY-236, 17/11/2026).

Collector SIG-0 chỉ tự nạp ``initial_bars`` ở lần chạy đầu rồi đi tiến theo cursor; dataset huấn
luyện cần nhiều tháng. Module này nạp **khoảng trước nến cũ nhất đang có** của nguồn
``BINANCE_USDM_REST_BACKFILL`` theo từng cửa sổ, dùng chung ``BinanceRestBackfillRunner`` (kiểm
hợp đồng từng nến, cổng quyền Source Registry chạy trước mọi request mạng) và khóa idempotent của
collector (``make_backfill_bar_version``) nên chạy lại hoặc chồng lấn chỉ ra DUPLICATE.

Mục đích quyền là ``BACKTEST_RESEARCH`` (nến dùng để dựng bundle replay). Không có cursor riêng:
lần chạy sau tự tính lại mốc cũ nhất từ DB.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from trading_bot.application.binance_rest_backfill import (
    BackfillRightsPurpose,
    BinanceRestBackfillRunner,
    RestBackfillRequest,
)
from trading_bot.application.binance_rest_backfill_ingestion import make_backfill_bar_version
from trading_bot.application.market_bar_collector import (
    COLLECTOR_INTERVALS,
    INSTRUMENT_SOURCE,
    PAGE_RECEIPT_ALLOWANCE,
    last_closed_boundary,
)

BACKFILL_BAR_SOURCE = "BINANCE_USDM_REST_BACKFILL"
MAX_REQUEST_DAYS = 366


class HistorySession(Protocol):
    market: Any
    rights_gate: Any


@dataclass(frozen=True, slots=True)
class HistoryBackfillResult:
    symbol: str
    interval: str
    status: str  # filled | no_older_history | up_to_date | error
    requested_start: datetime
    requested_end: datetime
    bars_written: int = 0
    windows: int = 0
    detail: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "status": self.status,
            "requested_start": self.requested_start.isoformat(),
            "requested_end": self.requested_end.isoformat(),
            "bars_written": self.bars_written,
            "windows": self.windows,
            "detail": self.detail,
        }


def align_down(moment: datetime, interval: str) -> datetime:
    """Mốc mở nến ``interval`` gần nhất không sau ``moment`` (UTC, neo 1970-01-01)."""
    duration = COLLECTOR_INTERVALS[interval]
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    steps = (moment.astimezone(UTC) - epoch) // duration
    return epoch + duration * steps


def plan_windows(
    start: datetime, end: datetime, interval: str, *, bars_per_window: int
) -> list[tuple[datetime, datetime]]:
    """Chia ``[start, end)`` thành các cửa sổ ≤ ``bars_per_window`` nến và ≤ 366 ngày."""
    if bars_per_window < 1:
        raise ValueError("bars_per_window phải dương.")
    duration = COLLECTOR_INTERVALS[interval]
    step = min(duration * bars_per_window, timedelta(days=MAX_REQUEST_DAYS))
    step = duration * max(1, step // duration)
    windows = []
    cursor = start
    while cursor < end:
        window_end = min(end, cursor + step)
        windows.append((cursor, window_end))
        cursor = window_end
    return windows


class MarketHistoryBackfill:
    def __init__(
        self,
        *,
        workspace_id: UUID,
        market_data: Any,
        session_scope: Callable[[], AbstractAsyncContextManager[HistorySession]],
        earliest_open_time: Callable[[HistorySession, str, str], Any],
        page_limit: int = 1500,
        max_pages: int = 4,
        pause_seconds: float = 0.5,
    ) -> None:
        self._workspace_id = workspace_id
        self._market_data = market_data
        self._session_scope = session_scope
        self._earliest_open_time = earliest_open_time
        self._page_limit = page_limit
        self._max_pages = max_pages
        self._pause = pause_seconds

    async def backfill(
        self, symbol: str, interval: str, *, days: int, now: datetime
    ) -> HistoryBackfillResult:
        """Nạp ``[now − days, nến cũ nhất đang có)``; chưa có nến thì tới nến đã đóng gần nhất.

        Coin niêm yết sau ``now − days`` thì Binance không trả nến cũ hơn: kết quả
        ``no_older_history`` (không phải lỗi)."""
        if interval not in COLLECTOR_INTERVALS:
            raise ValueError(f"Khung {interval} không được hỗ trợ.")
        instant = now.astimezone(UTC)
        start = align_down(instant - timedelta(days=days), interval)
        async with self._session_scope() as session:
            earliest = await self._earliest_open_time(session, symbol, interval)
        end = earliest if earliest is not None else last_closed_boundary(instant, interval)
        if start >= end:
            return HistoryBackfillResult(symbol, interval, "up_to_date", start, end)
        written = 0
        windows = plan_windows(
            start, end, interval, bars_per_window=self._page_limit * self._max_pages
        )
        for index, (window_start, window_end) in enumerate(windows):
            if index:
                await asyncio.sleep(self._pause)
            try:
                written += await self._fill_window(
                    symbol, interval, window_start, window_end, instant
                )
            except Exception as error:  # noqa: BLE001 - báo lỗi theo luồng, không dừng cả lượt
                return HistoryBackfillResult(
                    symbol,
                    interval,
                    "error",
                    start,
                    end,
                    written,
                    index,
                    f"{type(error).__name__}: {str(error)[:160]}",
                )
        status = "filled" if written else "no_older_history"
        return HistoryBackfillResult(symbol, interval, status, start, end, written, len(windows))

    async def _fill_window(
        self, symbol: str, interval: str, start: datetime, end: datetime, now: datetime
    ) -> int:
        request = RestBackfillRequest(
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
            page_limit=self._page_limit,
        )
        async with self._session_scope() as session:
            runner = BinanceRestBackfillRunner(
                market_data=self._market_data, rights_gate=session.rights_gate
            )
            batch = await runner.run_batch(
                request, now=now + PAGE_RECEIPT_ALLOWANCE, max_pages=self._max_pages
            )
            if not batch.bars:
                return 0
            instrument = await session.market.get_instrument(
                workspace_id=self._workspace_id,
                source=INSTRUMENT_SOURCE,
                symbol=symbol,
                as_of=now,
            )
            if instrument is None:
                raise LookupError(f"Chưa có instrument {symbol}; chạy collector trước.")
            for bar in batch.bars:
                versioned, version = make_backfill_bar_version(
                    workspace_id=self._workspace_id, bar=bar
                )
                await session.market.append_bar(
                    workspace_id=self._workspace_id,
                    instrument_id=UUID(str(instrument.id)),
                    bar=versioned,
                    version=version,
                )
            return len(batch.bars)


__all__ = [
    "BACKFILL_BAR_SOURCE",
    "HistoryBackfillResult",
    "MarketHistoryBackfill",
    "align_down",
    "plan_windows",
]

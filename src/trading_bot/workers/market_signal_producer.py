"""Worker producer độc lập, chỉ đọc market và ghi signal decision."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from trading_bot.application.market_signal_scanner import (
    MarketSignalScanResult,
)

logger = logging.getLogger(__name__)


class MarketSignalScannerPort(Protocol):
    """Cổng scanner không chứa planner, execution gateway hoặc Telegram sender."""

    async def scan_once(self, *, workspace_id: UUID, now: datetime) -> MarketSignalScanResult: ...


class MarketSignalProducerWorker:
    """Rà nến đã đóng theo chu kỳ, độc lập với PAPER supervisor và delivery worker."""

    def __init__(
        self,
        scanner: MarketSignalScannerPort,
        *,
        workspace_id: UUID,
        tick_seconds: float = 5.0,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        if not 0.1 <= tick_seconds <= 300:
            raise ValueError("tick_seconds phải nằm trong khoảng 0.1–300.")
        self._scanner = scanner
        self._workspace_id = workspace_id
        self._tick_seconds = tick_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self.last_tick_succeeded = False
        self.last_tick_at: datetime | None = None
        self.last_error_code: str | None = None
        self.last_scan_result: MarketSignalScanResult | None = None

    async def tick(self) -> MarketSignalScanResult:
        """Chạy một lượt scanner với mốc UTC có timezone."""
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Thời điểm scanner phải có múi giờ.")
        result = await self._scanner.scan_once(
            workspace_id=self._workspace_id,
            now=now.astimezone(UTC),
        )
        self.last_scan_result = result
        return result

    async def run(self, stopping: asyncio.Event) -> None:
        """Poll đến khi lifespan dừng; lỗi được ghi dạng mã an toàn rồi thử lại."""
        while not stopping.is_set():
            try:
                await self.tick()
                self.last_tick_succeeded = True
                self.last_tick_at = self._now()
                self.last_error_code = None
            except Exception as error:
                self.last_tick_succeeded = False
                self.last_error_code = _error_code(error)
                logger.warning("Market signal producer tick lỗi; code=%s", self.last_error_code)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=self._tick_seconds)
            except TimeoutError:
                continue


def _error_code(error: BaseException) -> str:
    return f"MARKET_SIGNAL_TICK_{type(error).__name__.upper()[:80]}"

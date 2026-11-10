"""Poll bền vững các quyết định PAPER cho observer learning chỉ đọc metadata."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from trading_bot.application.learning_shadow import (
    LearningShadowApplication,
    ShadowObservationBatch,
)

logger = logging.getLogger(__name__)
WaitForStop = Callable[[asyncio.Event, float], Awaitable[None]]


class LearningShadowObserverSupervisor:
    """Xử lý tối đa một batch mỗi tick, retry có backoff và dừng sạch."""

    def __init__(
        self,
        *,
        application: LearningShadowApplication,
        workspace_id: UUID,
        poll_seconds: float = 5.0,
        max_backoff_seconds: float = 60.0,
        batch_size: int = 100,
        wait_for_stop: WaitForStop | None = None,
    ) -> None:
        if (
            not callable(getattr(application, "observe_pending", None))
            or type(workspace_id) is not UUID
            or workspace_id.int == 0
            or not isinstance(poll_seconds, (int, float))
            or isinstance(poll_seconds, bool)
            or not 0 < poll_seconds <= 300
            or not isinstance(max_backoff_seconds, (int, float))
            or isinstance(max_backoff_seconds, bool)
            or not poll_seconds <= max_backoff_seconds <= 3600
            or type(batch_size) is not int
            or not 1 <= batch_size <= 1000
        ):
            raise ValueError("Cấu hình learning shadow observer không hợp lệ.")
        self._application = application
        self._workspace_id = workspace_id
        self._poll_seconds = float(poll_seconds)
        self._max_backoff_seconds = float(max_backoff_seconds)
        self._batch_size = batch_size
        self._wait_for_stop = wait_for_stop or _wait_for_stop

    async def run_once(self) -> ShadowObservationBatch:
        """Quan sát một batch trong workspace đã cố định ở deployment."""
        return await self._application.observe_pending(
            workspace_id=self._workspace_id,
            limit=self._batch_size,
        )

    async def run(self, stopping: asyncio.Event) -> None:
        """Lặp theo batch; lỗi được ghi dạng tổng quát, không log payload hoặc DSN."""
        delay = self._poll_seconds
        while not stopping.is_set():
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Learning shadow observer tick lỗi; sẽ thử lại: %s",
                    type(error).__name__,
                )
                await self._wait_for_stop(stopping, delay)
                delay = min(self._max_backoff_seconds, delay * 2)
                continue

            delay = self._poll_seconds
            if result.created_count == 0 or result.scanned_count < self._batch_size:
                await self._wait_for_stop(stopping, delay)


async def _wait_for_stop(stopping: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(stopping.wait(), timeout=timeout)
    except TimeoutError:
        return


__all__ = ["LearningShadowObserverSupervisor"]

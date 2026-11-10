"""Poll bền vững hàng đợi trainer trong phạm vi một workspace."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from trading_bot.workers.learning_training_worker import LearningTrainingAttemptWorker

logger = logging.getLogger(__name__)
WaitForStop = Callable[[asyncio.Event, float], Awaitable[None]]


class LearningTrainingJobSupervisor:
    """Poll queue, giãn retry khi lỗi và dừng sạch cùng application lifespan."""

    def __init__(
        self,
        *,
        worker: LearningTrainingAttemptWorker,
        workspace_id: UUID,
        worker_id: str,
        poll_seconds: float = 5.0,
        max_backoff_seconds: float = 60.0,
        wait_for_stop: WaitForStop | None = None,
    ) -> None:
        if (
            type(workspace_id) is not UUID
            or not isinstance(worker_id, str)
            or not worker_id.strip()
            or not isinstance(poll_seconds, (int, float))
            or not 0.1 <= poll_seconds <= 300
            or not isinstance(max_backoff_seconds, (int, float))
            or not poll_seconds <= max_backoff_seconds <= 3600
        ):
            raise ValueError("Cấu hình learning training supervisor không hợp lệ.")
        self._worker = worker
        self._workspace_id = workspace_id
        self._worker_id = worker_id
        self._poll_seconds = float(poll_seconds)
        self._max_backoff_seconds = float(max_backoff_seconds)
        self._wait_for_stop = wait_for_stop or _wait_for_stop

    async def run(self, stopping: asyncio.Event) -> None:
        """Xử lý job liên tiếp; queue rỗng chờ poll, lỗi tăng backoff có trần."""
        delay = self._poll_seconds
        while not stopping.is_set():
            try:
                result = await self._worker.work_once(
                    workspace_id=self._workspace_id,
                    worker_id=self._worker_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Learning trainer tick lỗi; sẽ thử lại: %s",
                    type(error).__name__,
                )
                await self._wait_for_stop(stopping, delay)
                delay = min(self._max_backoff_seconds, delay * 2)
                continue

            delay = self._poll_seconds
            if result.status == "idle":
                await self._wait_for_stop(stopping, delay)


async def _wait_for_stop(stopping: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(stopping.wait(), timeout=timeout)
    except TimeoutError:
        return

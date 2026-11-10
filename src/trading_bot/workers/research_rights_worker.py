"""Worker chạy scanner hết hạn và rights consumer trong phạm vi một workspace."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.dataset_rights_invalidation import (
    DatasetRightsChangeBatchResult,
    DatasetRightsChangeConsumer,
)
from trading_bot.application.source_policy_expiry import SourcePolicyExpiryScanner

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResearchRightsTickResult:
    """Kết quả xử lý một tick, không mang payload rights event."""

    expiry_events_enqueued: int
    rights_batch: DatasetRightsChangeBatchResult


class ResearchRightsMaintenanceWorker:
    """Quét policy hết hạn và áp invalidation bền vững cho đúng một workspace."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        workspace_id: UUID,
        worker_id: str,
        tick_seconds: float = 5.0,
        batch_size: int = 100,
    ) -> None:
        if (
            type(workspace_id) is not UUID
            or not isinstance(worker_id, str)
            or not worker_id.strip()
            or not isinstance(tick_seconds, (int, float))
            or not 0 < tick_seconds <= 300
            or type(batch_size) is not int
            or not 1 <= batch_size <= 100
        ):
            raise ValueError("Cấu hình rights worker không hợp lệ.")
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._worker_id = worker_id
        self._tick_seconds = float(tick_seconds)
        self._batch_size = batch_size
        self._last_tick_succeeded = False

    @property
    def last_tick_succeeded(self) -> bool:
        """Cho health probe biết tick gần nhất có commit thành công hay không."""
        return self._last_tick_succeeded

    async def run_once(self) -> ResearchRightsTickResult:
        """Chạy scanner và consumer trong một transaction workspace-scoped."""
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(self._workspace_id)},
            )
            database_now = await session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
            if (
                not isinstance(database_now, datetime)
                or database_now.tzinfo is None
                or database_now.utcoffset() is None
            ):
                raise RuntimeError("database clock không trả thời điểm có timezone.")

            expiry_count = await SourcePolicyExpiryScanner(
                SourceRegistryRepository(session)
            ).scan_workspace(self._workspace_id, batch_size=self._batch_size)
            rights_batch = await DatasetRightsChangeConsumer(session).process_batch(
                workspace_id=self._workspace_id,
                worker_id=self._worker_id,
                now=database_now,
                batch_size=self._batch_size,
            )
        return ResearchRightsTickResult(
            expiry_events_enqueued=expiry_count,
            rights_batch=rights_batch,
        )

    async def run(self, stopping: asyncio.Event) -> None:
        """Lặp tới khi shutdown; lỗi một tick được rollback và thử lại ở tick sau."""
        while not stopping.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._last_tick_succeeded = False
                logger.warning(
                    "Rights worker tick thất bại; sẽ thử lại: %s",
                    type(error).__name__,
                )
            else:
                self._last_tick_succeeded = True
            try:
                await asyncio.wait_for(stopping.wait(), timeout=self._tick_seconds)
            except TimeoutError:
                continue

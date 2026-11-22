"""Ghi và đọc heartbeat an toàn của các worker ingest qua PostgreSQL."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.worker_heartbeats import WorkerHeartbeatRepository
from trading_bot.application.outbox import UnitOfWork
from trading_bot.observability.readiness import WorkerReadinessReport, WorkerReadinessState

logger = logging.getLogger(__name__)
_COUNTER_NAMES = ("bars_appended", "events_written", "quotes_written", "reconnect_count")


@dataclass(frozen=True, slots=True)
class WorkerHeartbeatSnapshot:
    """Bản chụp không chứa payload thị trường, secret hay thông báo lỗi thô."""

    component: str
    state: WorkerReadinessState
    reason_code: str | None
    detail_vi: str | None
    heartbeat_at: datetime
    last_tick_at: datetime | None
    counters: dict[str, int]

    def to_record(self) -> dict[str, Any]:
        """Chuyển thành hàng ghi DB; counter chỉ gồm tên đã cho phép."""
        return {
            "component": self.component,
            "state": self.state.value,
            "reason_code": self.reason_code,
            "detail_vi": self.detail_vi,
            "heartbeat_at": self.heartbeat_at,
            "last_tick_at": self.last_tick_at,
            "counters": dict(self.counters),
        }


@dataclass(frozen=True, slots=True)
class WorkerHeartbeatSource:
    """Liên kết component với worker và task đang chạy trong ingestion process."""

    component: str
    worker: Any
    task: asyncio.Task[Any] | None


def build_heartbeat_snapshot(
    *, component: str, worker: Any, task_active: bool, now: datetime
) -> WorkerHeartbeatSnapshot:
    """Chuyển trạng thái worker thành snapshot có bộ đếm được allowlist."""
    counters = {
        name: value
        for name in _COUNTER_NAMES
        if type(value := getattr(worker, name, None)) is int and value >= 0
    }
    tick = getattr(worker, "last_tick_at", None)
    if not isinstance(tick, datetime):
        tick = None

    if worker is None:
        state = WorkerReadinessState.NOT_READY
        reason_code = "WORKER_NOT_STARTED"
        detail_vi = "Worker chưa được khởi tạo."
    elif not task_active:
        state = WorkerReadinessState.NOT_READY
        reason_code = "WORKER_TASK_STOPPED"
        detail_vi = "Task nền của worker chưa chạy hoặc đã dừng."
    elif getattr(worker, "is_healthy", False):
        state = WorkerReadinessState.READY
        reason_code = None
        detail_vi = None
    elif getattr(worker, "last_error_code", None):
        state = WorkerReadinessState.DEGRADED
        reason_code = str(worker.last_error_code)[:120]
        detail_vi = "Worker đang nối lại sau lỗi ở phiên gần nhất."
    else:
        state = WorkerReadinessState.NOT_READY
        reason_code = "WORKER_NOT_READY"
        detail_vi = "Worker chưa có phiên chạy khỏe mạnh."

    return WorkerHeartbeatSnapshot(
        component=component,
        state=state,
        reason_code=reason_code,
        detail_vi=detail_vi,
        heartbeat_at=now,
        last_tick_at=tick,
        counters=counters,
    )


def heartbeat_to_readiness_report(
    row: dict[str, Any], *, now: datetime, stale_after: timedelta
) -> WorkerReadinessReport:
    """Đánh dấu heartbeat hết hạn là not-ready nhưng giữ counter để chẩn đoán."""
    heartbeat_at = row.get("heartbeat_at")
    stale = not isinstance(heartbeat_at, datetime) or now - heartbeat_at > stale_after
    metadata = dict(row.get("metadata") or {})
    metadata["heartbeat_at"] = (
        heartbeat_at.isoformat() if isinstance(heartbeat_at, datetime) else None
    )
    metadata["heartbeat_stale"] = stale
    if stale:
        return WorkerReadinessReport(
            component=str(row.get("component") or "unknown_worker"),
            state=WorkerReadinessState.NOT_READY,
            reason_code="WORKER_HEARTBEAT_STALE",
            detail_vi="Heartbeat worker đã quá hạn; kiểm tra process ingestion-worker.",
            last_tick_at=row.get("last_tick_at"),
            metadata=metadata,
        )
    try:
        state = WorkerReadinessState(str(row.get("state")))
    except ValueError:
        state = WorkerReadinessState.NOT_READY
    return WorkerReadinessReport(
        component=str(row.get("component") or "unknown_worker"),
        state=state,
        reason_code=row.get("reason_code"),
        detail_vi=row.get("detail_vi"),
        last_tick_at=row.get("last_tick_at"),
        metadata=metadata,
    )


class IngestionWorkerHeartbeatPublisher:
    """Ghi heartbeat định kỳ; lỗi lưu trạng thái không dừng việc thu thập dữ liệu."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        workspace_id: UUID,
        sources: Sequence[WorkerHeartbeatSource],
        interval: float = 5.0,
        instance_id: UUID | None = None,
    ) -> None:
        if not sources or interval <= 0:
            raise ValueError("Cần worker source và heartbeat interval dương.")
        self._sessions = sessions
        self._workspace_id = workspace_id
        self._sources = tuple(sources)
        self._interval = interval
        self._instance_id = instance_id or uuid4()

    async def publish_once(self, *, now: datetime | None = None) -> None:
        heartbeat_at = now or datetime.now(UTC)
        snapshots = [
            build_heartbeat_snapshot(
                component=source.component,
                worker=source.worker,
                task_active=source.task is not None and not source.task.done(),
                now=heartbeat_at,
            ).to_record()
            for source in self._sources
        ]
        async with UnitOfWork(self._sessions, workspace_id=self._workspace_id) as unit:
            await WorkerHeartbeatRepository(unit.session).upsert_many(
                workspace_id=self._workspace_id,
                instance_id=self._instance_id,
                snapshots=snapshots,
            )

    async def run(self, stopping: asyncio.Event) -> None:
        """Ghi ngay khi khởi động rồi đợi theo chu kỳ cho tới khi lifespan dừng."""
        while not stopping.is_set():
            try:
                await self.publish_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Không ghi được worker heartbeat; error_code=%s",
                    type(error).__name__.upper()[:80],
                )
            try:
                await asyncio.wait_for(stopping.wait(), timeout=self._interval)
            except TimeoutError:
                continue


class WorkerHeartbeatReadService:
    """Đọc trạng thái mới nhất trong workspace và áp ngưỡng heartbeat hết hạn."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        stale_after: timedelta = timedelta(seconds=15),
    ) -> None:
        self._sessions = sessions
        self._stale_after = stale_after

    async def list_readiness(
        self, *, workspace_id: UUID, now: datetime | None = None
    ) -> list[WorkerReadinessReport]:
        async with UnitOfWork(
            self._sessions, workspace_id=workspace_id, snapshot_read_only=True
        ) as unit:
            rows = await WorkerHeartbeatRepository(unit.session).latest_by_component(
                workspace_id=workspace_id
            )
        current = now or datetime.now(UTC)
        return [
            heartbeat_to_readiness_report(row, now=current, stale_after=self._stale_after)
            for row in rows
        ]

"""Worker nền cho job replay PAPER có lease, tiến độ và điểm hủy an toàn."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
    BacktestDatasetManifestRepository,
)
from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
    BacktestDatasetReplayBundleRepository,
)
from trading_bot.adapters.database.repositories.backtest_job_persistence import (
    BacktestJobPersistenceRepository,
)
from trading_bot.adapters.database.repositories.backtest_outcomes import BacktestOutcomeRepository
from trading_bot.adapters.database.repositories.backtest_replay_jobs import (
    BacktestReplayJobRepository,
)
from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.backtest import ReplayCancelled
from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayLoader
from trading_bot.application.backtest_dataset_replay_bundle_loader import (
    BacktestDatasetReplayBundleLoader,
)
from trading_bot.application.backtest_dataset_replay_execution import (
    BacktestDatasetReplayExecutionService,
    BacktestReplayExecutionCancelled,
)
from trading_bot.application.backtest_job_persistence import BacktestJobPersistToken
from trading_bot.application.backtest_replay_executor import build_backtest_replay_plan
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
from trading_bot.application.binance_rest_backfill_rights import SourceRegistryBackfillRightsGate
from trading_bot.application.outbox import UnitOfWork
from trading_bot.config.settings import AppSettings

LEASE_LOST = "BACKTEST_WORKER_LEASE_LOST"
_LOGGER = logging.getLogger(__name__)
UnitOfWorkFactory = Callable[[UUID], AbstractAsyncContextManager[Any]]


class BacktestReplayJobWorker:
    """Claim và chạy từng job; load, PAPER replay và persist không giữ chung transaction."""

    def __init__(
        self,
        *,
        read_sessions: async_sessionmaker[AsyncSession],
        persist_sessions: async_sessionmaker[AsyncSession],
        workspace_id: UUID,
        worker_id: str | None = None,
        tick_seconds: float = 2.0,
        lease_seconds: int = 120,
        read_unit_of_work_factory: UnitOfWorkFactory | None = None,
        persist_unit_of_work_factory: UnitOfWorkFactory | None = None,
    ) -> None:
        if type(workspace_id) is not UUID or workspace_id.int == 0:
            raise ValueError("Replay worker cần workspace UUID đã provision.")
        if tick_seconds <= 0 or lease_seconds < 15:
            raise ValueError("Tick hoặc lease replay worker không hợp lệ.")
        self._read_sessions = read_sessions
        self._persist_sessions = persist_sessions
        self._workspace_id = workspace_id
        self._worker_id = worker_id or f"backtest-{uuid4().hex}"
        self._tick = tick_seconds
        self._lease_duration = timedelta(seconds=lease_seconds)
        self._read_unit_of_work_factory = read_unit_of_work_factory or (
            lambda workspace_id: UnitOfWork(read_sessions, workspace_id=workspace_id)
        )
        self._persist_unit_of_work_factory = persist_unit_of_work_factory or (
            lambda workspace_id: UnitOfWork(persist_sessions, workspace_id=workspace_id)
        )

    async def run(self, stopping: asyncio.Event) -> None:
        """Chạy poll loop; chờ ngắn khi hàng đợi rỗng để không quay CPU liên tục."""
        while not stopping.is_set():
            claimed = await self.run_once()
            if not claimed:
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=self._tick)
                except TimeoutError:
                    pass

    async def run_once(self) -> bool:
        """Claim tối đa một job; trả False nếu workspace chưa có việc sẵn sàng."""
        now = datetime.now(UTC)
        async with self._read_unit_of_work_factory(self._workspace_id) as uow:
            row = await BacktestReplayJobRepository(uow.session).claim_next(
                workspace_id=self._workspace_id,
                worker_id=self._worker_id,
                now=now,
                lease_duration=self._lease_duration,
            )
            claimed = _Claim.from_row(row) if row is not None else None
        if claimed is None:
            return False
        if claimed.status in {"CANCELLED", "FAILED"}:
            return True
        await self._process(claimed)
        return True

    async def _process(self, claim: _Claim) -> None:
        cancelled = Event()
        lease_lost = Event()
        finished = asyncio.Event()
        monitor = asyncio.create_task(
            self._monitor(claim, cancelled, lease_lost, finished),
            name=f"backtest-lease-{claim.job_id}",
        )
        monitor_awaited = False

        async def stop_monitor() -> None:
            nonlocal monitor_awaited
            finished.set()
            if not monitor_awaited:
                monitor_awaited = True
                # Đợi query/cleanup đang chạy hoàn tất, không hủy che khuất lỗi monitor.
                await monitor

        @asynccontextmanager
        async def persist_unit_of_work(workspace_id: UUID) -> AsyncIterator[Any]:
            await stop_monitor()
            if lease_lost.is_set():
                raise ReplayLeaseLost()
            if cancelled.is_set():
                raise ReplayCancelled("Replay bị yêu cầu hủy trước khi khóa persist.")
            async with self._persist_unit_of_work_factory(workspace_id) as uow:
                yield uow

        try:
            request = BacktestReplayJobSubmission.from_payload(claim.request_payload)
            canonical = json.dumps(
                request.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if (
                canonical != claim.canonical_request
                or request.bundle_hash != claim.bundle_hash
                or request.strategy_release_id != claim.strategy_release_id
                or request.execution_interval != claim.execution_interval
            ):
                raise ValueError("Payload job không khớp cột scope hoặc canonical bytes.")

            async with self._read_unit_of_work_factory(self._workspace_id) as uow:
                bundle = await _bundle_loader(uow, self._workspace_id).load_for_replay(
                    workspace_id=self._workspace_id,
                    bundle_hash=request.bundle_hash,
                    now=datetime.now(UTC),
                )
            plan = build_backtest_replay_plan(
                self._workspace_id,
                claim.job_id,
                bundle,
                request,
            )
            await self._update_progress(claim, stage="load", progress=10, lease_lost=lease_lost)
            if cancelled.is_set():
                raise ReplayCancelled("Replay bị yêu cầu hủy trước khi bắt đầu.")

            execution_service = BacktestDatasetReplayExecutionService(
                load_unit_of_work_factory=self._read_unit_of_work_factory,
                persist_unit_of_work_factory=persist_unit_of_work,
                bundle_loader_factory=lambda uow: _bundle_loader(uow, self._workspace_id),
                outcome_repository_factory=lambda uow: BacktestOutcomeRepository(uow.session),
                executor=plan.executor,
                clock=lambda: datetime.now(UTC),
                job_persistence_factory=lambda uow: _JobPersistenceAdapter(uow.session),
            )

            loop = asyncio.get_running_loop()
            last_percent = [15]
            last_update = [0.0]

            def report_observation_progress(completed: int, total: int) -> None:
                if lease_lost.is_set():
                    raise ReplayLeaseLost()
                if cancelled.is_set():
                    raise ReplayCancelled("Replay bị yêu cầu hủy tại ranh giới nến.")
                if total <= 0:
                    return
                percent = min(85, 20 + int(65 * completed / total))
                now_monotonic = time.monotonic()
                if percent <= last_percent[0] and now_monotonic - last_update[0] < 1.0:
                    return
                future = asyncio.run_coroutine_threadsafe(
                    self._update_progress(
                        claim,
                        stage="replay",
                        progress=percent,
                        lease_lost=lease_lost,
                    ),
                    loop,
                )
                future.result(timeout=max(10.0, self._tick * 3))
                last_percent[0] = percent
                last_update[0] = now_monotonic

            async def report_phase(phase: str) -> None:
                values = {"load": 10, "replay": 20}
                if phase not in values:
                    raise ValueError("Execution service trả phase không hợp lệ.")
                await self._update_progress(
                    claim,
                    stage=phase,
                    progress=values[phase],
                    lease_lost=lease_lost,
                )

            await execution_service.execute(
                workspace_id=self._workspace_id,
                bundle_hash=request.bundle_hash,
                manifest=plan.manifest,
                phase_callback=report_phase,
                cancel_requested=cancelled.is_set,
                progress_callback=report_observation_progress,
                job_token=BacktestJobPersistToken(
                    workspace_id=self._workspace_id,
                    job_id=claim.job_id,
                    worker_id=self._worker_id,
                    generation=claim.generation,
                ),
            )
        except ReplayCancelled, BacktestReplayExecutionCancelled:
            if not lease_lost.is_set():
                await self._finish(claim, status="CANCELLED", stage="cancelled")
        except ReplayLeaseLost:
            lease_lost.set()
        except Exception as error:
            if not lease_lost.is_set():
                error_code = _error_code(error)
                _LOGGER.exception("Job replay %s thất bại với mã %s.", claim.job_id, error_code)
                await self._finish(
                    claim,
                    status="FAILED",
                    stage="failed",
                    error_code=error_code,
                )
        finally:
            await stop_monitor()

    async def _monitor(
        self,
        claim: _Claim,
        cancelled: Event,
        lease_lost: Event,
        finished: asyncio.Event,
    ) -> None:
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=self._tick)
                return
            except TimeoutError:
                pass
            now = datetime.now(UTC)
            async with self._read_unit_of_work_factory(self._workspace_id) as uow:
                repository = BacktestReplayJobRepository(uow.session)
                was_cancelled = await repository.cancel_requested(
                    workspace_id=self._workspace_id,
                    job_id=claim.job_id,
                    worker_id=self._worker_id,
                    generation=claim.generation,
                    now=now,
                )
                lease_refreshed = await repository.refresh_lease(
                    workspace_id=self._workspace_id,
                    job_id=claim.job_id,
                    worker_id=self._worker_id,
                    generation=claim.generation,
                    now=now,
                    lease_duration=self._lease_duration,
                )
            if was_cancelled:
                cancelled.set()
            if not lease_refreshed:
                lease_lost.set()
                cancelled.set()
                return

    async def _update_progress(
        self,
        claim: _Claim,
        *,
        stage: str,
        progress: int,
        lease_lost: Event,
    ) -> None:
        if lease_lost.is_set():
            raise ReplayLeaseLost()
        now = datetime.now(UTC)
        async with self._read_unit_of_work_factory(self._workspace_id) as uow:
            updated = await BacktestReplayJobRepository(uow.session).update_progress(
                workspace_id=self._workspace_id,
                job_id=claim.job_id,
                worker_id=self._worker_id,
                generation=claim.generation,
                stage=stage,
                progress=max(claim.progress, progress),
                now=now,
                lease_duration=self._lease_duration,
            )
        if not updated:
            if stage == "persist":
                async with self._read_unit_of_work_factory(self._workspace_id) as uow:
                    cancellation_won = await BacktestReplayJobRepository(
                        uow.session
                    ).cancel_requested(
                        workspace_id=self._workspace_id,
                        job_id=claim.job_id,
                        worker_id=self._worker_id,
                        generation=claim.generation,
                        now=datetime.now(UTC),
                    )
                if cancellation_won:
                    raise ReplayCancelled("Yêu cầu hủy đã đến trước khi lưu kết quả.")
            lease_lost.set()
            raise ReplayLeaseLost()

    async def _finish(
        self,
        claim: _Claim,
        *,
        status: str,
        stage: str,
        result_run_ids: tuple[str, ...] = (),
        error_code: str | None = None,
    ) -> bool:
        now = datetime.now(UTC)
        async with self._read_unit_of_work_factory(self._workspace_id) as uow:
            return await BacktestReplayJobRepository(uow.session).finish(
                workspace_id=self._workspace_id,
                job_id=claim.job_id,
                worker_id=self._worker_id,
                generation=claim.generation,
                status=status,
                stage=stage,
                progress=100,
                now=now,
                result_run_ids=result_run_ids,
                error_code=error_code,
            )


class _JobPersistenceAdapter:
    """Chuyển token sang capability DB trên chính session persist được cung cấp."""

    def __init__(self, session: AsyncSession) -> None:
        self._repository = BacktestJobPersistenceRepository(session)

    async def begin(self, token: BacktestJobPersistToken) -> None:
        try:
            await self._repository.begin(
                workspace_id=token.workspace_id,
                job_id=token.job_id,
                worker_id=token.worker_id,
                generation=token.generation,
            )
        except DBAPIError as error:
            reason = str(error.orig)
            if "WP07C_PERSIST_CANCELLED" in reason:
                raise ReplayCancelled("Yêu cầu hủy đã thắng trước khi khóa persist.") from error
            if any(
                code in reason
                for code in (
                    "WP07C_PERSIST_OWNER_INVALID",
                    "WP07C_PERSIST_LEASE_EXPIRED",
                    "WP07C_PERSIST_JOB_MISSING",
                    "WP07C_PERSIST_STATUS_INVALID",
                )
            ):
                raise ReplayLeaseLost() from error
            raise

    async def complete(
        self, token: BacktestJobPersistToken, result_run_ids: tuple[UUID, ...]
    ) -> None:
        await self._repository.complete(
            workspace_id=token.workspace_id,
            job_id=token.job_id,
            worker_id=token.worker_id,
            generation=token.generation,
            result_run_ids=result_run_ids,
        )


def _bundle_loader(uow: Any, workspace_id: UUID) -> BacktestDatasetReplayBundleLoader:
    manifests = BacktestDatasetManifestRepository(uow.session)
    rights_repository = SourceRegistryRepository(uow.session)
    replay_loader = BacktestDatasetReplayLoader(
        repository=manifests,
        rights_gate=SourceRegistryBackfillRightsGate(
            workspace_id=workspace_id,
            repository=rights_repository,
        ),
        market_data=MarketDataRepository(uow.session),
    )
    return BacktestDatasetReplayBundleLoader(
        repository=BacktestDatasetReplayBundleRepository(uow.session),
        replay_loader=replay_loader,
    )


@dataclass(frozen=True, slots=True)
class _Claim:
    job_id: UUID
    status: str
    generation: int
    progress: int
    request_payload: dict[str, Any]
    canonical_request: str
    bundle_hash: str
    strategy_release_id: str
    execution_interval: str

    @classmethod
    def from_row(cls, row: Any) -> _Claim:
        return cls(
            job_id=row.id,
            status=row.status,
            generation=row.lease_generation,
            progress=row.progress,
            request_payload=row.request_payload,
            canonical_request=row.canonical_request,
            bundle_hash=row.bundle_hash,
            strategy_release_id=row.strategy_release_id,
            execution_interval=row.execution_interval,
        )


class ReplayLeaseLost(RuntimeError):
    """Worker cũ dừng khi lease đã được generation khác tiếp quản."""


def _error_code(error: Exception) -> str:
    if isinstance(error, PermissionError):
        return "BACKTEST_RIGHTS_DENIED"
    if isinstance(error, LookupError):
        return "BACKTEST_BUNDLE_NOT_FOUND"
    if isinstance(error, ValueError):
        return "BACKTEST_REPLAY_INPUT_INVALID"
    return "BACKTEST_REPLAY_FAILED"


async def run_backtest_worker(settings: AppSettings, *, once: bool = False) -> None:
    """Dựng hai connection pool tách quyền: đọc job/data và persist qua trainer_worker."""
    if settings.process_role != "backtest-worker":
        raise ValueError("CLI worker chỉ chạy với process_role=backtest-worker.")
    if not (settings.database_user and settings.database_password_file):
        raise ValueError("backtest-worker thiếu credential database đọc/claim.")
    if not (
        settings.backtest_trainer_database_user and settings.backtest_trainer_database_password_file
    ):
        raise ValueError("backtest-worker thiếu credential trainer_worker.")

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )

    read_url = database_url_from_secret(
        username=settings.database_user,
        password_file=settings.database_password_file,
        host=settings.database_host,
        database=settings.database_name,
    )
    persist_url = database_url_from_secret(
        username=settings.backtest_trainer_database_user,
        password_file=settings.backtest_trainer_database_password_file,
        host=settings.database_host,
        database=settings.database_name,
    )
    read_engine = create_database_engine(read_url)
    persist_engine = create_database_engine(persist_url)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in ("SIGINT", "SIGTERM"):
        import signal

        with suppress(NotImplementedError):
            loop.add_signal_handler(getattr(signal, sig), stop.set)
    worker = BacktestReplayJobWorker(
        read_sessions=create_session_factory(read_engine),
        persist_sessions=create_session_factory(persist_engine),
        workspace_id=_configured_workspace(settings),
        tick_seconds=settings.backtest_worker_tick_seconds,
        lease_seconds=settings.backtest_worker_lease_seconds,
    )
    try:
        if once:
            await worker.run_once()
        else:
            await worker.run(stop)
    finally:
        await asyncio.gather(read_engine.dispose(), persist_engine.dispose())


def _configured_workspace(settings: AppSettings) -> UUID:
    workspace_id = settings.backtest_worker_workspace_id
    if workspace_id is None or workspace_id.int == 0:
        raise ValueError("backtest-worker cần workspace UUID đã provision.")
    return workspace_id


__all__ = ["BacktestReplayJobWorker", "run_backtest_worker"]

"""Điều phối replay bundle có kiểm quyền trước khi lưu outcome."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.repositories.backtest_outcomes import (
    BacktestOutcomeAppendResult,
)
from trading_bot.application.backtest import ReplayManifest
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetReplayBundle,
    BacktestDatasetReplayBundleReference,
)
from trading_bot.application.backtest_job_persistence import (
    BacktestJobPersistToken,
    JobPersistenceFactory,
    ReplayJobPersistencePort,
)
from trading_bot.application.backtest_sensitivity import SensitivityRun
from trading_bot.application.runtime_replay_evaluator import (
    RuntimePaperReplayComparison,
)
from trading_bot.domain.learning.evaluation import EvaluationTrade


class ReplayExecutionUnitOfWork(Protocol):
    """Giao diện tối thiểu cho một transaction gắn với workspace."""

    @property
    def session(self) -> AsyncSession: ...


class BacktestReplayExecutionCancelled(RuntimeError):
    """Job bị hủy tại ranh giới an toàn trước khi persist outcome."""


class ReplayBundleExecutionPort(Protocol):
    """Load bundle đã lưu và kiểm lại policy hiện hành."""

    async def load_for_replay(
        self, *, workspace_id: UUID, bundle_hash: str, now: datetime
    ) -> BacktestDatasetReplayBundle: ...

    async def revalidate_current_rights(
        self, bundle: BacktestDatasetReplayBundle, *, now: datetime
    ) -> BacktestDatasetReplayBundle: ...


class ReplayOutcomeExecutionPort(Protocol):
    """Append run/outcome/biên nhận vào transaction hiện hành."""

    async def append_sensitivity_run(
        self,
        *,
        workspace_id: UUID,
        sensitivity_run: SensitivityRun,
        trade_outcomes: tuple[EvaluationTrade, ...],
        dataset_bundle: BacktestDatasetReplayBundle,
    ) -> BacktestOutcomeAppendResult: ...


UnitOfWorkFactory = Callable[[UUID], AbstractAsyncContextManager[ReplayExecutionUnitOfWork]]
BundleLoaderFactory = Callable[[ReplayExecutionUnitOfWork], ReplayBundleExecutionPort]
OutcomeRepositoryFactory = Callable[[ReplayExecutionUnitOfWork], ReplayOutcomeExecutionPort]
ReplayExecutor = Callable[
    ...,
    RuntimePaperReplayComparison,
]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class BacktestDatasetReplayExecutionResult:
    """Kết quả đã chuẩn hóa sau khi mọi child outcome được commit."""

    workspace_id: UUID
    bundle_hash: str
    outcomes: tuple[BacktestOutcomeAppendResult, ...]

    @property
    def run_ids(self) -> tuple[UUID, ...]:
        return tuple(outcome.run_id for outcome in self.outcomes)

    @property
    def report_ids(self) -> tuple[UUID | None, ...]:
        return tuple(outcome.report_id for outcome in self.outcomes)

    @property
    def scenario_ids(self) -> tuple[str, ...]:
        return tuple(outcome.scenario_id for outcome in self.outcomes)


class BacktestDatasetReplayExecutionService:
    """Chạy runtime ngoài transaction rồi persist atomically sau khi tái kiểm quyền."""

    def __init__(
        self,
        *,
        load_unit_of_work_factory: UnitOfWorkFactory,
        persist_unit_of_work_factory: UnitOfWorkFactory,
        bundle_loader_factory: BundleLoaderFactory,
        outcome_repository_factory: OutcomeRepositoryFactory,
        executor: ReplayExecutor,
        clock: Clock,
        job_persistence_factory: JobPersistenceFactory | None = None,
    ) -> None:
        self._load_unit_of_work_factory = load_unit_of_work_factory
        self._persist_unit_of_work_factory = persist_unit_of_work_factory
        self._bundle_loader_factory = bundle_loader_factory
        self._outcome_repository_factory = outcome_repository_factory
        self._executor = executor
        self._clock = clock
        self._job_persistence_factory = job_persistence_factory

    async def execute(
        self,
        *,
        workspace_id: UUID,
        bundle_hash: str,
        manifest: ReplayManifest,
        phase_callback: Callable[[str], Awaitable[None]] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        job_token: BacktestJobPersistToken | None = None,
    ) -> BacktestDatasetReplayExecutionResult:
        """Load/authorize, chạy PAPER ngoài DB rồi append mọi scenario trong một UoW."""
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        if job_token is not None:
            if type(job_token) is not BacktestJobPersistToken:
                raise ValueError("job_token phải là BacktestJobPersistToken.")
            if self._job_persistence_factory is None:
                raise ValueError("job_token yêu cầu job_persistence_factory.")
            if job_token.workspace_id != workspace_id:
                raise ValueError("job_token không khớp workspace yêu cầu.")
        if (
            not isinstance(bundle_hash, str)
            or len(bundle_hash) != 64
            or any(char not in "0123456789abcdef" for char in bundle_hash)
        ):
            raise ValueError("bundle_hash phải là SHA-256 hex.")
        if type(manifest) is not ReplayManifest:
            raise ValueError("manifest phải là ReplayManifest đã xác thực.")
        if cancel_requested is not None and cancel_requested():
            raise BacktestReplayExecutionCancelled("Replay đã được yêu cầu hủy.")
        if phase_callback is not None:
            await phase_callback("load")

        async with self._load_unit_of_work_factory(workspace_id) as uow:
            loader = self._bundle_loader_factory(uow)
            bundle = await loader.load_for_replay(
                workspace_id=workspace_id,
                bundle_hash=bundle_hash,
                now=self._now(),
            )
        self._validate_bundle(bundle, workspace_id=workspace_id, bundle_hash=bundle_hash)
        self._validate_manifest(manifest, bundle)

        if cancel_requested is not None and cancel_requested():
            raise BacktestReplayExecutionCancelled("Replay đã được yêu cầu hủy sau bước nạp.")
        if phase_callback is not None:
            await phase_callback("replay")
        comparison = await asyncio.to_thread(
            self._executor,
            bundle,
            manifest,
            progress_callback=progress_callback,
            cancel_requested=cancel_requested,
        )
        self._validate_comparison(comparison, manifest=manifest, bundle=bundle)

        if cancel_requested is not None and cancel_requested():
            raise BacktestReplayExecutionCancelled(
                "Replay đã được yêu cầu hủy trước khi lưu kết quả."
            )
        if phase_callback is not None and job_token is None:
            await phase_callback("persist")
        async with self._persist_unit_of_work_factory(workspace_id) as uow:
            job_persistence: ReplayJobPersistencePort | None = None
            if job_token is not None:
                assert self._job_persistence_factory is not None
                job_persistence = self._job_persistence_factory(uow)
                await job_persistence.begin(job_token)
            loader = self._bundle_loader_factory(uow)
            current_bundle = await loader.revalidate_current_rights(bundle, now=self._now())
            self._validate_same_bundle(bundle, current_bundle)
            if cancel_requested is not None and cancel_requested():
                raise BacktestReplayExecutionCancelled(
                    "Replay đã được yêu cầu hủy trước transaction lưu kết quả."
                )
            repository = self._outcome_repository_factory(uow)
            persisted: list[BacktestOutcomeAppendResult] = []
            for run in comparison.runs:
                persisted.append(
                    await repository.append_sensitivity_run(
                        workspace_id=workspace_id,
                        sensitivity_run=run.sensitivity_run,
                        trade_outcomes=run.trade_outcomes,
                        dataset_bundle=current_bundle,
                    )
                )
            outcomes = tuple(persisted)
            if job_persistence is not None and job_token is not None:
                await job_persistence.complete(
                    job_token, tuple(outcome.run_id for outcome in outcomes)
                )

        return BacktestDatasetReplayExecutionResult(
            workspace_id=workspace_id,
            bundle_hash=bundle.dataset_hash,
            outcomes=outcomes,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock phải trả datetime có múi giờ.")
        return value

    @staticmethod
    def _validate_bundle(
        bundle: BacktestDatasetReplayBundle, *, workspace_id: UUID, bundle_hash: str
    ) -> None:
        if type(bundle) is not BacktestDatasetReplayBundle:
            raise ValueError("Bundle loader trả kết quả không hợp lệ.")
        if bundle.workspace_id != workspace_id or bundle.dataset_hash != bundle_hash:
            raise ValueError("Bundle loader trả sai workspace hoặc hash request.")

    @staticmethod
    def _validate_manifest(manifest: ReplayManifest, bundle: BacktestDatasetReplayBundle) -> None:
        if (
            manifest.dataset_hash != bundle.dataset_hash
            or manifest.range_start != bundle.evaluation_start
            or manifest.range_end != bundle.range_end
        ):
            raise ValueError("ReplayManifest không khớp bundle hoặc evaluation window.")

    @classmethod
    def _validate_comparison(
        cls,
        comparison: RuntimePaperReplayComparison,
        *,
        manifest: ReplayManifest,
        bundle: BacktestDatasetReplayBundle,
    ) -> None:
        if type(comparison) is not RuntimePaperReplayComparison:
            raise ValueError("Executor phải trả RuntimePaperReplayComparison.")
        base_runs = [run for run in comparison.runs if run.sensitivity_run.scenario.kind == "BASE"]
        if len(base_runs) != 1 or base_runs[0].sensitivity_run.manifest != manifest:
            raise ValueError("Comparison phải có đúng run BASE ghim ReplayManifest request.")
        for run in comparison.runs:
            child_manifest = run.sensitivity_run.manifest
            cls._validate_manifest(child_manifest, bundle)

    @staticmethod
    def _validate_same_bundle(
        original: BacktestDatasetReplayBundle, refreshed: BacktestDatasetReplayBundle
    ) -> None:
        if type(refreshed) is not BacktestDatasetReplayBundle:
            raise ValueError("Rights revalidation không trả bundle hợp lệ.")
        if (
            refreshed.workspace_id != original.workspace_id
            or refreshed.dataset_hash != original.dataset_hash
            or BacktestDatasetReplayBundleReference.from_bundle(refreshed)
            != BacktestDatasetReplayBundleReference.from_bundle(original)
        ):
            raise ValueError("Rights revalidation làm đổi workspace, scope hoặc hash bundle.")
        if any(component.current_rights.checked_at is None for component in refreshed.components):
            raise PermissionError("Rights revalidation thiếu checked_at hiện hành.")


__all__ = [
    "BacktestDatasetReplayExecutionResult",
    "BacktestReplayExecutionCancelled",
    "BacktestDatasetReplayExecutionService",
    "BundleLoaderFactory",
    "Clock",
    "OutcomeRepositoryFactory",
    "ReplayBundleExecutionPort",
    "ReplayExecutor",
    "ReplayOutcomeExecutionPort",
    "ReplayExecutionUnitOfWork",
    "UnitOfWorkFactory",
]

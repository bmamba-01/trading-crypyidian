"""Token quyền persist job và port dùng cùng transaction ghi outcome."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from trading_bot.application.backtest_dataset_replay_execution import ReplayExecutionUnitOfWork


@dataclass(frozen=True, slots=True)
class BacktestJobPersistToken:
    """Định danh workspace/job/worker/generation bất biến sau khi claim."""

    workspace_id: UUID
    job_id: UUID
    worker_id: str
    generation: int


class ReplayJobPersistencePort(Protocol):
    """Capability giữ khóa và hoàn tất job trên transaction caller hiện hành."""

    async def begin(self, token: BacktestJobPersistToken) -> None: ...

    async def complete(
        self, token: BacktestJobPersistToken, result_run_ids: tuple[UUID, ...]
    ) -> None: ...


JobPersistenceFactory = Callable[["ReplayExecutionUnitOfWork"], ReplayJobPersistencePort]

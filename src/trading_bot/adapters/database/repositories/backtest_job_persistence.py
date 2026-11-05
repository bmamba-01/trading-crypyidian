"""Giữ quyền persist job bằng capability trên chính transaction ghi outcome."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class BacktestJobPersistenceRepository:
    """Không mở session hoặc commit; caller giữ khóa đến commit hay rollback."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def begin(
        self, *, workspace_id: UUID, job_id: UUID, worker_id: str, generation: int
    ) -> None:
        """Khóa job và kiểm lease bằng đồng hồ database trước khi ghi kết quả."""
        await self._session.execute(
            text(
                "SELECT public.begin_backtest_job_persist("
                ":workspace_id, :job_id, :worker_id, :generation)"
            ),
            {
                "workspace_id": workspace_id,
                "job_id": job_id,
                "worker_id": worker_id,
                "generation": generation,
            },
        )

    async def complete(
        self,
        *,
        workspace_id: UUID,
        job_id: UUID,
        worker_id: str,
        generation: int,
        result_run_ids: tuple[UUID, ...],
    ) -> None:
        """Hoàn tất job cùng transaction outcome; từ chối ID chưa có kiểu UUID."""
        if (
            not isinstance(result_run_ids, tuple)
            or not result_run_ids
            or any(not isinstance(run_id, UUID) for run_id in result_run_ids)
        ):
            raise ValueError("Danh sách kết quả phải có ít nhất một UUID trong tuple.")
        await self._session.execute(
            text(
                "SELECT public.complete_backtest_job_persist("
                ":workspace_id, :job_id, :worker_id, :generation, CAST(:result_run_ids AS uuid[]))"
            ),
            {
                "workspace_id": workspace_id,
                "job_id": job_id,
                "worker_id": worker_id,
                "generation": generation,
                "result_run_ids": list(result_run_ids),
            },
        )

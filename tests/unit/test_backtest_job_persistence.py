"""Hợp đồng capability phải bind tham số và giữ transaction của caller."""

from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

from trading_bot.adapters.database.repositories.backtest_job_persistence import (
    BacktestJobPersistenceRepository,
)

WORKSPACE_ID = UUID(int=701)
JOB_ID = UUID(int=702)
RUN_ID = UUID(int=703)


class RecordingSession:
    """Chỉ mô phỏng ranh giới SQL; không có commit để bắt tự commit sai hợp đồng."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[TextClause, dict[str, Any]]] = []
        self.error = error

    async def execute(self, statement: TextClause, parameters: dict[str, Any]) -> None:
        self.calls.append((statement, parameters))
        if self.error is not None:
            raise self.error


async def test_begin_and_complete_bind_identity_using_caller_session() -> None:
    session = RecordingSession()
    repository = BacktestJobPersistenceRepository(cast(AsyncSession, session))
    worker_id = "worker'; SELECT 1; --"
    identity: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "job_id": JOB_ID,
        "worker_id": worker_id,
        "generation": 4,
    }
    await repository.begin(**identity)
    await repository.complete(**identity, result_run_ids=(RUN_ID,))
    assert len(session.calls) == 2
    for statement, parameters in session.calls:
        assert isinstance(statement, TextClause)
        assert worker_id not in str(statement)
        assert parameters.items() >= identity.items()
    assert "public.begin_backtest_job_persist" in str(session.calls[0][0])
    assert "public.complete_backtest_job_persist" in str(session.calls[1][0])
    assert session.calls[1][1]["result_run_ids"] == [RUN_ID]


async def test_begin_error_propagates_without_completing_job() -> None:
    session = RecordingSession(ValueError("WP07C_PERSIST_OWNER_INVALID"))
    repository = BacktestJobPersistenceRepository(cast(AsyncSession, session))
    with pytest.raises(ValueError, match="WP07C_PERSIST_OWNER_INVALID"):
        await repository.begin(
            workspace_id=WORKSPACE_ID, job_id=JOB_ID, worker_id="worker", generation=4
        )
    assert len(session.calls) == 1


@pytest.mark.parametrize("invalid_ids", [(), (str(RUN_ID),), [RUN_ID], (None,), (1,)])
async def test_complete_rejects_non_uuid_tuple_before_executing_sql(invalid_ids: Any) -> None:
    session = RecordingSession()
    repository = BacktestJobPersistenceRepository(cast(AsyncSession, session))
    with pytest.raises(ValueError, match="UUID"):
        await repository.complete(
            workspace_id=WORKSPACE_ID,
            job_id=JOB_ID,
            worker_id="worker",
            generation=4,
            result_run_ids=invalid_ids,
        )
    assert session.calls == []

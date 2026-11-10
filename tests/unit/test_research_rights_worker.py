import asyncio
import logging
from uuid import UUID

import pytest

from trading_bot.workers.research_rights_worker import ResearchRightsMaintenanceWorker

WORKSPACE_ID = UUID("33333333-3333-3333-3333-333333333333")


class _RetryingWorker(ResearchRightsMaintenanceWorker):
    def __init__(self) -> None:
        super().__init__(
            session_factory=object(),  # type: ignore[arg-type]
            workspace_id=WORKSPACE_ID,
            worker_id="rights-test-worker",
            tick_seconds=0.001,
        )
        self.tick_count = 0
        self.second_tick = asyncio.Event()

    async def run_once(self):
        self.tick_count += 1
        if self.tick_count == 1:
            raise RuntimeError("private outbox payload must not be logged")
        self.second_tick.set()


class _CountingWorker(ResearchRightsMaintenanceWorker):
    def __init__(self) -> None:
        super().__init__(
            session_factory=object(),  # type: ignore[arg-type]
            workspace_id=WORKSPACE_ID,
            worker_id="rights-test-worker",
            tick_seconds=0.001,
        )
        self.tick_count = 0

    async def run_once(self):
        self.tick_count += 1


@pytest.mark.asyncio
async def test_worker_retries_failed_tick_without_logging_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = _RetryingWorker()
    stopping = asyncio.Event()
    caplog.set_level(logging.WARNING, logger="trading_bot.workers.research_rights_worker")

    task = asyncio.create_task(worker.run(stopping))
    await asyncio.wait_for(worker.second_tick.wait(), timeout=0.2)
    stopping.set()
    await asyncio.wait_for(task, timeout=0.2)

    assert worker.tick_count == 2
    assert "RuntimeError" in caplog.text
    assert "private outbox payload" not in caplog.text


@pytest.mark.asyncio
async def test_worker_does_not_start_a_tick_after_shutdown_requested() -> None:
    worker = _CountingWorker()
    stopping = asyncio.Event()
    stopping.set()

    await worker.run(stopping)

    assert worker.tick_count == 0

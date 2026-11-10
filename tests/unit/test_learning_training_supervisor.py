from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_bot.workers.learning_training_supervisor import LearningTrainingJobSupervisor


@pytest.mark.asyncio
async def test_supervisor_backs_off_after_failures_and_resets_after_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace_id = uuid4()
    stopping = asyncio.Event()
    delays: list[float] = []

    class _Worker:
        def __init__(self) -> None:
            self.responses = ["failure", "failure", "idle", "failure"]

        async def work_once(self, **_: object) -> SimpleNamespace:
            response = self.responses.pop(0)
            if response == "failure":
                raise RuntimeError("private database payload")
            return SimpleNamespace(status=response)

    async def wait_for_stop(event: asyncio.Event, timeout: float) -> None:
        delays.append(timeout)
        if len(delays) == 4:
            event.set()

    caplog.set_level(logging.WARNING, logger="trading_bot.workers.learning_training_supervisor")
    supervisor = LearningTrainingJobSupervisor(
        worker=_Worker(),
        workspace_id=workspace_id,
        worker_id="trainer-supervisor-test",
        poll_seconds=5,
        max_backoff_seconds=60,
        wait_for_stop=wait_for_stop,
    )

    await supervisor.run(stopping)

    assert delays == [5, 10, 5, 5]
    assert "RuntimeError" in caplog.text
    assert "private database payload" not in caplog.text


@pytest.mark.asyncio
async def test_supervisor_immediately_drains_jobs_until_queue_is_idle() -> None:
    stopping = asyncio.Event()
    delays: list[float] = []

    class _Worker:
        def __init__(self) -> None:
            self.calls = 0

        async def work_once(self, **_: object) -> SimpleNamespace:
            self.calls += 1
            if self.calls == 3:
                stopping.set()
            return SimpleNamespace(status="succeeded")

    async def wait_for_stop(event: asyncio.Event, timeout: float) -> None:
        delays.append(timeout)
        event.set()

    worker = _Worker()
    supervisor = LearningTrainingJobSupervisor(
        worker=worker,
        workspace_id=uuid4(),
        worker_id="trainer-drain-test",
        poll_seconds=5,
        max_backoff_seconds=60,
        wait_for_stop=wait_for_stop,
    )

    await supervisor.run(stopping)

    assert worker.calls == 3
    assert delays == []

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_bot.workers.learning_shadow_observer import LearningShadowObserverSupervisor


@pytest.mark.asyncio
async def test_supervisor_backs_off_after_errors_and_resets_after_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace_id = uuid4()
    stopping = asyncio.Event()
    delays: list[float] = []

    class _Application:
        def __init__(self) -> None:
            self.responses = ["error", "error", "empty", "error"]

        async def observe_pending(self, **_: object) -> SimpleNamespace:
            response = self.responses.pop(0)
            if response == "error":
                raise RuntimeError("private database payload")
            return SimpleNamespace(created_count=0, scanned_count=0)

    async def wait_for_stop(event: asyncio.Event, timeout: float) -> None:
        delays.append(timeout)
        if len(delays) == 4:
            event.set()

    caplog.set_level(logging.WARNING, logger="trading_bot.workers.learning_shadow_observer")
    supervisor = LearningShadowObserverSupervisor(
        application=_Application(),
        workspace_id=workspace_id,
        poll_seconds=5,
        max_backoff_seconds=60,
        wait_for_stop=wait_for_stop,
    )

    await supervisor.run(stopping)

    assert delays == [5, 10, 5, 5]
    assert "RuntimeError" in caplog.text
    assert "private database payload" not in caplog.text


@pytest.mark.asyncio
async def test_supervisor_drains_full_batches_and_waits_after_no_progress() -> None:
    stopping = asyncio.Event()
    delays: list[float] = []

    class _Application:
        def __init__(self) -> None:
            self.calls = 0

        async def observe_pending(self, **_: object) -> SimpleNamespace:
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(created_count=2, scanned_count=2)
            return SimpleNamespace(created_count=0, scanned_count=0)

    async def wait_for_stop(event: asyncio.Event, timeout: float) -> None:
        delays.append(timeout)
        if len(delays) == 1:
            event.set()

    application = _Application()
    supervisor = LearningShadowObserverSupervisor(
        application=application,
        workspace_id=uuid4(),
        poll_seconds=5,
        max_backoff_seconds=30,
        batch_size=2,
        wait_for_stop=wait_for_stop,
    )

    await supervisor.run(stopping)

    assert application.calls == 2
    assert delays == [5]

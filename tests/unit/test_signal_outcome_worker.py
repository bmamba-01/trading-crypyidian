"""CRY-430: worker chấm kết quả tín hiệu định kỳ."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from trading_bot.application.signal_outcomes import SignalOutcomeScorer, SignalOutcomeTickResult
from trading_bot.workers.signal_outcome_worker import SignalOutcomeWorker

WORKSPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


class _Scorer:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[UUID, datetime]] = []
        self.fail = fail

    async def score_once(self, *, workspace_id: UUID, now: datetime) -> SignalOutcomeTickResult:
        self.calls.append((workspace_id, now))
        if self.fail:
            raise RuntimeError("db down")
        return SignalOutcomeTickResult(bots=1, decisions_examined=2, scored=3)


@pytest.mark.asyncio
async def test_tick_passes_workspace_and_utc_now() -> None:
    scorer = _Scorer()
    worker = SignalOutcomeWorker(scorer, workspace_id=WORKSPACE, now=lambda: NOW)
    result = await worker.tick()
    assert result.scored == 3
    assert scorer.calls == [(WORKSPACE, NOW)]


@pytest.mark.asyncio
async def test_run_records_safe_error_code_and_stops() -> None:
    scorer = _Scorer(fail=True)
    worker = SignalOutcomeWorker(scorer, workspace_id=WORKSPACE, tick_seconds=1, now=lambda: NOW)
    stopping = asyncio.Event()
    task = asyncio.create_task(worker.run(stopping))
    await asyncio.sleep(0.05)
    stopping.set()
    await asyncio.wait_for(task, timeout=2)
    assert worker.last_tick_succeeded is False
    assert worker.last_error_code == "SIGNAL_OUTCOME_TICK_RUNTIMEERROR"


def test_rejects_bad_configuration() -> None:
    with pytest.raises(ValueError, match="workspace_id"):
        SignalOutcomeWorker(_Scorer(), workspace_id="x", tick_seconds=60)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="tick_seconds"):
        SignalOutcomeWorker(_Scorer(), workspace_id=WORKSPACE, tick_seconds=0.5)
    with pytest.raises(ValueError, match="batch_limit"):
        SignalOutcomeScorer(object(), batch_limit=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="horizons"):
        SignalOutcomeScorer(object(), horizons=(0,))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_scorer_rejects_naive_now() -> None:
    scorer = SignalOutcomeScorer(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="múi giờ"):
        await scorer.score_once(
            workspace_id=WORKSPACE,
            now=datetime(2026, 11, 17),  # noqa: DTZ001 - cố ý thiếu múi giờ
        )

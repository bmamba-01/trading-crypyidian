from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest

from trading_bot.application.market_signal_scanner import MarketSignalScanResult
from trading_bot.workers.market_signal_producer import MarketSignalProducerWorker

WORKSPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
NOW = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)


@dataclass
class _Scanner:
    calls: list[tuple[UUID, datetime]]
    stopping: asyncio.Event | None = None

    async def scan_once(self, *, workspace_id: UUID, now: datetime):
        self.calls.append((workspace_id, now))
        if self.stopping is not None:
            self.stopping.set()
        return MarketSignalScanResult((), 0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_market_signal_worker_runs_independent_scanner_tick() -> None:
    scanner = _Scanner([])
    worker = MarketSignalProducerWorker(
        scanner,
        workspace_id=WORKSPACE,
        tick_seconds=5,
        now=lambda: NOW,
    )

    result = await worker.tick()

    assert result == MarketSignalScanResult((), 0, 0, 0, 0, 0, 0)
    assert scanner.calls == [(WORKSPACE, NOW)]


@pytest.mark.asyncio
async def test_market_signal_worker_readiness_tracks_tick_success_and_failure() -> None:
    scanner = _Scanner([])
    worker = MarketSignalProducerWorker(
        scanner,
        workspace_id=WORKSPACE,
        tick_seconds=5,
        now=lambda: NOW,
    )
    stopping = asyncio.Event()
    scanner.stopping = stopping
    await worker.run(stopping)

    assert worker.last_tick_succeeded
    assert worker.last_tick_at == NOW
    assert worker.last_error_code is None


@pytest.mark.asyncio
async def test_market_signal_worker_records_safe_error_code_and_keeps_running() -> None:
    class BrokenScanner:
        async def scan_once(self, *, workspace_id: UUID, now: datetime):
            raise RuntimeError("contains no secret in emitted code")

    worker = MarketSignalProducerWorker(
        BrokenScanner(),
        workspace_id=WORKSPACE,
        tick_seconds=5,
        now=lambda: NOW,
    )
    stopping = asyncio.Event()

    async def stop_after_error(*, workspace_id: UUID, now: datetime):
        stopping.set()
        raise RuntimeError("contains no secret in emitted code")

    worker._scanner = type("Broken", (), {"scan_once": staticmethod(stop_after_error)})()
    await worker.run(stopping)

    assert not worker.last_tick_succeeded
    assert worker.last_tick_at is None
    assert worker.last_error_code == "MARKET_SIGNAL_TICK_RUNTIMEERROR"

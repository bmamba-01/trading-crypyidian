"""Unit test readiness reason-coded cho producer, sender, backfill worker và admin-web (CRY-375)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.config.settings import AppSettings
from trading_bot.observability.readiness import (
    WorkerReadinessState,
    evaluate_worker_readiness,
)
from trading_bot.workers.backtest_replay_worker import BacktestReplayJobWorker
from trading_bot.workers.market_signal_producer import MarketSignalProducerWorker
from trading_bot.workers.telegram_signal_worker import TelegramSignalDeliveryWorker

WORKSPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


async def _db_ok() -> bool:
    return True


async def _db_down() -> bool:
    return False


def test_evaluate_worker_readiness_reason_codes() -> None:
    assert (
        evaluate_worker_readiness(
            component="telegram_signal_worker",
            worker=None,
            missing_secret_reason="TELEGRAM_TOKEN_MISSING",
        ).reason_code
        == "TELEGRAM_TOKEN_MISSING"
    )
    assert (
        evaluate_worker_readiness(component="market_signal_worker", worker=None).reason_code
        == "WORKER_NOT_STARTED"
    )

    class _DoneTask:
        def done(self) -> bool:
            return True

    class _RunningTask:
        def done(self) -> bool:
            return False

    class _Worker:
        last_tick_succeeded = False
        last_error_code = "SOURCE_RIGHTS_UNAVAILABLE"
        last_tick_at = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)

    stopped = evaluate_worker_readiness(
        component="market_signal_worker",
        worker=_Worker(),
        task=_DoneTask(),
    )
    assert stopped.state is WorkerReadinessState.NOT_READY
    assert stopped.reason_code == "SOURCE_RIGHTS_UNAVAILABLE"

    not_ticked = evaluate_worker_readiness(
        component="market_signal_worker",
        worker=type(
            "_NoTick",
            (),
            {"last_tick_succeeded": False, "last_error_code": None, "last_tick_at": None},
        )(),
        task=_RunningTask(),
    )
    assert not_ticked.state is WorkerReadinessState.NOT_READY
    assert not_ticked.reason_code == "NO_SUCCESSFUL_TICK"

    degraded = evaluate_worker_readiness(
        component="backtest_worker",
        worker=type(
            "_Ok",
            (),
            {
                "last_tick_succeeded": True,
                "last_error_code": None,
                "last_tick_at": datetime(2026, 11, 18, 9, 0, tzinfo=UTC),
            },
        )(),
        task=_RunningTask(),
        missing_rights_reason="BACKFILL_RIGHTS_DENIED",
    )
    assert degraded.state is WorkerReadinessState.DEGRADED
    assert degraded.reason_code == "BACKFILL_RIGHTS_DENIED"

    stale = evaluate_worker_readiness(
        component="market_signal_worker",
        worker=type(
            "_Stale",
            (),
            {
                "last_tick_succeeded": True,
                "last_error_code": None,
                "last_tick_at": datetime(2026, 11, 18, 9, 0, tzinfo=UTC),
            },
        )(),
        task=_RunningTask(),
        max_tick_age=timedelta(minutes=5),
        now=datetime(2026, 11, 18, 9, 10, tzinfo=UTC),
    )
    assert stale.state is WorkerReadinessState.DEGRADED
    assert stale.reason_code == "TICK_STALE"


def test_workers_expose_readiness_snapshot_methods() -> None:
    scanner = MagicMock()
    producer = MarketSignalProducerWorker(scanner, workspace_id=WORKSPACE)
    assert producer.readiness_snapshot().reason_code == "NO_SUCCESSFUL_TICK"
    producer.last_tick_succeeded = True
    producer.last_tick_at = datetime.now(UTC)
    assert producer.readiness_snapshot().state is WorkerReadinessState.READY

    sender_worker = TelegramSignalDeliveryWorker(
        session_factory=MagicMock(),
        workspace_id=WORKSPACE,
        sender=MagicMock(),
        worker_id="sig-1",
    )
    assert sender_worker.readiness_snapshot().reason_code == "NO_SUCCESSFUL_TICK"
    assert (
        sender_worker.readiness_snapshot(missing_secret_reason="TELEGRAM_TOKEN_MISSING").reason_code
        == "TELEGRAM_TOKEN_MISSING"
    )

    backtest_worker = BacktestReplayJobWorker(
        read_sessions=MagicMock(),
        persist_sessions=MagicMock(),
        workspace_id=WORKSPACE,
    )
    assert backtest_worker.readiness_snapshot().reason_code == "NO_SUCCESSFUL_TICK"
    backtest_worker.last_tick_succeeded = True
    assert backtest_worker.readiness_snapshot().state is WorkerReadinessState.READY


@pytest.mark.asyncio
async def test_market_signal_worker_readyz_returns_reason_codes_when_not_ready() -> None:
    class _FailingWorker:
        last_tick_succeeded = False
        last_error_code = "MARKET_SIGNAL_TICK_RUNTIMEERROR"

        async def run(self, stopping: asyncio.Event) -> None:
            await stopping.wait()

    app = create_app(
        AppSettings(
            app_env="test",
            process_role="market-signal-worker",
            market_signal_workspace_id=WORKSPACE,
        ),
        session_factory=object(),  # type: ignore[arg-type]
        database_probe=_db_down,
        market_signal_worker_factory=lambda **_: _FailingWorker(),
    )

    with TestClient(app) as client:
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "not_ready"
        assert body["reasons"]["database"] == "DATABASE_UNAVAILABLE"
        assert body["reasons"]["market_signal_worker"] == "MARKET_SIGNAL_TICK_RUNTIMEERROR"


@pytest.mark.asyncio
async def test_signal_worker_readyz_reports_missing_token_file_reason(tmp_path) -> None:
    token_file = tmp_path / "telegram_token"
    token_file.write_text("12345:TEST_TOKEN", encoding="utf-8")

    class _OkWorker:
        last_tick_succeeded = True

        async def run(self, stopping: asyncio.Event) -> None:
            await stopping.wait()

    app = create_app(
        AppSettings(
            app_env="test",
            process_role="signal-worker",
            signal_workspace_id=WORKSPACE,
            telegram_bot_token_file=token_file,
        ),
        session_factory=object(),  # type: ignore[arg-type]
        database_probe=_db_ok,
        signal_worker_factory=lambda **_: _OkWorker(),
    )

    with TestClient(app) as client:
        token_file.unlink()
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["reasons"]["telegram_signal_worker"] == "TELEGRAM_TOKEN_MISSING"


@pytest.mark.asyncio
async def test_backtest_worker_readyz_reports_reason_codes_and_ready(tmp_path) -> None:
    pw_file = tmp_path / "replay_pw"
    pw_file.write_text("dummy", encoding="utf-8")
    app = create_app(
        AppSettings(
            app_env="test",
            process_role="backtest-worker",
            database_user="crypidian_backtest_worker",
            database_password_file=pw_file,
            backtest_worker_workspace_id=WORKSPACE,
            backtest_replay_database_user="crypidian_replay_writer",
            backtest_replay_database_password_file=pw_file,
        ),
        session_factory=object(),  # type: ignore[arg-type]
        database_probe=_db_ok,
    )
    with TestClient(app) as client:
        not_ready = client.get("/health/ready")
        assert not_ready.status_code == 503
        assert not_ready.json()["reasons"]["backtest_worker"] == "WORKER_NOT_STARTED"

        worker = BacktestReplayJobWorker(
            read_sessions=MagicMock(),
            persist_sessions=MagicMock(),
            workspace_id=WORKSPACE,
        )
        worker.last_tick_succeeded = True
        task = AsyncMock()
        task.done = MagicMock(return_value=False)
        app.state.backtest_worker = worker
        app.state.backtest_worker_task = task

        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}

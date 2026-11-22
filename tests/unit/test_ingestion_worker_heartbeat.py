from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from trading_bot.admin.web_router import _collect_worker_readiness
from trading_bot.application.ingestion_worker_heartbeat import (
    build_heartbeat_snapshot,
    heartbeat_to_readiness_report,
)
from trading_bot.observability.readiness import (
    WorkerReadinessReport,
    WorkerReadinessState,
)

T0 = datetime(2026, 11, 22, 3, 0, tzinfo=UTC)


def test_snapshot_captures_safe_counters_and_last_successful_tick() -> None:
    worker = SimpleNamespace(
        is_healthy=True,
        last_error_code=None,
        last_tick_at=T0 - timedelta(seconds=3),
        bars_appended=27,
        events_written=8,
        reconnect_count=2,
    )

    snapshot = build_heartbeat_snapshot(
        component="live_market_ingestion",
        worker=worker,
        task_active=True,
        now=T0,
    )

    assert snapshot.state is WorkerReadinessState.READY
    assert snapshot.heartbeat_at == T0
    assert snapshot.last_tick_at == T0 - timedelta(seconds=3)
    assert snapshot.counters == {"bars_appended": 27, "events_written": 8, "reconnect_count": 2}


def test_snapshot_reports_stopped_or_degraded_workers_without_exposing_errors() -> None:
    stopped = build_heartbeat_snapshot(
        component="live_market_ingestion",
        worker=SimpleNamespace(is_healthy=True, last_error_code=None, reconnect_count=0),
        task_active=False,
        now=T0,
    )
    degraded = build_heartbeat_snapshot(
        component="live_market_events",
        worker=SimpleNamespace(
            is_healthy=False,
            last_error_code="LIVE_INGEST_CONNECTIONRESETERROR",
            events_written=3,
            reconnect_count=1,
        ),
        task_active=True,
        now=T0,
    )

    assert stopped.state is WorkerReadinessState.NOT_READY
    assert stopped.reason_code == "WORKER_TASK_STOPPED"
    assert degraded.state is WorkerReadinessState.DEGRADED
    assert degraded.reason_code == "LIVE_INGEST_CONNECTIONRESETERROR"
    assert "ConnectionResetError" not in (degraded.detail_vi or "")


def test_stale_persisted_heartbeat_becomes_not_ready_and_keeps_diagnostics() -> None:
    report = heartbeat_to_readiness_report(
        {
            "component": "live_market_ingestion",
            "state": "ready",
            "reason_code": None,
            "detail_vi": None,
            "last_tick_at": T0 - timedelta(minutes=1),
            "heartbeat_at": T0 - timedelta(seconds=31),
            "metadata": {"bars_appended": 10, "reconnect_count": 2},
        },
        now=T0,
        stale_after=timedelta(seconds=15),
    )

    assert report.state is WorkerReadinessState.NOT_READY
    assert report.reason_code == "WORKER_HEARTBEAT_STALE"
    payload = report.to_dict()
    assert payload["last_tick_at"] == (T0 - timedelta(minutes=1)).isoformat()
    assert payload["metadata"]["bars_appended"] == 10
    assert payload["metadata"]["reconnect_count"] == 2
    assert payload["metadata"]["heartbeat_stale"] is True


def test_fresh_persisted_heartbeat_preserves_worker_state() -> None:
    report = heartbeat_to_readiness_report(
        {
            "component": "live_market_quotes",
            "state": "degraded",
            "reason_code": "LIVE_INGEST_TIMEOUTERROR",
            "detail_vi": "Worker đang nối lại.",
            "last_tick_at": T0 - timedelta(seconds=4),
            "heartbeat_at": T0 - timedelta(seconds=2),
            "metadata": {"quotes_written": 5, "reconnect_count": 1},
        },
        now=T0,
        stale_after=timedelta(seconds=15),
    )

    assert report.state is WorkerReadinessState.DEGRADED
    assert report.reason_code == "LIVE_INGEST_TIMEOUTERROR"
    assert report.detail_vi == "Worker đang nối lại."
    assert report.metadata == {
        "quotes_written": 5,
        "reconnect_count": 1,
        "heartbeat_at": (T0 - timedelta(seconds=2)).isoformat(),
        "heartbeat_stale": False,
    }


@pytest.mark.asyncio
async def test_admin_readiness_merges_database_heartbeats_with_local_reports() -> None:
    workspace_id = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
    app = FastAPI()
    app.state.worker_readiness_provider = lambda: [{"component": "local_worker", "state": "ready"}]

    class FakeReader:
        async def list_readiness(self, *, workspace_id: UUID) -> list[WorkerReadinessReport]:
            assert workspace_id == UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
            return [
                WorkerReadinessReport(
                    component="live_market_ingestion",
                    state=WorkerReadinessState.READY,
                    last_tick_at=T0,
                    metadata={"bars_appended": 27, "reconnect_count": 2},
                )
            ]

    app.state.worker_heartbeat_reader = FakeReader()
    request = Request({"type": "http", "app": app})

    reports = await _collect_worker_readiness(request, workspace_id=workspace_id)

    assert [item["component"] for item in reports] == [
        "local_worker",
        "live_market_ingestion",
    ]
    assert reports[1]["metadata"]["reconnect_count"] == 2

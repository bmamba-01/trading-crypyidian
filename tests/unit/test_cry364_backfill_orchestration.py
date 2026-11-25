"""Unit tests cho backfill orchestration BACKTEST_RESEARCH → manifest → bundle (CRY-364)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.application.backfill_orchestration import (
    BackfillDatasetOrchestrationService,
    BackfillJobStatus,
    BackfillOrchestrationError,
    StoredBarPoint,
    compute_backfill_quality_report,
)
from trading_bot.cli.backfill_orchestration import run_cli

START = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


def _bars_hourly(count: int, *, skip_indices: set[int] | None = None) -> list[StoredBarPoint]:
    skip = skip_indices or set()
    return [
        StoredBarPoint(
            open_time=START + timedelta(hours=i),
            close_price=f"{90000 + i * 10}",
            volume="125.5",
            revision=1,
        )
        for i in range(count)
        if i not in skip
    ]


def test_default_scope_and_locked_live_connector_guard() -> None:
    """Phạm vi mặc định chỉ nhận BTCUSDT/ETHUSDT và khóa connector live khi chưa có policy."""
    svc = BackfillDatasetOrchestrationService()
    ws = uuid4()

    with pytest.raises(BackfillOrchestrationError) as exc_sym:
        svc.create_job(
            workspace_id=ws,
            symbol="DOGEUSDT",
            interval="1h",
            range_start=START,
            range_end=START + timedelta(hours=4),
            created_by="op-1",
        )
    assert exc_sym.value.reason_code == "SYMBOL_OUT_OF_SCOPE"

    with pytest.raises(BackfillOrchestrationError) as exc_conn:
        svc.create_job(
            workspace_id=ws,
            symbol="BTCUSDT",
            interval="1h",
            range_start=START,
            range_end=START + timedelta(hours=4),
            created_by="op-1",
            connector_enabled=True,
            policy_verified=False,
        )
    assert exc_conn.value.reason_code == "BACKFILL_LIVE_CONNECTOR_LOCKED"


def test_quality_report_detects_gaps_duplicates_and_complete_coverage() -> None:
    """Tính chính xác coverage %, khoảng trống gap, duplicate count và bars_content_hash."""
    full_bars = _bars_hourly(6)
    full_rep = compute_backfill_quality_report(
        full_bars,
        symbol="BTCUSDT",
        interval="1h",
        range_start=START,
        range_end=START + timedelta(hours=6),
    )
    assert full_rep.is_complete is True
    assert full_rep.coverage_pct == 100.0
    assert full_rep.gaps == ()
    assert len(full_rep.bars_content_hash) == 64

    # Thiếu nến giờ 2 & 3, và có 1 bản ghi duplicate/revision=2 ở giờ 4
    gapped_bars = _bars_hourly(6, skip_indices={2, 3})
    gapped_bars.append(
        StoredBarPoint(
            open_time=START + timedelta(hours=4),
            close_price="90045",
            volume="130.0",
            revision=2,
        )
    )
    gap_rep = compute_backfill_quality_report(
        gapped_bars,
        symbol="BTCUSDT",
        interval="1h",
        range_start=START,
        range_end=START + timedelta(hours=6),
    )
    assert gap_rep.is_complete is False
    assert gap_rep.observed_bars == 4
    assert gap_rep.expected_bars == 6
    assert len(gap_rep.gaps) == 1
    assert gap_rep.gaps[0].missing_bars == 2
    assert gap_rep.duplicate_count == 1


def test_job_lifecycle_create_pause_resume_ready_cancel_and_cli() -> None:
    """Kiểm vòng đời job (create -> pause -> resume -> READY -> export) và CLI."""
    svc = BackfillDatasetOrchestrationService()
    ws = uuid4()

    job = svc.create_job(
        workspace_id=ws,
        symbol="ETHUSDT",
        interval="1h",
        range_start=START,
        range_end=START + timedelta(hours=4),
        created_by="owner-1",
    )
    assert job.status is BackfillJobStatus.QUEUED
    assert job.connector_enabled is False

    paused = svc.pause_job(job.job_id)
    assert paused.status is BackfillJobStatus.PAUSED

    ready = svc.run_or_resume_job(
        job.job_id,
        stored_bars=_bars_hourly(4),
        policy_fingerprint="p" * 64,
    )
    assert ready.status is BackfillJobStatus.READY
    assert ready.progress_pct == 100.0
    assert ready.manifest_hash is not None
    assert len(ready.manifest_hash) == 64
    assert ready.bundle_hash is not None
    assert len(ready.bundle_hash) == 64

    exported = svc.export_quality_report(job.job_id)
    assert exported["quality_report"]["is_complete"] is True
    assert exported["manifest_hash"] == ready.manifest_hash

    # Tạo job thứ 2 và hủy -> không thể resume sau khi đã CANCELLED
    job2 = svc.create_job(
        workspace_id=ws,
        symbol="BTCUSDT",
        interval="1h",
        range_start=START,
        range_end=START + timedelta(hours=2),
        created_by="owner-1",
    )
    cancelled = svc.cancel_job(job2.job_id, reason="Operator dừng bảo trì")
    assert cancelled.status is BackfillJobStatus.CANCELLED
    with pytest.raises(BackfillOrchestrationError) as exc_can:
        svc.run_or_resume_job(job2.job_id, stored_bars=_bars_hourly(2), policy_fingerprint="p" * 64)
    assert exc_can.value.reason_code == "JOB_CANCELLED"

    # CLI fixture-safe
    cli_out = run_cli(
        [
            "create",
            "--workspace-id",
            str(ws),
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--range-start",
            START.isoformat(),
            "--range-end",
            (START + timedelta(hours=3)).isoformat(),
        ],
        stored_bars=_bars_hourly(3),
        policy_fingerprint="p" * 64,
    )
    assert cli_out["status"] == "READY"
    assert cli_out["connector_enabled"] is False
    assert cli_out["quality_report"]["coverage_pct"] == 100.0

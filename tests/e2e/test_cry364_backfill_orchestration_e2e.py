"""PostgreSQL Compose E2E cho backfill orchestration BACKTEST_RESEARCH (WP-07C / CRY-364).

Chứng minh trên PostgreSQL thật (fixture-only, connector live tắt):
1. Seed `Workspace`, `MarketInstrument` và `MarketBar` (4 nến đầy đủ và 1 cửa sổ thiếu 1 nến).
2. Đọc `MarketBar` thật từ PostgreSQL, chạy `BackfillDatasetOrchestrationService` tạo manifest
   `READY`, `bundle_hash` bất biến và xuất `BackfillQualityReport` phát hiện chính xác gap.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from scripts.docker_image import build_image_reference, ensure_image_available


def test_cry364_backfill_orchestration_from_postgres_stored_bars() -> None:
    """Compose PostgreSQL E2E: đọc market_bar thật -> manifest READY + bundle + quality report."""
    repo_root = Path(__file__).resolve().parents[2]
    compose_file = repo_root / "compose.yaml"
    project_name = f"crypidian-cry364-bfo-{uuid4().hex[:10]}"
    env = os.environ.copy()
    env.setdefault("APP_IMAGE", build_image_reference(repo_root))
    env["CRYPIDIAN_APP_IMAGE"] = env["APP_IMAGE"]
    env["COMPOSE_PROJECT_NAME"] = project_name

    ensure_image_available(repo_root, env["APP_IMAGE"])
    subprocess.run(
        [sys.executable, str(repo_root / "scripts/prepare_local_secrets.py")],
        cwd=repo_root,
        check=True,
    )
    base_cmd = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--project-name",
        project_name,
        "-f",
        str(compose_file),
    ]

    drill_code = """
import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models.entities import Workspace
from trading_bot.adapters.database.models.market_entities import MarketBar, MarketInstrument
from trading_bot.application.backfill_orchestration import (
    BackfillDatasetOrchestrationService,
    BackfillJobStatus,
    StoredBarPoint,
)

async def run_drill() -> None:
    with open("/run/secrets/postgres_password", "r", encoding="utf-8") as fh:
        pw = fh.read().strip()
    url = f"postgresql+psycopg://crypidian_admin:{pw}@postgres:5432/crypidian"
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    ws_id, inst_id = uuid4(), uuid4()
    start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)

    async with sessions() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
            )
            session.add(
                Workspace(
                    id=ws_id,
                    slug=f"bfo-{ws_id.hex[:8]}",
                    name="CRY-364 E2E",
                    mode="PAPER",
                    status="ACTIVE",
                )
            )
            await session.flush()
            session.add(
                MarketInstrument(
                    id=inst_id,
                    workspace_id=ws_id,
                    source="BINANCE_USDM_REST_BACKFILL",
                    idempotency_key="inst-btc-364",
                    symbol="BTCUSDT",
                    base_asset="BTC",
                    quote_asset="USDT",
                    tick_size=Decimal("0.1"),
                    quantity_step=Decimal("0.001"),
                    min_notional=Decimal("5"),
                    status="TRADING",
                    revision=1,
                    checksum="a" * 64,
                    event_at=start,
                    received_at=start,
                    available_at=start,
                )
            )
            await session.flush()
            # Seed 3 nến trong cửa sổ 4 giờ (bỏ thiếu giờ 2 để kiểm gap detection)
            for idx in (0, 1, 3):
                t_open = start + timedelta(hours=idx)
                t_close = t_open + timedelta(hours=1)
                session.add(
                    MarketBar(
                        workspace_id=ws_id,
                        instrument_id=inst_id,
                        symbol="BTCUSDT",
                        source="BINANCE_USDM_REST_BACKFILL",
                        logical_key=f"BTCUSDT:1h:{t_open.isoformat()}",
                        idempotency_key=f"bar-364-{idx}",
                        timeframe="1h",
                        open_time=t_open,
                        close_time=t_close,
                        event_at=t_close,
                        received_at=t_close,
                        available_at=t_close + timedelta(seconds=1),
                        open=Decimal("90000"),
                        high=Decimal("90500"),
                        low=Decimal("89500"),
                        close=Decimal(f"{90100 + idx * 10}"),
                        volume=Decimal("100"),
                        quote_volume=Decimal("9000000"),
                        is_closed=True,
                        revision=1,
                        checksum=f"{idx:064x}",
                    )
                )

    async with sessions() as session:
        await session.execute(
            text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
        )
        rows = (
            await session.scalars(
                select(MarketBar)
                .where(MarketBar.workspace_id == ws_id, MarketBar.instrument_id == inst_id)
                .order_by(MarketBar.open_time)
            )
        ).all()
        stored = [
            StoredBarPoint(
                open_time=r.open_time,
                close_price=str(r.close),
                volume=str(r.volume),
                revision=r.revision,
            )
            for r in rows
        ]

    svc = BackfillDatasetOrchestrationService()
    job = svc.create_job(
        workspace_id=ws_id,
        symbol="BTCUSDT",
        interval="1h",
        range_start=start,
        range_end=start + timedelta(hours=4),
        created_by="e2e-owner",
        connector_enabled=False,
    )
    ready = svc.run_or_resume_job(
        job.job_id,
        stored_bars=stored,
        policy_fingerprint="f" * 64,
    )
    assert ready.status is BackfillJobStatus.READY
    exported = svc.export_quality_report(job.job_id)
    rep = exported["quality_report"]
    assert rep["expected_bars"] == 4
    assert rep["observed_bars"] == 3
    assert rep["coverage_pct"] == 75.0
    assert rep["gap_count"] == 1
    assert rep["gaps"][0]["missing_bars"] == 1
    assert len(exported["manifest_hash"]) == 64
    assert len(exported["bundle_hash"]) == 64

    await engine.dispose()
    print(json.dumps({"ok": True, "coverage": rep["coverage_pct"], "gaps": rep["gap_count"]}))

asyncio.run(run_drill())
"""

    try:
        subprocess.run(
            [*base_cmd, "up", "--detach", "--wait", "--wait-timeout", "180", "postgres"],
            env=env,
            cwd=repo_root,
            check=True,
        )
        subprocess.run(
            [*base_cmd, "run", "--rm", "--no-deps", "db-provisioner"],
            env=env,
            cwd=repo_root,
            check=True,
        )
        subprocess.run(
            [*base_cmd, "run", "--rm", "--no-deps", "-T", "migrator", "alembic", "upgrade", "head"],
            env=env,
            cwd=repo_root,
            check=True,
        )
        result = subprocess.run(
            [
                *base_cmd,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "-v",
                f"{repo_root / '.secrets'}:/run/secrets:ro",
                "-v",
                f"{repo_root / 'src/trading_bot'}:/app/src/trading_bot:ro",
                "migrator",
                "python",
                "-c",
                drill_code,
            ],
            env=env,
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + "\n" + result.stderr
        assert '"ok": true' in result.stdout, result.stdout
    finally:
        subprocess.run(
            [*base_cmd, "down", "-v", "--remove-orphans"],
            env=env,
            cwd=repo_root,
            check=False,
        )

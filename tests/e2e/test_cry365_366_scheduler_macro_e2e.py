"""PostgreSQL Compose E2E cho CRY-365 (scheduler) và CRY-366 (macro + MarketRawReference replay)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from scripts.docker_image import build_image_reference, ensure_image_available


def test_cry365_366_scheduler_and_macro_raw_reference_on_compose_postgres() -> None:
    """Compose PostgreSQL E2E: lưu MarketRawReference thật, chạy macro as-of và scheduler tick."""
    repo_root = Path(__file__).resolve().parents[2]
    compose_file = repo_root / "compose.yaml"
    project_name = f"crypidian-cry365-366-{uuid4().hex[:10]}"
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
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models.entities import Workspace
from trading_bot.adapters.database.models.market_entities import MarketRawReference
from trading_bot.application.replay_macro_raw_evidence import (
    ReplayMacroEvidencePoint,
    ReplayMacroStatus,
    attach_macro_and_raw_evidence_to_bundle,
    build_replay_raw_reference,
    evaluate_replay_macro_as_of,
)
from trading_bot.application.workbench_job_scheduler import (
    ScheduledJobKind,
    WorkbenchJobScheduler,
)

async def run_drill() -> None:
    with open("/run/secrets/postgres_password", "r", encoding="utf-8") as fh:
        pw = fh.read().strip()
    url = f"postgresql+psycopg://crypidian_admin:{pw}@postgres:5432/crypidian"
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    ws_id, raw_id = uuid4(), uuid4()
    raw_ref = build_replay_raw_reference(
        raw_reference_id=raw_id,
        source="FED_FOMC_CALENDAR",
        object_key="raw/fomc/2026-11.html",
        raw_bytes=b"<html>FOMC 2026-11-25</html>",
        content_type="text/html",
        captured_at=now - timedelta(days=2),
    )

    # 1. Lưu MarketRawReference thật vào PostgreSQL (không nhồi raw payload vào DB)
    async with sessions() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
            )
            session.add(
                Workspace(
                    id=ws_id,
                    slug=f"m366-{ws_id.hex[:8]}",
                    name="CRY-365/366 E2E",
                    mode="PAPER",
                    status="ACTIVE",
                )
            )
            await session.flush()
            session.add(
                MarketRawReference(
                    id=raw_ref.raw_reference_id,
                    workspace_id=ws_id,
                    source=raw_ref.source,
                    idempotency_key="raw-fomc-366",
                    object_key=raw_ref.object_key,
                    checksum=raw_ref.checksum,
                    byte_length=raw_ref.byte_length,
                    content_type=raw_ref.content_type,
                )
            )

    # 2. Đọc lại từ PostgreSQL và đánh giá macro as-of + gắn envelope vào bundle
    async with sessions() as session:
        await session.execute(
            text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
        )
        saved = await session.scalar(
            select(MarketRawReference).where(MarketRawReference.id == raw_id)
        )
        assert saved is not None
        assert saved.checksum == raw_ref.checksum

    macro_pt = ReplayMacroEvidencePoint(
        source_id="FED_FOMC_CALENDAR",
        event_name="FOMC Statement",
        release_at=now + timedelta(minutes=15),
        available_at=now - timedelta(hours=1),
        is_high_impact=True,
        raw_reference_id=saved.id,
        raw_checksum=saved.checksum,
    )
    st_blk = evaluate_replay_macro_as_of(
        [macro_pt], decision_time=now, required_sources=frozenset({"FED_FOMC_CALENDAR"})
    )
    assert st_blk.status is ReplayMacroStatus.BLACKOUT
    st_clr = evaluate_replay_macro_as_of(
        [macro_pt],
        decision_time=now + timedelta(minutes=80),
        required_sources=frozenset({"FED_FOMC_CALENDAR"}),
    )
    assert st_clr.status is ReplayMacroStatus.CLEAR
    env = attach_macro_and_raw_evidence_to_bundle(
        bundle_hash="d" * 64, macro_points=[macro_pt], raw_references=[raw_ref]
    )
    assert len(env.envelope_hash) == 64

    # 3. Scheduler chạy định kỳ ngắn hạn không giữ transaction DB dài
    sched = WorkbenchJobScheduler()
    s = sched.create_schedule(
        workspace_id=ws_id,
        kind=ScheduledJobKind.REPLAY,
        target_ref=env.envelope_hash,
        interval_seconds=600,
        actor_id="owner-e2e",
        actor_role="owner",
        start_at=now,
    )
    dispatched = []
    res = sched.tick(
        now=now,
        active_runs=0,
        rights_checker=lambda _: True,
        dispatcher=lambda item, slot: dispatched.append((item.schedule_id, slot)),
    )
    assert res.dispatched_schedule_ids == (s.schedule_id,)
    assert len(dispatched) == 1

    await engine.dispose()
    print(json.dumps({"ok": True, "envelope": env.envelope_hash[:12], "dispatched": 1}))

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

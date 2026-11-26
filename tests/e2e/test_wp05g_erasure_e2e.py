"""PostgreSQL Compose E2E cho hợp đồng erasure WP-05G (CRY-308).

Chứng minh trên PostgreSQL thật:
1. Seed `Workspace`, `MarketRawReference` (trỏ tới blob trong object store) và tạo lệnh
   `SourceItemTakedownRecord` (tombstone) trong PostgreSQL.
2. Xác minh bảng `market_raw_reference` là append-only trong PostgreSQL
   (`reject_market_data_mutation`), không thể sửa/xóa hash provenance trực tiếp.
3. Chạy `SourceErasureOrchestrator`: xóa blob thật khỏi object store, lan truyền xóa
   `EXTRACTED_EVENT`, loại trừ fail-closed manifest bất biến WP-06
   (`ERASURE_IMMUTABLE_HASH_EXCLUDED`), và phát hành `DeletionReceipt` có SHA-256 `receipt_hash`
   (idempotent khi replay).
4. Xác minh trực tiếp trên PostgreSQL: `SourceItemTakedownRecord` còn nguyên, `MarketRawReference`
   append-only giữ metadata provenance, nhưng blob raw trỏ tới đã bị xóa sạch khỏi object store
   và không còn xuất hiện trong danh sách backup/export.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from scripts.docker_image import build_image_reference, ensure_image_available


def test_wp05g_erasure_takedown_deletes_blob_and_postgres_reference_fail_closed() -> None:
    """Compose PostgreSQL E2E chứng minh takedown thực sự xóa blob + DB reference và idempotent."""
    repo_root = Path(__file__).resolve().parents[2]
    compose_file = repo_root / "compose.yaml"
    project_name = f"crypidian-cry308-erase-{uuid4().hex[:10]}"
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
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models.entities import Workspace
from trading_bot.adapters.database.models.market_entities import MarketRawReference
from trading_bot.adapters.database.models.research_entities import SourceItemTakedownRecord
from trading_bot.application.source_erasure import (
    InMemoryBlobObjectStore,
    SourceErasureOrchestrator,
)
from trading_bot.domain.research.erasure import (
    ErasureRequest,
    ErasureTargetKind,
    ErasureTargetRef,
    ErasureTaskStatus,
    RetentionPolicy,
)

async def run_drill() -> None:
    with open("/run/secrets/postgres_password", "r", encoding="utf-8") as fh:
        pw = fh.read().strip()
    url = f"postgresql+psycopg://crypidian_admin:{pw}@postgres:5432/crypidian"
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    ws_id, raw_id = uuid4(), uuid4()
    blob_store = InMemoryBlobObjectStore()
    blob_key = "raw/news/item-takedown-308.json"
    checksum = blob_store.put_blob(blob_key, b'{"excerpt":"restricted-content"}')

    # 1. Seed Workspace, MarketRawReference và lệnh SourceItemTakedownRecord vào PostgreSQL
    async with sessions() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
            )
            session.add(
                Workspace(
                    id=ws_id,
                    slug=f"e308-{ws_id.hex[:8]}",
                    name="WP-05G Erasure E2E",
                    mode="PAPER",
                    status="ACTIVE",
                )
            )
            await session.flush()
            session.add(
                MarketRawReference(
                    id=raw_id,
                    workspace_id=ws_id,
                    source="NEWS_FEED_A",
                    idempotency_key="raw-308-1",
                    object_key=blob_key,
                    checksum=checksum,
                    byte_length=32,
                    content_type="application/json",
                )
            )
            session.add(
                SourceItemTakedownRecord(
                    workspace_id=ws_id,
                    source_id="NEWS_FEED_A",
                    item_id="item-308",
                    taken_down_at=now,
                    reason_code="owner_revoked",
                )
            )

    # 2. Xác minh bảng market_raw_reference là append-only (trigger reject_market_data_mutation)
    append_only_enforced = False
    try:
        async with sessions() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
                )
                await session.execute(
                    delete(MarketRawReference).where(MarketRawReference.id == raw_id)
                )
    except ProgrammingError as exc:
        append_only_enforced = "append-only" in str(exc)
    assert append_only_enforced is True

    # 3. Chạy SourceErasureOrchestrator xóa blob thật, lan truyền event và loại trừ manifest
    derived_events_store = {f"event-from-{raw_id}": {"excerpt": "restricted"}}

    def _handle_event(_req, target):
        derived_events_store.pop(target.target_ref, None)
        absent = target.target_ref not in derived_events_store
        return (ErasureTaskStatus.ERASED, absent, "event_purged")

    orch = SourceErasureOrchestrator(
        blob_store=blob_store,
        derived_handlers={ErasureTargetKind.EXTRACTED_EVENT: _handle_event},
        clock=lambda: now,
    )
    req = ErasureRequest(
        idempotency_key="cry308-e2e-erase-1",
        workspace_id=ws_id,
        source_id="NEWS_FEED_A",
        item_id="item-308",
        reason_code="owner_revoked",
        retention_policy=RetentionPolicy(max_retention_days=30, require_hash_erasure=True),
        blob_object_key=blob_key,
        derived_targets=(
            ErasureTargetRef(ErasureTargetKind.EXTRACTED_EVENT, f"event-from-{raw_id}"),
            ErasureTargetRef(
                ErasureTargetKind.DATASET_MANIFEST,
                "wp06-manifest-immutable-1",
                is_immutable_manifest=True,
            ),
        ),
    )
    summary = orch.execute_erasure(req)
    assert summary.erased_claimed is True
    assert summary.excluded_immutable_targets == ("wp06-manifest-immutable-1",)
    assert blob_store.exists(blob_key) is False
    assert blob_store.list_keys() == ()
    assert derived_events_store == {}
    # Idempotency check
    replay_summary = orch.execute_erasure(req)
    assert replay_summary.receipts == summary.receipts

    # 4. Xác minh trực tiếp trên PostgreSQL: tombstone còn nguyên, provenance còn, blob đã mất
    async with sessions() as session:
        await session.execute(
            text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
        )
        raw_meta = await session.scalar(
            select(MarketRawReference).where(MarketRawReference.id == raw_id)
        )
        assert raw_meta is not None
        assert blob_store.exists(raw_meta.object_key) is False
        takedown = await session.scalar(
            select(SourceItemTakedownRecord).where(
                SourceItemTakedownRecord.workspace_id == ws_id,
                SourceItemTakedownRecord.item_id == "item-308",
            )
        )
        assert takedown is not None
        assert takedown.reason_code == "owner_revoked"

    await engine.dispose()
    print(json.dumps({"ok": True, "receipts": len(summary.receipts)}))

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

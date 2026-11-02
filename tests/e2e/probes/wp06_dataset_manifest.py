"""Probe PostgreSQL WP-06: persistencia, RLS y bloqueo de mutación."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import DatasetManifestRecord, Workspace
from trading_bot.adapters.database.repositories.dataset_manifest import DatasetManifestRepository
from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSample, DatasetSplit

STATE = json.loads(os.environ.get("WP06_STATE", "{}"))
PHASE = os.environ.get("WP06_PHASE", "")
ADMIN = "crypidian_admin_web"
TRAINER = "crypidian_trainer_worker"
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


def _engine(role: str | None = None):
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    url = URL.create(
        "postgresql+psycopg",
        username=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )
    options = {"options": f"-c role={role}"} if role else {}
    return create_async_engine(url, connect_args=options)


def _manifest(workspace_id: UUID, *, variant: str = "a") -> DatasetManifest:
    return DatasetManifest(
        workspace_id=workspace_id,
        query_hash="1" * 64,
        symbols=("BTCUSDT",),
        data_start=NOW - timedelta(days=1),
        data_end=NOW + timedelta(days=1),
        as_of=NOW + timedelta(minutes=1),
        feature_schema_version="feature-schema-v1",
        feature_version="feature-v1",
        label_version="label-v1",
        sample_schema_version="sample-schema-v1",
        split_policy_version="split-v1",
        samples=(
            DatasetSample(
                sample_id=f"sample-{variant}",
                decision_at=NOW,
                feature_available_at=NOW - timedelta(minutes=1),
                outcome_start=NOW,
                outcome_end=NOW + timedelta(seconds=1),
                feature_snapshot_hash="2" * 64,
                outcome_id=f"trade-{variant}",
                outcome_hash="3" * 64,
                label_hash="4" * 64,
                label_value=1,
                label_version="label-v1",
                split=DatasetSplit.TRAIN,
                source_dependent=False,
                source_rights_hashes=(),
                source_revision_refs=(),
            ),
        ),
        exclusions=(),
        holdout_sealed_at=None,
        retention_policy_ref="retention-v1",
    )


async def _seed() -> dict[str, object]:
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        session.add_all(
            [
                Workspace(
                    id=UUID(STATE["workspace_id"]),
                    name="WP06 manifest workspace",
                    slug=f"wp06-{STATE['workspace_id'][:8]}",
                ),
                Workspace(
                    id=UUID(STATE["other_workspace_id"]),
                    name="WP06 other workspace",
                    slug=f"wp06-{STATE['other_workspace_id'][:8]}",
                ),
            ]
        )
    await engine.dispose()
    return {"seeded": True}


async def _persist() -> dict[str, object]:
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = UUID(STATE["workspace_id"])
    other_workspace_id = UUID(STATE["other_workspace_id"])
    manifest = _manifest(workspace_id)
    async with sessions() as session, session.begin():
        repository = DatasetManifestRepository(session)
        first = await repository.append(workspace_id=workspace_id, manifest=manifest)
        duplicate = await repository.append(workspace_id=workspace_id, manifest=manifest)
        loaded = await repository.get_by_hash(
            workspace_id=workspace_id,
            manifest_hash=manifest.content_hash,
        )
        hidden = await repository.get_by_hash(
            workspace_id=other_workspace_id,
            manifest_hash=manifest.content_hash,
        )
        assert not first.is_duplicate
        assert duplicate.is_duplicate
        assert loaded == manifest
        assert hidden is None
    await engine.dispose()
    return {"manifest_hash": manifest.content_hash}


async def _assert_roles_and_immutability() -> dict[str, object]:
    workspace_id = UUID(STATE["workspace_id"])
    manifest = _manifest(workspace_id)
    errors: list[str] = []

    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        try:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                    {"workspace_id": str(workspace_id)},
                )
                await session.execute(
                    update(DatasetManifestRecord)
                    .where(DatasetManifestRecord.manifest_hash == manifest.content_hash)
                    .values(manifest_hash="b" * 64)
                )
        except DBAPIError as error:
            errors.append(str(error.orig))
    await engine.dispose()
    assert any("WP06_DATASET_MANIFEST_IMMUTABLE" in error for error in errors), repr(errors)

    engine = _engine(TRAINER)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        repository = DatasetManifestRepository(session)
        loaded = await repository.get_by_hash(
            workspace_id=workspace_id,
            manifest_hash=manifest.content_hash,
        )
        assert loaded == manifest
    await engine.dispose()

    engine = _engine(TRAINER)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        try:
            async with session.begin():
                await DatasetManifestRepository(session).append(
                    workspace_id=workspace_id,
                    manifest=_manifest(workspace_id, variant="b"),
                )
        except DBAPIError as error:
            errors.append(str(error.orig))
    await engine.dispose()
    assert any("permission denied" in error.lower() for error in errors)
    return {"trainer_select": True, "trainer_insert_denied": True, "append_only": True}


async def _count_without_workspace_scope() -> dict[str, object]:
    engine = _engine(ADMIN)
    async with engine.connect() as connection:
        count = await connection.scalar(
            select(text("count(*)")).select_from(text("dataset_manifest"))
        )
    await engine.dispose()
    assert count == 0
    return {"unscoped_rows": count}


async def _main() -> None:
    handlers = {
        "seed": _seed,
        "persist": _persist,
        "roles": _assert_roles_and_immutability,
        "unscoped": _count_without_workspace_scope,
    }
    if PHASE not in handlers:
        raise ValueError("WP06_PHASE không hợp lệ.")
    print(json.dumps(await handlers[PHASE](), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())

"""Probe metadata tổng hợp WP05C: không nội dung thật, network hoặc lệnh giao dịch."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import (
    SourceItemRevisionRecord,
    Workspace,
)
from trading_bot.adapters.database.repositories.source_items import SourceItemRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.domain.research.source_item_history import SourceItemHistoryConflict
from trading_bot.domain.research.source_items import SourceItemRevision
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry

STATE = json.loads(os.environ.get("WP05C_STATE", "{}"))
PHASE = os.environ.get("WP05C_PHASE", "")
ADMIN = "crypidian_admin_web"
INGESTION = "crypidian_ingestion_worker"
URL_REF = "https://example.invalid/synthetic/announcement?order=1,2"
WORKSPACE = UUID(STATE["workspace_id"])
OTHER = UUID(STATE["other_workspace_id"])
SOURCE = "synthetic-calendar"


def _engine(role: str | None = None):
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    url = URL.create(
        "postgresql+psycopg",
        username=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )
    return create_async_engine(url, connect_args={"options": f"-c role={role}"} if role else {})


def _revision(revision_id="r1", **changes):
    at = datetime.fromisoformat(STATE["at"])
    return replace(
        SourceItemRevision(
            source_id=SOURCE,
            item_id="item",
            revision_id=revision_id,
            canonical_url=URL_REF,
            observed_at=at,
            available_at=at,
            event_at=at + timedelta(days=5),
            published_at=at - timedelta(seconds=1),
            edited_at=at,
        ),
        **changes,
    )


def _revisions():
    at = datetime.fromisoformat(STATE["at"])
    return (
        _revision(),
        _revision(
            "r2", observed_at=at + timedelta(microseconds=1), available_at=at + timedelta(days=1)
        ),
        _revision(
            "r3",
            observed_at=at + timedelta(microseconds=2),
            available_at=at + timedelta(microseconds=2),
        ),
    )


async def _scope(session, workspace=WORKSPACE):
    await session.execute(
        text("SELECT set_config('app.workspace_id', :workspace, true)"),
        {"workspace": str(workspace)},
    )


async def _seed():
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        session.add_all(
            [
                Workspace(id=WORKSPACE, name="Metadata fixture", slug=f"item-{WORKSPACE.hex[:10]}"),
                Workspace(id=OTHER, name="Other metadata fixture", slug=f"item-{OTHER.hex[:10]}"),
            ]
        )
    await engine.dispose()
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    entry = SourceRegistryEntry.discover(
        source_id=SOURCE,
        publisher="Nhà xuất bản giả lập",
        endpoint_url="https://example.invalid/synthetic.json",
        access_method="GET",
        auth_scope="fixture-only",
        actor_id="fixture-operator",
        at=datetime.now(UTC),
        reason="Metadata giả lập, không fetch",
        budget=SourceBudget(
            window=timedelta(hours=1),
            max_requests=1,
            max_bytes=100,
            max_cost=Decimal(0),
            cost_unit="USD",
        ),
    )
    async with sessions() as session, session.begin():
        repository = SourceRegistryRepository(session)
        initial = await repository.append_revision(workspace_id=WORKSPACE, revision=entry.current)
        entry = SourceRegistryEntry(SOURCE, (initial.revision,))
        entry = entry.transition(
            policy_id="fixture-pending",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="fixture-operator",
            at=datetime.now(UTC),
            reason="Rà fixture",
        )
        pending = await repository.append_revision(workspace_id=WORKSPACE, revision=entry.current)
        entry = SourceRegistryEntry(SOURCE, (initial.revision, pending.revision))
        reviewed_at = datetime.now(UTC)
        entry = entry.transition(
            policy_id="fixture-verified",
            state=SourcePolicyState.VERIFIED,
            actor_id="fixture-operator",
            approved_by="fixture-reviewer",
            at=reviewed_at,
            reason="Chỉ metadata fixture",
            evidence_ref="fixture:synthetic-rights",
            reviewed_at=reviewed_at,
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
        )
        await repository.append_revision(workspace_id=WORKSPACE, revision=entry.current)
    async with sessions() as session, session.begin():
        at = await session.scalar(text("SELECT clock_timestamp()"))
    await engine.dispose()
    return {"at": at.isoformat()}


async def _persist():
    engine = _engine(INGESTION)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        repository = SourceItemRepository(session)
        prior = None
        for revision in _revisions():
            result = await repository.append_revision(
                workspace_id=WORKSPACE, revision=revision, predecessor_revision_id=prior
            )
            assert not result.is_duplicate
            prior = revision.revision_id
        duplicate = await repository.append_revision(
            workspace_id=WORKSPACE, revision=_revision(), predecessor_revision_id=None
        )
        assert duplicate.is_duplicate
        try:
            await repository.append_revision(
                workspace_id=WORKSPACE,
                revision=_revision(canonical_url="https://example.invalid/conflict"),
                predecessor_revision_id=None,
            )
        except SourceItemHistoryConflict:
            pass
        else:
            raise AssertionError("Retry khác payload đã được chấp nhận.")
        for workspace, revision, expiry in (
            (WORKSPACE, _revision(item_id="repost"), None),
            (WORKSPACE, _revision(source_id="other-source", item_id="other-source-item"), None),
            (OTHER, _revision(item_id="other-tenant-item"), None),
            (WORKSPACE, _revision(item_id="retained"), datetime.fromisoformat(STATE["at"])),
            (
                WORKSPACE,
                _revision(
                    item_id="zero-microseconds",
                    canonical_url="https://example.invalid/zero",
                    observed_at=_revision().observed_at.replace(microsecond=0),
                    available_at=_revision().available_at.replace(microsecond=0),
                    edited_at=None,
                ),
                None,
            ),
        ):
            await repository.append_revision(
                workspace_id=workspace,
                revision=revision,
                predecessor_revision_id=None,
                retention_expires_at=expiry,
            )
    await engine.dispose()
    return {"persisted": True}


async def _readback():
    engine = _engine(INGESTION)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        repository = SourceItemRepository(session)
        history = await repository.get_history(
            workspace_id=WORKSPACE, source_id=SOURCE, item_id="item"
        )
        assert tuple(entry.revision for entry in history.entries) == _revisions()
        assert history.entries[2].predecessor_hash == history.entries[1].revision_hash
        at = datetime.fromisoformat(STATE["at"])
        before = await repository.select_accessible(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id="item",
            purpose=SourceUsePurpose.METADATA,
            as_of=at - timedelta(microseconds=1),
        )
        assert before is None
        earlier = await repository.select_accessible(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id="item",
            purpose=SourceUsePurpose.METADATA,
            as_of=at + timedelta(microseconds=1),
        )
        assert earlier.revision.revision_id == "r1"
        latest = await repository.select_accessible(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id="item",
            purpose=SourceUsePurpose.METADATA,
        )
        assert latest.revision.revision_id == "r3"
        assert (
            await repository.select_accessible(
                workspace_id=WORKSPACE,
                source_id=SOURCE,
                item_id="retained",
                purpose=SourceUsePurpose.METADATA,
            )
            is None
        )
        assert (
            await repository.select_accessible(
                workspace_id=WORKSPACE,
                source_id=SOURCE,
                item_id="item",
                purpose=SourceUsePurpose.TRAINING,
            )
            is None
        )
        assert await repository.find_group(
            workspace_id=WORKSPACE, source_id=SOURCE, canonical_url=URL_REF
        ) == ("item", "repost", "retained")
        assert not (
            await repository.get_history(workspace_id=OTHER, source_id=SOURCE, item_id="item")
        ).entries
    await engine.dispose()
    return {"restart_lineage_and_asof": True}


async def _expect_denied(
    role, statement, expected="permission denied", workspace=WORKSPACE, parameters=None
):
    engine = _engine(role)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session, session.begin():
            await _scope(session, workspace)
            failure = None
            try:
                async with session.begin_nested():
                    await session.execute(text(statement), parameters or {})
            except DBAPIError as error:
                failure = str(error.orig)
            else:
                raise AssertionError(f"DB chấp nhận thao tác bị cấm với {role}.")
            assert failure is not None
            assert expected in failure, failure
    finally:
        await engine.dispose()


async def _roles():
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        assert (
            await session.scalar(select(text("count(*)")).select_from(SourceItemRevisionRecord))
            == 0
        )
        await _scope(session, OTHER)
        rows = (await session.scalars(select(SourceItemRevisionRecord))).all()
        assert len(rows) == 1
        assert rows[0].item_id == "other-tenant-item"
        await _scope(session)
        assert all(
            row.workspace_id == WORKSPACE
            for row in (await session.scalars(select(SourceItemRevisionRecord))).all()
        )
    await engine.dispose()
    for role in (
        "crypidian_trading_runtime",
        "crypidian_trainer_worker",
        "crypidian_execution_gateway",
    ):
        for table in ("source_item_revision", "source_item_takedown"):
            await _expect_denied(role, f"SELECT * FROM {table}")
    await _expect_denied(ADMIN, "INSERT INTO source_item_revision DEFAULT VALUES")
    await _expect_denied(INGESTION, "INSERT INTO source_item_takedown DEFAULT VALUES")
    for table in ("source_item_revision", "source_item_takedown"):
        for role in (ADMIN, INGESTION):
            await _expect_denied(role, f"DELETE FROM {table}")
            await _expect_denied(role, f"UPDATE {table} SET item_id = item_id")
    return {"rls_and_role_split": True}


async def _guards():
    engine = _engine(INGESTION)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await _scope(session)
        row = await session.scalar(
            select(SourceItemRevisionRecord).where(
                SourceItemRevisionRecord.item_id == "item", SourceItemRevisionRecord.sequence == 3
            )
        )
        values = {
            column.name: getattr(row, column.name)
            for column in SourceItemRevisionRecord.__table__.columns
        }
    await engine.dispose()
    from uuid import uuid4

    for expected, changes in (
        ("WP05_ITEM_PREDECESSOR_INVALID", {"sequence": 5}),
        ("WP05_ITEM_PREDECESSOR_INVALID", {"predecessor_hash": "0" * 64}),
        ("WP05_ITEM_HASH_INVALID", {"revision_hash": "0" * 64}),
    ):
        parameters = (
            values
            | {
                "id": uuid4(),
                "sequence": 4,
                "revision_id": "r4",
                "predecessor_revision_id": "r3",
                "predecessor_hash": row.revision_hash,
            }
            | changes
        )
        columns = ", ".join(parameters)
        placeholders = ", ".join(f":{key}" for key in parameters)
        await _expect_denied(
            INGESTION,
            f"INSERT INTO source_item_revision ({columns}) VALUES ({placeholders})",
            expected,
            parameters=parameters,
        )
    from trading_bot.domain.research.source_item_history import SourceItemRevisionEntry

    foreign_entry = SourceItemRevisionEntry.create(
        _revision(item_id="foreign-item"),
        predecessor_revision_id=None,
        predecessor_hash=None,
    )
    foreign_row = SourceItemRevisionRecord.from_entry(WORKSPACE, 1, foreign_entry)
    parameters = {
        column.name: getattr(foreign_row, column.name)
        for column in SourceItemRevisionRecord.__table__.columns
        if column.name not in {"id", "created_at"}
    } | {"id": uuid4()}
    columns = ", ".join(parameters)
    placeholders = ", ".join(f":{key}" for key in parameters)
    await _expect_denied(
        INGESTION,
        f"INSERT INTO source_item_revision ({columns}) VALUES ({placeholders})",
        "row-level security",
        workspace=OTHER,
        parameters=parameters,
    )
    for operation in (
        "UPDATE source_item_revision SET revision_hash = revision_hash",
        "DELETE FROM source_item_revision",
    ):
        await _expect_denied(None, operation, "WP05_SOURCE_HISTORY_APPEND_ONLY")
    return {"raw_lineage_guards": True}


async def _withdraw():
    engine = _engine(INGESTION)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await SourceItemRepository(session).append_revision(
            workspace_id=WORKSPACE,
            revision=_revision("r2", item_id="repost", tombstoned=True),
            predecessor_revision_id="r1",
        )
    await engine.dispose()
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        repository = SourceItemRepository(session)
        marker = await repository.take_down(
            workspace_id=WORKSPACE, source_id=SOURCE, item_id="item", reason_code="rights_withdrawn"
        )
        assert (
            await repository.take_down(
                workspace_id=WORKSPACE,
                source_id=SOURCE,
                item_id="item",
                reason_code="rights_withdrawn",
            )
        ).id == marker.id
    await engine.dispose()
    for operation in (
        "UPDATE source_item_takedown SET reason_code = reason_code",
        "DELETE FROM source_item_takedown",
    ):
        await _expect_denied(None, operation, "WP05_SOURCE_HISTORY_APPEND_ONLY")
    return {"withdrawn": True}


async def _withdrawn_readback():
    engine = _engine(INGESTION)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        repository = SourceItemRepository(session)
        for item_id in ("item", "repost"):
            assert (
                await repository.select_accessible(
                    workspace_id=WORKSPACE,
                    source_id=SOURCE,
                    item_id=item_id,
                    purpose=SourceUsePurpose.METADATA,
                    as_of=datetime.fromisoformat(STATE["at"]) + timedelta(microseconds=1),
                )
                is None
            )
            try:
                await repository.append_revision(
                    workspace_id=WORKSPACE,
                    revision=_revision(
                        "forbidden",
                        item_id=item_id,
                        observed_at=datetime.fromisoformat(STATE["at"]) + timedelta(seconds=1),
                        available_at=datetime.fromisoformat(STATE["at"]) + timedelta(seconds=1),
                    ),
                    predecessor_revision_id="r3" if item_id == "item" else "r2",
                )
            except SourceItemHistoryConflict:
                pass
            else:
                raise AssertionError("Append sau withdrawal được chấp nhận.")
    await engine.dispose()
    return {"no_resurrection_after_restart": True}


async def _main():
    handlers = {
        "seed": _seed,
        "persist": _persist,
        "readback": _readback,
        "roles": _roles,
        "guards": _guards,
        "withdraw": _withdraw,
        "withdrawn_readback": _withdrawn_readback,
    }
    print(json.dumps(await handlers[PHASE](), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())

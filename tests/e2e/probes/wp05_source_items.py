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
    SourceItemTakedownRecord,
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
        first_recorded_cutoff = None
        for revision in _revisions():
            result = await repository.append_revision(
                workspace_id=WORKSPACE, revision=revision, predecessor_revision_id=prior
            )
            assert not result.is_duplicate
            if prior is None:
                first_recorded_cutoff = await session.scalar(text("SELECT clock_timestamp()"))
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
    return {"persisted": True, "first_recorded_cutoff": first_recorded_cutoff.isoformat()}


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
            as_of=datetime.fromisoformat(STATE["first_recorded_cutoff"]),
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
                    as_of=datetime.fromisoformat(STATE["first_recorded_cutoff"]),
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


async def _legacy_recording_seed():
    """Hàng trước migration mới không có bằng chứng DB time đáng tin cậy."""
    from uuid import uuid4

    from trading_bot.domain.research.source_item_history import SourceItemRevisionEntry

    engine = _engine(INGESTION)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await _scope(session)
        candidate = _revision(item_id="legacy-row", canonical_url="https://example.invalid/legacy")
        entry = SourceItemRevisionEntry.create(
            candidate, predecessor_revision_id=None, predecessor_hash=None
        )
        record = SourceItemRevisionRecord.from_entry(WORKSPACE, 1, entry)
        values = {
            column.name: getattr(record, column.name)
            for column in SourceItemRevisionRecord.__table__.columns
            if column.name not in {"id", "created_at", "recorded_at"}
        }
        values |= {"id": uuid4(), "created_at": candidate.observed_at - timedelta(days=1)}
        columns = ", ".join(values)
        placeholders = ", ".join(f":{key}" for key in values)
        await session.execute(
            text(f"INSERT INTO source_item_revision ({columns}) VALUES ({placeholders})"), values
        )
        cutoff = await session.scalar(text("SELECT clock_timestamp()"))
    await engine.dispose()
    return {"legacy_cutoff": cutoff.isoformat()}


async def _legacy_recording_readback():
    """Backfill chỉ mở lịch sử từ lúc migration, không dùng created_at caller cũ."""
    engine = _engine(INGESTION)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        repository = SourceItemRepository(session)
        before = await repository.select_accessible(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id="legacy-row",
            purpose=SourceUsePurpose.METADATA,
            as_of=datetime.fromisoformat(STATE["legacy_cutoff"]),
        )
        assert before is None
        after = await repository.select_accessible(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id="legacy-row",
            purpose=SourceUsePurpose.METADATA,
        )
        assert after is not None
    await engine.dispose()
    return {"conservative_backfill": True}


async def _review_regressions():
    """Tái hiện source timestamp backdate, raw timestamp giả và URL bypass domain."""
    from dataclasses import asdict
    from types import SimpleNamespace
    from uuid import uuid4

    from trading_bot.domain.research.source_item_history import (
        SourceItemRevisionEntry,
        _revision_digest,
    )

    failures = []
    engine = _engine(INGESTION)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        before = await session.scalar(text("SELECT clock_timestamp()"))
        await SourceItemRepository(session).append_revision(
            workspace_id=WORKSPACE,
            revision=_revision(
                item_id="late-arrival", canonical_url="https://example.invalid/late"
            ),
            predecessor_revision_id=None,
        )
    async with sessions() as session, session.begin():
        repository = SourceItemRepository(session)
        early = await repository.select_accessible(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id="late-arrival",
            purpose=SourceUsePurpose.METADATA,
            as_of=before,
        )
        if early is not None:
            failures.append("Revision đến muộn bị lộ trước thời điểm DB ghi nhận")
        after = await session.scalar(text("SELECT clock_timestamp()"))
        visible = await repository.select_accessible(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id="late-arrival",
            purpose=SourceUsePurpose.METADATA,
            as_of=after,
        )
        assert visible is not None

    async with sessions() as session, session.begin():
        await _scope(session)
        candidate = _revision(item_id="raw-clock", canonical_url="https://example.invalid/raw")
        entry = SourceItemRevisionEntry.create(
            candidate, predecessor_revision_id=None, predecessor_hash=None
        )
        record = SourceItemRevisionRecord.from_entry(WORKSPACE, 1, entry)
        spoofed = before - timedelta(days=10)
        values = {
            column.name: getattr(record, column.name)
            for column in SourceItemRevisionRecord.__table__.columns
            if column.name not in {"id", "created_at", "recorded_at"}
        }
        values |= {"id": uuid4(), "created_at": spoofed}
        if "recorded_at" in SourceItemRevisionRecord.__table__.columns:
            values["recorded_at"] = spoofed
        columns = ", ".join(values)
        placeholders = ", ".join(f":{key}" for key in values)
        actual = (
            await session.execute(
                text(
                    f"INSERT INTO source_item_revision ({columns}) VALUES ({placeholders}) "
                    "RETURNING created_at"
                ),
                values,
            )
        ).scalar_one()
        if actual < before:
            failures.append("Raw INSERT giả được thời điểm DB tạo row")
        if "recorded_at" in SourceItemRevisionRecord.__table__.columns:
            recorded = await session.scalar(
                text("SELECT recorded_at FROM source_item_revision WHERE id = :id"),
                {"id": values["id"]},
            )
            assert recorded >= before
            assert recorded == actual
            assert record.to_entry().revision_hash == entry.revision_hash

        for number, url in enumerate(
            (
                "https://",
                "https://-invalid.example/path",
                "https://:443/path",
                "https://example.invalid:/path",
                "https://EXAMPLE.invalid/path",
                "https://example.invalid/path#fragment",
                "https://[:::]/path",
                "https://example..invalid/path",
                "https://example.invalid/%zz",
                "https://example.invalid:70000/path",
            ),
            1,
        ):
            candidate = _revision(item_id=f"raw-url-{number}")
            entry = SourceItemRevisionEntry.create(
                candidate, predecessor_revision_id=None, predecessor_hash=None
            )
            record = SourceItemRevisionRecord.from_entry(WORKSPACE, 1, entry)
            values = {
                column.name: getattr(record, column.name)
                for column in SourceItemRevisionRecord.__table__.columns
                if column.name not in {"id", "created_at", "recorded_at"}
            }
            values |= {
                "id": uuid4(),
                "canonical_url": url,
                "revision_hash": _revision_digest(
                    SimpleNamespace(**(asdict(candidate) | {"canonical_url": url})), None
                ),
            }
            columns = ", ".join(values)
            placeholders = ", ".join(f":{key}" for key in values)
            error_code = None
            try:
                async with session.begin_nested():
                    await session.execute(
                        text(
                            f"INSERT INTO source_item_revision ({columns}) VALUES ({placeholders})"
                        ),
                        values,
                    )
            except DBAPIError as error:
                error_code = error.orig.sqlstate
            if error_code != "23514":
                failures.append(f"Raw INSERT chấp nhận canonical URL không hợp lệ: {url}")
        for number, url in enumerate(
            (
                "https://example.invalid",
                "https://example.invalid:443/path?a=%20&b='x'",
                "https://[::1]/synthetic",
                "https://[::ffff:192.0.2.1]:443/fixture",
            ),
            1,
        ):
            candidate = _revision(item_id=f"valid-url-{number}", canonical_url=url)
            await SourceItemRepository(session).append_revision(
                workspace_id=WORKSPACE,
                revision=candidate,
                predecessor_revision_id=None,
            )
            loaded = await session.scalar(
                select(SourceItemRevisionRecord).where(
                    SourceItemRevisionRecord.item_id == candidate.item_id,
                )
            )
            assert loaded.to_entry().revision == candidate
    await engine.dispose()

    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        result = await SourceItemRepository(session).take_down(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id="withdraw-response",
            reason_code="rights_withdrawn",
        )
        initial_timestamp = result.taken_down_at
        persisted = await session.scalar(
            select(SourceItemTakedownRecord.taken_down_at).where(
                SourceItemTakedownRecord.item_id == "withdraw-response"
            )
        )
    async with sessions() as session, session.begin():
        retry = await SourceItemRepository(session).take_down(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id="withdraw-response",
            reason_code="rights_withdrawn",
        )
        if initial_timestamp != persisted or persisted != retry.taken_down_at:
            failures.append("Timestamp takedown lần đầu khác DB hoặc retry")
    await engine.dispose()
    assert not failures, failures
    return {"review_regressions": True}


async def _raw_timestamp_insert(session, item_id, changes):
    """Tạo hash bằng SQL để probe giá trị PostgreSQL nằm ngoài miền Python."""
    candidate = _revision(item_id=item_id, canonical_url="https://example.invalid/time-range")
    values = {
        "workspace_id": str(WORKSPACE),
        "source_id": SOURCE,
        "item_id": item_id,
        "revision_id": "r1",
        "canonical_url": candidate.canonical_url,
        "observed_at": candidate.observed_at.isoformat(),
        "available_at": candidate.available_at.isoformat(),
        "event_at": candidate.event_at.isoformat(),
        "published_at": candidate.published_at.isoformat(),
        "edited_at": candidate.edited_at.isoformat(),
        "retention_expires_at": None,
        "created_at": candidate.observed_at.isoformat(),
        "recorded_at": candidate.observed_at.isoformat(),
    } | changes
    typed = [
        "gen_random_uuid() AS id",
        "1 AS sequence",
        "false AS tombstoned",
        "NULL::text AS predecessor_revision_id",
        "NULL::text AS predecessor_hash",
    ]
    for field in values:
        dtype = (
            "uuid"
            if field == "workspace_id"
            else "timestamptz"
            if field.endswith("_at")
            else "text"
        )
        typed.append(f"CAST(:{field} AS {dtype}) AS {field}")
    payload = (
        "json_build_object("
        + ", ".join(
            (
                "'available_at', public.source_item_timestamp(c.available_at)",
                "'canonical_url', c.canonical_url",
                "'edited_at', public.source_item_timestamp(c.edited_at)",
                "'event_at', public.source_item_timestamp(c.event_at)",
                "'item_id', c.item_id",
                "'observed_at', public.source_item_timestamp(c.observed_at)",
                "'predecessor_hash', c.predecessor_hash",
                "'published_at', public.source_item_timestamp(c.published_at)",
                "'revision_id', c.revision_id",
                "'source_id', c.source_id",
                "'tombstoned', c.tombstoned",
            )
        )
        + ")::text"
    )
    digest = (
        f"encode(sha256(convert_to(replace(replace({payload}, ' : ', ':'), "
        "', ', ','), 'UTF8')), 'hex')"
    )
    columns = [column.name for column in SourceItemRevisionRecord.__table__.columns]
    selected = [digest if field == "revision_hash" else f"c.{field}" for field in columns]
    statement = (
        f"WITH c AS (SELECT {', '.join(typed)}) "
        f"INSERT INTO source_item_revision ({', '.join(columns)}) "
        f"SELECT {', '.join(selected)} FROM c RETURNING 1"
    )
    return await session.scalar(text(statement), values)


async def _legacy_timestamp_validation():
    """Upgrade phải fail rõ và rollback CHECK, giữ nguyên hàng cũ sai timestamp."""
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path("/app/migrations/versions/d1a3c5e7f9b2_wp05_python_timestamp_range.py")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def apply_upgrade(connection):
        original = module.op
        module.op = Operations(MigrationContext.configure(connection))
        try:
            module.upgrade()
        finally:
            module.op = original

    engine = _engine()
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await _raw_timestamp_insert(
                connection, "legacy-invalid-time", {"retention_expires_at": "infinity"}
            )
            before = (
                await connection.execute(
                    text(
                        "SELECT revision_hash, retention_expires_at::text "
                        "FROM source_item_revision "
                        "WHERE item_id = 'legacy-invalid-time'"
                    )
                )
            ).one()
            nested = await connection.begin_nested()
            diagnostic = None
            try:
                await connection.run_sync(apply_upgrade)
            except DBAPIError as error:
                diagnostic = (error.orig.sqlstate, error.orig.diag.constraint_name)
            finally:
                await nested.rollback()
            assert diagnostic == (
                "23514",
                "ck_source_item_revision_sir_retention_expires_at_pytime",
            )
            after = (
                await connection.execute(
                    text(
                        "SELECT revision_hash, retention_expires_at::text "
                        "FROM source_item_revision "
                        "WHERE item_id = 'legacy-invalid-time'"
                    )
                )
            ).one()
            assert before == after
            assert after[1] == "infinity"
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_constraint "
                        "WHERE conrelid = 'source_item_revision'::regclass "
                        "AND conname LIKE '%_pytime'"
                    )
                )
                == 0
            )
        finally:
            await transaction.rollback()
    await engine.dispose()
    return {"invalid_legacy_row_blocks_upgrade_unchanged": True}


async def _timestamp_range():
    """Raw TIMESTAMPTZ ngoài miền Python phải bị từ chối trước khi thành history."""
    failures = []
    engine = _engine(INGESTION)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await _scope(session)
        for number, instant in enumerate(
            ("infinity", "-infinity", "0001-01-01 00:00:00 BC", "10000-01-01 00:00:00+00")
        ):
            for field in (
                "observed_at",
                "available_at",
                "event_at",
                "published_at",
                "edited_at",
                "retention_expires_at",
            ):
                changes = {field: instant}
                if field in {"observed_at", "available_at", "published_at", "edited_at"}:
                    changes |= dict.fromkeys(
                        ("observed_at", "available_at", "published_at", "edited_at"), instant
                    )
                error_code = None
                constraint = None
                nested = await session.begin_nested()
                try:
                    await _raw_timestamp_insert(session, f"bad-time-{number}-{field}", changes)
                except DBAPIError as error:
                    error_code = error.orig.sqlstate
                    constraint = error.orig.diag.constraint_name
                finally:
                    await nested.rollback()
                if error_code != "23514" or "_pytime" not in (constraint or ""):
                    failures.append(
                        f"Raw {field}={instant} chưa bị CHECK miền Python chặn: "
                        f"{error_code}/{constraint}"
                    )
        for number, instant in enumerate(
            ("infinity", "-infinity", "0001-01-01 00:00:00 BC", "10000-01-01 00:00:00+00")
        ):
            assert (
                await _raw_timestamp_insert(
                    session,
                    f"server-time-{number}",
                    {"created_at": instant, "recorded_at": instant},
                )
                == 1
            )
            row = await session.scalar(
                select(SourceItemRevisionRecord).where(
                    SourceItemRevisionRecord.item_id == f"server-time-{number}"
                )
            )
            assert row.recorded_at == row.created_at
            assert row.recorded_at.year >= 1
            assert row.recorded_at.year <= 9999
        for number, instant in enumerate(
            ("0001-01-01 00:00:00+00", "9999-12-31 23:59:59.999999+00")
        ):
            assert (
                await _raw_timestamp_insert(
                    session,
                    f"valid-time-{number}",
                    {"event_at": instant, "retention_expires_at": instant},
                )
                == 1
            )
            row = await session.scalar(
                select(SourceItemRevisionRecord).where(
                    SourceItemRevisionRecord.item_id == f"valid-time-{number}"
                )
            )
            assert row.event_at.year == (1 if number == 0 else 9999)
            assert row.retention_expires_at == row.event_at
            row.to_entry()
        assert (
            await SourceItemRepository(session).select_accessible(
                workspace_id=WORKSPACE,
                source_id=SOURCE,
                item_id="valid-time-0",
                purpose=SourceUsePurpose.METADATA,
            )
            is None
        )
        assert (
            await SourceItemRepository(session).select_accessible(
                workspace_id=WORKSPACE,
                source_id=SOURCE,
                item_id="valid-time-1",
                purpose=SourceUsePurpose.METADATA,
            )
            is not None
        )
    await engine.dispose()
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await _scope(session)
        for number, instant in enumerate(
            ("infinity", "-infinity", "0001-01-01 00:00:00 BC", "10000-01-01 00:00:00+00")
        ):
            actual = await session.scalar(
                text(
                    "INSERT INTO source_item_takedown "
                    "(id, workspace_id, source_id, item_id, taken_down_at, reason_code) "
                    "VALUES (gen_random_uuid(), :workspace_id, :source_id, :item_id, "
                    "CAST(:instant AS timestamptz), 'rights_withdrawn') RETURNING taken_down_at"
                ),
                {
                    "workspace_id": WORKSPACE,
                    "source_id": SOURCE,
                    "item_id": f"server-takedown-{number}",
                    "instant": instant,
                },
            )
            assert 1 <= actual.year <= 9999
    await engine.dispose()
    assert not failures, failures
    return {"finite_python_datetime": True}


async def _main():
    handlers = {
        "seed": _seed,
        "review_regressions": _review_regressions,
        "timestamp_range": _timestamp_range,
        "legacy_timestamp_validation": _legacy_timestamp_validation,
        "legacy_recording_seed": _legacy_recording_seed,
        "legacy_recording_readback": _legacy_recording_readback,
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

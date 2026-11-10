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
    SourceItemObservationRecord,
    SourceItemRevisionRecord,
    SourceItemTakedownRecord,
    Workspace,
)
from trading_bot.adapters.database.repositories.source_items import SourceItemRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.adapters.database.source_observer import PostCommitSourceObserver
from trading_bot.domain.research.source_item_history import SourceItemHistoryConflict
from trading_bot.domain.research.source_items import SourceItemRevision
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry
from trading_bot.domain.research.source_revision_access import SourceAccessReason

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
        from types import SimpleNamespace
        from uuid import uuid4

        from trading_bot.adapters.database.models import SourcePolicyRevisionRecord

        await _scope(session)

        async def append_legacy(revision):
            record = SourcePolicyRevisionRecord.from_domain(WORKSPACE, revision)
            values = {
                column.name: getattr(record, column.name)
                for column in SourcePolicyRevisionRecord.__table__.columns
                if column.name != "barrier_txid"
            }
            values["id"] = uuid4()
            values["allowed_purposes"] = json.dumps(values["allowed_purposes"])
            columns = ", ".join(values)
            placeholders = ", ".join(
                "CAST(:allowed_purposes AS jsonb)" if name == "allowed_purposes" else f":{name}"
                for name in values
            )
            await session.execute(
                text(f"INSERT INTO source_policy_revision ({columns}) VALUES ({placeholders})"),
                values,
            )
            return SimpleNamespace(revision=revision)

        initial = await append_legacy(entry.current)
        entry = SourceRegistryEntry(SOURCE, (initial.revision,))
        entry = entry.transition(
            policy_id="fixture-pending",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="fixture-operator",
            at=datetime.now(UTC),
            reason="Rà fixture",
        )
        pending = await append_legacy(entry.current)
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
        await append_legacy(entry.current)
    async with sessions() as session, session.begin():
        at = await session.scalar(text("SELECT clock_timestamp()"))
    await engine.dispose()
    return {"at": at.isoformat()}


async def _observe_legacy():
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    progress = await PostCommitSourceObserver(sessions).recover_policy(
        workspace_id=WORKSPACE, source_id=SOURCE
    )
    assert progress.complete
    await engine.dispose()
    engine = _engine(INGESTION)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    progress = await PostCommitSourceObserver(sessions).recover_item(
        workspace_id=WORKSPACE, source_id=SOURCE, item_id="legacy-row"
    )
    assert progress.complete
    await engine.dispose()
    return {"legacy_observed_after_migration_commit": True}


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
    observer = PostCommitSourceObserver(sessions)
    for item_id in ("item", "repost", "retained", "zero-microseconds"):
        assert (
            await observer.recover_item(workspace_id=WORKSPACE, source_id=SOURCE, item_id=item_id)
        ).complete
    async with sessions() as session, session.begin():
        await _scope(session)
        known = await session.scalar(
            select(SourceItemObservationRecord.known_at).where(
                SourceItemObservationRecord.item_id == "item",
                SourceItemObservationRecord.sequence == 1,
            )
        )
        first_recorded_cutoff = known + timedelta(microseconds=1)
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
        envelope = await repository.select_access_envelope(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id="item",
            purpose=SourceUsePurpose.METADATA,
        )
        assert envelope.decision.permitted is True
        assert envelope.revision.revision.revision_id == "r3"
        assert envelope.revision_known_at_upper_bound <= envelope.as_of
        assert envelope.historical_policy_id is not None
        assert envelope.historical_policy_sequence is not None
        assert envelope.current_policy_id == envelope.historical_policy_id
        assert envelope.current_policy_sequence == envelope.historical_policy_sequence
        assert envelope.rights_hash is not None
        assert len(envelope.rights_hash) == 64
        assert envelope.purpose is SourceUsePurpose.METADATA
        assert envelope.evaluated_at >= envelope.as_of
        purpose_denied = await repository.select_access_envelope(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id="item",
            purpose=SourceUsePurpose.TRAINING,
        )
        assert purpose_denied.decision.reason is SourceAccessReason.PURPOSE_NOT_APPROVED
        assert purpose_denied.revision is None
        retention_denied = await repository.select_access_envelope(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id="retained",
            purpose=SourceUsePurpose.METADATA,
        )
        assert retention_denied.decision.reason is SourceAccessReason.RETENTION_EXPIRED
        assert retention_denied.retention_expires_at is not None
        assert retention_denied.rights_hash is not None
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
            if column.name != "barrier_txid"
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
    await PostCommitSourceObserver(sessions).recover_item(
        workspace_id=WORKSPACE, source_id=SOURCE, item_id="repost"
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
            if column.name not in {"id", "created_at", "recorded_at", "barrier_txid"}
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
    await PostCommitSourceObserver(sessions).recover_item(
        workspace_id=WORKSPACE, source_id=SOURCE, item_id="late-arrival"
    )
    async with sessions() as session, session.begin():
        repository = SourceItemRepository(session)
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
            if column.name not in {"id", "created_at", "recorded_at", "barrier_txid"}
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
                if column.name not in {"id", "created_at", "recorded_at", "barrier_txid"}
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
            else "xid8"
            if field == "barrier_txid"
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
    columns = [
        column.name
        for column in SourceItemRevisionRecord.__table__.columns
        if column.name != "barrier_txid" or "barrier_txid" in values
    ]
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
    for item_id in ("valid-time-0", "valid-time-1"):
        await PostCommitSourceObserver(sessions).recover_item(
            workspace_id=WORKSPACE, source_id=SOURCE, item_id=item_id
        )
    async with sessions() as session, session.begin():
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


async def _utc_engine_boundary():
    """App engine ép UTC dù role mặc định và raw session render ngoài miền Python."""
    from trading_bot.adapters.database.engine import (
        admin_web_session_timeouts,
        create_database_engine,
    )

    raw = _engine()
    async with raw.connect() as connection:
        await connection.execute(
            text("ALTER ROLE CURRENT_USER SET timezone = 'Pacific/Kiritimati'")
        )
        await connection.commit()
    try:
        for timeouts in (None, admin_web_session_timeouts()):
            app = create_database_engine(raw.url, session_timeouts=timeouts)
            async with app.connect() as connection:
                assert await connection.scalar(text("SHOW TimeZone")) == "UTC"
            await app.dispose()
        async with raw.connect() as connection, connection.begin():
            for number, (zone, instant) in enumerate(
                (
                    ("Etc/GMT+12", "0001-01-01 00:00:00+00"),
                    ("Pacific/Kiritimati", "9999-12-31 23:59:59.999999+00"),
                )
            ):
                await connection.execute(
                    text("SELECT set_config('TimeZone', :zone, true)"), {"zone": zone}
                )
                await _raw_timestamp_insert(
                    connection, f"timezone-bound-{number}", {"event_at": instant}
                )
                nested = await connection.begin_nested()
                failed = False
                try:
                    await connection.scalar(
                        text("SELECT event_at FROM source_item_revision WHERE item_id = :item"),
                        {"item": f"timezone-bound-{number}"},
                    )
                except DBAPIError, ValueError, OverflowError:
                    failed = True
                finally:
                    await nested.rollback()
                assert failed, "Raw session không được tự clamp timestamp ngoài miền Python."
        app = create_database_engine(raw.url)
        sessions = async_sessionmaker(app, expire_on_commit=False)
        async with sessions() as session, session.begin():
            assert await session.scalar(text("SHOW TimeZone")) == "UTC"
            for number, year in ((0, 1), (1, 9999)):
                row = await session.scalar(
                    select(SourceItemRevisionRecord).where(
                        SourceItemRevisionRecord.item_id == f"timezone-bound-{number}"
                    )
                )
                assert row.event_at.year == year
                row.to_entry()
        await app.dispose()
    finally:
        async with raw.connect() as connection:
            await connection.execute(text("ALTER ROLE CURRENT_USER RESET timezone"))
            await connection.commit()
        await raw.dispose()
    return {"app_engine_utc_roundtrip": True}


async def _raw_item_receipt(session, item_id, revision_id):
    await session.execute(
        text(
            "INSERT INTO source_item_observation "
            "(id, workspace_id, source_id, item_id, revision_id, sequence, known_at) "
            "VALUES (gen_random_uuid(), :workspace, :source, :item, :revision, "
            "999, '0001-01-01+00'::timestamptz)"
        ),
        {"workspace": WORKSPACE, "source": SOURCE, "item": item_id, "revision": revision_id},
    )


async def _observer_boundary():
    """Hai connection và raw receipt kiểm commit barrier, cutoff và recovery thực."""
    writer_engine = _engine(INGESTION)
    reader_engine = _engine(INGESTION)
    writers = async_sessionmaker(writer_engine, expire_on_commit=False)
    readers = async_sessionmaker(reader_engine, expire_on_commit=False)
    try:
        # Lỗi top-level rollback cả source insert; không dùng flush giả commit.
        failure = None
        try:
            async with writers() as writer, writer.begin():
                await SourceItemRepository(writer).append_revision(
                    workspace_id=WORKSPACE,
                    revision=_revision(item_id="same-top-level"),
                    predecessor_revision_id=None,
                )
                await _raw_item_receipt(writer, "same-top-level", "r1")
        except DBAPIError as error:
            failure = str(error.orig)
        assert failure is not None
        assert "WP05_OBSERVATION_REQUIRES_NEW_TRANSACTION" in failure

        async with writers() as writer, readers() as reader:
            async with writer.begin():
                await _scope(writer)
                await _raw_timestamp_insert(writer, "forged-source-barrier", {"barrier_txid": "0"})
                assert await writer.scalar(
                    text(
                        "SELECT barrier_txid = pg_current_xact_id() FROM source_item_revision "
                        "WHERE item_id = 'forged-source-barrier'"
                    )
                )
                await SourceItemRepository(writer).append_revision(
                    workspace_id=WORKSPACE,
                    revision=_revision(
                        item_id="held-over-cutoff", canonical_url="https://example.invalid/held"
                    ),
                    predecessor_revision_id=None,
                )
                failure = None
                try:
                    async with writer.begin_nested():
                        await _raw_item_receipt(writer, "held-over-cutoff", "r1")
                except DBAPIError as error:
                    failure = str(error.orig)
                assert failure is not None
                assert "WP05_OBSERVATION_REQUIRES_NEW_TRANSACTION" in failure
                barrier, top = (
                    await writer.execute(
                        text(
                            "SELECT barrier_txid::text, pg_current_xact_id()::text "
                            "FROM source_item_revision WHERE item_id = 'held-over-cutoff'"
                        )
                    )
                ).one()
                assert barrier == top
                async with reader.begin():
                    assert not (
                        await SourceItemRepository(reader).get_history(
                            workspace_id=WORKSPACE, source_id=SOURCE, item_id="held-over-cutoff"
                        )
                    ).entries
                    try:
                        await SourceItemRepository(reader).finalize_revision(
                            workspace_id=WORKSPACE,
                            source_id=SOURCE,
                            item_id="held-over-cutoff",
                            revision_id="r1",
                        )
                    except LookupError:
                        pass
                    else:
                        raise AssertionError("Observer thấy source chưa commit.")
                    cutoff = await reader.scalar(text("SELECT clock_timestamp()"))
            async with reader.begin():
                assert (
                    await SourceItemRepository(reader).select_accessible(
                        workspace_id=WORKSPACE,
                        source_id=SOURCE,
                        item_id="held-over-cutoff",
                        purpose=SourceUsePurpose.METADATA,
                        as_of=cutoff,
                    )
                    is None
                )
                await _scope(reader)
                await _raw_item_receipt(reader, "held-over-cutoff", "r1")
                receipt = await reader.scalar(
                    select(SourceItemObservationRecord).where(
                        SourceItemObservationRecord.item_id == "held-over-cutoff"
                    )
                )
                known, receipt_id = receipt.known_at, receipt.id
                assert known > cutoff
                assert receipt.sequence == 1
            async with reader.begin():
                repository = SourceItemRepository(reader)
                assert (
                    await repository.select_accessible(
                        workspace_id=WORKSPACE,
                        source_id=SOURCE,
                        item_id="held-over-cutoff",
                        purpose=SourceUsePurpose.METADATA,
                        as_of=known,
                    )
                    is None
                )
                assert (
                    await repository.select_accessible(
                        workspace_id=WORKSPACE,
                        source_id=SOURCE,
                        item_id="held-over-cutoff",
                        purpose=SourceUsePurpose.METADATA,
                        as_of=known + timedelta(microseconds=1),
                    )
                    is not None
                )
                retry = await repository.finalize_revision(
                    workspace_id=WORKSPACE,
                    source_id=SOURCE,
                    item_id="held-over-cutoff",
                    revision_id="r1",
                )
                assert (retry.id, retry.known_at) == (receipt_id, known)

        # Mất process giữa commit/finalize: không receipt thì deny; recovery có cursor.
        async with writers() as writer, writer.begin():
            repository = SourceItemRepository(writer)
            await repository.append_revision(
                workspace_id=WORKSPACE,
                revision=_revision(
                    item_id="recover-gap", canonical_url="https://example.invalid/recovery"
                ),
                predecessor_revision_id=None,
            )
            await repository.append_revision(
                workspace_id=WORKSPACE,
                revision=_revision(
                    "r2", item_id="recover-gap", canonical_url="https://example.invalid/recovery"
                ),
                predecessor_revision_id="r1",
            )
        await _expect_denied(
            INGESTION,
            "INSERT INTO source_item_observation "
            "(id, workspace_id, source_id, item_id, revision_id, sequence) "
            "VALUES (gen_random_uuid(), :workspace, :source, 'recover-gap', 'r2', 2)",
            "WP05_OBSERVATION_PREDECESSOR_MISSING",
            parameters={"workspace": WORKSPACE, "source": SOURCE},
        )
        observer = PostCommitSourceObserver(readers)
        first = await observer.recover_item(
            workspace_id=WORKSPACE, source_id=SOURCE, item_id="recover-gap", limit=1
        )
        assert (first.last_sequence, first.complete) == (1, False)
        async with readers() as reader, reader.begin():
            assert (
                await SourceItemRepository(reader).select_accessible(
                    workspace_id=WORKSPACE,
                    source_id=SOURCE,
                    item_id="recover-gap",
                    purpose=SourceUsePurpose.METADATA,
                )
                is None
            )
        second = await observer.recover_item(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id="recover-gap",
            after_sequence=1,
            limit=1,
        )
        assert (second.last_sequence, second.complete) == (2, True)
        _, progress = await observer.append_item(
            workspace_id=WORKSPACE,
            revision=_revision(
                item_id="adapter-write", canonical_url="https://example.invalid/adapter"
            ),
            predecessor_revision_id=None,
        )
        assert progress.complete
    finally:
        await writer_engine.dispose()
        await reader_engine.dispose()

    # Policy INSERT giữ qua cutoff: latest raw unfinalized đóng cả quota và access.
    engine = _engine(ADMIN)
    peer = _engine(ADMIN)
    writes = async_sessionmaker(engine, expire_on_commit=False)
    observes = async_sessionmaker(peer, expire_on_commit=False)
    try:
        policy_source = "policy-observer-fixture"
        at = datetime.now(UTC)
        entry = SourceRegistryEntry.discover(
            source_id=policy_source,
            publisher="Giả lập",
            endpoint_url="https://example.invalid/policy",
            access_method="GET",
            auth_scope="fixture-only",
            actor_id="operator",
            at=at,
            reason="Fixture observer",
            budget=SourceBudget(
                window=timedelta(hours=1),
                max_requests=3,
                max_bytes=1000,
                max_cost=Decimal(0),
                cost_unit="USD",
            ),
        )
        entry = entry.transition(
            policy_id="observer-pending",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="operator",
            at=at,
            reason="Rà quyền",
        )
        entry = entry.transition(
            policy_id="observer-verified",
            state=SourcePolicyState.VERIFIED,
            actor_id="operator",
            approved_by="reviewer",
            at=at,
            reviewed_at=at,
            evidence_ref="fixture:observer",
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
            reason="Duyệt fixture",
        )
        async with writes() as writer, observes() as reader:
            async with writer.begin():
                for revision in entry.revisions:
                    await SourceRegistryRepository(writer).append_revision(
                        workspace_id=WORKSPACE, revision=revision
                    )
                failure = None
                try:
                    async with writer.begin_nested():
                        await writer.execute(
                            text(
                                "INSERT INTO source_policy_observation "
                                "(id, workspace_id, source_id, policy_id, sequence, known_at) "
                                "VALUES (gen_random_uuid(), :workspace, :source, :policy, "
                                "3, '0001-01-01+00')"
                            ),
                            {
                                "workspace": WORKSPACE,
                                "source": policy_source,
                                "policy": entry.current.policy.policy_id,
                            },
                        )
                except DBAPIError as error:
                    failure = str(error.orig)
                assert failure is not None
                assert "WP05_OBSERVATION_REQUIRES_NEW_TRANSACTION" in failure
                async with reader.begin():
                    assert (
                        await SourceRegistryRepository(reader).get_entry(
                            workspace_id=WORKSPACE, source_id=policy_source
                        )
                        is None
                    )
                    try:
                        await SourceRegistryRepository(reader).finalize_policy(
                            workspace_id=WORKSPACE,
                            source_id=policy_source,
                            policy_id=entry.current.policy.policy_id,
                        )
                    except LookupError:
                        pass
                    else:
                        raise AssertionError("Policy observer thấy source chưa commit.")
                    cutoff = await reader.scalar(text("SELECT clock_timestamp()"))
            async with reader.begin():
                assert (
                    await SourceRegistryRepository(reader).get_observed_entry(
                        workspace_id=WORKSPACE, source_id=policy_source
                    )
                    is None
                )
            item_engine = _engine(INGESTION)
            item_sessions = async_sessionmaker(item_engine, expire_on_commit=False)
            await PostCommitSourceObserver(item_sessions).append_item(
                workspace_id=WORKSPACE,
                revision=_revision(
                    source_id=policy_source,
                    item_id="policy-gate",
                    canonical_url="https://example.invalid/policy-gate",
                ),
                predecessor_revision_id=None,
            )
            observer = PostCommitSourceObserver(observes, quota_sessions=item_sessions)
            first = await observer.recover_policy(
                workspace_id=WORKSPACE, source_id=policy_source, limit=2
            )
            assert (first.last_sequence, first.complete) == (2, False)
            async with reader.begin():
                assert (
                    await SourceRegistryRepository(reader).get_observed_entry(
                        workspace_id=WORKSPACE, source_id=policy_source
                    )
                    is None
                )
            progress = await observer.recover_policy(
                workspace_id=WORKSPACE, source_id=policy_source, after_sequence=2
            )
            assert progress.complete
            async with reader.begin():
                observed = await SourceRegistryRepository(reader).get_observed_entry(
                    workspace_id=WORKSPACE, source_id=policy_source
                )
                known = observed.known_at[entry.current.policy.policy_id]
                assert known > cutoff
                audit = observed.history.current
                assert audit.created_at < known
                receipt = await SourceRegistryRepository(reader).finalize_policy(
                    workspace_id=WORKSPACE,
                    source_id=policy_source,
                    policy_id=audit.policy.policy_id,
                )
                assert receipt.known_at == known
        # Item dùng policy mới để kiểm strict policy observation boundary thật.
        async with item_sessions() as reader, reader.begin():
            for boundary, expected in ((known, False), (known + timedelta(microseconds=1), True)):
                selected = await SourceItemRepository(reader).select_accessible(
                    workspace_id=WORKSPACE,
                    source_id=policy_source,
                    item_id="policy-gate",
                    purpose=SourceUsePurpose.METADATA,
                    as_of=boundary,
                )
                assert (selected is not None) is expected
            assert (
                await SourceItemRepository(reader).select_accessible(
                    workspace_id=WORKSPACE,
                    source_id=policy_source,
                    item_id="policy-gate",
                    purpose=SourceUsePurpose.METADATA,
                    as_of=cutoff,
                )
                is None
            )
            assert (
                await SourceItemRepository(reader).select_accessible(
                    workspace_id=WORKSPACE,
                    source_id=policy_source,
                    item_id="policy-gate",
                    purpose=SourceUsePurpose.METADATA,
                )
                is not None
            )
        async with writes() as writer, writer.begin():
            current = await SourceRegistryRepository(writer).get_entry(
                workspace_id=WORKSPACE, source_id=policy_source
            )
            revoked = current.transition(
                policy_id="observer-revoked",
                state=SourcePolicyState.REVOKED,
                actor_id="operator",
                at=datetime.now(UTC),
                reason="Thu hồi fixture",
            )
            await SourceRegistryRepository(writer).append_revision(
                workspace_id=WORKSPACE, revision=revoked.current
            )
        async with item_sessions() as reader, reader.begin():
            repository = SourceItemRepository(reader)
            assert (
                await repository.select_accessible(
                    workspace_id=WORKSPACE,
                    source_id=policy_source,
                    item_id="policy-gate",
                    purpose=SourceUsePurpose.METADATA,
                )
                is None
            )
            denied = False
            try:
                await SourceRegistryRepository(reader).reserve_quota(
                    workspace_id=WORKSPACE,
                    source_id=policy_source,
                    idempotency_key="unfinalized-revoke",
                    purpose=SourceUsePurpose.METADATA,
                    reserved_bytes=1,
                    reserved_cost="0",
                )
            except PermissionError:
                denied = True
            assert denied
        assert (
            await observer.recover_policy(
                workspace_id=WORKSPACE, source_id=policy_source, after_sequence=3
            )
        ).complete
        async with item_sessions() as reader, reader.begin():
            assert (
                await SourceItemRepository(reader).select_accessible(
                    workspace_id=WORKSPACE,
                    source_id=policy_source,
                    item_id="policy-gate",
                    purpose=SourceUsePurpose.METADATA,
                )
                is None
            )
        recheck = revoked.transition(
            policy_id="observer-recheck",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="operator",
            at=datetime.now(UTC),
            reason="Rà quyền lại",
        )
        from uuid import uuid4

        from trading_bot.application.source_registry import SourceRegistryService
        from trading_bot.auth.policy import Principal, Role

        owner = Principal(uuid4(), uuid4(), WORKSPACE, frozenset({Role.OWNER}), datetime.now(UTC))
        reviewer = Principal(
            uuid4(), uuid4(), WORKSPACE, frozenset({Role.ANALYST_REVIEWER}), datetime.now(UTC)
        )
        facade = SourceRegistryService(observer).for_principal(owner)
        await facade.append_revision(recheck.current)
        now = datetime.now(UTC)
        approved = recheck.transition(
            policy_id="observer-app-verified",
            state=SourcePolicyState.VERIFIED,
            actor_id="forged",
            approved_by="forged-reviewer",
            at=now,
            reviewed_at=now,
            evidence_ref="fixture:app-rights",
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
            reason="Duyệt app fixture",
        )
        saved = await facade.append_revision(approved.current, reviewer=reviewer)
        assert saved.actor_id == str(owner.user_id)
        assert saved.approved_by == str(reviewer.user_id)
        retry = await facade.append_revision(approved.current, reviewer=reviewer)
        assert retry == saved
        usage = await facade.reserve_usage(
            source_id=policy_source,
            idempotency_key="principal-observer-quota",
            purpose=SourceUsePurpose.METADATA,
            reserved_bytes=1,
            reserved_cost="0",
        )
        assert not usage.is_duplicate
        await item_engine.dispose()
    finally:
        await engine.dispose()
        await peer.dispose()

    for role in (
        "crypidian_trading_runtime",
        "crypidian_trainer_worker",
        "crypidian_execution_gateway",
    ):
        for table in ("source_item_observation", "source_policy_observation"):
            await _expect_denied(role, f"SELECT * FROM {table}")
    await _expect_denied(ADMIN, "INSERT INTO source_item_observation DEFAULT VALUES")
    await _expect_denied(INGESTION, "INSERT INTO source_policy_observation DEFAULT VALUES")
    for table in ("source_item_observation", "source_policy_observation"):
        await _expect_denied(None, f"DELETE FROM {table}", "WP05_SOURCE_HISTORY_APPEND_ONLY")
        await _expect_denied(
            None, f"UPDATE {table} SET known_at = known_at", "WP05_SOURCE_HISTORY_APPEND_ONLY"
        )
    engine = _engine(INGESTION)
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM source_item_observation")) == 0
        await connection.execute(
            text("SELECT set_config('app.workspace_id', :workspace, false)"),
            {"workspace": str(OTHER)},
        )
        assert await connection.scalar(text("SELECT count(*) FROM source_item_observation")) == 0
        await connection.execute(
            text("SELECT set_config('app.workspace_id', :workspace, false)"),
            {"workspace": str(WORKSPACE)},
        )
        assert await connection.scalar(text("SELECT count(*) FROM source_item_observation")) > 0
        assert await connection.scalar(text("SELECT count(*) FROM source_policy_observation")) > 0
    await engine.dispose()
    await _expect_denied(
        INGESTION,
        "INSERT INTO source_item_observation "
        "(id, workspace_id, source_id, item_id, revision_id, sequence) "
        "VALUES (gen_random_uuid(), :workspace, :source, 'held-over-cutoff', 'r1', 1)",
        "WP05_OBSERVATION_SCOPE_INVALID",
        workspace=OTHER,
        parameters={"workspace": WORKSPACE, "source": SOURCE},
    )
    return {"post_commit_observation": True, "strict_boundary_and_policy_gate": True}


async def _main():
    handlers = {
        "seed": _seed,
        "review_regressions": _review_regressions,
        "timestamp_range": _timestamp_range,
        "utc_boundary": _utc_engine_boundary,
        "observe_legacy": _observe_legacy,
        "observer_boundary": _observer_boundary,
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

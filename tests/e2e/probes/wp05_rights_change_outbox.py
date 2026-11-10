"""Kiểm tra quyền nguồn tạo outbox bền vững, không dùng dữ liệu mạng thật."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import (
    OutboxEvent,
    SourceItemRevisionRecord,
    Workspace,
)
from trading_bot.adapters.database.repositories.source_items import SourceItemRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.domain.research.source_item_history import SourceItemHistory
from trading_bot.domain.research.source_items import SourceItemRevision
from trading_bot.domain.research.source_policy import SourcePolicyState
from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry

STATE = json.loads(os.environ["WP05_OUTBOX_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
SOURCE = "outbox-fixture"
EVENT_TYPE = "research.rights_changed.v1"
ADMIN = "crypidian_admin_web"
INGESTION = "crypidian_ingestion_worker"
APP = "crypidian_app"
PRIVATE_NOTE = "private-source-note-must-not-enter-event"


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


async def _scope(session):
    await session.execute(
        text("SELECT set_config('app.workspace_id', :workspace, true)"),
        {"workspace": str(WORKSPACE)},
    )


async def _count_events(sessions) -> int:
    async with sessions() as session, session.begin():
        await _scope(session)
        count = await session.scalar(
            select(text("count(*)"))
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.workspace_id == WORKSPACE,
                OutboxEvent.event_type == EVENT_TYPE,
            )
        )
        return int(count or 0)


async def _run() -> dict[str, object]:
    base_engine = _engine()
    base_sessions = async_sessionmaker(base_engine, expire_on_commit=False)
    async with base_sessions() as session, session.begin():
        session.add(
            Workspace(
                id=WORKSPACE,
                name="Rights outbox fixture",
                slug=f"rights-{WORKSPACE.hex[:10]}",
            )
        )

    admin_engine = _engine(ADMIN)
    admin_sessions = async_sessionmaker(admin_engine, expire_on_commit=False)
    at = datetime.now(UTC)
    budget = SourceBudget(
        window=timedelta(hours=1),
        max_requests=3,
        max_bytes=1000,
        max_cost=Decimal(0),
        cost_unit="USD",
    )
    entry = SourceRegistryEntry.discover(
        source_id=SOURCE,
        publisher="Nhà xuất bản giả lập",
        endpoint_url="https://example.invalid/feed.json",
        access_method="GET",
        auth_scope="fixture-only",
        budget=budget,
        actor_id="fixture-operator",
        at=at,
        reason=PRIVATE_NOTE,
    )
    entry = SourceRegistryEntry(
        SOURCE,
        (
            replace(
                entry.current,
                policy=replace(entry.current.policy, policy_id=PRIVATE_NOTE),
            ),
        ),
    )
    async with admin_sessions() as session, session.begin():
        await _scope(session)
        repository = SourceRegistryRepository(session)
        first = await repository.append_revision(workspace_id=WORKSPACE, revision=entry.current)
        entry = SourceRegistryEntry(SOURCE, (first.revision,))
        pending = entry.transition(
            policy_id=f"{SOURCE}:policy:2",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="fixture-operator",
            at=first.revision.created_at + timedelta(microseconds=1),
            reason=PRIVATE_NOTE,
        )
        second = await repository.append_revision(workspace_id=WORKSPACE, revision=pending.current)
        entry = SourceRegistryEntry(SOURCE, (first.revision, second.revision))

    ingestion_engine = _engine(INGESTION)
    ingestion_sessions = async_sessionmaker(ingestion_engine, expire_on_commit=False)
    app_engine = _engine(APP)
    app_sessions = async_sessionmaker(app_engine, expire_on_commit=False)
    observed_at = datetime.now(UTC)
    revision = SourceItemRevision(
        source_id=SOURCE,
        item_id="synthetic-item",
        revision_id="revision-1",
        canonical_url="https://example.invalid/path?private=body-marker",
        observed_at=observed_at,
        available_at=observed_at,
    )
    history = SourceItemHistory.empty(source_id=SOURCE, item_id=revision.item_id).append(
        revision, predecessor_revision_id=None
    )
    record = SourceItemRevisionRecord.from_entry(WORKSPACE, 1, history.entries[-1])
    record.id = uuid4()
    raw_columns = tuple(
        column.name
        for column in SourceItemRevisionRecord.__table__.columns
        if column.name not in {"barrier_txid", "recorded_at", "created_at"}
    )
    raw_values = {name: getattr(record, name) for name in raw_columns}
    raw_column_sql = ", ".join(raw_columns)
    raw_bind_sql = ", ".join(f":{name}" for name in raw_columns)
    async with ingestion_sessions() as session, session.begin():
        await _scope(session)
        await session.execute(
            text(f"INSERT INTO source_item_revision ({raw_column_sql}) VALUES ({raw_bind_sql})"),
            raw_values,
        )

    async with admin_sessions() as session, session.begin():
        await _scope(session)
        await SourceItemRepository(session).take_down(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id=revision.item_id,
            reason_code="license_withdrawal",
        )

    initial_count = await _count_events(base_sessions)
    async with admin_sessions() as session, session.begin():
        await _scope(session)
        repository = SourceRegistryRepository(session)
        duplicate_policy = await repository.append_revision(
            workspace_id=WORKSPACE, revision=pending.current
        )
        assert duplicate_policy.is_duplicate
    async with ingestion_sessions() as session, session.begin():
        await _scope(session)
        duplicate_item = await SourceItemRepository(session).append_revision(
            workspace_id=WORKSPACE, revision=revision, predecessor_revision_id=None
        )
        assert duplicate_item.is_duplicate
    duplicate_count = await _count_events(base_sessions)

    async with admin_sessions() as session:
        transaction = await session.begin()
        await _scope(session)
        blocked = entry.transition(
            policy_id=f"{SOURCE}:policy:3",
            state=SourcePolicyState.BLOCKED,
            actor_id="fixture-operator",
            at=entry.current.created_at + timedelta(microseconds=1),
            reason=PRIVATE_NOTE,
        )
        await SourceRegistryRepository(session).append_revision(
            workspace_id=WORKSPACE, revision=blocked.current
        )
        await transaction.rollback()
    rollback_count = await _count_events(base_sessions)

    async with app_sessions() as session:
        try:
            async with session.begin():
                await _scope(session)
                session.add(
                    OutboxEvent(
                        workspace_id=WORKSPACE,
                        event_type=EVENT_TYPE,
                        aggregate_type="source_policy",
                        aggregate_id=uuid4(),
                        occurred_at=datetime.now(UTC),
                        correlation_id=uuid4(),
                        idempotency_key="caller-forged-rights-event",
                        schema_version=1,
                        payload={"workspace_id": str(WORKSPACE), "source_id": SOURCE},
                    )
                )
                await session.flush()
        except DBAPIError:
            forged_rejected = True
        else:
            forged_rejected = False

    async with app_sessions() as session:
        try:
            async with session.begin():
                await _scope(session)
                event = await session.scalar(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.workspace_id == WORKSPACE,
                        OutboxEvent.event_type == EVENT_TYPE,
                    )
                    .limit(1)
                    .with_for_update()
                )
                assert event is not None
                event.payload = {"changed": True}
                await session.flush()
        except DBAPIError:
            payload_rewrite_rejected = True
        else:
            payload_rewrite_rejected = False

    async with base_sessions() as session, session.begin():
        await _scope(session)
        event = await session.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.workspace_id == WORKSPACE, OutboxEvent.event_type == EVENT_TYPE)
            .limit(1)
            .with_for_update()
        )
        assert event is not None
        event.status = "PROCESSING"
        event.attempts += 1
        event.lease_owner = "fixture-worker"
        event.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    delivery_update_allowed = True

    async with base_sessions() as session, session.begin():
        await _scope(session)
        rows = await session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.workspace_id == WORKSPACE, OutboxEvent.event_type == EVENT_TYPE)
            .order_by(OutboxEvent.id)
        )
        events = list(rows.all())
        encoded = json.dumps(
            [
                {"idempotency_key": event.idempotency_key, "payload": event.payload}
                for event in events
            ],
            sort_keys=True,
        )
        payloads_are_source_only = (
            all("body" not in event.payload and "url" not in event.payload for event in events)
            and PRIVATE_NOTE not in encoded
            and "private=body-marker" not in encoded
            and len(events) == 4
        )
        assert len(events) == 4, [
            (event.event_type, event.aggregate_type, event.payload) for event in events
        ]

    await admin_engine.dispose()
    await ingestion_engine.dispose()
    await app_engine.dispose()
    await base_engine.dispose()
    return {
        "rights_event_count": initial_count,
        "duplicate_retry_count": duplicate_count,
        "rollback_preserved_count": rollback_count,
        "event_payloads_are_source_only": payloads_are_source_only,
        "forged_event_rejected": forged_rejected,
        "payload_rewrite_rejected": payload_rewrite_rejected,
        "delivery_update_allowed": delivery_update_allowed,
    }


print(json.dumps(asyncio.run(_run())))

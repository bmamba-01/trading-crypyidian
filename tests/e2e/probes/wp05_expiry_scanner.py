"""Probe DB-clock expiry event, idempotency và renewal giữ nguyên lịch sử."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import (
    OutboxEvent,
    SourcePolicyRevisionRecord,
    Workspace,
)
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.source_policy_expiry import SourcePolicyExpiryScanner
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry

STATE = json.loads(os.environ["WP05_EXPIRY_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
SOURCE = "expiry-fixture"
ADMIN = "crypidian_admin_web"
INGESTION = "crypidian_ingestion_worker"
EVENT_TYPE = "research.rights_changed.v1"


def _engine(role: str):
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    url = URL.create(
        "postgresql+psycopg",
        username=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )
    return create_async_engine(url, connect_args={"options": f"-c role={role}"})


async def _scope(session) -> None:
    await session.execute(
        text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
        {"workspace_id": str(WORKSPACE)},
    )


async def _db_now(session) -> datetime:
    value = await session.scalar(text("SELECT clock_timestamp()"))
    assert isinstance(value, datetime)
    assert value.tzinfo is not None
    return value.astimezone(UTC)


async def _scan(sessions) -> int:
    async with sessions() as session, session.begin():
        writer = SourceRegistryRepository(session)
        return await SourcePolicyExpiryScanner(writer).scan_workspace(WORKSPACE, batch_size=10)


async def _expiry_event_data(sessions) -> tuple[int, bool]:
    async with sessions() as session, session.begin():
        await _scope(session)
        rows = await session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.workspace_id == WORKSPACE,
                OutboxEvent.event_type == EVENT_TYPE,
            )
        )
        expiry_events = [
            row for row in rows.all() if row.payload.get("change_type") == "policy_expired"
        ]
        safe = len(expiry_events) == 1 and all(
            PRIVATE_MARKER not in json.dumps({"payload": row.payload, "key": row.idempotency_key})
            for row in expiry_events
        )
        return len(expiry_events), safe


PRIVATE_MARKER = "private-expiry-policy-id-marker"


async def _run() -> dict[str, object]:
    default_engine = _engine("crypidian_admin")
    default_sessions = async_sessionmaker(default_engine, expire_on_commit=False)
    async with default_sessions() as session, session.begin():
        session.add(
            Workspace(id=WORKSPACE, name="Expiry fixture", slug=f"expiry-{WORKSPACE.hex[:10]}")
        )

    admin_engine = _engine(ADMIN)
    admin_sessions = async_sessionmaker(admin_engine, expire_on_commit=False)
    budget = SourceBudget(
        window=timedelta(hours=1),
        max_requests=3,
        max_bytes=1000,
        max_cost=Decimal(0),
        cost_unit="USD",
    )
    async with admin_sessions() as session, session.begin():
        await _scope(session)
        database_now = await _db_now(session)
        entry = SourceRegistryEntry.discover(
            source_id=SOURCE,
            publisher="Nhà xuất bản giả lập",
            endpoint_url="https://example.invalid/expiry.json",
            access_method="GET",
            auth_scope="fixture-only",
            budget=budget,
            actor_id="expiry-operator",
            at=database_now,
            reason="fixture only",
        )
        first = await SourceRegistryRepository(session).append_revision(
            workspace_id=WORKSPACE, revision=entry.current
        )
        entry = SourceRegistryEntry(SOURCE, (first.revision,))
        pending = entry.transition(
            policy_id=f"{SOURCE}:policy:2",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="expiry-operator",
            at=first.revision.created_at + timedelta(microseconds=1),
            reason="fixture only",
        )
        second = await SourceRegistryRepository(session).append_revision(
            workspace_id=WORKSPACE, revision=pending.current
        )
        entry = SourceRegistryEntry(SOURCE, (first.revision, second.revision))
        expiry_now = await _db_now(session)
        grant = entry.transition(
            policy_id=PRIVATE_MARKER,
            state=SourcePolicyState.VERIFIED,
            actor_id="expiry-operator",
            approved_by="separate-reviewer",
            evidence_ref="fixture:approved-metadata-only",
            reviewed_at=expiry_now,
            valid_until=expiry_now + timedelta(seconds=2),
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
            at=expiry_now + timedelta(microseconds=1),
            reason="fixture only",
        )
        third = await SourceRegistryRepository(session).append_revision(
            workspace_id=WORKSPACE, revision=grant.current
        )
        history = (first.revision, second.revision, third.revision)

    ingestion_engine = _engine(INGESTION)
    ingestion_sessions = async_sessionmaker(ingestion_engine, expire_on_commit=False)
    before_expiry_events = await _scan(ingestion_sessions)
    async with ingestion_sessions() as session, session.begin():
        await session.execute(text("SELECT pg_catalog.pg_sleep(2.1)"))
    first_scan_events = await _scan(ingestion_sessions)
    repeat_scan_events = await _scan(ingestion_sessions)
    expiry_event_count, expiry_payload_is_safe = await _expiry_event_data(default_sessions)

    async with admin_sessions() as session, session.begin():
        await _scope(session)
        now = await _db_now(session)
        history_entry = SourceRegistryEntry(SOURCE, history)
        renewal_pending = history_entry.transition(
            policy_id=f"{SOURCE}:policy:4",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="expiry-operator",
            at=now + timedelta(microseconds=1),
            reason="renewal fixture only",
        )
        fourth = await SourceRegistryRepository(session).append_revision(
            workspace_id=WORKSPACE, revision=renewal_pending.current
        )
        renewed_entry = SourceRegistryEntry(SOURCE, (*history, fourth.revision))
        renewal_now = await _db_now(session)
        renewed = renewed_entry.transition(
            policy_id=f"{SOURCE}:policy:5",
            state=SourcePolicyState.VERIFIED,
            actor_id="expiry-operator",
            approved_by="separate-reviewer",
            evidence_ref="fixture:renewed-metadata-only",
            reviewed_at=renewal_now,
            valid_until=renewal_now + timedelta(days=1),
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
            at=renewal_now + timedelta(microseconds=1),
            reason="renewal fixture only",
        )
        await SourceRegistryRepository(session).append_revision(
            workspace_id=WORKSPACE, revision=renewed.current
        )
        old_grant = await session.scalar(
            select(SourcePolicyRevisionRecord).where(
                SourcePolicyRevisionRecord.workspace_id == WORKSPACE,
                SourcePolicyRevisionRecord.policy_id == PRIVATE_MARKER,
            )
        )
        assert old_grant is not None
        assert old_grant.state == "verified"
        expired_policy_history_unchanged = old_grant.state == "verified"

    renewed_scan_events = await _scan(ingestion_sessions)
    final_expiry_event_count, safe_after_renewal = await _expiry_event_data(default_sessions)
    await admin_engine.dispose()
    await ingestion_engine.dispose()
    await default_engine.dispose()
    return {
        "before_expiry_events": before_expiry_events,
        "first_scan_events": first_scan_events,
        "repeat_scan_events": repeat_scan_events,
        "renewed_scan_events": renewed_scan_events,
        "expiry_event_count_after_renewal": final_expiry_event_count,
        "expired_policy_history_unchanged": expired_policy_history_unchanged,
        "expiry_payload_is_safe": expiry_payload_is_safe and safe_after_renewal,
    }


print(json.dumps(asyncio.run(_run())))

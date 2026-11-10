"""Probe PostgreSQL cho consumer idempotent và gate khi rights event chưa xử lý."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import (
    DatasetManifestEligibilityEventRecord,
    OutboxEvent,
    SourcePolicyRevisionRecord,
    Workspace,
)
from trading_bot.adapters.database.repositories.dataset_manifest import DatasetManifestRepository
from trading_bot.adapters.database.repositories.dataset_manifest_eligibility import (
    DatasetManifestEligibilityRepository,
)
from trading_bot.adapters.database.repositories.source_items import SourceItemRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.domain.learning.dataset import (
    DatasetManifest,
    DatasetSample,
    DatasetSourceReference,
    DatasetSplit,
)
from trading_bot.domain.research.source_items import SourceItemRevision
from trading_bot.domain.research.source_policy import (
    SourcePolicyState,
    SourceUsePurpose,
)
from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry
from trading_bot.workers.research_rights_worker import ResearchRightsMaintenanceWorker

STATE = json.loads(os.environ["WP06_RIGHTS_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
OTHER_WORKSPACE = UUID(STATE["other_workspace_id"])
SOURCE = "rights-fixture"
OTHER_SOURCE = "other-tenant-rights-fixture"
ITEM = "item-1"
ADMIN = "crypidian_admin_web"
TRAINER = "crypidian_trainer_worker"
INGESTION = "crypidian_ingestion_worker"


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


async def _scope(session) -> None:
    await session.execute(
        text("SELECT pg_catalog.set_config('app.workspace_id', :workspace_id, true)"),
        {"workspace_id": str(WORKSPACE)},
    )


async def _db_now(session) -> datetime:
    value = await session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
    assert isinstance(value, datetime)
    assert value.tzinfo is not None
    return value.astimezone(UTC)


async def _process(ingestion_sessions) -> dict[str, int]:
    result = await ResearchRightsMaintenanceWorker(
        session_factory=ingestion_sessions,
        workspace_id=WORKSPACE,
        worker_id="wp06-rights-e2e",
        tick_seconds=5,
    ).run_once()
    return {
        "expiry_events_enqueued": result.expiry_events_enqueued,
        "claimed": result.rights_batch.claimed,
        "processed": result.rights_batch.processed,
        "invalidated_references": result.rights_batch.invalidated_references,
        "failed": result.rights_batch.failed,
        "dead_lettered": result.rights_batch.dead_lettered,
    }


async def _ingestion_outbox_scope(ingestion_sessions) -> dict[str, int]:
    async with ingestion_sessions() as session, session.begin():
        await _scope(session)
        current_workspace_rows = await session.scalar(select(func.count()).select_from(OutboxEvent))
        await session.execute(
            text("SELECT pg_catalog.set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(OTHER_WORKSPACE)},
        )
        other_workspace_rows = await session.scalar(select(func.count()).select_from(OutboxEvent))
        await session.execute(text("SELECT pg_catalog.set_config('app.workspace_id', '', true)"))
        unscoped_rows = await session.scalar(select(func.count()).select_from(OutboxEvent))
        return {
            "current_workspace_rows": int(current_workspace_rows or 0),
            "other_workspace_rows": int(other_workspace_rows or 0),
            "unscoped_rows": int(unscoped_rows or 0),
        }


async def _seed(default_sessions, admin_sessions) -> str:
    async with default_sessions() as session, session.begin():
        session.add(
            Workspace(id=WORKSPACE, name="WP06 rights", slug=f"rights-{WORKSPACE.hex[:10]}")
        )
        session.add(
            Workspace(
                id=OTHER_WORKSPACE,
                name="WP06 other tenant",
                slug=f"rights-other-{OTHER_WORKSPACE.hex[:10]}",
            )
        )

    budget = SourceBudget(
        window=timedelta(hours=1),
        max_requests=5,
        max_bytes=1000,
        max_cost=Decimal(0),
        cost_unit="USD",
    )
    async with admin_sessions() as session, session.begin():
        await _scope(session)
        now = await _db_now(session)
        discovered = SourceRegistryEntry.discover(
            source_id=SOURCE,
            publisher="Nhà xuất bản giả lập",
            endpoint_url="https://example.invalid/rights.json",
            access_method="GET",
            auth_scope="fixture-only",
            budget=budget,
            actor_id="rights-operator",
            at=now,
            reason="fixture only",
        )
        first = await SourceRegistryRepository(session).append_revision(
            workspace_id=WORKSPACE,
            revision=discovered.current,
        )

    async with admin_sessions() as session, session.begin():
        await session.execute(
            text("SELECT pg_catalog.set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(OTHER_WORKSPACE)},
        )
        now = await _db_now(session)
        discovered = SourceRegistryEntry.discover(
            source_id=OTHER_SOURCE,
            publisher="Nhà xuất bản tenant khác",
            endpoint_url="https://example.invalid/other-tenant-rights.json",
            access_method="GET",
            auth_scope="fixture-only",
            budget=budget,
            actor_id="rights-operator",
            at=now,
            reason="fixture only",
        )
        await SourceRegistryRepository(session).append_revision(
            workspace_id=OTHER_WORKSPACE,
            revision=discovered.current,
        )

    async with default_sessions() as session, session.begin():
        now = await _db_now(session)
        session.add(
            OutboxEvent(
                workspace_id=WORKSPACE,
                event_type="fixture.unrelated.v1",
                aggregate_type="fixture",
                aggregate_id=UUID("44444444-4444-4444-4444-444444444444"),
                occurred_at=now,
                correlation_id=UUID("55555555-5555-5555-5555-555555555555"),
                idempotency_key="fixture-unrelated-rights-worker",
                schema_version=1,
                payload={"fixture": True},
            )
        )

    ingestion_engine = _engine(INGESTION)
    ingestion_sessions = async_sessionmaker(ingestion_engine, expire_on_commit=False)
    async with ingestion_sessions() as session, session.begin():
        await _scope(session)
        observed_at = await _db_now(session)
        await SourceItemRepository(session).append_revision(
            workspace_id=WORKSPACE,
            revision=SourceItemRevision(
                source_id=SOURCE,
                item_id=ITEM,
                revision_id="revision-1",
                canonical_url="https://example.invalid/wp06/rights-item",
                observed_at=observed_at,
                available_at=observed_at,
            ),
            predecessor_revision_id=None,
        )

    async with admin_sessions() as session, session.begin():
        await _scope(session)
        now = await _db_now(session)
        reference = DatasetSourceReference(
            source_id=SOURCE,
            item_id=ITEM,
            revision_id="revision-1",
            policy_id=first.revision.policy.policy_id,
            policy_sequence=first.revision.sequence,
            purpose=SourceUsePurpose.TRAINING,
            rights_hash="a" * 64,
        )
        sample = DatasetSample(
            sample_id="sample-1",
            decision_at=now + timedelta(hours=1),
            feature_available_at=now,
            outcome_start=now + timedelta(hours=1),
            outcome_end=now + timedelta(hours=2),
            feature_snapshot_hash="b" * 64,
            outcome_id="outcome-1",
            outcome_hash="c" * 64,
            label_hash="d" * 64,
            label_value=1,
            label_version="labels-v1",
            split=DatasetSplit.TRAIN,
            source_dependent=True,
            source_rights_hashes=(),
            source_revision_refs=(),
            source_references=(reference,),
        )
        manifest = DatasetManifest(
            workspace_id=WORKSPACE,
            query_hash="e" * 64,
            symbols=("BTCUSDT",),
            data_start=now,
            data_end=now + timedelta(hours=3),
            as_of=now + timedelta(hours=3),
            feature_schema_version="features-v1",
            feature_version="feature-v1",
            label_version="labels-v1",
            sample_schema_version="samples-v1",
            split_policy_version="split-v1",
            samples=(sample,),
            exclusions=(),
            holdout_sealed_at=None,
            manifest_version="dataset-manifest-v3",
        )
        await DatasetManifestRepository(session).append(workspace_id=WORKSPACE, manifest=manifest)

        entry = SourceRegistryEntry(SOURCE, (first.revision,))
        second_policy = entry.transition(
            policy_id=f"{SOURCE}:policy:2",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="rights-operator",
            at=first.revision.created_at + timedelta(microseconds=1),
            reason="fixture only",
        )
        await SourceRegistryRepository(session).append_revision(
            workspace_id=WORKSPACE,
            revision=second_policy.current,
        )

    async with ingestion_sessions() as session, session.begin():
        await _scope(session)
        observed_at = await _db_now(session)
        await SourceItemRepository(session).append_revision(
            workspace_id=WORKSPACE,
            revision=SourceItemRevision(
                source_id=SOURCE,
                item_id=ITEM,
                revision_id="revision-2",
                canonical_url="https://example.invalid/wp06/rights-item?revision=2",
                observed_at=observed_at,
                available_at=observed_at,
            ),
            predecessor_revision_id="revision-1",
        )

    async with admin_sessions() as session, session.begin():
        await _scope(session)
        await SourceItemRepository(session).take_down(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id=ITEM,
            reason_code="license_withdrawal",
        )
    await ingestion_engine.dispose()
    return manifest.content_hash


async def _requeue_second_event(default_sessions) -> None:
    async with default_sessions() as session, session.begin():
        await session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.workspace_id == WORKSPACE,
                OutboxEvent.event_type == "research.rights_changed.v1",
                OutboxEvent.payload["policy_sequence"].as_integer() == 2,
            )
            .values(
                status="PENDING",
                attempts=0,
                available_at=func.clock_timestamp() - text("INTERVAL '1 second'"),
                lease_owner=None,
                lease_expires_at=None,
                completed_at=None,
                last_error_code=None,
            )
        )


async def _append_late_policy(admin_sessions) -> None:
    async with admin_sessions() as session, session.begin():
        await _scope(session)
        entry = await SourceRegistryRepository(session).get_entry(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
        )
        assert entry is not None
        blocked = entry.transition(
            policy_id=f"{SOURCE}:policy:3",
            state=SourcePolicyState.BLOCKED,
            actor_id="rights-operator",
            at=await _db_now(session) + timedelta(microseconds=1),
            reason="fixture only",
        )
        await SourceRegistryRepository(session).append_revision(
            workspace_id=WORKSPACE,
            revision=blocked.current,
        )


async def _trainer_write_denied(trainer_sessions, statement: str) -> bool:
    async with trainer_sessions() as session:
        try:
            async with session.begin():
                await _scope(session)
                await session.execute(text(statement), {"workspace_id": WORKSPACE})
        except DBAPIError:
            return True
    return False


async def _ingestion_write_denied(ingestion_sessions, statement: str, params: dict) -> bool:
    async with ingestion_sessions() as session:
        try:
            async with session.begin():
                await _scope(session)
                await session.execute(text(statement), params)
        except DBAPIError:
            return True
    return False


async def _run() -> dict[str, object]:
    default_engine = _engine()
    default_sessions = async_sessionmaker(default_engine, expire_on_commit=False)
    admin_engine = _engine(ADMIN)
    admin_sessions = async_sessionmaker(admin_engine, expire_on_commit=False)
    ingestion_engine = _engine(INGESTION)
    ingestion_sessions = async_sessionmaker(ingestion_engine, expire_on_commit=False)
    trainer_engine = _engine(TRAINER)
    trainer_sessions = async_sessionmaker(trainer_engine, expire_on_commit=False)

    manifest_hash = await _seed(default_sessions, admin_sessions)
    outbox_scope = await _ingestion_outbox_scope(ingestion_sessions)
    first = await _process(ingestion_sessions)
    assert first == {
        "expiry_events_enqueued": 0,
        "claimed": 5,
        "processed": 5,
        "invalidated_references": 3,
        "failed": 0,
        "dead_lettered": 0,
    }, first
    await _requeue_second_event(default_sessions)
    duplicate = await _process(ingestion_sessions)
    assert duplicate == {
        "expiry_events_enqueued": 0,
        "claimed": 1,
        "processed": 1,
        "invalidated_references": 0,
        "failed": 0,
        "dead_lettered": 0,
    }, duplicate

    async with trainer_sessions() as session, session.begin():
        await _scope(session)
        ledger_rows = await session.scalar(
            select(func.count()).select_from(DatasetManifestEligibilityEventRecord)
        )
        policy_rows = await session.scalar(
            select(func.count()).select_from(SourcePolicyRevisionRecord)
        )
        grouped_reasons = dict(
            (
                await session.execute(
                    select(
                        DatasetManifestEligibilityEventRecord.reason_code,
                        func.count(),
                    ).group_by(DatasetManifestEligibilityEventRecord.reason_code)
                )
            ).all()
        )
        processed_event_pending = await DatasetManifestEligibilityRepository(
            session
        ).has_unprocessed_rights_event(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id=ITEM,
        )
        invalidation_reason = await DatasetManifestEligibilityRepository(session).find_invalidation(
            workspace_id=WORKSPACE,
            manifest_hash=manifest_hash,
            sample_id="sample-1",
            reference=DatasetSourceReference(
                source_id=SOURCE,
                item_id=ITEM,
                revision_id="revision-1",
                policy_id=f"{SOURCE}:policy:1",
                policy_sequence=1,
                purpose=SourceUsePurpose.TRAINING,
                rights_hash="a" * 64,
            ),
        )
        assert ledger_rows == 3
        assert grouped_reasons == {
            "source_item_superseded": 1,
            "source_item_takedown": 1,
            "source_policy_changed": 1,
        }
        assert policy_rows == 2
        assert processed_event_pending is False
        assert invalidation_reason == "source_policy_changed"

    await _append_late_policy(admin_sessions)
    async with trainer_sessions() as session, session.begin():
        await _scope(session)
        late_event_pending = await DatasetManifestEligibilityRepository(
            session
        ).has_unprocessed_rights_event(
            workspace_id=WORKSPACE,
            source_id=SOURCE,
            item_id=ITEM,
        )
    assert late_event_pending is True

    denied_insert = await _trainer_write_denied(
        trainer_sessions,
        "INSERT INTO public.dataset_manifest_eligibility_event (id) "
        "VALUES (pg_catalog.gen_random_uuid())",
    )
    denied_update = await _trainer_write_denied(
        trainer_sessions,
        "UPDATE public.dataset_manifest_eligibility_event "
        "SET reason_code = 'source_item_takedown' WHERE workspace_id = :workspace_id",
    )
    denied_delete = await _trainer_write_denied(
        trainer_sessions,
        "DELETE FROM public.dataset_manifest_eligibility_event WHERE workspace_id = :workspace_id",
    )
    trainer_denied_writes = denied_insert and denied_update and denied_delete

    async with ingestion_sessions() as session, session.begin():
        await _scope(session)
        event_id = await session.scalar(
            select(OutboxEvent.id)
            .where(OutboxEvent.event_type == "research.rights_changed.v1")
            .limit(1)
        )
        assert isinstance(event_id, UUID)

    ingestion_denied_writes = all(
        [
            await _ingestion_write_denied(
                ingestion_sessions,
                "UPDATE public.outbox_event SET payload = '{}'::jsonb "
                "WHERE workspace_id = :workspace_id",
                {"workspace_id": WORKSPACE},
            ),
            await _ingestion_write_denied(
                ingestion_sessions,
                "INSERT INTO public.consumer_receipt "
                "(id, workspace_id, outbox_event_id, consumer_name, processed_at) "
                "VALUES (pg_catalog.gen_random_uuid(), :workspace_id, :event_id, "
                ":consumer_name, pg_catalog.clock_timestamp())",
                {
                    "workspace_id": WORKSPACE,
                    "event_id": event_id,
                    "consumer_name": "other.consumer.v1",
                },
            ),
            await _ingestion_write_denied(
                ingestion_sessions,
                "UPDATE public.dataset_manifest_eligibility_event "
                "SET reason_code = 'source_item_takedown' WHERE workspace_id = :workspace_id",
                {"workspace_id": WORKSPACE},
            ),
            await _ingestion_write_denied(
                ingestion_sessions,
                "DELETE FROM public.dataset_manifest_eligibility_event "
                "WHERE workspace_id = :workspace_id",
                {"workspace_id": WORKSPACE},
            ),
        ]
    )
    manifest_payload_select_denied = await _ingestion_write_denied(
        ingestion_sessions,
        "SELECT payload FROM public.dataset_manifest WHERE workspace_id = :workspace_id",
        {"workspace_id": WORKSPACE},
    )

    await ingestion_engine.dispose()
    await trainer_engine.dispose()
    await admin_engine.dispose()
    await default_engine.dispose()
    return {
        "first_batch": first,
        "duplicate_retry": duplicate,
        "outbox_scope": outbox_scope,
        "ledger_rows_after_retry": ledger_rows,
        "ledger_reasons": grouped_reasons,
        "ledger_reason": invalidation_reason,
        "gate_before_late_event": processed_event_pending,
        "gate_after_late_event": late_event_pending,
        "trainer_can_read_source_policy": policy_rows == 2,
        "trainer_cannot_write_ledger": trainer_denied_writes,
        "ingestion_cannot_write_outbox_identity_or_payload": ingestion_denied_writes,
        "ingestion_cannot_read_manifest_payload": manifest_payload_select_denied,
    }


print(json.dumps(asyncio.run(_run())))

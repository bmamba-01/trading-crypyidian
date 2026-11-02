"""Probe PostgreSQL WP-05 chạy trong container; dữ liệu phase đi qua environment."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import Workspace
from trading_bot.adapters.database.repositories.source_registry import (
    SourceQuotaExceededError,
    SourceRegistryRepository,
)
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry

ADMIN = "crypidian_admin_web"
INGESTION = "crypidian_ingestion_worker"
STATE = json.loads(os.environ.get("WP05_STATE", "{}"))
PHASE = os.environ.get("WP05_PHASE", "")
AT = datetime(2026, 11, 1, 9, tzinfo=UTC)


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


def _candidate(source_id: str) -> SourceRegistryEntry:
    return SourceRegistryEntry.discover(
        source_id=source_id,
        publisher="Official Publisher",
        endpoint_url="https://example.gov/calendar.json",
        access_method="GET",
        auth_scope="public",
        budget=SourceBudget(
            window=timedelta(hours=1),
            max_requests=1,
            max_bytes=100,
            max_cost=Decimal("1.00"),
            cost_unit="USD",
        ),
        actor_id="operator-e2e",
        at=AT,
        reason="E2E fixture policy",
    )


async def _seed() -> dict[str, object]:
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        session.add_all(
            [
                Workspace(
                    id=UUID(STATE["workspace_id"]),
                    name="WP05 registry workspace",
                    slug=f"wp05-{STATE['workspace_id'][:8]}",
                ),
                Workspace(
                    id=UUID(STATE["other_workspace_id"]),
                    name="WP05 other workspace",
                    slug=f"wp05-{STATE['other_workspace_id'][:8]}",
                ),
            ]
        )
    await engine.dispose()
    return {"seeded": True}


async def _assert_append_only() -> dict[str, object]:
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = UUID(STATE["workspace_id"])
    source_id = STATE["source_id"]
    try:
        async with sessions() as session, session.begin():
            for table, update_sql, delete_sql in (
                (
                    "source_policy_revision",
                    "UPDATE source_policy_revision SET reason = reason "
                    "WHERE workspace_id = :workspace_id AND source_id = :source_id",
                    "DELETE FROM source_policy_revision WHERE workspace_id = :workspace_id "
                    "AND source_id = :source_id",
                ),
                (
                    "source_quota_event",
                    "UPDATE source_quota_event SET reserved_bytes = reserved_bytes "
                    "WHERE workspace_id = :workspace_id AND source_id = :source_id",
                    "DELETE FROM source_quota_event WHERE workspace_id = :workspace_id "
                    "AND source_id = :source_id",
                ),
            ):
                del table
                for statement in (update_sql, delete_sql):
                    try:
                        async with session.begin_nested():
                            await session.execute(
                                text(statement),
                                {"workspace_id": workspace_id, "source_id": source_id},
                            )
                    except DBAPIError as error:
                        sqlstate = getattr(error.orig, "sqlstate", None)
                        assert sqlstate == "P0001"
                    else:
                        raise AssertionError("Append-only trigger chấp nhận UPDATE/DELETE.")
        return {"append_only": True}
    finally:
        await engine.dispose()


async def _assert_policy_transition_guard() -> dict[str, object]:
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = UUID(STATE["workspace_id"])
    source_id = STATE["source_id"]
    invalid_first_source = f"{source_id}-invalid-first"
    failures: list[str] = []
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            inserts = (
                (
                    "WP05_POLICY_FIRST_REVISION_MUST_BE_DISCOVERED",
                    """INSERT INTO source_policy_revision (
                           workspace_id, source_id, sequence, policy_id, state,
                           allowed_purposes, publisher, endpoint_url, access_method,
                           auth_scope, attribution, retention_ref, quota_window_seconds,
                           quota_max_requests, quota_max_bytes, quota_max_cost,
                           quota_cost_unit, actor_id, reason, id
                       ) VALUES (
                           :workspace_id, :source_id, 1, :policy_id, 'rights_pending',
                           '[]'::jsonb, 'Official Publisher', 'https://example.gov/calendar.json',
                           'GET', 'public', 'Official Publisher', 'terms-v1', 3600,
                           1, 100, 1.00, 'USD', 'operator-e2e', 'invalid first state', :id
                       )""",
                    {
                        "workspace_id": workspace_id,
                        "source_id": invalid_first_source,
                        "policy_id": f"{invalid_first_source}:policy:1",
                        "id": uuid4(),
                    },
                ),
                (
                    "WP05_POLICY_TRANSITION_INVALID",
                    """INSERT INTO source_policy_revision (
                           workspace_id, source_id, sequence, policy_id, state,
                           allowed_purposes, publisher, endpoint_url, access_method,
                           auth_scope, attribution, retention_ref, quota_window_seconds,
                           quota_max_requests, quota_max_bytes, quota_max_cost,
                           quota_cost_unit, actor_id, reason, id
                       ) VALUES (
                           :workspace_id, :source_id, 4, :policy_id, 'discovered',
                           '[]'::jsonb, 'Official Publisher', 'https://example.gov/calendar.json',
                           'GET', 'public', 'Official Publisher', 'terms-v1', 3600,
                           1, 100, 1.00, 'USD', 'operator-e2e', 'invalid transition', :id
                       )""",
                    {
                        "workspace_id": workspace_id,
                        "source_id": source_id,
                        "policy_id": f"{source_id}:policy:invalid-transition",
                        "id": uuid4(),
                    },
                ),
            )
            for expected, statement, params in inserts:
                try:
                    async with session.begin_nested():
                        await session.execute(text(statement), params)
                except DBAPIError as error:
                    message = str(error.orig)
                    assert expected in message, message
                    failures.append(expected)
                else:
                    raise AssertionError(f"DB chấp nhận INSERT sai state graph: {expected}")
            self_source = f"{source_id}-self-approved"
            await session.execute(
                text(
                    """INSERT INTO source_policy_revision (
                           workspace_id, source_id, sequence, policy_id, state,
                           allowed_purposes, publisher, endpoint_url, access_method,
                           auth_scope, attribution, retention_ref, quota_window_seconds,
                           quota_max_requests, quota_max_bytes, quota_max_cost,
                           quota_cost_unit, actor_id, reason, id
                       ) VALUES (
                           :workspace_id, :source_id, 1, :policy_id, 'discovered',
                           '[]'::jsonb, 'Official Publisher', 'https://example.gov/calendar.json',
                           'GET', 'public', 'Official Publisher', 'terms-v1', 3600,
                           1, 100, 1.00, 'USD', 'operator-e2e', 'candidate', :id
                       )"""
                ),
                {
                    "workspace_id": workspace_id,
                    "source_id": self_source,
                    "policy_id": f"{self_source}:policy:1",
                    "id": uuid4(),
                },
            )
            await session.execute(
                text(
                    """INSERT INTO source_policy_revision (
                           workspace_id, source_id, sequence, policy_id, state,
                           allowed_purposes, publisher, endpoint_url, access_method,
                           auth_scope, attribution, retention_ref, quota_window_seconds,
                           quota_max_requests, quota_max_bytes, quota_max_cost,
                           quota_cost_unit, actor_id, reason, id
                       ) VALUES (
                           :workspace_id, :source_id, 2, :policy_id, 'rights_pending',
                           '[]'::jsonb, 'Official Publisher', 'https://example.gov/calendar.json',
                           'GET', 'public', 'Official Publisher', 'terms-v1', 3600,
                           1, 100, 1.00, 'USD', 'operator-e2e', 'terms review', :id
                       )"""
                ),
                {
                    "workspace_id": workspace_id,
                    "source_id": self_source,
                    "policy_id": f"{self_source}:policy:2",
                    "id": uuid4(),
                },
            )
            try:
                async with session.begin_nested():
                    await session.execute(
                        text(
                            """INSERT INTO source_policy_revision (
                                   workspace_id, source_id, sequence, policy_id, state,
                                   allowed_purposes, evidence_ref, reviewed_at, valid_until,
                                   publisher, endpoint_url, access_method, auth_scope,
                                   attribution, retention_ref, quota_window_seconds,
                                   quota_max_requests, quota_max_bytes, quota_max_cost,
                                   quota_cost_unit, actor_id, reason, approved_by, id
                               ) VALUES (
                                   :workspace_id, :source_id, 3, :policy_id, 'verified',
                                   '[\"metadata\"]'::jsonb, 'https://example.gov/terms',
                                   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + interval '1 day',
                                   'Official Publisher', 'https://example.gov/calendar.json',
                                   'GET', 'public', 'Official Publisher', 'terms-v1', 3600,
                                   1, 100, 1.00, 'USD', 'operator-e2e', 'self approval',
                                   'operator-e2e', :id
                               )"""
                        ),
                        {
                            "workspace_id": workspace_id,
                            "source_id": self_source,
                            "policy_id": f"{self_source}:policy:3",
                            "id": uuid4(),
                        },
                    )
            except DBAPIError as error:
                if "source_policy_verified_has_approval" not in str(error.orig):
                    raise AssertionError("DB chặn self-approval vì constraint khác.") from None
                failures.append("WP05_POLICY_SELF_APPROVAL_BLOCKED")
            else:
                raise AssertionError("DB chấp nhận policy do actor tự duyệt.")
        return {"transition_failures": failures}
    finally:
        await engine.dispose()


async def _registry() -> dict[str, object]:
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = UUID(STATE["workspace_id"])
    other_workspace_id = UUID(STATE["other_workspace_id"])
    source_id = STATE["source_id"]
    try:
        entry = _candidate(source_id)
        pending = entry.transition(
            policy_id=f"{source_id}:policy:2",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="operator-e2e",
            at=AT + timedelta(minutes=1),
            reason="Terms awaiting review",
        )
        verified = pending.transition(
            policy_id=f"{source_id}:policy:3",
            state=SourcePolicyState.VERIFIED,
            actor_id="operator-e2e",
            at=AT + timedelta(minutes=2),
            reason="Fixture rights approved",
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
            evidence_ref="https://example.gov/terms",
            reviewed_at=AT + timedelta(minutes=1),
            valid_until=AT + timedelta(days=1),
            approved_by="reviewer-e2e",
        )
        async with sessions() as session, session.begin():
            repository = SourceRegistryRepository(session)
            persisted_revisions = []
            for revision in verified.revisions:
                database_before_append = await session.scalar(text("SELECT clock_timestamp()"))
                assert isinstance(database_before_append, datetime)
                result = await repository.append_revision(
                    workspace_id=workspace_id,
                    revision=revision,
                )
                assert result.is_duplicate is False
                assert result.revision.created_at >= database_before_append
                if result.revision.state is SourcePolicyState.VERIFIED:
                    assert result.revision.policy.reviewed_at == result.revision.created_at
                persisted_revisions.append(result.revision)
            restored = await repository.get_entry(
                workspace_id=workspace_id,
                source_id=source_id,
            )
            assert restored == SourceRegistryEntry(source_id, tuple(persisted_revisions))
        async with sessions() as session, session.begin():
            missing = await SourceRegistryRepository(session).get_entry(
                workspace_id=other_workspace_id,
                source_id=source_id,
            )
            privileges = await session.execute(
                text(
                    "SELECT has_table_privilege(current_user, 'source_policy_revision', 'INSERT'), "
                    "has_table_privilege(current_user, 'source_quota_event', 'INSERT')"
                )
            )
            admin_can_insert_policy, admin_can_insert_quota = privileges.one()
            assert missing is None
            assert admin_can_insert_policy is True
            assert admin_can_insert_quota is False
        async with sessions() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(other_workspace_id)},
            )
            visible_rows = await session.execute(
                text("SELECT source_id FROM source_policy_revision WHERE source_id = :source_id"),
                {"source_id": source_id},
            )
            assert visible_rows.all() == []
        return {"policy_sequence": verified.current.sequence}
    finally:
        await engine.dispose()


async def _concurrent_reservations() -> dict[str, object]:
    engine = _engine(INGESTION)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = UUID(STATE["workspace_id"])
    source_id = STATE["source_id"]

    async def reserve(key: str) -> tuple[str, str]:
        async with sessions() as session, session.begin():
            result = await SourceRegistryRepository(session).reserve_quota(
                workspace_id=workspace_id,
                source_id=source_id,
                idempotency_key=key,
                purpose=SourceUsePurpose.METADATA,
                reserved_bytes=60,
                reserved_cost=Decimal("0.25"),
            )
            return key, "duplicate" if result.is_duplicate else "reserved"

    async def contender(key: str) -> tuple[str, str]:
        try:
            return await reserve(key)
        except SourceQuotaExceededError:
            return key, "quota_exceeded"

    try:
        results = await asyncio.gather(contender("worker-a"), contender("worker-b"))
        assert sorted(result for _, result in results) == ["quota_exceeded", "reserved"]
        reserved_key = next(key for key, status in results if status == "reserved")
        async with sessions() as session, session.begin():
            duplicate = await SourceRegistryRepository(session).reserve_quota(
                workspace_id=workspace_id,
                source_id=source_id,
                idempotency_key=reserved_key,
                purpose=SourceUsePurpose.METADATA,
                reserved_bytes=60,
                reserved_cost=Decimal("0.25"),
            )
            assert duplicate.is_duplicate is True
        async with sessions() as session, session.begin():
            worker_can_insert_policy, worker_can_insert_quota = (
                await session.execute(
                    text(
                        "SELECT has_table_privilege(current_user, "
                        "'source_policy_revision', 'INSERT'), "
                        "has_table_privilege(current_user, 'source_quota_event', 'INSERT')"
                    )
                )
            ).one()
            assert worker_can_insert_policy is False
            assert worker_can_insert_quota is True
            await session.execute(
                text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": STATE["other_workspace_id"]},
            )
            invisible_events = await session.execute(
                text("SELECT id FROM source_quota_event WHERE source_id = :source_id"),
                {"source_id": source_id},
            )
            assert invisible_events.all() == []
        return {"reservations": results, "reserved_key": reserved_key}
    finally:
        await engine.dispose()


async def _revoke() -> dict[str, object]:
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = UUID(STATE["workspace_id"])
    source_id = STATE["source_id"]
    try:
        async with sessions() as session, session.begin():
            repository = SourceRegistryRepository(session)
            current = await repository.get_entry(workspace_id=workspace_id, source_id=source_id)
            assert current is not None
            database_now = await session.scalar(text("SELECT clock_timestamp()"))
            assert isinstance(database_now, datetime)
            revoked = current.transition(
                policy_id=f"{source_id}:policy:revoked",
                state=SourcePolicyState.REVOKED,
                actor_id="reviewer-e2e",
                at=database_now,
                reason="E2E revoke",
            )
            await repository.append_revision(
                workspace_id=workspace_id,
                revision=revoked.current,
            )
        return {"revoked_sequence": revoked.current.sequence}
    finally:
        await engine.dispose()


async def _revoked_retry() -> dict[str, object]:
    engine = _engine(INGESTION)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = UUID(STATE["workspace_id"])
    source_id = STATE["source_id"]
    try:
        async with sessions() as session, session.begin():
            repository = SourceRegistryRepository(session)
            duplicate = await repository.reserve_quota(
                workspace_id=workspace_id,
                source_id=source_id,
                idempotency_key=STATE["reserved_key"],
                purpose=SourceUsePurpose.METADATA,
                reserved_bytes=60,
                reserved_cost=Decimal("0.25"),
            )
            assert duplicate.is_duplicate is True
            try:
                await repository.reserve_quota(
                    workspace_id=workspace_id,
                    source_id=source_id,
                    idempotency_key="new-after-revoke",
                    purpose=SourceUsePurpose.METADATA,
                    reserved_bytes=1,
                    reserved_cost=Decimal("0.01"),
                )
            except PermissionError:
                denied = True
            else:
                denied = False
            assert denied is True
        return {"duplicate": duplicate.is_duplicate, "new_denied": denied}
    finally:
        await engine.dispose()


async def _main() -> None:
    phases = {
        "seed": _seed,
        "append_only": _assert_append_only,
        "policy_transition_guard": _assert_policy_transition_guard,
        "registry": _registry,
        "concurrent_reservations": _concurrent_reservations,
        "revoke": _revoke,
        "revoked_retry": _revoked_retry,
    }
    result = await phases[PHASE]()
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    asyncio.run(_main())

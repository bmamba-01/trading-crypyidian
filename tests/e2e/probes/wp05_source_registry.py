"""Probe PostgreSQL WP-05 chạy trong container; dữ liệu phase đi qua environment."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import insert, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import (
    SourceLicenseEvidenceRecord,
    SourcePolicyRevisionRecord,
    Workspace,
)
from trading_bot.adapters.database.repositories.source_registry import (
    SourceQuotaExceededError,
    SourceRegistryRepository,
)
from trading_bot.adapters.database.source_observer import PostCommitSourceObserver
from trading_bot.domain.research.license_scope import (
    SourceLicenseScope,
    canonical_scope_json,
    scope_sha256,
)
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import (
    SourceBudget,
    SourceRegistryEntry,
    source_rights_hash,
)

ADMIN = "crypidian_admin_web"
RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
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


def _scope(source_id: str) -> SourceLicenseScope:
    return SourceLicenseScope(
        source_id=source_id,
        provider="PROVIDER",
        product="CALENDAR",
        venue="PUBLIC",
        contract="RESEARCH",
        symbols=("BTCUSDT", "ETHUSDT"),
        intervals=("1h", "15m"),
        data_start=AT - timedelta(days=365),
        data_end=AT + timedelta(days=3650),
        allowed_purposes=(
            SourceUsePurpose.METADATA,
            SourceUsePurpose.TRAINING,
            SourceUsePurpose.PAPER_DEMO,
        ),
        allowed_outputs=("aggregate_report", "derived_features", "trained_model"),
        derived_use=("aggregate_reporting", "feature_extraction", "model_training"),
        attribution="Official Publisher",
        max_retention_days=None,
    )


def _evidence(
    workspace_id: UUID, source_id: str, policy_id: str, evidence_id: UUID
) -> SourceLicenseEvidenceRecord:
    scope = _scope(source_id)
    return SourceLicenseEvidenceRecord(
        id=evidence_id,
        workspace_id=workspace_id,
        source_id=source_id,
        policy_id=policy_id,
        idempotency_key=uuid4(),
        evidence_ref="https://example.gov/terms",
        digest="a" * 64,
        byte_count=123,
        media_type="text/plain",
        scope_version=scope.scope_version,
        license_scope=json.loads(canonical_scope_json(scope)),
        canonical_scope=canonical_scope_json(scope),
        scope_hash=scope_sha256(scope),
        actor_id="operator-e2e",
        created_at=AT,
    )


def _pending(entry: SourceRegistryEntry) -> SourceRegistryEntry:
    scope = _scope(entry.source_id)
    return entry.transition(
        policy_id=f"{entry.source_id}:policy:2",
        state=SourcePolicyState.RIGHTS_PENDING,
        actor_id="operator-e2e",
        at=AT + timedelta(minutes=1),
        reason="Chờ duyệt bằng chứng",
        evidence_ref="https://example.gov/terms",
        provider=scope.provider,
        product=scope.product,
        venue=scope.venue,
        contract=scope.contract,
        license_scope=scope,
        attribution=scope.attribution,
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


async def _seed_downgrade_guard() -> dict[str, object]:
    """Tạo hàng tương thích WP-05A để kiểm guard trước các revision kế tiếp."""
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = UUID(STATE["workspace_id"])
    source_id = f"{STATE['source_id']}-rollback-guard"
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
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
                           :workspace_id, :source_id, 1, :policy_id, 'discovered',
                           '[]'::jsonb, 'Official Publisher',
                           'https://example.gov/calendar.json', 'GET', 'public',
                           'Official Publisher', 'terms-v1', 3600, 1, 100, 1.00,
                           'USD', 'operator-e2e', 'migration rollback guard', :id
                       )"""
                ),
                {
                    "workspace_id": workspace_id,
                    "source_id": source_id,
                    "policy_id": f"{source_id}:policy:1",
                    "id": uuid4(),
                },
            )
        async with sessions() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :workspace, true)"),
                {"workspace": str(workspace_id)},
            )
            for sequence, state in ((1, "discovered"), (2, "rights_pending"), (3, "verified")):
                await session.execute(
                    text(
                        "INSERT INTO source_policy_revision (id, workspace_id, source_id, "
                        "sequence, "
                        "policy_id, state, allowed_purposes, evidence_ref, reviewed_at, "
                        "valid_until, "
                        "publisher, endpoint_url, access_method, auth_scope, attribution, "
                        "retention_ref, "
                        "quota_window_seconds, quota_max_requests, quota_max_bytes, "
                        "quota_max_cost, "
                        "quota_cost_unit, actor_id, reason, approved_by) SELECT :id, workspace_id, "
                        ":source, :sequence, :policy, :state, CAST(:purposes AS jsonb), "
                        ":reference, "
                        ":reviewed, :valid, publisher, endpoint_url, access_method, auth_scope, "
                        "attribution, retention_ref, quota_window_seconds, quota_max_requests, "
                        "quota_max_bytes, quota_max_cost, quota_cost_unit, actor_id, reason, "
                        ":reviewer "
                        "FROM source_policy_revision WHERE source_id=:template AND sequence=1"
                    ),
                    {
                        "id": uuid4(),
                        "source": f"{STATE['source_id']}-legacy",
                        "sequence": sequence,
                        "policy": f"{STATE['source_id']}-legacy:{sequence}",
                        "state": state,
                        "purposes": '["metadata"]' if sequence == 3 else "[]",
                        "reference": "https://example.gov/terms" if sequence == 3 else None,
                        "reviewed": AT if sequence == 3 else None,
                        "valid": AT + timedelta(days=3650) if sequence == 3 else None,
                        "reviewer": "reviewer-e2e" if sequence == 3 else None,
                        "template": source_id,
                    },
                )
        return {"seeded": True, "source_id": source_id}
    finally:
        await engine.dispose()


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
        pending = _pending(entry)
        evidence_id = uuid4()
        scope = _scope(source_id)
        verified = pending.transition(
            policy_id=f"{source_id}:policy:3",
            state=SourcePolicyState.VERIFIED,
            actor_id="operator-e2e",
            at=AT + timedelta(minutes=2),
            reason="Fixture rights approved",
            license_scope=scope,
            license_evidence_id=evidence_id,
            license_digest="a" * 64,
            license_scope_hash=scope_sha256(scope),
            allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
            evidence_ref="https://example.gov/terms",
            reviewed_at=AT + timedelta(minutes=1),
            # Fixture có mốc AT cố định; thời hạn dài tránh hết hạn theo ngày chạy E2E.
            valid_until=AT + timedelta(days=3650),
            approved_by="reviewer-e2e",
        )
        async with sessions() as session, session.begin():
            repository = SourceRegistryRepository(session)
            persisted_revisions = []
            for revision in verified.revisions:
                if revision.state is SourcePolicyState.VERIFIED:
                    session.add(
                        _evidence(
                            workspace_id, source_id, pending.current.policy.policy_id, evidence_id
                        )
                    )
                    await session.flush()
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


async def _license_schema() -> dict[str, object]:
    engine = _engine()
    try:
        async with engine.connect() as connection:
            flags = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_catalog.pg_class "
                        "WHERE oid=to_regclass('public.source_license_evidence')"
                    )
                )
            ).one_or_none()
            assert flags == (True, True), "Ledger cần RLS ENABLE + FORCE."
            columns = (
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name='source_license_evidence'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert not {"bytes", "body", "filename", "content", "secret"} & set(columns)
        return {"license_schema": True}
    finally:
        await engine.dispose()


async def _license_ledger() -> dict[str, object]:
    """Kiểm boundary DB thật: canonical/hash, FK, ACL, RLS và lịch sử bất biến."""
    engine = _engine(ADMIN)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = UUID(STATE["workspace_id"])
    source_id = f"{STATE['source_id']}-evidence"
    evidence_id = uuid4()
    pending = _pending(_candidate(source_id))
    rejected: list[str] = []

    async def reject(session, statement, params, expected: str, label: str) -> None:
        try:
            async with session.begin_nested():
                await session.execute(statement, params)
        except DBAPIError as error:
            if (
                expected not in str(error.orig)
                and getattr(error.orig, "sqlstate", None) != expected
            ):
                raise AssertionError(f"Sai lỗi cho biến thể {label}: {error.orig}") from error
            rejected.append(label)
        else:
            raise AssertionError(f"DB chấp nhận biến thể sai: {label}")

    try:
        async with sessions() as session, session.begin():
            repository = SourceRegistryRepository(session)
            for revision in pending.revisions:
                await repository.append_revision(workspace_id=workspace_id, revision=revision)
            evidence = _evidence(
                workspace_id, source_id, pending.current.policy.policy_id, evidence_id
            )
            session.add(evidence)
            await session.flush()
            assert (
                evidence.created_at == AT
            )  # Giá trị ORM chưa refresh không chứng minh đồng hồ DB.
            await session.refresh(evidence)
            assert evidence.created_at > AT
            scope = _scope(source_id)
            legacy_history = await repository.get_entry(
                workspace_id=workspace_id, source_id=f"{STATE['source_id']}-legacy"
            )
            assert legacy_history is not None
            assert legacy_history.current.state is SourcePolicyState.VERIFIED
            assert not legacy_history.current.has_license_evidence
            try:
                legacy_history.current.require_license_evidence()
            except ValueError:
                pass
            else:
                raise AssertionError("Legacy không được thành bằng chứng license hợp lệ.")
            legacy_hash = await session.scalar(
                text(
                    "SELECT public.wp06_license_source_policy_fingerprint(policy) "
                    "FROM source_policy_revision AS policy "
                    "WHERE workspace_id=:workspace AND source_id=:source AND sequence=3"
                ),
                {"workspace": workspace_id, "source": legacy_history.source_id},
            )
            assert legacy_hash == source_rights_hash(legacy_history.current)
            sql_canonical, sql_hash = (
                await session.execute(
                    text(
                        "SELECT public.wp06_license_canonical_json(CAST(:scope AS jsonb)), "
                        "encode(sha256(convert_to(:canonical, 'UTF8')), 'hex')"
                    ),
                    {
                        "scope": canonical_scope_json(scope),
                        "canonical": canonical_scope_json(scope),
                    },
                )
            ).one()
            assert sql_canonical == canonical_scope_json(scope)
            assert sql_hash == scope_sha256(scope)
            # Unicode, escape và microsecond cũng theo đúng bytes canonical của Python.
            from dataclasses import replace

            special_scope = replace(
                scope,
                attribution='Nguồn "đã duyệt"\n\t\\🙂',
                data_start=scope.data_start.replace(microsecond=123456),
            )
            special = canonical_scope_json(special_scope)
            rendered, valid = (
                await session.execute(
                    text(
                        "SELECT public.wp06_license_canonical_json(CAST(:document AS jsonb)), "
                        "public.wp06_license_scope_valid(CAST(:document AS jsonb))"
                    ),
                    {"document": special},
                )
            ).one()
            assert rendered == special
            assert valid is True
            for invalid_document in (
                json.loads(special) | {"symbols": ["BTCUSDT", "BTCUSDT"]},
                json.loads(special) | {"intervals": ["1h", "15m"]},
                json.loads(special) | {"allowed_outputs": ["unapproved_output"]},
                json.loads(special) | {"data_end": "2026-02-30T00:00:00.000000Z"},
                json.loads(special) | {"max_retention_days": True},
            ):
                assert (
                    await session.scalar(
                        text("SELECT public.wp06_license_scope_valid(CAST(:document AS jsonb))"),
                        {"document": json.dumps(invalid_document)},
                    )
                    is False
                )
            values = {
                column.name: getattr(evidence, column.name) for column in evidence.__table__.columns
            }
            from hashlib import sha256

            variants = [
                ("hash", {"scope_hash": "f" * 64}, "23514"),
                (
                    "canonical-spacing",
                    {
                        "canonical_scope": evidence.canonical_scope + " ",
                        "scope_hash": sha256((evidence.canonical_scope + " ").encode()).hexdigest(),
                    },
                    "23514",
                ),
                ("invalid-digest", {"digest": "x" * 64}, "23514"),
                ("empty-body", {"byte_count": 0}, "23514"),
                ("oversize-body", {"byte_count": 10485761}, "23514"),
                ("media-type", {"media_type": "application/zip"}, "23514"),
                (
                    "reference",
                    {"evidence_ref": "https://other.test/archive"},
                    "WP06_LICENSE_PENDING_SCOPE_MISMATCH",
                ),
                (
                    "cross-workspace",
                    {"workspace_id": UUID(STATE["other_workspace_id"])},
                    "WP06_LICENSE_PENDING_SCOPE_MISMATCH",
                ),
                (
                    "cross-source",
                    {"source_id": source_id + "-other"},
                    "WP06_LICENSE_PENDING_SCOPE_MISMATCH",
                ),
            ]
            for label, changes, expected in variants:
                candidate = values | {"id": uuid4(), "idempotency_key": uuid4()} | changes
                await reject(
                    session,
                    insert(SourceLicenseEvidenceRecord).values(**candidate),
                    {},
                    expected,
                    label,
                )
            await reject(
                session,
                insert(SourceLicenseEvidenceRecord).values(**(values | {"id": uuid4()})),
                {},
                "23505",
                "idempotency-key",
            )
            # VERIFIED thiếu evidence bị chặn ngay cả khi bypass repository/domain validator.
            verified = pending.transition(
                policy_id=f"{source_id}:policy:3",
                state=SourcePolicyState.VERIFIED,
                actor_id="operator-e2e",
                at=AT + timedelta(minutes=2),
                reason="Duyệt fixture",
                allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
                evidence_ref="https://example.gov/terms",
                reviewed_at=AT,
                valid_until=AT + timedelta(days=3650),
                approved_by="reviewer-e2e",
            ).current
            row = SourcePolicyRevisionRecord.from_domain(workspace_id, verified)
            policy_values = {
                column.name: getattr(row, column.name)
                for column in row.__table__.columns
                if column.name not in {"id", "barrier_txid"}
            }
            await reject(
                session,
                insert(SourcePolicyRevisionRecord).values(**policy_values),
                {},
                "WP06_LICENSE_EVIDENCE_REQUIRED",
                "verified-missing-evidence",
            )
            licensed = pending.transition(
                policy_id=f"{source_id}:policy:3",
                state=SourcePolicyState.VERIFIED,
                actor_id="operator-e2e",
                at=AT + timedelta(minutes=2),
                reason="Duyệt fixture",
                allowed_purposes=frozenset({SourceUsePurpose.METADATA}),
                evidence_ref="https://example.gov/terms",
                reviewed_at=AT,
                valid_until=AT + timedelta(days=3650),
                approved_by="reviewer-e2e",
                license_scope=scope,
                license_evidence_id=evidence_id,
                license_digest="a" * 64,
                license_scope_hash=scope_sha256(scope),
            ).current
            row = SourcePolicyRevisionRecord.from_domain(workspace_id, licensed)
            policy_values = {
                column.name: getattr(row, column.name)
                for column in row.__table__.columns
                if column.name not in {"id", "barrier_txid"}
            }
            await reject(
                session,
                insert(SourcePolicyRevisionRecord).values(
                    **(policy_values | {"license_digest": "b" * 64})
                ),
                {},
                "WP06_LICENSE_EVIDENCE_MISMATCH",
                "verified-digest-mismatch",
            )
            await reject(
                session,
                insert(SourcePolicyRevisionRecord).values(
                    **(policy_values | {"license_evidence_id": uuid4()})
                ),
                {},
                "23503",
                "workspace-source-evidence-fk",
            )
            # ID tồn tại ở cùng workspace nhưng source khác vẫn phải vi phạm FK ghép.
            other_source = source_id + "-other"
            other_pending = _pending(_candidate(other_source))
            for other_revision in other_pending.revisions:
                await repository.append_revision(workspace_id=workspace_id, revision=other_revision)
            other_values = policy_values | {
                "source_id": other_source,
                "policy_id": f"{other_source}:policy:3",
                "license_scope": json.loads(canonical_scope_json(_scope(other_source))),
                "license_scope_hash": scope_sha256(_scope(other_source)),
            }
            await reject(
                session,
                insert(SourcePolicyRevisionRecord).values(**other_values),
                {},
                "23503",
                "existing-evidence-other-source-fk",
            )
            result = await repository.append_revision(workspace_id=workspace_id, revision=licensed)
            sql_fingerprint = await session.scalar(
                text(
                    "SELECT public.wp06_license_source_policy_fingerprint(policy) "
                    "FROM public.source_policy_revision AS policy "
                    "WHERE workspace_id=:workspace AND source_id=:source AND sequence=3"
                ),
                {"workspace": workspace_id, "source": source_id},
            )
            assert sql_fingerprint == source_rights_hash(result.revision)
            # Raw INSERT không được làm mất hoặc đổi identity đã set.
            revoke_values = policy_values | {
                "policy_id": f"{source_id}:policy:4",
                "sequence": 4,
                "state": "revoked",
                "approved_by": None,
                "provider": "OTHER",
                "license_scope": None,
                "license_evidence_id": None,
                "license_digest": None,
                "license_scope_hash": None,
            }
            await reject(
                session,
                insert(SourcePolicyRevisionRecord).values(**revoke_values),
                {},
                "WP06_LICENSE_IDENTITY_IMMUTABLE",
                "immutable-identity",
            )
            await session.execute(
                text("SELECT set_config('app.workspace_id', :workspace, true)"),
                {"workspace": STATE["other_workspace_id"]},
            )
            assert (
                await session.scalar(
                    select(SourceLicenseEvidenceRecord.id).where(
                        SourceLicenseEvidenceRecord.id == evidence_id
                    )
                )
                is None
            )
            await session.execute(
                text("SELECT set_config('app.workspace_id', :workspace, true)"),
                {"workspace": str(workspace_id)},
            )
            assert (
                await session.scalar(
                    select(SourceLicenseEvidenceRecord.id).where(
                        SourceLicenseEvidenceRecord.id == evidence_id
                    )
                )
                == evidence_id
            )
            for role in (
                ADMIN,
                INGESTION,
                "crypidian_trainer_worker",
                "crypidian_market_signal_worker",
                RIGHTS_ROLE,
            ):
                grants = (
                    await session.execute(
                        text(
                            "SELECT has_table_privilege(:role, 'source_license_evidence', "
                            "'SELECT'), "
                            "has_table_privilege(:role, 'source_license_evidence', 'INSERT'), "
                            "has_table_privilege(:role, 'source_license_evidence', 'UPDATE'), "
                            "has_table_privilege(:role, 'source_license_evidence', 'DELETE')"
                        ),
                        {"role": role},
                    )
                ).one()
                assert grants == (
                    (True, True, False, False) if role == ADMIN else (False, False, False, False)
                )
        worker = _engine(INGESTION)
        try:
            async with async_sessionmaker(worker)() as session, session.begin():
                await session.execute(
                    text("SELECT set_config('app.workspace_id', :workspace, true)"),
                    {"workspace": str(workspace_id)},
                )
                await reject(
                    session,
                    select(SourceLicenseEvidenceRecord),
                    {},
                    "42501",
                    "worker-select-denied",
                )
                await reject(
                    session,
                    insert(SourceLicenseEvidenceRecord).values(**values),
                    {},
                    "42501",
                    "worker-insert-denied",
                )
        finally:
            await worker.dispose()
        # Owner thử UPDATE/DELETE: trigger vẫn từ chối, không chỉ dựa vào grant của admin.
        owner = _engine()
        try:
            async with async_sessionmaker(owner)() as session, session.begin():
                for statement in (
                    "UPDATE source_license_evidence SET digest=digest WHERE id=:id",
                    "DELETE FROM source_license_evidence WHERE id=:id",
                ):
                    await reject(
                        session,
                        text(statement),
                        {"id": evidence_id},
                        "WP05_SOURCE_HISTORY_APPEND_ONLY",
                        "append-only",
                    )
                await reject(
                    session,
                    text(
                        "UPDATE source_policy_revision SET state='rights_pending', "
                        "approved_by=NULL "
                        "WHERE source_id=:source AND sequence=3"
                    ),
                    {"source": f"{STATE['source_id']}-legacy"},
                    "WP05_SOURCE_HISTORY_APPEND_ONLY",
                    "legacy-update-bypass",
                )
                legacy = (
                    await session.execute(
                        text(
                            "SELECT policy.* FROM source_policy_revision AS policy "
                            "WHERE source_id=:source AND sequence=3"
                        ),
                        {"source": f"{STATE['source_id']}-legacy"},
                    )
                ).one_or_none()
                assert legacy is not None
                assert legacy.license_evidence_id is None
        finally:
            await owner.dispose()
        return {"license_ledger": True, "rejected": rejected}
    finally:
        await engine.dispose()


async def _main() -> None:
    phases = {
        "seed": _seed,
        "license_schema": _license_schema,
        "license_ledger": _license_ledger,
        "seed_downgrade_guard": _seed_downgrade_guard,
        "append_only": _assert_append_only,
        "policy_transition_guard": _assert_policy_transition_guard,
        "registry": _registry,
        "concurrent_reservations": _concurrent_reservations,
        "revoke": _revoke,
        "revoked_retry": _revoked_retry,
    }
    result = await phases[PHASE]()
    if PHASE in {"registry", "revoke"}:
        engine = _engine(ADMIN)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        progress = await PostCommitSourceObserver(sessions).recover_policy(
            workspace_id=UUID(STATE["workspace_id"]), source_id=STATE["source_id"]
        )
        assert progress.complete
        await engine.dispose()
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    asyncio.run(_main())

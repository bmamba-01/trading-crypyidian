"""Capability persist: roles thật, workspace thật và transaction PostgreSQL thật."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from sqlalchemy import text

STATE = json.loads(os.environ["WP07_PERSIST_STATE"])
WORKSPACE_ID = UUID(STATE["workspace_id"])
OTHER_WORKSPACE_ID = UUID(STATE["other_workspace_id"])
JOB_ID = UUID(STATE["job_id"])
RUN_ID = UUID(STATE["run_id"])
OWNER = "crypidian_backtest_persist_owner"
TRAINER = "crypidian_trainer_worker"
REPLAY_WRITER = "crypidian_replay_writer"
BEGIN = "public.begin_backtest_job_persist(%s,%s,%s,%s)"
COMPLETE = "public.complete_backtest_job_persist(%s,%s,%s,%s,%s::uuid[])"
IDENTITY = (WORKSPACE_ID, JOB_ID, "fixture-worker", 1)


def connect(role: str = "crypidian_admin") -> psycopg.Connection:
    suffix = role.removeprefix("crypidian_")
    filename = "postgres_password" if suffix == "admin" else f"database_{suffix}_password"
    return psycopg.connect(
        host="postgres",
        dbname="crypidian",
        user=role,
        password=Path("/run/secrets", filename).read_text().strip(),
        autocommit=True,
    )


def scope(connection: psycopg.Connection, workspace_id: UUID | None) -> None:
    connection.execute(
        "SELECT set_config('app.workspace_id',%s,false)",
        (str(workspace_id) if workspace_id else "",),
    )


def snapshot() -> dict[str, object]:
    with connect() as connection:
        job = connection.execute(
            "SELECT to_jsonb(j) FROM public.backtest_replay_job j ORDER BY id"
        ).fetchall()
        runs = connection.execute(
            "SELECT to_jsonb(r) FROM public.backtest_run r ORDER BY id"
        ).fetchall()
        outcomes = connection.execute(
            "SELECT to_jsonb(o) FROM public.backtest_trade_outcome o ORDER BY id"
        ).fetchall()
        return {
            "jobs": [r[0] for r in job],
            "runs": [r[0] for r in runs],
            "outcomes": [r[0] for r in outcomes],
        }


def snapshot_delta(before: dict[str, object], after: dict[str, object]) -> dict[str, object]:
    """Tóm tắt khác biệt downgrade để lỗi E2E nêu đúng cột/hàng bị đổi."""

    def first_difference(left: object, right: object, path: str = "$") -> str | None:
        if type(left) is not type(right):
            return f"{path}: type {type(left).__name__} != {type(right).__name__}"
        if isinstance(left, dict) and isinstance(right, dict):
            if left.keys() != right.keys():
                missing = sorted(left.keys() - right.keys())
                added = sorted(right.keys() - left.keys())
                return f"{path}: removed={missing[:20]}, added={added[:20]}"
            for key in sorted(left):
                difference = first_difference(left[key], right[key], f"{path}.{key}")
                if difference is not None:
                    return difference
        elif isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                return f"{path}: length {len(left)} != {len(right)}"
            for index, (old_value, new_value) in enumerate(zip(left, right, strict=True)):
                difference = first_difference(old_value, new_value, f"{path}[{index}]")
                if difference is not None:
                    return difference
        elif left != right:
            return f"{path}: {left!r} != {right!r}"
        return None

    delta: dict[str, object] = {
        "zzzz_structure": {
            "before_keys": sorted(before),
            "after_keys": sorted(after),
            "before_equals_after": before == after,
            "first_difference": first_difference(before, after),
        }
    }
    for table, after_rows_value in after.items():
        before_rows_value = before[table]
        before_rows = before_rows_value if isinstance(before_rows_value, list) else []
        after_rows = after_rows_value if isinstance(after_rows_value, list) else []
        old_by_id = {
            str(row.get("id", index)): row
            for index, row in enumerate(before_rows)
            if isinstance(row, dict)
        }
        new_by_id = {
            str(row.get("id", index)): row
            for index, row in enumerate(after_rows)
            if isinstance(row, dict)
        }
        changed_rows = []
        for row_id in sorted(old_by_id.keys() & new_by_id.keys()):
            old_row, new_row = old_by_id[row_id], new_by_id[row_id]
            changed = {
                key: [repr(old_row.get(key))[:100], repr(new_row.get(key))[:100]]
                for key in sorted(old_row.keys() | new_row.keys())
                if old_row.get(key) != new_row.get(key)
            }
            if changed:
                changed_rows.append({"id": row_id, "columns": changed})
            removed_columns = sorted(old_row.keys() - new_row.keys())
            added_columns = sorted(new_row.keys() - old_row.keys())
            if removed_columns or added_columns:
                changed_rows.append(
                    {
                        "id": row_id,
                        "removed_columns": removed_columns,
                        "added_columns": added_columns,
                    }
                )
        delta[table] = {
            "before_count": len(before_rows),
            "after_count": len(after_rows),
            "removed_ids": sorted(old_by_id.keys() - new_by_id.keys()),
            "added_ids": sorted(new_by_id.keys() - old_by_id.keys()),
            "ordered_ids_changed": [
                str(row.get("id", index)) for index, row in enumerate(before_rows)
            ]
            != [str(row.get("id", index)) for index, row in enumerate(after_rows)],
            "changed_rows": changed_rows,
        }
    return delta


def seed() -> None:
    with connect() as connection:
        scope(connection, WORKSPACE_ID)
        user_id = uuid4()
        connection.execute(
            "INSERT INTO user_account(id,email_normalized,password_hash,status) "
            "VALUES (%s,%s,'fixture','ACTIVE')",
            (user_id, f"{user_id}@example.test"),
        )
        for workspace_id in (WORKSPACE_ID, OTHER_WORKSPACE_ID):
            membership_id = uuid4()
            connection.execute(
                "INSERT INTO workspace(id,name,slug,mode,status) VALUES "
                "(%s,'Fixture',%s,'PAPER','ACTIVE')",
                (workspace_id, f"persist-{workspace_id.hex}"),
            )
            connection.execute(
                "INSERT INTO user_membership(id,workspace_id,user_id,status) VALUES "
                "(%s,%s,%s,'ACTIVE')",
                (membership_id, workspace_id, user_id),
            )
            connection.execute(
                "INSERT INTO role_binding(id,workspace_id,membership_id,role) VALUES "
                "(%s,%s,%s,'owner')",
                (uuid4(), workspace_id, membership_id),
            )
            connection.execute(
                "INSERT INTO backtest_replay_job(id,workspace_id,requested_by,bundle_hash,"
                "strategy_release_id,execution_interval,request_payload,canonical_request,"
                "status,stage,progress,attempt_count,lease_owner,lease_generation,lease_until) "
                "VALUES (%s,%s,%s,%s,'fixture','1m','{}','{}','RUNNING','replay',80,1,"
                "'fixture-worker',1,clock_timestamp()+interval '10 minutes')",
                (
                    JOB_ID if workspace_id == WORKSPACE_ID else uuid4(),
                    workspace_id,
                    membership_id,
                    "a" * 64,
                ),
            )
        connection.execute(
            "INSERT INTO backtest_run(id,workspace_id,manifest_hash,manifest,canonical_manifest) "
            "VALUES (%s,%s,%s,'{}','{}')",
            (RUN_ID, WORKSPACE_ID, "b" * 64),
        )
        trial_id = uuid4()
        connection.execute(
            """INSERT INTO backtest_trial(
                id,workspace_id,run_id,trial_id,manifest_hash,parameter_family_hash,
                trial_index,trial_count,status,observation_count,completed_observation_count,
                trade_count,content_hash,canonical_trial
            ) VALUES (%s,%s,%s,%s,%s,%s,0,1,'COMPLETED',1,1,1,%s,'{}')""",
            (trial_id, WORKSPACE_ID, RUN_ID, "c" * 64, "b" * 64, "d" * 64, "e" * 64),
        )
        outcome = {
            "closed_at": "2026-11-10T00:01:00Z",
            "exposure": 0,
            "fees": 0,
            "funding": 0,
            "gross_pnl": 0,
            "opened_at": "2026-11-10T00:00:00Z",
            "slippage": 0,
            "trade_id": "fixture-trade",
        }
        canonical_outcome = json.dumps(outcome, sort_keys=True, separators=(",", ":"))
        connection.execute(
            """INSERT INTO backtest_trade_outcome(
                id,workspace_id,run_id,trial_record_id,manifest_hash,ordinal,trade_id,
                outcome_hash,outcome,canonical_json
            ) VALUES (%s,%s,%s,%s,%s,0,'fixture-trade',%s,%s::jsonb,%s)""",
            (
                uuid4(),
                WORKSPACE_ID,
                RUN_ID,
                trial_id,
                "b" * 64,
                hashlib.sha256(canonical_outcome.encode()).hexdigest(),
                json.dumps(outcome, separators=(",", ":")),
                canonical_outcome,
            ),
        )


def denied(
    connection: psycopg.Connection,
    statement: str,
    parameters: tuple[Any, ...] = (),
    reason: str = "permission denied",
) -> None:
    try:
        with connection.transaction():
            connection.execute(statement, parameters)
    except psycopg.Error as error:
        if reason not in str(error):
            raise AssertionError(str(error)) from error
    else:
        raise AssertionError(f"Capability đã cho phép thao tác phải bị từ chối: {statement}")


def scalar(connection: psycopg.Connection, statement: str, parameters: tuple[Any, ...] = ()) -> Any:
    """Truy vấn catalog phải trả một hàng, tránh bỏ qua kết quả không tồn tại."""
    row = connection.execute(statement, parameters).fetchone()
    assert row is not None
    return row[0]


def catalog() -> None:
    with connect() as connection:
        role_flags = connection.execute(
            "SELECT rolcanlogin,rolsuper,rolbypassrls,rolinherit,rolcreaterole,"
            "rolcreatedb,rolreplication FROM pg_roles WHERE rolname=%s",
            (OWNER,),
        ).fetchone()
        assert role_flags == (False,) * 7, role_flags
        assert (
            scalar(
                connection,
                "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r "
                "ON r.oid=m.roleid OR r.oid=m.member WHERE r.rolname=%s",
                (OWNER,),
            )
            == 0
        )
        writer_flags = connection.execute(
            "SELECT rolcanlogin,rolsuper,rolbypassrls,rolinherit,rolcreaterole,"
            "rolcreatedb,rolreplication FROM pg_roles WHERE rolname=%s",
            (REPLAY_WRITER,),
        ).fetchone()
        assert writer_flags == (True, False, False, False, False, False, False), writer_flags
        assert (
            scalar(
                connection,
                "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r "
                "ON r.oid=m.roleid OR r.oid=m.member WHERE r.rolname=%s",
                (REPLAY_WRITER,),
            )
            == 0
        )
        for signature in (
            "public.begin_backtest_job_persist(uuid,uuid,text,integer)",
            "public.complete_backtest_job_persist(uuid,uuid,text,integer,uuid[]) ",
        ):
            assert scalar(
                connection,
                "SELECT has_function_privilege(%s,%s,'EXECUTE')",
                (REPLAY_WRITER, signature.rstrip()),
            )
            assert not scalar(
                connection,
                "SELECT has_function_privilege(%s,%s,'EXECUTE')",
                (TRAINER, signature.rstrip()),
            )
        for table in (
            "backtest_run",
            "backtest_trial",
            "backtest_report",
            "backtest_scenario",
            "backtest_trade_outcome",
            "backtest_run_dataset_rights",
        ):
            assert scalar(
                connection,
                "SELECT has_table_privilege(%s,%s,'SELECT,INSERT')",
                (REPLAY_WRITER, f"public.{table}"),
            )
            assert not scalar(
                connection,
                "SELECT has_table_privilege(%s,%s,'INSERT')",
                (TRAINER, f"public.{table}"),
            )
        for table in ("source_policy_revision", "source_policy_observation"):
            assert scalar(
                connection,
                "SELECT has_table_privilege(%s,%s,'SELECT')",
                (REPLAY_WRITER, f"public.{table}"),
            )
        for table in (
            "source_item_revision",
            "backtest_dataset_manifest",
            "backtest_dataset_replay_bundle",
            "market_bar",
        ):
            assert not scalar(
                connection,
                "SELECT has_table_privilege(%s,%s,'SELECT')",
                (REPLAY_WRITER, f"public.{table}"),
            )
        for role in ("crypidian_app", "crypidian_ingestion_worker"):
            for signature in (
                "public.begin_backtest_job_persist(uuid,uuid,text,integer)",
                "public.complete_backtest_job_persist(uuid,uuid,text,integer,uuid[])",
            ):
                assert not scalar(
                    connection,
                    "SELECT has_function_privilege(%s,%s,'EXECUTE')",
                    (role, signature),
                )
        functions = connection.execute(
            "SELECT p.prosecdef,p.proconfig,r.rolname FROM pg_proc p JOIN pg_roles r "
            "ON r.oid=p.proowner WHERE p.oid IN "
            "('public.begin_backtest_job_persist(uuid,uuid,text,integer)'::regprocedure,"
            "'public.complete_backtest_job_persist(uuid,uuid,text,integer,uuid[])'::regprocedure)"
        ).fetchall()
        assert len(functions) == 2
        assert all(row == (True, ["search_path=pg_catalog, pg_temp"], OWNER) for row in functions)
        for column in ("request_payload", "canonical_request", "bundle_hash", "requested_by"):
            assert not scalar(
                connection,
                "SELECT has_column_privilege(%s,'public.backtest_replay_job',%s,'SELECT')",
                (OWNER, column),
            )
        assert not scalar(
            connection,
            "SELECT has_table_privilege(%s,'public.backtest_replay_job','UPDATE')",
            (OWNER,),
        )
        assert (
            scalar(
                connection,
                "SELECT count(*) FROM pg_proc p, LATERAL aclexplode(p.proacl) a "
                "WHERE p.proname IN ('begin_backtest_job_persist','complete_backtest_job_persist') "
                "AND a.grantee=0",
            )
            == 0
        )


def invalid_identity_cases() -> None:
    before = snapshot()
    with connect(REPLAY_WRITER) as connection:
        for workspace in (None, OTHER_WORKSPACE_ID):
            scope(connection, workspace)
            denied(connection, "SELECT " + BEGIN, IDENTITY, "WP07C_PERSIST_WORKSPACE_INVALID")
        scope(connection, WORKSPACE_ID)
        for identity, reason in (
            ((WORKSPACE_ID, uuid4(), "fixture-worker", 1), "WP07C_PERSIST_JOB_MISSING"),
            ((WORKSPACE_ID, JOB_ID, "other-worker", 1), "WP07C_PERSIST_OWNER_INVALID"),
            ((WORKSPACE_ID, JOB_ID, "fixture-worker", 2), "WP07C_PERSIST_OWNER_INVALID"),
            ((None, JOB_ID, "fixture-worker", 1), "WP07C_PERSIST_WORKSPACE_INVALID"),
        ):
            denied(connection, "SELECT " + BEGIN, identity, reason)
        scope(connection, OTHER_WORKSPACE_ID)
        denied(
            connection,
            "SELECT " + BEGIN,
            (OTHER_WORKSPACE_ID, JOB_ID, "fixture-worker", 1),
            "WP07C_PERSIST_JOB_MISSING",
        )
        scope(connection, WORKSPACE_ID)
        denied(
            connection, "SELECT " + COMPLETE, (*IDENTITY, [RUN_ID]), "WP07C_PERSIST_BEGIN_REQUIRED"
        )
    empty_result_snapshot = snapshot()
    with connect(REPLAY_WRITER) as writer:
        scope(writer, WORKSPACE_ID)
        with writer.transaction(force_rollback=True):
            writer.execute("SELECT " + BEGIN, IDENTITY)
            denied(
                writer,
                "SELECT " + COMPLETE,
                (*IDENTITY, []),
                "WP07C_PERSIST_RESULTS_INVALID",
            )
    assert snapshot() == empty_result_snapshot
    assert snapshot() == before
    with connect() as admin, connect(REPLAY_WRITER) as writer:
        scope(writer, WORKSPACE_ID)
        admin.execute(
            "UPDATE backtest_replay_job SET lease_until=clock_timestamp()-interval '1 "
            "second' WHERE id=%s",
            (JOB_ID,),
        )
        expired = snapshot()
        denied(writer, "SELECT " + BEGIN, IDENTITY, "WP07C_PERSIST_LEASE_EXPIRED")
        assert snapshot() == expired
        admin.execute(
            "UPDATE backtest_replay_job SET lease_until=clock_timestamp()+interval '10 "
            "minutes',cancel_requested=true,cancel_requested_at=clock_timestamp(),cance"
            "l_requested_by=requested_by WHERE id=%s",
            (JOB_ID,),
        )
        cancelled = snapshot()
        denied(writer, "SELECT " + BEGIN, IDENTITY, "WP07C_PERSIST_CANCELLED")
        assert snapshot() == cancelled
        # Giữ bằng chứng hủy bất biến; dùng job mới cho kiểm tra persist phía sau.
        admin.execute(
            "UPDATE backtest_replay_job SET "
            "status='CANCELLED',lease_owner=NULL,lease_until=NULL WHERE id=%s",
            (JOB_ID,),
        )
        denied(writer, "SELECT " + BEGIN, IDENTITY, "WP07C_PERSIST_STATUS_INVALID")


def role_boundaries() -> None:
    with connect(TRAINER) as trainer:
        scope(trainer, WORKSPACE_ID)
        for statement in (
            "SELECT request_payload FROM public.backtest_replay_job",
            "SELECT canonical_request FROM public.backtest_replay_job",
            "SELECT * FROM public.backtest_dataset_manifest",
            "SELECT * FROM public.backtest_dataset_replay_bundle",
            "SELECT * FROM public.market_bar",
            "SELECT * FROM public.market_instrument",
            "SELECT * FROM public.market_funding",
            "SELECT * FROM public.market_open_interest",
            "UPDATE public.backtest_replay_job SET progress=100",
        ):
            denied(trainer, statement)
        for table in (
            "backtest_run",
            "backtest_trial",
            "backtest_report",
            "backtest_scenario",
            "backtest_trade_outcome",
            "backtest_run_dataset_rights",
        ):
            denied(trainer, f"INSERT INTO public.{table} DEFAULT VALUES")
        for signature, parameters in (
            (BEGIN, IDENTITY),
            (COMPLETE, (*IDENTITY, [RUN_ID])),
        ):
            denied(trainer, "SELECT " + signature, parameters)
    for role in (
        "crypidian_admin_web",
        "crypidian_backtest_worker",
        "crypidian_trading_runtime",
        "crypidian_ingestion_worker",
    ):
        with connect(role) as connection:
            scope(connection, WORKSPACE_ID)
            denied(connection, "SELECT " + BEGIN, IDENTITY)
            denied(connection, "SELECT " + COMPLETE, (*IDENTITY, [RUN_ID]))


async def repository_transaction() -> None:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.repositories.backtest_job_persistence import (
        BacktestJobPersistenceRepository,
    )

    engine = create_database_engine(
        database_url_from_secret(
            username=REPLAY_WRITER,
            password_file=Path("/run/secrets/database_replay_writer_password"),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    try:
        with connect() as admin:
            # Job mới giữ fixture terminal phía trên để tiếp tục kiểm tính bất biến.
            fresh_job = uuid4()
            admin.execute(
                """INSERT INTO backtest_replay_job
                SELECT (jsonb_populate_record(NULL::backtest_replay_job,
                    to_jsonb(j)||jsonb_build_object(
                        'id',%s::text,'status','RUNNING','cancel_requested',false,
                        'cancel_requested_at',NULL,'cancel_requested_by',NULL,
                        'lease_owner','fixture-worker',
                        'lease_until',clock_timestamp()+interval '10 minutes'
                    ))).* FROM backtest_replay_job j WHERE id=%s""",
                (str(fresh_job), JOB_ID),
            )
        identity: dict[str, Any] = {
            "workspace_id": WORKSPACE_ID,
            "job_id": fresh_job,
            "worker_id": "fixture-worker",
            "generation": 1,
        }
        before = snapshot()
        async with sessions() as session:
            await session.execute(
                text("SELECT set_config('app.workspace_id',:workspace,true)"),
                {"workspace": str(WORKSPACE_ID)},
            )
            repository = BacktestJobPersistenceRepository(session)
            await repository.begin(**identity)
            await repository.complete(**identity, result_run_ids=(RUN_ID,))
            await session.rollback()
        assert snapshot() == before
        async with sessions() as session:
            await session.execute(
                text("SELECT set_config('app.workspace_id',:workspace,true)"),
                {"workspace": str(WORKSPACE_ID)},
            )
            repository = BacktestJobPersistenceRepository(session)
            await repository.begin(**identity)
            await repository.complete(**identity, result_run_ids=(RUN_ID,))
            await session.commit()
        with connect() as admin:
            row = admin.execute(
                "SELECT status,progress,result_run_ids,lease_owner,lease_until FROM "
                "backtest_replay_job WHERE id=%s",
                (fresh_job,),
            ).fetchone()
            assert row == ("SUCCEEDED", 100, [str(RUN_ID)], None, None), row
    finally:
        await engine.dispose()


def race_job(workspace_id: UUID, *, expired: bool = False, request: Any = None) -> UUID:
    """Tạo tenant riêng để outcome của từng race không trùng fixture đã commit."""
    job_id, user_id, membership_id = uuid4(), uuid4(), uuid4()
    payload = request.to_payload() if request is not None else {}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with connect() as admin:
        admin.execute(
            "INSERT INTO workspace(id,name,slug,mode,status) "
            "VALUES (%s,'Race',%s,'PAPER','ACTIVE')",
            (workspace_id, f"race-{workspace_id.hex}"),
        )
        admin.execute(
            "INSERT INTO user_account(id,email_normalized,password_hash,status) "
            "VALUES (%s,%s,'fixture','ACTIVE')",
            (user_id, f"{user_id}@example.test"),
        )
        admin.execute(
            "INSERT INTO user_membership(id,workspace_id,user_id,status) "
            "VALUES (%s,%s,%s,'ACTIVE')",
            (membership_id, workspace_id, user_id),
        )
        admin.execute(
            "INSERT INTO role_binding(id,workspace_id,membership_id,role) "
            "VALUES (%s,%s,%s,'owner')",
            (uuid4(), workspace_id, membership_id),
        )
        admin.execute(
            """INSERT INTO backtest_replay_job(
                id,workspace_id,requested_by,bundle_hash,strategy_release_id,execution_interval,
                request_payload,canonical_request,status,stage,progress,attempt_count,
                lease_owner,lease_generation,lease_until
            ) VALUES (%s,%s,%s,%s,%s,'1h',%s::jsonb,%s,'RUNNING','replay',80,1,
                'worker-A',1,clock_timestamp()+%s::interval)""",
            (
                job_id,
                workspace_id,
                membership_id,
                request.bundle_hash if request is not None else "a" * 64,
                request.strategy_release_id if request is not None else "fixture",
                canonical,
                canonical,
                "-1 second" if expired else "10 minutes",
            ),
        )
    return job_id


def workspace_outcomes(workspace_id: UUID) -> dict[str, list[Any]]:
    with connect() as admin:
        return {
            table: [
                row[0]
                for row in admin.execute(
                    f"SELECT to_jsonb(r) FROM public.{table} r WHERE workspace_id=%s ORDER BY id",
                    (workspace_id,),
                ).fetchall()
            ]
            for table in (
                "backtest_run",
                "backtest_trial",
                "backtest_report",
                "backtest_scenario",
                "backtest_trade_outcome",
                "backtest_run_dataset_rights",
            )
        }


async def deterministic_races() -> None:
    """Barrier được tạo sau begin trong service thật; không suy race từ sleep ngẫu nhiên."""
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal
    from importlib import import_module
    from unittest.mock import patch

    from sqlalchemy import select

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.models.backtest_job_entities import BacktestReplayJobRecord
    from trading_bot.adapters.database.repositories.backtest_outcomes import (
        BacktestOutcomeRepository,
    )
    from trading_bot.adapters.database.repositories.backtest_replay_jobs import (
        BacktestReplayJobRepository,
    )
    from trading_bot.application.backtest import ReplayCancelled
    from trading_bot.application.backtest_dataset_replay_execution import (
        BacktestDatasetReplayExecutionService,
    )
    from trading_bot.application.backtest_job_persistence import BacktestJobPersistToken
    from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
    from trading_bot.application.backtest_sensitivity import SensitivityScenario
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.workers import backtest_replay_worker as worker_module
    from trading_bot.workers.backtest_replay_worker import ReplayLeaseLost, _JobPersistenceAdapter

    build_fixture = import_module("wp07_runtime_replay_fixture").build_h01_dataset_runtime_replay
    engines = {
        role: create_database_engine(
            database_url_from_secret(
                username=role,
                password_file=Path("/run/secrets", f"database_{role[10:]}_password"),
                host="postgres",
                database="crypidian",
            )
        )
        for role in (TRAINER, REPLAY_WRITER, "crypidian_backtest_worker", "crypidian_admin_web")
    }
    factories = {role: create_session_factory(engine) for role, engine in engines.items()}
    read_sessions = factories["crypidian_backtest_worker"]
    replay_writer_sessions = factories[REPLAY_WRITER]
    admin_sessions = factories["crypidian_admin_web"]

    async def claim(workspace_id: UUID, worker_id: str) -> tuple[UUID, int] | None:
        async with UnitOfWork(read_sessions, workspace_id=workspace_id) as uow:
            row = await BacktestReplayJobRepository(uow.session).claim_next(
                workspace_id=workspace_id,
                worker_id=worker_id,
                now=datetime.now(UTC),
                lease_duration=timedelta(minutes=5),
            )
            return None if row is None else (row.id, row.lease_generation)

    def fixture(workspace_id: UUID) -> Any:
        return build_fixture(
            workspace_id,
            scenarios=(
                SensitivityScenario("base", "BASE"),
                SensitivityScenario(
                    "cost",
                    "COST",
                    cost_overrides=(
                        ("entry_fee_rate", "0.002"),
                        ("exit_fee_rate", "0.002"),
                    ),
                ),
            ),
        )

    def service(
        workspace_id: UUID,
        bundle: Any,
        comparison: Any,
        *,
        acquired: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
        fail_child: bool = False,
        partial: list[Any] | None = None,
    ) -> BacktestDatasetReplayExecutionService:
        class Loader:
            def __init__(self, uow: Any) -> None:
                self.uow = uow

            async def load_for_replay(self, **kwargs: Any) -> Any:
                return bundle

            async def revalidate_current_rights(self, bundle: Any, **kwargs: Any) -> Any:
                source = bundle
                # Chỉ reached khi DB begin đã giữ row lock trên chính session trainer.
                if acquired is not None:
                    acquired.set()
                    assert release is not None
                    await release.wait()
                checked_at = await self.uow.session.scalar(text("SELECT clock_timestamp()"))
                return replace(
                    source,
                    components=tuple(
                        replace(
                            component,
                            current_rights=replace(
                                component.current_rights,
                                checked_at=checked_at,
                                valid_until=checked_at + timedelta(days=1),
                            ),
                        )
                        for component in source.components
                    ),
                )

        class Writer:
            def __init__(self, uow: Any) -> None:
                self.repository = BacktestOutcomeRepository(uow.session)
                self.calls = 0

            async def append_sensitivity_run(self, **kwargs: Any) -> Any:
                self.calls += 1
                if fail_child and self.calls == 2:
                    raise RuntimeError("Lỗi child thứ hai trong transaction thật.")
                result = await self.repository.append_sensitivity_run(**kwargs)
                if partial is not None:
                    partial.append(result)
                return result

        return BacktestDatasetReplayExecutionService(
            load_unit_of_work_factory=lambda ws: UnitOfWork(read_sessions, workspace_id=ws),
            persist_unit_of_work_factory=lambda ws: UnitOfWork(
                replay_writer_sessions, workspace_id=ws
            ),
            bundle_loader_factory=Loader,
            outcome_repository_factory=Writer,
            executor=lambda *args, **kwargs: comparison,
            clock=lambda: datetime.now(UTC),
            job_persistence_factory=lambda uow: _JobPersistenceAdapter(uow.session),
        )

    async def execute(
        instance: BacktestDatasetReplayExecutionService,
        workspace_id: UUID,
        bundle: Any,
        comparison: Any,
        job_id: UUID | None = None,
        worker: str = "worker-A",
        generation: int = 1,
    ) -> Any:
        return await instance.execute(
            workspace_id=workspace_id,
            bundle_hash=bundle.dataset_hash,
            manifest=comparison.runs[0].sensitivity_run.manifest,
            job_token=None
            if job_id is None
            else BacktestJobPersistToken(
                workspace_id,
                job_id,
                worker,
                generation,
            ),
        )

    async def expire(workspace_id: UUID, job_id: UUID) -> None:
        # Fixture clock chỉ được đổi ngoài persist transaction đã rollback/commit.
        with connect() as admin:
            admin.execute(
                "UPDATE backtest_replay_job SET lease_until=clock_timestamp()-"
                "interval '1 second' WHERE workspace_id=%s AND id=%s",
                (workspace_id, job_id),
            )

    try:
        # A đã begin giữ lock; B đủ điều kiện reclaim theo lease hết hạn nhưng phải SKIP LOCKED.
        workspace_id = uuid4()
        job_id = race_job(workspace_id)
        bundle, comparison = fixture(workspace_id)
        with connect() as admin:
            admin.execute(
                "UPDATE backtest_replay_job SET lease_until=clock_timestamp()+"
                "interval '1 second' WHERE id=%s",
                (job_id,),
            )
        acquired, release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(
            execute(
                service(workspace_id, bundle, comparison, acquired=acquired, release=release),
                workspace_id,
                bundle,
                comparison,
                job_id,
            )
        )
        async with asyncio.timeout(15):
            await acquired.wait()
            async with UnitOfWork(read_sessions, workspace_id=workspace_id) as uow:
                await uow.session.execute(
                    text(
                        "SELECT pg_sleep_until(lease_until+interval '1 millisecond') "
                        "FROM backtest_replay_job WHERE id=:job_id"
                    ),
                    {"job_id": job_id},
                )
                assert await uow.session.scalar(
                    text(
                        "SELECT lease_until<clock_timestamp() "
                        "FROM backtest_replay_job WHERE id=:job_id"
                    ),
                    {"job_id": job_id},
                )
            assert await claim(workspace_id, "worker-B") is None
            cancel_started = asyncio.Event()
            cancel_pid: list[int] = []

            async def cancel_while_locked() -> Any:
                async with UnitOfWork(admin_sessions, workspace_id=workspace_id) as uow:
                    cancel_pid.append(await uow.session.scalar(text("SELECT pg_backend_pid()")))
                    membership = await uow.session.scalar(
                        text("SELECT requested_by FROM backtest_replay_job WHERE id=:job_id"),
                        {"job_id": job_id},
                    )
                    cancel_started.set()
                    return await BacktestReplayJobRepository(uow.session).request_cancel(
                        workspace_id=workspace_id, job_id=job_id, requested_by=membership
                    )

            cancel_task = asyncio.create_task(cancel_while_locked())
            await cancel_started.wait()
            # Chờ PostgreSQL chứng minh cancel đã bị chặn bởi lock của A trước khi nhả barrier.
            async with read_sessions() as observer:
                while not await observer.scalar(
                    text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": cancel_pid[0]}
                ):
                    pass
            release.set()
            result = await task
            cancelled = await cancel_task
            assert cancelled is not None
            assert cancelled.status == "SUCCEEDED"
            assert not cancelled.cancel_requested
        assert len(result.run_ids) == 2
        assert await claim(workspace_id, "worker-B") is None
        saved = workspace_outcomes(workspace_id)
        async with UnitOfWork(admin_sessions, workspace_id=workspace_id) as uow:
            membership = await uow.session.scalar(
                text("SELECT requested_by FROM backtest_replay_job WHERE id=:job_id"),
                {"job_id": job_id},
            )
            cancelled = await BacktestReplayJobRepository(uow.session).request_cancel(
                workspace_id=workspace_id, job_id=job_id, requested_by=membership
            )
            assert cancelled is not None
            assert cancelled.status == "SUCCEEDED"
            assert not cancelled.cancel_requested
        assert workspace_outcomes(workspace_id) == saved

        # B reclaim và commit generation mới trước begin của A; A không được ghi bất kỳ child.
        workspace_id = uuid4()
        job_id = race_job(workspace_id, expired=True)
        bundle, comparison = fixture(workspace_id)
        assert await claim(workspace_id, "worker-B") == (job_id, 2)
        before = workspace_outcomes(workspace_id)
        try:
            await execute(
                service(workspace_id, bundle, comparison), workspace_id, bundle, comparison, job_id
            )
        except ReplayLeaseLost:
            pass
        else:
            raise AssertionError("Worker A stale generation vẫn persist được.")
        assert workspace_outcomes(workspace_id) == before

        # Queued worker thật: plan/executor, monitor, token adapter và DB repositories đều thật.
        workspace_id = uuid4()
        bundle, _ = fixture(workspace_id)
        request = BacktestReplayJobSubmission(
            bundle_hash=bundle.dataset_hash,
            strategy_release_id="h01-v1",
            execution_interval="1h",
            starting_equity=Decimal("10000"),
            per_trade_risk_fraction=Decimal("0.01"),
            fee_rate=Decimal("0.001"),
            slippage_rate=Decimal("0.002"),
            seed=17,
        )
        job_id = race_job(workspace_id, request=request)

        class WorkerLoader:
            def __init__(self, uow: Any) -> None:
                self.uow = uow

            async def load_for_replay(self, **kwargs: Any) -> Any:
                return bundle

            async def revalidate_current_rights(self, source: Any, **kwargs: Any) -> Any:
                checked_at = await self.uow.session.scalar(text("SELECT clock_timestamp()"))
                return replace(
                    source,
                    components=tuple(
                        replace(
                            component,
                            current_rights=replace(
                                component.current_rights,
                                checked_at=checked_at,
                                valid_until=checked_at + timedelta(days=1),
                            ),
                        )
                        for component in source.components
                    ),
                )

        async with UnitOfWork(read_sessions, workspace_id=workspace_id) as uow:
            row = await uow.session.scalar(
                select(BacktestReplayJobRecord).where(
                    BacktestReplayJobRecord.id == job_id,
                )
            )
            assert row is not None
            worker_claim = worker_module._Claim.from_row(row)
        worker = worker_module.BacktestReplayJobWorker(
            read_sessions=read_sessions,
            persist_sessions=replay_writer_sessions,
            workspace_id=workspace_id,
            worker_id="worker-A",
            tick_seconds=0.01,
            lease_seconds=30,
        )
        with patch.object(worker_module, "_bundle_loader", lambda uow, ws: WorkerLoader(uow)):
            await worker._process(worker_claim)
        async with UnitOfWork(read_sessions, workspace_id=workspace_id) as uow:
            terminal = (
                await uow.session.execute(
                    text(
                        "SELECT status,result_run_ids,lease_owner,lease_until "
                        "FROM backtest_replay_job "
                        "WHERE id=:job_id"
                    ),
                    {"job_id": job_id},
                )
            ).one()
            assert terminal[0] == "SUCCEEDED"
            assert len(terminal[1]) == 1
            assert terminal[2:] == (None, None)
        worker_saved = workspace_outcomes(workspace_id)
        assert len(worker_saved["backtest_run_dataset_rights"]) == 2
        assert terminal[1][0] in {row["id"] for row in worker_saved["backtest_run"]}

        # Child 1/receipts đã append nhưng child 2 lỗi: rollback cả job/outcomes rồi B retry.
        workspace_id = uuid4()
        job_id = race_job(workspace_id)
        bundle, comparison = fixture(workspace_id)
        partial: list[Any] = []
        before = workspace_outcomes(workspace_id)
        try:
            await execute(
                service(workspace_id, bundle, comparison, fail_child=True, partial=partial),
                workspace_id,
                bundle,
                comparison,
                job_id,
            )
        except RuntimeError as error:
            if "child thứ hai" not in str(error):
                raise
        else:
            raise AssertionError("Fixture child failure không được thực thi.")
        assert len(partial) == 1
        assert workspace_outcomes(workspace_id) == before
        with connect() as admin:
            assert admin.execute(
                "SELECT status,stage,result_run_ids FROM backtest_replay_job WHERE id=%s", (job_id,)
            ).fetchone() == ("RUNNING", "replay", [])
        await expire(workspace_id, job_id)
        assert await claim(workspace_id, "worker-B") == (job_id, 2)
        committed = await execute(
            service(workspace_id, bundle, comparison),
            workspace_id,
            bundle,
            comparison,
            job_id,
            "worker-B",
            2,
        )
        saved = workspace_outcomes(workspace_id)
        assert len(saved["backtest_trial"]) == len(saved["backtest_report"]) == 2
        assert len(saved["backtest_run_dataset_rights"]) == 4
        assert {row["manifest_hash"] for row in saved["backtest_run"]} == {
            run.sensitivity_run.manifest.content_hash for run in comparison.runs
        }
        retry = await execute(
            service(workspace_id, bundle, comparison), workspace_id, bundle, comparison
        )
        assert retry.run_ids == committed.run_ids
        assert all(outcome.duplicate for outcome in retry.outcomes)
        assert workspace_outcomes(workspace_id) == saved

        # Cancel commit thắng trước begin, không có outcome/receipt mới.
        workspace_id = uuid4()
        job_id = race_job(workspace_id)
        bundle, comparison = fixture(workspace_id)
        async with UnitOfWork(admin_sessions, workspace_id=workspace_id) as uow:
            membership = await uow.session.scalar(
                text("SELECT requested_by FROM backtest_replay_job WHERE id=:job_id"),
                {"job_id": job_id},
            )
            await BacktestReplayJobRepository(uow.session).request_cancel(
                workspace_id=workspace_id, job_id=job_id, requested_by=membership
            )
        before = workspace_outcomes(workspace_id)
        try:
            await execute(
                service(workspace_id, bundle, comparison), workspace_id, bundle, comparison, job_id
            )
        except ReplayCancelled:
            pass
        else:
            raise AssertionError("Cancel đã commit nhưng begin vẫn cho persist.")
        assert workspace_outcomes(workspace_id) == before
    finally:
        await asyncio.gather(*(engine.dispose() for engine in engines.values()))


def main() -> None:
    phase = os.environ["WP07_PERSIST_PHASE"]
    if phase == "main":
        seed()
        # Lời gọi hợp lệ phải RED vì capability chưa tồn tại trước migration.
        with connect(REPLAY_WRITER) as writer:
            scope(writer, WORKSPACE_ID)
            with writer.transaction(force_rollback=True):
                writer.execute("SELECT " + BEGIN, IDENTITY)
        catalog()
        role_boundaries()
        invalid_identity_cases()
        asyncio.run(repository_transaction())
        asyncio.run(deterministic_races())
        print(
            json.dumps(
                {
                    "snapshot": snapshot(),
                    "role_boundaries": True,
                    "transactions": True,
                    "deterministic_races": True,
                }
            )
        )
    elif phase == "fence_snapshot":
        with connect(TRAINER) as trainer:
            scope(trainer, WORKSPACE_ID)
            assert scalar(
                trainer,
                "SELECT has_function_privilege(%s,%s,'EXECUTE')",
                (TRAINER, "public.begin_backtest_job_persist(uuid,uuid,text,integer)"),
            )
            assert scalar(
                trainer,
                "SELECT has_table_privilege(%s,%s,'INSERT')",
                (TRAINER, "public.backtest_run"),
            )
            assert not scalar(
                trainer,
                "SELECT has_table_privilege(%s,%s,'SELECT')",
                (TRAINER, "public.market_bar"),
            )
        print(json.dumps({"snapshot": snapshot()}))
    else:
        restored = snapshot()
        assert restored == STATE["snapshot"], json.dumps(
            snapshot_delta(STATE["snapshot"], restored),
            sort_keys=True,
            default=str,
        )
        with connect(TRAINER) as trainer:
            scope(trainer, WORKSPACE_ID)
            if phase == "down":
                assert scalar(
                    trainer,
                    "SELECT pg_catalog.to_regprocedure(%s) IS NULL",
                    ("public.begin_backtest_job_persist(uuid,uuid,text,integer)",),
                )
                assert not scalar(
                    trainer,
                    "SELECT has_table_privilege(%s,%s,'INSERT')",
                    (REPLAY_WRITER, "public.backtest_run"),
                )
                assert scalar(
                    trainer,
                    "SELECT has_table_privilege(%s,%s,'INSERT')",
                    (TRAINER, "public.backtest_run"),
                )
                assert scalar(
                    trainer,
                    "SELECT has_table_privilege(%s,%s,'SELECT')",
                    (TRAINER, "public.market_bar"),
                )
            else:
                assert scalar(
                    trainer,
                    "SELECT has_function_privilege(%s,%s,'EXECUTE')",
                    (TRAINER, "public.begin_backtest_job_persist(uuid,uuid,text,integer)"),
                )
                assert scalar(
                    trainer,
                    "SELECT has_table_privilege(%s,%s,'INSERT')",
                    (TRAINER, "public.backtest_run"),
                )
                assert not scalar(
                    trainer,
                    "SELECT has_table_privilege(%s,%s,'SELECT')",
                    (TRAINER, "public.market_bar"),
                )
        print(json.dumps({"preserved": True}))


if __name__ == "__main__":
    main()

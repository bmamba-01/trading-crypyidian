"""Kiểm runtime boolean projection thật; seed synthetic không phải bằng chứng rights."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.repositories.learning_incident_entry_holds import (
    LearningIncidentEntryHoldRepository,
    LearningIncidentHoldUnavailable,
)

STATE = json.loads(sys.stdin.read())
WORKSPACE = UUID(STATE["workspace_id"])
OTHER = UUID(STATE["other_workspace_id"])


def _load_schema_probe():
    path = Path("/e2e-tests/e2e/probes/wp06_learning_incident_entry_hold_schema.py")
    spec = importlib.util.spec_from_file_location("wp06_hold_schema_seed", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run() -> dict[str, bool]:
    schema_probe = _load_schema_probe()
    root = schema_probe._engine(
        os.environ["TRADING_BOT_DATABASE_USER"],
        os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"],
    )
    runtime_engine = create_async_engine(
        URL.create(
            "postgresql+psycopg",
            username="crypidian_trading_runtime",
            password=Path("/run/secrets/database_trading_runtime_password").read_text().strip(),
            host="postgres",
            database="crypidian",
        )
    )
    admin_engine = create_async_engine(
        URL.create(
            "postgresql+psycopg",
            username="crypidian_admin_web",
            password=Path("/run/secrets/database_admin_web_password").read_text().strip(),
            host="postgres",
            database="crypidian",
        )
    )
    runtime_sessions = async_sessionmaker(runtime_engine, expire_on_commit=False)
    admin_sessions = async_sessionmaker(admin_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with root.begin() as connection:
        for workspace_id in (WORKSPACE, OTHER):
            connection.execute(
                text(
                    "INSERT INTO public.workspace(id,name,slug,mode,status) VALUES "
                    "(:id,'Runtime incident',:slug,'PAPER','ACTIVE')"
                ),
                {"id": workspace_id, "slug": f"incident-runtime-{workspace_id.hex}"},
            )
        actor_id = uuid4()
        holds = tuple(
            schema_probe._seed_metadata(connection, WORKSPACE, actor_id, now, seed_hold=True)
            for _ in range(2)
        )
        schema_probe._seed_metadata(connection, OTHER, uuid4(), now, seed_hold=False)

    async def read(workspace_id: UUID) -> bool:
        async with runtime_sessions() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :workspace, true)"),
                {"workspace": str(workspace_id)},
            )
            repository = LearningIncidentEntryHoldRepository(session)
            await repository.lock_runtime_entry_scope(workspace_id=workspace_id)
            return await repository.active_hold_exists(workspace_id=workspace_id)

    try:
        assert await read(WORKSPACE) is True
        assert await read(OTHER) is False
        # Giao dịch mới tượng trưng cho runtime khởi động lại; ledger giữ trạng thái active.
        assert await read(WORKSPACE) is True

        # Lệnh governance của admin và PAPER entry phải tranh cùng khóa L1 workspace.
        async with admin_sessions() as admin_session, admin_session.begin():
            await admin_session.execute(
                text("SELECT set_config('app.workspace_id', :workspace, true)"),
                {"workspace": str(WORKSPACE)},
            )
            await admin_session.execute(
                text("SELECT public.wp06_lock_learning_incident_scope(:workspace, NULL)"),
                {"workspace": str(WORKSPACE)},
            )
            async with runtime_sessions() as runtime_session, runtime_session.begin():
                await runtime_session.execute(
                    text("SELECT set_config('app.workspace_id', :workspace, true)"),
                    {"workspace": str(WORKSPACE)},
                )
                acquired = await runtime_session.scalar(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": f"workspace-settings:{WORKSPACE}"},
                )
                assert acquired is False
        async with runtime_sessions() as runtime_session, runtime_session.begin():
            await LearningIncidentEntryHoldRepository(runtime_session).lock_runtime_entry_scope(
                workspace_id=WORKSPACE
            )

        for hold in holds:
            with root.begin() as connection:
                schema_probe._scope(connection, WORKSPACE)
                connection.execute(text("SET LOCAL session_replication_role = replica"))
                schema_probe._insert_event(connection, schema_probe._release(hold))
                connection.execute(text("SET LOCAL session_replication_role = origin"))
            if hold is holds[0]:
                assert await read(WORKSPACE) is True
        assert await read(WORKSPACE) is False

        unavailable = False
        try:
            async with runtime_sessions() as session, session.begin():
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                await session.execute(
                    text("SELECT set_config('app.workspace_id', :workspace, true)"),
                    {"workspace": str(WORKSPACE)},
                )
                await LearningIncidentEntryHoldRepository(session).active_hold_exists(
                    workspace_id=WORKSPACE
                )
        except LearningIncidentHoldUnavailable:
            unavailable = True
        assert unavailable is True
        return {
            "runtime_only_projection": True,
            "workspace_scoped": True,
            "restart_persists_hold": True,
            "multiple_holds_require_all_releases": True,
            "read_committed_fail_closed": True,
            "governance_entry_l1_serialized": True,
        }
    finally:
        await runtime_engine.dispose()
        await admin_engine.dispose()
        root.dispose()


print(json.dumps(asyncio.run(_run()), sort_keys=True))

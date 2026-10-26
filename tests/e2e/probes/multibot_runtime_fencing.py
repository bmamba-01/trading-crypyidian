"""Probe MB05.2–05.4 trên PostgreSQL thật: lease/fencing, control append-only và incident.

Chạy trong container Compose; mã probe đi qua stdin (giới hạn dòng lệnh Windows). Pha:
- `seed` (migrator/superuser): workspace W (owner, operator có grant pause bot A), hai account
  PAPER A/B (bot tự tạo bởi trigger MB-01) và workspace W2.
- `runtime_fencing` (role `crypidian_trading_runtime`, hai engine = hai kết nối/"process"):
  checkpoint chuyển tiếp NULL, acquire/takeover/stale token, trigger DB chặn ghi khi bỏ qua
  application, lease hết hạn, race acquire lần đầu, RLS chéo bot/workspace.
- `admin_control` (role `crypidian_admin_web`): control append-only theo quyền, resolution incident.
- `runtime_control_read` (runtime): đọc lại control trong transaction.
- `prepare_downgrade` (migrator): dọn hàng runtime để kiểm downgrade guard rồi downgrade thật.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from trading_bot.adapters.database.repositories.account_activity_incidents import (
    AccountActivityIncidentRepository,
)
from trading_bot.adapters.database.repositories.bot_runtime_controls import (
    BotRuntimeControlRepository,
    read_runtime_control,
)
from trading_bot.adapters.database.repositories.bot_runtime_leases import (
    BotRuntimeLease,
    require_current_fence,
)
from trading_bot.adapters.database.repositories.bot_runtime_states import (
    BotRuntimeStateRepository,
)
from trading_bot.adapters.database.repositories.paper_checkpoints import (
    PaperCheckpointRepository,
)
from trading_bot.adapters.database.repositories.workspace_settings import (
    WorkspaceSettingsRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.execution.paper import PaperExecutionGateway, PaperExecutionPolicy
from trading_bot.domain.execution.runtime_control import (
    BotEntryState,
    BotReadiness,
    FencingToken,
    RuntimeControlError,
    RuntimeFence,
    WorkspaceKillState,
)
from trading_bot.ports.account_activity import (
    ActivityClassification,
    ActivityVerdict,
    ObservedOrder,
)

STATE = json.loads(os.environ.get("MB05_STATE", "{}"))


def _password() -> str:
    return Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()


def _connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=_password(),
        autocommit=True,
    )


def _engine() -> AsyncEngine:
    return create_async_engine(
        URL.create(
            "postgresql+psycopg",
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password=_password(),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        ),
        pool_size=2,
    )


def _insert(connection: psycopg.Connection, table: str, **values: object) -> None:
    connection.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(",").join(map(sql.Identifier, values)),
            sql.SQL(",").join(sql.Placeholder() for _ in values),
        ),
        tuple(values.values()),
    )


def seed() -> dict[str, str]:
    ws, ws2 = uuid4(), uuid4()
    accounts = {"acc_a": uuid4(), "acc_b": uuid4(), "acc_other": uuid4()}
    members = {"owner": uuid4(), "operator": uuid4(), "other_owner": uuid4()}
    with _connect() as connection, connection.transaction():
        for workspace, slug in ((ws, "mb05-w1"), (ws2, "mb05-w2")):
            _insert(
                connection,
                "workspace",
                id=workspace,
                name=slug,
                slug=f"{slug}-{workspace.hex[:6]}",
                mode="PAPER",
                status="ACTIVE",
            )
        for name, member in members.items():
            workspace = ws2 if name == "other_owner" else ws
            user = uuid4()
            _insert(
                connection,
                "user_account",
                id=user,
                email_normalized=f"{name}-{user.hex[:6]}@example.test",
                password_hash="unused",
                status="ACTIVE",
            )
            _insert(
                connection,
                "user_membership",
                id=member,
                workspace_id=workspace,
                user_id=user,
                status="ACTIVE",
            )
            _insert(
                connection,
                "role_binding",
                id=uuid4(),
                workspace_id=workspace,
                membership_id=member,
                role="operator" if name == "operator" else "owner",
            )
        for name, account in accounts.items():
            _insert(
                connection,
                "trading_account",
                id=account,
                workspace_id=ws2 if name == "acc_other" else ws,
                name=f"{name}-{account.hex[:6]}",
                venue="binance_usdm",
                mode="PAPER",
                environment="local",
                status="ENABLED",
                max_notional=Decimal("10000"),
            )
        bots = {
            name.replace("acc", "bot"): connection.execute(
                "SELECT id FROM bot_instance WHERE trading_account_id = %s", (account,)
            ).fetchone()[0]
            for name, account in accounts.items()
        }
        _insert(
            connection,
            "bot_grant",
            id=uuid4(),
            workspace_id=ws,
            bot_id=bots["bot_a"],
            membership_id=members["operator"],
            permission="bot:runtime:pause",
            granted_by=members["owner"],
        )
    return {
        "ws": str(ws),
        "ws2": str(ws2),
        **{key: str(value) for key, value in accounts.items()},
        **{key: str(value) for key, value in bots.items()},
        **{key: str(value) for key, value in members.items()},
    }


def _ids() -> dict[str, UUID]:
    return {key: UUID(value) for key, value in STATE.items()}


def _reason(error: BaseException) -> str:
    if isinstance(error, RuntimeControlError):
        return error.reason_code
    message = str(getattr(error, "orig", error))
    for token in message.replace("\n", " ").split():
        if token.startswith("MB05_"):
            return token
    sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
    return f"SQLSTATE_{sqlstate}" if sqlstate else type(error).__name__


async def _attempt(coroutine: Any) -> str:
    try:
        await coroutine
    except (RuntimeControlError, DBAPIError) as error:
        return _reason(error)
    return "OK"


SNAPSHOT = PaperExecutionGateway(
    PaperExecutionPolicy(
        version_id="mb05-e2e",
        latency=timedelta(0),
        spread_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.001"),
        fee_rate=Decimal("0.001"),
    )
).export_state()


async def _append_checkpoint(
    sessions: async_sessionmaker[Any],
    ws: UUID,
    bot: UUID,
    account: UUID,
    key: str,
    token: FencingToken | None,
) -> None:
    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot) as uow:
        await PaperCheckpointRepository().append(
            uow,
            account_id=account,
            event_key=key,
            event_hash=hashlib.sha256(key.encode()).hexdigest(),
            snapshot=SNAPSHOT,
            occurred_at=await BotRuntimeLease(uow.session).database_now(),
            fencing_token=token,
        )


async def _raw_checkpoint(
    sessions: async_sessionmaker[Any], ws: UUID, bot: UUID, account: UUID, token: int
) -> None:
    """Bỏ qua application: INSERT thẳng với token tùy ý, chỉ trigger DB bảo vệ."""
    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot) as uow:
        revision = await uow.session.scalar(
            text(
                "SELECT coalesce(max(revision), 0) + 1 FROM paper_execution_checkpoint "
                "WHERE workspace_id = :ws AND account_id = :account"
            ),
            {"ws": ws, "account": account},
        )
        await uow.session.execute(
            text(
                "INSERT INTO paper_execution_checkpoint (id, workspace_id, bot_id, account_id, "
                "revision, event_key, event_hash, event_at, schema_version, policy_hash, "
                "snapshot_hash, snapshot_payload, fencing_token) VALUES (gen_random_uuid(), :ws, "
                ":bot, :account, :revision, :key, repeat('a', 64), clock_timestamp(), 1, "
                "repeat('b', 64), repeat('c', 64), '{}', :token)"
            ),
            {
                "ws": ws,
                "bot": bot,
                "account": account,
                "revision": revision,
                "key": f"raw-{uuid4()}",
                "token": token,
            },
        )


async def _acquire(
    sessions: async_sessionmaker[Any], ws: UUID, bot: UUID, owner: str, ttl: float
) -> int:
    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot) as uow:
        lease = BotRuntimeLease(uow.session)
        now = await lease.database_now()
        grant = await lease.acquire_grant(ws, bot, owner, now + timedelta(seconds=ttl))
        return int(grant.fencing_token)


async def _count(sessions: async_sessionmaker[Any], ws: UUID, bot: UUID, query: str) -> int:
    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot) as uow:
        return int(await uow.session.scalar(text(query)) or 0)


async def runtime_fencing() -> dict[str, Any]:
    ids = _ids()
    ws, bot_a, acc_a, bot_b, acc_b = (
        ids["ws"],
        ids["bot_a"],
        ids["acc_a"],
        ids["bot_b"],
        ids["acc_b"],
    )
    engine_a, engine_b = _engine(), _engine()
    proc_a = async_sessionmaker(engine_a, expire_on_commit=False)
    proc_b = async_sessionmaker(engine_b, expire_on_commit=False)
    out: dict[str, Any] = {}
    checkpoints = "SELECT count(*) FROM paper_execution_checkpoint"
    try:
        # Chuyển tiếp: bot chưa từng có lease -> checkpoint NULL token hợp lệ.
        out["transitional_null"] = await _attempt(
            _append_checkpoint(proc_a, ws, bot_a, acc_a, "seed-null", None)
        )
        out["token_a1"] = await _acquire(proc_a, ws, bot_a, "proc-a", 2.0)
        out["b_before_expiry"] = await _attempt(_acquire(proc_b, ws, bot_a, "proc-b", 2.0))
        out["null_after_lease"] = await _attempt(
            _append_checkpoint(proc_a, ws, bot_a, acc_a, "null-after-lease", None)
        )
        out["a_write_token1"] = await _attempt(
            _append_checkpoint(proc_a, ws, bot_a, acc_a, "a-1", FencingToken(1))
        )
        # A giữ FOR SHARE hàng lease (fence application); takeover sau hạn phải chờ.
        async with UnitOfWork(proc_a, workspace_id=ws, bot_id=bot_a) as uow:
            await WorkspaceSettingsRepository(uow.session).lock_workspace(ws)
            await require_current_fence(
                uow, bot_id=bot_a, account_id=acc_a, owner_id="proc-a", token=FencingToken(1)
            )
            await asyncio.sleep(2.5)

            async def blocked_takeover() -> None:
                async with UnitOfWork(proc_b, workspace_id=ws, bot_id=bot_a) as uow_b:
                    await uow_b.session.execute(text("SET LOCAL lock_timeout = '400ms'"))
                    lease = BotRuntimeLease(uow_b.session)
                    now = await lease.database_now()
                    await lease.acquire_grant(ws, bot_a, "proc-b", now + timedelta(seconds=30))

            out["takeover_while_fenced"] = await _attempt(blocked_takeover())
        out["token_b2"] = await _acquire(proc_b, ws, bot_a, "proc-b", 30.0)
        before = await _count(proc_b, ws, bot_a, checkpoints)

        # Worker cũ (token 1) bị chặn ở mọi tầng.
        async def stale_application_fence() -> None:
            async with UnitOfWork(proc_a, workspace_id=ws, bot_id=bot_a) as uow:
                await WorkspaceSettingsRepository(uow.session).lock_workspace(ws)
                await require_current_fence(
                    uow, bot_id=bot_a, account_id=acc_a, owner_id="proc-a", token=FencingToken(1)
                )

        out["stale_application"] = await _attempt(stale_application_fence())
        out["stale_repository_append"] = await _attempt(
            _append_checkpoint(proc_a, ws, bot_a, acc_a, "a-stale", FencingToken(1))
        )
        out["stale_raw_insert"] = await _attempt(_raw_checkpoint(proc_a, ws, bot_a, acc_a, 1))
        out["stale_state"] = await _attempt(
            _state(proc_a, ws, bot_a, acc_a, RuntimeFence("proc-a", FencingToken(1)))
        )
        out["checkpoints_unchanged"] = before == await _count(proc_b, ws, bot_a, checkpoints)
        out["b_write_token2"] = await _attempt(
            _append_checkpoint(proc_b, ws, bot_a, acc_a, "b-2", FencingToken(2))
        )
        out["b_state"] = await _attempt(
            _state(proc_b, ws, bot_a, acc_a, RuntimeFence("proc-b", FencingToken(2)))
        )
        # Lease hết hạn theo đồng hồ DB: ghi bằng token hiện hành vẫn bị trigger chặn.
        out["token_b2_short"] = await _acquire(proc_b, ws, bot_a, "proc-b", 1.0)
        await asyncio.sleep(1.5)
        out["expired_raw_insert"] = await _attempt(_raw_checkpoint(proc_b, ws, bot_a, acc_a, 2))
        out["reacquire_same_owner"] = await _acquire(proc_b, ws, bot_a, "proc-b", 30.0)

        async def overlong_expiry() -> None:
            async with UnitOfWork(proc_b, workspace_id=ws, bot_id=bot_a) as uow:
                await uow.session.execute(
                    text(
                        "UPDATE bot_runtime_lease SET expires_at = clock_timestamp() "
                        "+ interval '1 hour' WHERE bot_id = :bot"
                    ),
                    {"bot": bot_a},
                )

        out["overlong_expiry"] = await _attempt(overlong_expiry())

        async def token_rollback() -> None:
            async with UnitOfWork(proc_b, workspace_id=ws, bot_id=bot_a) as uow:
                await uow.session.execute(
                    text("UPDATE bot_runtime_lease SET fencing_token = 1 WHERE bot_id = :bot"),
                    {"bot": bot_a},
                )

        out["token_decrease"] = await _attempt(token_rollback())

        async def delete_lease() -> None:
            async with UnitOfWork(proc_b, workspace_id=ws, bot_id=bot_a) as uow:
                await uow.session.execute(text("DELETE FROM bot_runtime_lease"))

        out["delete_lease"] = await _attempt(delete_lease())
        # Race acquire lần đầu cho bot B trên hai kết nối: đúng một chủ thắng.
        race = await asyncio.gather(
            _attempt(_acquire(proc_a, ws, bot_b, "proc-a", 30.0)),
            _attempt(_acquire(proc_b, ws, bot_b, "proc-b", 30.0)),
        )
        out["race_bot_b"] = sorted(race)
        holder = "proc-a" if race[0] == "OK" else "proc-b"
        holder_sessions = proc_a if holder == "proc-a" else proc_b
        out["incident_open"] = await _attempt(
            _incident(holder_sessions, ws, bot_b, acc_b, RuntimeFence(holder, FencingToken(1)))
        )
        out["incident_stale"] = await _attempt(
            _incident(
                holder_sessions, ws, bot_b, acc_b, RuntimeFence(holder, FencingToken(9)), "e" * 64
            )
        )
        # RLS: scope bot A không thấy lease bot B; workspace khác không thấy gì.
        out["bot_a_sees_leases"] = await _count(
            proc_a, ws, bot_a, "SELECT count(*) FROM bot_runtime_lease"
        )
        out["other_workspace_sees"] = await _count(
            proc_a, ids["ws2"], ids["bot_other"], "SELECT count(*) FROM bot_runtime_lease"
        )
        out["lease_tokens"] = {
            "bot_a": await _count(proc_a, ws, bot_a, "SELECT fencing_token FROM bot_runtime_lease"),
            "bot_b": await _count(proc_a, ws, bot_b, "SELECT fencing_token FROM bot_runtime_lease"),
        }
    finally:
        await engine_a.dispose()
        await engine_b.dispose()
    return out


async def _state(
    sessions: async_sessionmaker[Any], ws: UUID, bot: UUID, account: UUID, fence: RuntimeFence
) -> None:
    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot) as uow:
        await BotRuntimeStateRepository(uow.session).upsert(
            fence=fence,
            workspace_id=ws,
            bot_id=bot,
            account_id=account,
            readiness=BotReadiness.READY,
            reason_code=None,
            heartbeat_at=await BotRuntimeLease(uow.session).database_now(),
        )


async def _incident(
    sessions: async_sessionmaker[Any],
    ws: UUID,
    bot: UUID,
    account: UUID,
    fence: RuntimeFence,
    fingerprint: str = "f" * 64,
) -> None:
    async with UnitOfWork(sessions, workspace_id=ws, bot_id=bot) as uow:
        await AccountActivityIncidentRepository(uow.session).open_incident(
            fence=fence,
            workspace_id=ws,
            bot_id=bot,
            account_id=account,
            classification=ActivityClassification(
                verdict=ActivityVerdict.EXTERNAL_ACTIVITY_UNRECONCILED,
                reason_code="EXTERNAL_ACTIVITY_UNRECONCILED",
                foreign_orders=(
                    ObservedOrder("manual-1", "BTCUSDT", "BUY", Decimal("0.1"), "NEW"),
                ),
                fingerprint=fingerprint,
                snapshot_hash="a" * 64,
            ),
            source="SCRIPTED",
            cursor="c-1",
        )


async def admin_control() -> dict[str, Any]:
    ids = _ids()
    ws = ids["ws"]
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    out: dict[str, Any] = {}

    async def bot_control(member: str, bot: str, target: BotEntryState, expected: int | None):
        async with UnitOfWork(sessions, workspace_id=ws, actor_membership_id=ids[member]) as uow:
            result = await BotRuntimeControlRepository(uow.session).append_bot_control(
                workspace_id=ws,
                bot_id=ids[bot],
                target=target,
                expected_revision=expected,
                changed_by=ids[member],
                reason_code="E2E_PAUSE",
                reason_text="Kiểm thử MB-05",
                correlation_id=uuid4(),
            )
            return [result.revision, result.changed]

    async def workspace_control(member: str, target: WorkspaceKillState, expected: int | None):
        async with UnitOfWork(sessions, workspace_id=ws, actor_membership_id=ids[member]) as uow:
            result = await BotRuntimeControlRepository(uow.session).append_workspace_control(
                workspace_id=ws,
                target=target,
                expected_revision=expected,
                changed_by=ids[member],
                reason_code="E2E_KILL",
                reason_text="Kiểm thử MB-05",
                correlation_id=uuid4(),
            )
            return [result.revision, result.changed]

    async def outcome(coroutine: Any) -> Any:
        try:
            return await coroutine
        except (RuntimeControlError, DBAPIError) as error:
            return _reason(error)

    try:
        out["operator_pause_a"] = await outcome(
            bot_control("operator", "bot_a", BotEntryState.PAUSED, None)
        )
        out["operator_pause_a_again"] = await outcome(
            bot_control("operator", "bot_a", BotEntryState.PAUSED, None)
        )
        out["operator_pause_b"] = await outcome(
            bot_control("operator", "bot_b", BotEntryState.PAUSED, None)
        )
        out["operator_kill"] = await outcome(
            workspace_control("operator", WorkspaceKillState.KILLED, None)
        )
        out["owner_kill"] = await outcome(
            workspace_control("owner", WorkspaceKillState.KILLED, None)
        )
        out["owner_kill_again"] = await outcome(
            workspace_control("owner", WorkspaceKillState.KILLED, None)
        )
        out["owner_release_wrong_revision"] = await outcome(
            workspace_control("owner", WorkspaceKillState.ACTIVE, 7)
        )
        out["owner_release"] = await outcome(
            workspace_control("owner", WorkspaceKillState.ACTIVE, 1)
        )

        async def forged_signer() -> None:
            async with UnitOfWork(
                sessions, workspace_id=ws, actor_membership_id=ids["owner"]
            ) as uow:
                await uow.session.execute(
                    text(
                        "INSERT INTO workspace_runtime_control (id, workspace_id, revision, "
                        "kill_state, reason_code, reason_text, changed_by, correlation_id) "
                        "VALUES (gen_random_uuid(), :ws, 3, 'KILLED', 'x', 'x', :other, "
                        "gen_random_uuid())"
                    ),
                    {"ws": ws, "other": ids["operator"]},
                )

        out["forged_changed_by"] = await outcome(forged_signer())

        async def skipped_revision() -> None:
            async with UnitOfWork(
                sessions, workspace_id=ws, actor_membership_id=ids["owner"]
            ) as uow:
                await uow.session.execute(
                    text(
                        "INSERT INTO workspace_runtime_control (id, workspace_id, revision, "
                        "kill_state, reason_code, reason_text, changed_by, correlation_id) "
                        "VALUES (gen_random_uuid(), :ws, 9, 'KILLED', 'x', 'x', :owner, "
                        "gen_random_uuid())"
                    ),
                    {"ws": ws, "owner": ids["owner"]},
                )

        out["skipped_revision"] = await outcome(skipped_revision())

        async def other_workspace_view() -> int:
            async with UnitOfWork(
                sessions, workspace_id=ids["ws2"], actor_membership_id=ids["other_owner"]
            ) as uow:
                return int(
                    await uow.session.scalar(text("SELECT count(*) FROM workspace_runtime_control"))
                    or 0
                )

        out["other_workspace_controls"] = await other_workspace_view()

        async def resolve(member: str) -> Any:
            async with UnitOfWork(
                sessions, workspace_id=ws, actor_membership_id=ids[member]
            ) as uow:
                incident_id = await uow.session.scalar(
                    text(
                        "SELECT id FROM account_activity_incident WHERE bot_id = :bot "
                        "AND status = 'OPEN'"
                    ),
                    {"bot": ids["bot_b"]},
                )
                if incident_id is None:
                    return "INCIDENT_NOT_VISIBLE"
                record = await AccountActivityIncidentRepository(uow.session).resolve(
                    workspace_id=ws,
                    bot_id=ids["bot_b"],
                    incident_id=incident_id,
                    resolution_code="FALSE_POSITIVE_CONFIRMED",
                    reason="Đã xác minh",
                    resolved_by=ids[member],
                    resolved_at=await BotRuntimeLease(uow.session).database_now(),
                )
                return record.status

        out["operator_resolve_b"] = await outcome(resolve("operator"))
        out["owner_resolve_b"] = await outcome(resolve("owner"))
    finally:
        await engine.dispose()
    return out


async def runtime_control_read() -> dict[str, Any]:
    ids = _ids()
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with UnitOfWork(sessions, workspace_id=ids["ws"], bot_id=ids["bot_a"]) as uow:
            snapshot_a = await read_runtime_control(
                uow.session, workspace_id=ids["ws"], bot_id=ids["bot_a"]
            )
        async with UnitOfWork(sessions, workspace_id=ids["ws"], bot_id=ids["bot_b"]) as uow:
            snapshot_b = await read_runtime_control(
                uow.session, workspace_id=ids["ws"], bot_id=ids["bot_b"]
            )
    finally:
        await engine.dispose()
    return {
        "bot_a": snapshot_a.revisions() | {"entry": snapshot_a.bot_entry_state.value},
        "bot_b": snapshot_b.revisions() | {"entry": snapshot_b.bot_entry_state.value},
        "workspace": snapshot_a.workspace_kill_state.value,
    }


def prepare_downgrade() -> dict[str, Any]:
    with _connect() as connection, connection.transaction():
        # Superuser tắt trigger trong transaction để xuất/dọn lịch sử runtime trước downgrade.
        connection.execute("SET LOCAL session_replication_role = replica")
        deleted = {}
        for table in (
            "account_activity_incident",
            "bot_runtime_state",
            "bot_runtime_lease",
            "bot_runtime_control",
            "workspace_runtime_control",
        ):
            deleted[table] = connection.execute(
                sql.SQL("DELETE FROM {}").format(sql.Identifier(table))
            ).rowcount
        deleted["checkpoints"] = connection.execute(
            "DELETE FROM paper_execution_checkpoint WHERE fencing_token IS NOT NULL"
        ).rowcount
    return {"deleted": deleted}


phase = os.environ["MB05_PHASE"]
if phase == "seed":
    result: dict[str, Any] = seed()
elif phase == "runtime_fencing":
    result = asyncio.run(runtime_fencing())
elif phase == "admin_control":
    result = asyncio.run(admin_control())
elif phase == "runtime_control_read":
    result = asyncio.run(runtime_control_read())
elif phase == "prepare_downgrade":
    result = prepare_downgrade()
else:
    raise AssertionError(phase)
print(json.dumps(result, default=str))

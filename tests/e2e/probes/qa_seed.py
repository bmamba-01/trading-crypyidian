"""Probe E2E cho dataset QA; chạy trong container Compose với login DB thật của service.

Phase:
- `snapshot` (migrator, role owner): số hàng + digest nội dung các bảng QA để so idempotency.
- `admin_matrix` (admin-web, `crypidian_admin_web`): RLS theo membership/grant và
  `BotGrantRepository.require` ở tầng ứng dụng.
- `runtime_scope` (trading-runtime, `crypidian_trading_runtime`): RLS theo bot scope.
Ngữ cảnh (ID dataset, bot ID, danh sách kiểm tra) đến từ test qua stdin, không chứa bí mật.
"""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg import sql

PHASE = os.environ["QA_PHASE"]
CONTEXT = json.loads(os.environ["QA_CONTEXT"])
WORKSPACE_TABLES = (
    "user_membership",
    "role_binding",
    "mfa_factor",
    "secret_reference",
    "trading_account",
    "bot_instance",
    "bot_grant",
    "risk_profile_version",
    "workspace_settings_version",
    "paper_account_seed",
    "market_instrument",
    "market_bar",
)


def connect():
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    return psycopg.connect(
        host="postgres",
        dbname="crypidian",
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        autocommit=True,
    )


def digest(connection, table, column, identifiers):
    count, content = connection.execute(
        sql.SQL(
            "SELECT count(*), md5(COALESCE(string_agg(to_jsonb(t)::text, '|' ORDER BY t.id), '')) "
            "FROM {} t WHERE {} = ANY(%s)"
        ).format(sql.Identifier(table), sql.Identifier(column)),
        (identifiers,),
    ).fetchone()
    return [count, content]


def snapshot():
    workspace_ids = [UUID(value) for value in CONTEXT["workspaces"].values()]
    user_ids = [UUID(member["user_id"]) for member in CONTEXT["members"].values()]
    with connect() as connection:
        result = {
            "workspace": digest(connection, "workspace", "id", workspace_ids),
            "user_account": digest(connection, "user_account", "id", user_ids),
        }
        for table in WORKSPACE_TABLES:
            result[table] = digest(connection, table, "workspace_id", workspace_ids)
    return result


def scope(connection, workspace_id, *, membership_id="", bot_id=""):
    connection.execute(
        "SELECT set_config('app.workspace_id', %s, true), set_config('app.membership_id', %s, "
        "true), set_config('app.bot_id', %s, true)",
        (str(workspace_id), str(membership_id), str(bot_id)),
    )


def account_keys(connection, query):
    by_id = {UUID(account["id"]): key for key, account in CONTEXT["accounts"].items()}
    return sorted(by_id.get(row[0], "FOREIGN") for row in connection.execute(query).fetchall())


def admin_matrix():
    observed = {"matrix": {}, "require": {}}
    with connect() as connection:
        identity = connection.execute("SELECT current_user, session_user").fetchone()
        observed["identity"] = list(identity)
        for member_key, member in CONTEXT["members"].items():
            for workspace_key, workspace_id in CONTEXT["workspaces"].items():
                with connection.transaction():
                    scope(connection, workspace_id, membership_id=member["membership_id"])
                    observed["matrix"][f"{member_key}@{workspace_key}"] = {
                        "bot_instance": account_keys(
                            connection, "SELECT trading_account_id FROM bot_instance"
                        ),
                        "paper_account_seed": account_keys(
                            connection, "SELECT trading_account_id FROM paper_account_seed"
                        ),
                        "bot_grant": connection.execute(
                            "SELECT count(*) FROM bot_grant"
                        ).fetchone()[0],
                    }
        with connection.transaction():
            # Không có membership context: role admin_web không thấy hàng bot nào.
            scope(connection, CONTEXT["workspaces"]["qa"])
            observed["matrix"]["anonymous@qa"] = {
                "bot_instance": account_keys(
                    connection, "SELECT trading_account_id FROM bot_instance"
                ),
                "paper_account_seed": account_keys(
                    connection, "SELECT trading_account_id FROM paper_account_seed"
                ),
                "bot_grant": connection.execute("SELECT count(*) FROM bot_grant").fetchone()[0],
            }
    observed["require"] = asyncio.run(require_checks())
    return observed


async def require_checks():
    from sqlalchemy import text

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
    from trading_bot.auth.bot_policy import BotPermission
    from trading_bot.auth.policy import Principal, Role

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    results = {}
    try:
        for member_key, account_key, permission in CONTEXT["require_checks"]:
            member = CONTEXT["members"][member_key]
            workspace_id = UUID(CONTEXT["workspaces"][member["workspace"]])
            principal = Principal(
                user_id=UUID(member["user_id"]),
                membership_id=UUID(member["membership_id"]),
                workspace_id=workspace_id,
                roles=frozenset(Role(role) for role in member["roles"]),
                mfa_verified_at=datetime.now(UTC),
            )
            async with sessions() as session, session.begin():
                await session.execute(
                    text(
                        "SELECT set_config('app.workspace_id', :workspace, true), "
                        "set_config('app.membership_id', :membership, true)"
                    ),
                    {"workspace": str(workspace_id), "membership": member["membership_id"]},
                )
                try:
                    await BotGrantRepository(session).require(
                        principal,
                        bot_id=UUID(CONTEXT["bots"][account_key]),
                        permission=BotPermission(permission),
                    )
                except PermissionError:
                    allowed = False
                else:
                    allowed = True
            results[f"{member_key}:{account_key}:{permission}"] = allowed
    finally:
        await engine.dispose()
    return results


def runtime_scope():
    observed = {}
    accounts = CONTEXT["accounts"]
    with connect() as connection:
        observed["identity"] = list(
            connection.execute("SELECT current_user, session_user").fetchone()
        )
        for workspace_key, workspace_id in CONTEXT["workspaces"].items():
            for account_key, bot_id in CONTEXT["bots"].items():
                with connection.transaction():
                    scope(connection, workspace_id, bot_id=bot_id)
                    observed[f"{workspace_key}:{account_key}"] = account_keys(
                        connection, "SELECT trading_account_id FROM paper_account_seed"
                    )
        with connection.transaction():
            # Bot A cố đọc seed của account B bằng điều kiện tường minh: RLS vẫn trả 0 hàng.
            scope(connection, CONTEXT["workspaces"]["qa"], bot_id=CONTEXT["bots"]["A"])
            observed["bot_a_reads_account_b"] = connection.execute(
                "SELECT count(*) FROM paper_account_seed WHERE trading_account_id = %s",
                (UUID(accounts["B"]["id"]),),
            ).fetchone()[0]
            observed["bot_a_sees_qa_bots"] = len(
                connection.execute("SELECT id FROM bot_instance").fetchall()
            )
    return observed


PHASES = {"snapshot": snapshot, "admin_matrix": admin_matrix, "runtime_scope": runtime_scope}
print(json.dumps(PHASES[PHASE](), sort_keys=True))

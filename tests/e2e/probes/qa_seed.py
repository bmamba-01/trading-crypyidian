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


def mfa_references(connection):
    """Reference TOTP hiện tại của factor tất định từng thành viên."""
    references = {}
    for member_key, member in CONTEXT["members"].items():
        references[member_key] = connection.execute(
            "SELECT secret_reference FROM mfa_factor WHERE id = %s",
            (UUID(member["mfa_factor_id"]),),
        ).fetchone()[0]
    return references


def mutate():
    """Mô phỏng thao tác thủ công của QA làm lệch baseline; seed lại phải khôi phục."""
    workspace = UUID(CONTEXT["workspaces"]["qa"])
    members = {key: UUID(member["membership_id"]) for key, member in CONTEXT["members"].items()}
    bots = {key: UUID(value) for key, value in CONTEXT["bots"].items()}
    with connect() as connection, connection.transaction():
        before = mfa_references(connection)
        revoked = connection.execute(
            "UPDATE bot_grant SET revoked_at = now() WHERE workspace_id = %s AND bot_id = %s "
            "AND membership_id = %s AND permission = 'bot:runtime:pause' AND revoked_at IS NULL",
            (workspace, bots["A"], members["qa.operator"]),
        ).rowcount
        connection.execute(
            "INSERT INTO bot_grant (id, workspace_id, bot_id, membership_id, permission, "
            "granted_by) VALUES (gen_random_uuid(), %s, %s, %s, 'bot:read', %s)",
            (workspace, bots["B"], members["qa.operator"], members["qa.owner"]),
        )
        connection.execute(
            "UPDATE user_membership SET status = 'SUSPENDED' WHERE id = %s",
            (members["qa.analyst_reviewer"],),
        )
        connection.execute(
            "INSERT INTO role_binding (id, workspace_id, membership_id, role) "
            "VALUES (gen_random_uuid(), %s, %s, 'risk_admin')",
            (workspace, members["qa.operator"]),
        )
        connection.execute(
            "UPDATE trading_account SET status = 'DISABLED', name = 'qa-paper-a-mutated' "
            "WHERE id = %s",
            (UUID(CONTEXT["accounts"]["A"]["id"]),),
        )
        # Factor lạ trỏ CÙNG tệp TOTP đang dùng: xóa factor lạ không được xóa tệp còn sống.
        connection.execute(
            "INSERT INTO mfa_factor (id, workspace_id, membership_id, factor_type, "
            "secret_reference, verified_at) VALUES (gen_random_uuid(), %s, %s, 'TOTP', %s, now())",
            (workspace, members["qa.strategy_admin"], before["qa.strategy_admin"]),
        )
        bot_a = connection.execute(
            "SELECT status, name FROM bot_instance WHERE id = %s", (bots["A"],)
        ).fetchone()
    return {"references": before, "revoked": revoked, "bot_a": list(bot_a)}


def verify_reset():
    accounts = {UUID(account["id"]): key for key, account in CONTEXT["accounts"].items()}
    bot_accounts = {UUID(value): key for key, value in CONTEXT["bots"].items()}
    members = CONTEXT["members"]
    with connect() as connection:
        account_a = connection.execute(
            "SELECT status, name FROM trading_account WHERE id = %s",
            (UUID(CONTEXT["accounts"]["A"]["id"]),),
        ).fetchone()
        bot_a = connection.execute(
            "SELECT status, name, trading_account_id FROM bot_instance WHERE id = %s",
            (UUID(CONTEXT["bots"]["A"]),),
        ).fetchone()
        membership_status = {
            key: connection.execute(
                "SELECT status FROM user_membership WHERE id = %s",
                (UUID(member["membership_id"]),),
            ).fetchone()[0]
            for key, member in members.items()
        }
        operator = UUID(members["qa.operator"]["membership_id"])
        operator_roles = sorted(
            row[0]
            for row in connection.execute(
                "SELECT role FROM role_binding WHERE membership_id = %s", (operator,)
            ).fetchall()
        )
        strategy_factors = [
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM mfa_factor WHERE membership_id = %s",
                (UUID(members["qa.strategy_admin"]["membership_id"]),),
            ).fetchall()
        ]
        grants = [
            [str(row[0]), bot_accounts.get(row[1], "FOREIGN"), row[2], row[3]]
            for row in connection.execute(
                "SELECT id, bot_id, permission, revoked_at IS NULL FROM bot_grant "
                "WHERE membership_id = %s ORDER BY id",
                (operator,),
            ).fetchall()
        ]
        risk_admin = UUID(members["qa.risk_admin"]["secret_reference_id"])
        risk_admin_key = connection.execute(
            "SELECT reference_key FROM secret_reference WHERE id = %s", (risk_admin,)
        ).fetchone()[0]
        references = mfa_references(connection)
        counters = {
            key: connection.execute(
                "SELECT last_used_counter FROM mfa_factor WHERE id = %s",
                (UUID(member["mfa_factor_id"]),),
            ).fetchone()[0]
            for key, member in members.items()
        }
    return {
        "account_a": list(account_a),
        "bot_a": [bot_a[0], bot_a[1], accounts[bot_a[2]]],
        "membership_status": membership_status,
        "operator_roles": operator_roles,
        "strategy_factors": strategy_factors,
        "operator_grants": grants,
        "risk_admin_reference_key": risk_admin_key,
        "references": references,
        "counters": counters,
    }


def mfa_files():
    """Tồn tại tệp TOTP đã mã hóa theo reference (chạy trong service có volume MFA)."""
    directory = Path(os.environ["TRADING_BOT_MFA_SECRET_DIRECTORY"])
    result = {}
    for label, reference in CONTEXT["mfa_files"].items():
        _, _, identifier = reference.partition(":")
        result[label] = (directory / f"{UUID(identifier).hex}.bin").is_file()
    return result


PHASES = {
    "snapshot": snapshot,
    "admin_matrix": admin_matrix,
    "runtime_scope": runtime_scope,
    "mutate": mutate,
    "verify_reset": verify_reset,
    "mfa_files": mfa_files,
}
print(json.dumps(PHASES[PHASE](), sort_keys=True))

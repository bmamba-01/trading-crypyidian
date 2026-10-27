"""Probe SEC-01/SEC-02 chạy trong container Compose; mã được truyền qua stdin.

Pha `acl_snapshot`, `acl_compare`, `seed`, `matrix` chạy bằng login migrator (superuser);
pha `worker`, `runtime`, `admin` chạy bằng login thật của từng process role.
Ma trận kỳ vọng ở đây độc lập với migration để phát hiện grant thừa hoặc thiếu.
"""

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

ADMIN = "crypidian_admin_web"
RUNTIME = "crypidian_trading_runtime"
GATEWAY = "crypidian_execution_gateway"
INGESTION = "crypidian_ingestion_worker"
TRAINER = "crypidian_trainer_worker"
PROCESS_ROLES = (ADMIN, RUNTIME, GATEWAY, INGESTION, TRAINER)
WORKER_ROLES = (GATEWAY, INGESTION, TRAINER)

# Bảng identity/auth/config thuộc phạm vi SEC-01.
IDENTITY_TABLES = (
    "workspace",
    "user_account",
    "user_membership",
    "role_binding",
    "session",
    "mfa_factor",
    "telegram_binding",
    "secret_reference",
    "trading_account",
    "config_version",
    "risk_profile_version",
    "approval_request",
    "watchlist_version",
    "watchlist_item",
    "strategy_release",
    "workspace_settings_version",
)
# Quyền cấp bảng kỳ vọng sau SEC-01; UPDATE theo cột nằm ở COLUMN_UPDATE.
TABLE_PRIVILEGES = {
    ADMIN: {
        "workspace": {"SELECT", "INSERT", "UPDATE"},
        "user_account": {"SELECT", "INSERT", "UPDATE"},
        "user_membership": {"SELECT", "INSERT"},
        "role_binding": {"SELECT", "INSERT"},
        "session": {"SELECT", "INSERT"},
        "mfa_factor": {"SELECT", "INSERT"},
        "telegram_binding": {"SELECT"},
        "secret_reference": {"SELECT", "INSERT"},
        "trading_account": {"SELECT", "INSERT"},
        "config_version": {"SELECT"},
        "risk_profile_version": {"SELECT", "INSERT"},
        "approval_request": {"SELECT"},
        "watchlist_version": {"SELECT"},
        "watchlist_item": {"SELECT"},
        "strategy_release": {"SELECT"},
        "workspace_settings_version": {"SELECT", "INSERT", "UPDATE"},
    },
    RUNTIME: {
        "workspace": {"SELECT"},
        "trading_account": {"SELECT", "INSERT"},
        "config_version": {"SELECT"},
        "risk_profile_version": {"SELECT"},
        "approval_request": {"SELECT"},
        "strategy_release": {"SELECT"},
        "workspace_settings_version": {"SELECT"},
    },
}
COLUMN_UPDATE = {
    ADMIN: {
        # Cột lock-only (như R4): chỉ để SELECT ... FOR UPDATE, không phải dữ liệu nghiệp vụ.
        "user_membership": {"created_at"},
        "session": {"revoked_at", "csrf_token_hash", "mfa_verified_at"},
        "mfa_factor": {"verified_at", "last_used_counter"},
        "trading_account": {"updated_at"},
    },
    RUNTIME: {"trading_account": {"updated_at"}},
}
POLICY_ROLES = {
    "user_membership": {ADMIN},
    "role_binding": {ADMIN},
    "session": {ADMIN},
    "mfa_factor": {ADMIN},
    "telegram_binding": {ADMIN},
    "secret_reference": {ADMIN},
    "watchlist_version": {ADMIN},
    "watchlist_item": {ADMIN},
    "trading_account": {ADMIN, RUNTIME},
    "risk_profile_version": {ADMIN, RUNTIME},
    "strategy_release": {ADMIN, RUNTIME},
    "workspace_settings_version": {ADMIN, RUNTIME},
}
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
# Bảng MB-02/MB-03 (tích hợp đợt 2): chỉ grant trực tiếp, không qua crypidian_app; worker
# không có quyền. Kiểm cùng ma trận với bảng identity/config.
MULTIBOT_TABLES = (
    "bot_config_version",
    "bot_config_request",
    "shared_signal_evidence",
    "bot_decision_job",
    "bot_decision",
)
TABLE_PRIVILEGES[ADMIN] |= {
    "bot_config_version": {"SELECT", "INSERT", "UPDATE"},
    "bot_config_request": {"SELECT", "INSERT"},
    "shared_signal_evidence": {"SELECT"},
    "bot_decision_job": {"SELECT"},
    "bot_decision": {"SELECT"},
}
TABLE_PRIVILEGES[RUNTIME] |= {
    "bot_config_version": {"SELECT"},
    "shared_signal_evidence": {"SELECT", "INSERT"},
    "bot_decision_job": {"SELECT", "INSERT"},
    "bot_decision": {"SELECT", "INSERT"},
}
# Runtime chỉ chuyển trạng thái job (và khóa FOR UPDATE) qua quyền cấp cột; MB-05 thêm cột
# retry/poison job (attempts, last_error_code, next_attempt_at).
COLUMN_UPDATE[RUNTIME] |= {
    "bot_decision_job": {
        "status",
        "completed_at",
        "attempts",
        "last_error_code",
        "next_attempt_at",
    }
}
# Bảng registry runtime MB-05: admin ghi ý định control/resolution, runtime giữ lease ghi
# lease/state/incident; không DELETE, UPDATE chỉ cấp cột (SEC R10).
RUNTIME_REGISTRY_TABLES = (
    "workspace_runtime_control",
    "bot_runtime_control",
    "bot_runtime_lease",
    "bot_runtime_state",
    "account_activity_incident",
)
MULTIBOT_TABLES += RUNTIME_REGISTRY_TABLES
TABLE_PRIVILEGES[ADMIN] |= {
    "workspace_runtime_control": {"SELECT", "INSERT"},
    "bot_runtime_control": {"SELECT", "INSERT"},
    "bot_runtime_lease": {"SELECT"},
    "bot_runtime_state": {"SELECT"},
    "account_activity_incident": {"SELECT"},
}
TABLE_PRIVILEGES[RUNTIME] |= {
    "workspace_runtime_control": {"SELECT"},
    "bot_runtime_control": {"SELECT"},
    "bot_runtime_lease": {"SELECT", "INSERT"},
    "bot_runtime_state": {"SELECT", "INSERT"},
    "account_activity_incident": {"SELECT", "INSERT"},
}
COLUMN_UPDATE[RUNTIME] |= {
    "bot_runtime_lease": {"owner_id", "fencing_token", "acquired_at", "renewed_at", "expires_at"},
    "bot_runtime_state": {
        "readiness",
        "reason_code",
        "applied_bot_control_revision",
        "applied_workspace_control_revision",
        "last_checkpoint_revision",
        "activity_cursor",
        "activity_checked_at",
        "activity_status",
        "fanout_exclusion_code",
        "heartbeat_at",
        "fencing_token",
        "updated_at",
    },
}
COLUMN_UPDATE[ADMIN] |= {
    "account_activity_incident": {
        "status",
        "resolution_code",
        "resolution_reason",
        "resolved_by",
        "resolved_at",
    }
}
# MB-07a (migration f7b9d1e3a5c8): disable bot cập nhật đúng cột `trading_account.status`
# (ruling 29/34); bảng dedup Telegram append-only chỉ admin-web SELECT/INSERT.
COLUMN_UPDATE[ADMIN]["trading_account"] = {"updated_at", "status"}
MULTIBOT_TABLES += ("telegram_update_receipt",)
TABLE_PRIVILEGES[ADMIN] |= {"telegram_update_receipt": {"SELECT", "INSERT"}}


def identity(name):
    return uuid5(NAMESPACE_URL, "crypidian-sec01-e2e:" + name)


W1, W2 = identity("w1"), identity("w2")
M1, M2 = identity("m1"), identity("m2")
A1, A2 = identity("a1"), identity("a2")


def insert(connection, table, **values):
    values.setdefault("id", identity(table + str(values.get("workspace_id", ""))))
    connection.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(",").join(map(sql.Identifier, values)),
            sql.SQL(",").join(sql.Placeholder() for _ in values),
        ),
        tuple(values.values()),
    )


def acl_state(connection):
    """Trạng thái quyền so sánh được: bảng, cột, schema, policy, membership."""
    relations = connection.execute(
        "SELECT c.relname, COALESCE(r.rolname, 'PUBLIC'), g.rolname, x.privilege_type, "
        "x.is_grantable FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) x "
        "LEFT JOIN pg_roles r ON r.oid = x.grantee JOIN pg_roles g ON g.oid = x.grantor "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'S')"
    ).fetchall()
    columns = connection.execute(
        "SELECT c.relname, a.attname, COALESCE(r.rolname, 'PUBLIC'), g.rolname, "
        "x.privilege_type, x.is_grantable FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(a.attacl) x "
        "LEFT JOIN pg_roles r ON r.oid = x.grantee JOIN pg_roles g ON g.oid = x.grantor "
        "WHERE n.nspname = 'public' AND a.attacl IS NOT NULL"
    ).fetchall()
    schema = connection.execute(
        "SELECT COALESCE(r.rolname, 'PUBLIC'), g.rolname, x.privilege_type, x.is_grantable "
        "FROM pg_namespace n "
        "CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) x "
        "LEFT JOIN pg_roles r ON r.oid = x.grantee JOIN pg_roles g ON g.oid = x.grantor "
        "WHERE n.nspname = 'public'"
    ).fetchall()
    policies = connection.execute(
        "SELECT tablename, policyname, permissive, "
        "array_to_string(ARRAY(SELECT unnest(roles) ORDER BY 1), ','), cmd, qual, with_check "
        "FROM pg_policies WHERE schemaname = 'public'"
    ).fetchall()
    members = connection.execute(
        "SELECT r.rolname, m.rolname, g.rolname, a.admin_option, a.inherit_option, "
        "a.set_option FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid "
        "JOIN pg_roles m ON m.oid = a.member JOIN pg_roles g ON g.oid = a.grantor "
        "WHERE r.rolname LIKE 'crypidian%' OR m.rolname LIKE 'crypidian%'"
    ).fetchall()
    return {
        name: sorted([list(map(str, row)) for row in rows])
        for name, rows in (
            ("relations", relations),
            ("columns", columns),
            ("schema", schema),
            ("policies", policies),
            ("members", members),
        )
    }


def seed(connection):
    """Dữ liệu hai workspace; superuser bỏ qua RLS khi seed."""
    version = connection.execute("SELECT current_setting('server_version_num')::int").fetchone()
    assert version[0] >= 180006, f"SEC-02: server_version_num={version[0]} < 180006"
    for workspace, slug, user, member, account in (
        (W1, "sec01-w1", identity("u1"), M1, A1),
        (W2, "sec01-w2", identity("u2"), M2, A2),
    ):
        insert(
            connection,
            "workspace",
            id=workspace,
            name=slug,
            slug=slug,
            mode="PAPER",
            status="ACTIVE",
        )
        insert(
            connection,
            "user_account",
            id=user,
            email_normalized=f"{slug}@example.test",
            password_hash="unused",
            status="ACTIVE",
        )
        insert(
            connection,
            "user_membership",
            id=member,
            workspace_id=workspace,
            user_id=user,
            status="ACTIVE",
        )
        insert(
            connection, "role_binding", workspace_id=workspace, membership_id=member, role="owner"
        )
        insert(
            connection,
            "session",
            workspace_id=workspace,
            membership_id=member,
            token_hash=slug.ljust(64, "a"),
            expires_at=datetime(2999, 1, 1, tzinfo=UTC),
        )
        insert(
            connection,
            "mfa_factor",
            workspace_id=workspace,
            membership_id=member,
            factor_type="TOTP",
            secret_reference="ref-" + slug,
        )
        insert(
            connection,
            "secret_reference",
            workspace_id=workspace,
            provider="local_mfa",
            reference_key="ref-" + slug,
            status="ACTIVE",
        )
        insert(
            connection,
            "telegram_binding",
            workspace_id=workspace,
            membership_id=member,
            telegram_user_id="tg-" + slug,
        )
        insert(
            connection,
            "trading_account",
            id=account,
            workspace_id=workspace,
            name="acct-" + slug,
            venue="binance_usdm",
            mode="PAPER",
            environment="local",
            status="ENABLED",
            max_notional=10000,
        )
        insert(
            connection,
            "risk_profile_version",
            workspace_id=workspace,
            name="base",
            version=1,
            max_risk_fraction=Decimal("0.01"),
            settings=Jsonb({}),
            is_active=False,
        )
        insert(
            connection,
            "strategy_release",
            workspace_id=workspace,
            name="h01",
            version=1,
            status="PUBLISHED",
            recipe=Jsonb({}),
            content_hash="a" * 64,
        )
        insert(
            connection,
            "watchlist_version",
            id=identity("wl" + slug),
            workspace_id=workspace,
            name="core",
            version=1,
            status="ACTIVE",
        )
        insert(
            connection,
            "watchlist_item",
            workspace_id=workspace,
            watchlist_version_id=identity("wl" + slug),
            symbol="BTCUSDT",
            enabled=True,
            weight=Decimal("1"),
        )
        insert(
            connection,
            "workspace_settings_version",
            id=identity("wsv" + slug),
            workspace_id=workspace,
            version=1,
            schema_version=1,
            settings_json=Jsonb({"seed": True}),
            settings_hash="b" * 64,
            status="DRAFT",
            revision=1,
            created_by=member,
        )
        bot = connection.execute(
            "SELECT id FROM bot_instance WHERE workspace_id=%s AND trading_account_id=%s",
            (workspace, account),
        ).fetchone()[0]
        insert(
            connection,
            "config_version",
            workspace_id=workspace,
            bot_id=bot,
            trading_account_id=account,
            version=1,
            config=Jsonb({}),
            config_hash="c" * 64,
        )
        insert(
            connection,
            "approval_request",
            workspace_id=workspace,
            subject_type="workspace",
            subject_id=workspace,
            requested_by=member,
            status="PENDING",
        )
    return {"server_version_num": version[0]}


def check_matrix(connection):
    """has_table_privilege/has_column_privilege cho 5 process role và crypidian_app."""
    problems = []
    for role in (*PROCESS_ROLES, "crypidian_app"):
        for table in (*IDENTITY_TABLES, *MULTIBOT_TABLES):
            expected = TABLE_PRIVILEGES.get(role, {}).get(table, set())
            actual = {
                privilege
                for privilege in DML
                if connection.execute(
                    "SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)
                ).fetchone()[0]
            }
            if actual != expected:
                problems.append((role, table, sorted(actual), sorted(expected)))
            columns = [
                row[0]
                for row in connection.execute(
                    "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass "
                    "AND attnum > 0 AND NOT attisdropped",
                    (table,),
                ).fetchall()
            ]
            updatable = {
                column
                for column in columns
                if connection.execute(
                    "SELECT has_column_privilege(%s, %s, %s, 'UPDATE')", (role, table, column)
                ).fetchone()[0]
            }
            expected_update = (
                set(columns)
                if "UPDATE" in expected
                else COLUMN_UPDATE.get(role, {}).get(table, set())
            )
            if updatable != expected_update:
                problems.append((role, table, "UPDATE cols", sorted(updatable)))
    assert not problems, problems
    for role in PROCESS_ROLES:
        member = connection.execute(
            "SELECT pg_has_role(%s, 'crypidian_app', 'MEMBER')", (role,)
        ).fetchone()[0]
        assert member is (role in (ADMIN, RUNTIME)), (role, member)
        # Membership bị thu hồi; schema vẫn dùng được nhờ grant trực tiếp.
        assert connection.execute(
            "SELECT has_schema_privilege(%s, 'public', 'USAGE')", (role,)
        ).fetchone()[0]
    for table, roles in POLICY_ROLES.items():
        actual_roles = connection.execute(
            "SELECT roles::text[] FROM pg_policies WHERE schemaname='public' AND tablename=%s "
            "AND policyname='workspace_isolation'",
            (table,),
        ).fetchone()[0]
        assert set(actual_roles) == roles, (table, actual_roles)
    privileges = {
        (role, table, privilege): expected
        for role, table, privilege, expected in (
            # R1: worker giữ đúng grant trực tiếp cho việc của mình, mất DML vận hành của group.
            (INGESTION, "market_bar", "INSERT", True),
            (INGESTION, "ingestion_cursor", "UPDATE", True),
            (TRAINER, "backtest_run", "INSERT", True),
            (TRAINER, "market_bar", "SELECT", True),
            (GATEWAY, "audit_event", "INSERT", True),
            (INGESTION, "outbox_event", "INSERT", False),
            (TRAINER, "order_intent", "SELECT", False),
            (GATEWAY, "outbox_event", "UPDATE", False),
            (INGESTION, "feature_snapshot", "INSERT", False),
            # R2: admin/runtime vẫn thừa hưởng bảng vận hành từ group (SEC-01b còn mở).
            (RUNTIME, "outbox_event", "INSERT", True),
            (ADMIN, "order_intent", "SELECT", True),
            (RUNTIME, "bot_instance", "SELECT", True),
            (RUNTIME, "bot_grant", "SELECT", False),
        )
    }
    for (role, table, privilege), expected in privileges.items():
        actual = connection.execute(
            "SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)
        ).fetchone()[0]
        assert actual is expected, (role, table, privilege, actual)
    tables = len(IDENTITY_TABLES) + len(MULTIBOT_TABLES)
    return {"matrix_rows": len(PROCESS_ROLES + ("crypidian_app",)) * tables}


def scope(connection, workspace=W1, member="", bot=""):
    connection.execute(
        "SELECT set_config('app.workspace_id', %s, true), set_config('app.membership_id', %s, "
        "true), set_config('app.bot_id', %s, true)",
        (str(workspace), str(member), str(bot)),
    )


def denied(connection, statement, params=(), *, privilege=True):
    """Câu lệnh phải bị DB từ chối 42501 (mặc định: thiếu quyền bảng/cột, không phải RLS)."""
    try:
        with connection.transaction():
            scope(connection, member=M1)
            connection.execute(statement, params)
    except psycopg.Error as error:
        rejected_by_privilege = "permission denied" in str(error) or not privilege
        if error.sqlstate != "42501" or not rejected_by_privilege:
            raise AssertionError((statement, error.sqlstate, str(error))) from error
        return
    raise AssertionError(f"DB phải từ chối: {statement}")


def scalar(connection, statement, params=(), workspace=W1, member=""):
    with connection.transaction():
        scope(connection, workspace=workspace, member=member)
        return connection.execute(statement, params).fetchone()[0]


# Mọi role không phải admin đều phải bị chặn ở các thao tác identity/config này.
IDENTITY_ATTACKS = (
    (
        "INSERT INTO role_binding (id, workspace_id, membership_id, role) "
        "VALUES (gen_random_uuid(), %s, %s, 'analyst_reviewer')",
        (W1, M1),
    ),
    (
        "INSERT INTO user_membership (id, workspace_id, user_id, status) "
        "VALUES (gen_random_uuid(), %s, %s, 'ACTIVE')",
        (W1, identity("u2")),
    ),
    (
        "INSERT INTO session (id, workspace_id, membership_id, token_hash, expires_at) "
        "VALUES (gen_random_uuid(), %s, %s, 'forged', now() + interval '1 day')",
        (W1, M1),
    ),
    (
        "INSERT INTO mfa_factor (id, workspace_id, membership_id, factor_type, secret_reference) "
        "VALUES (gen_random_uuid(), %s, %s, 'TOTP', 'forged')",
        (W1, M1),
    ),
    (
        "INSERT INTO secret_reference (id, workspace_id, provider, reference_key, status) "
        "VALUES (gen_random_uuid(), %s, 'local_mfa', 'forged', 'ACTIVE')",
        (W1,),
    ),
    (
        "INSERT INTO telegram_binding (id, workspace_id, membership_id, telegram_user_id) "
        "VALUES (gen_random_uuid(), %s, %s, 'forged')",
        (W1, M1),
    ),
    ("UPDATE workspace_settings_version SET status = 'PUBLISHED' WHERE workspace_id = %s", (W1,)),
    (
        "INSERT INTO workspace_settings_version (id, workspace_id, version, schema_version, "
        "settings_json, settings_hash, status, revision, created_by) VALUES "
        "(gen_random_uuid(), %s, 9, 1, '{}', repeat('d', 64), 'DRAFT', 1, %s)",
        (W1, M1),
    ),
    (
        "INSERT INTO risk_profile_version (id, workspace_id, name, version, max_risk_fraction, "
        "settings, is_active) VALUES (gen_random_uuid(), %s, 'loose', 9, 1, '{}', true)",
        (W1,),
    ),
    ("UPDATE trading_account SET max_notional = 999999 WHERE workspace_id = %s", (W1,)),
    ("UPDATE trading_account SET status = 'DISABLED' WHERE workspace_id = %s", (W1,)),
    ("DELETE FROM trading_account WHERE workspace_id = %s", (W1,)),
    ("UPDATE approval_request SET status = 'APPROVED' WHERE workspace_id = %s", (W1,)),
    (
        "INSERT INTO strategy_release (id, workspace_id, name, version, status, recipe, "
        "content_hash) VALUES (gen_random_uuid(), %s, 'evil', 9, 'PUBLISHED', '{}', "
        "repeat('e', 64))",
        (W1,),
    ),
    ("SELECT * FROM mfa_factor", ()),
    ("SELECT * FROM session", ()),
    ("SELECT * FROM secret_reference", ()),
    ("SELECT * FROM user_membership", ()),
    ("SELECT * FROM role_binding", ()),
    ("SELECT * FROM telegram_binding", ()),
    ("SELECT * FROM watchlist_item", ()),
)


RUNTIME_REGISTRY_ATTACKS = (
    (
        "INSERT INTO workspace_runtime_control (id, workspace_id, revision, kill_state, "
        "reason_code, reason_text, changed_by, correlation_id) VALUES (gen_random_uuid(), %s, "
        "1, 'ACTIVE', 'forged', 'forged', %s, gen_random_uuid())",
        (W1, M1),
    ),
    (
        "INSERT INTO bot_runtime_control (id, workspace_id, bot_id, account_id, revision, "
        "entry_state, reason_code, reason_text, changed_by, correlation_id) SELECT "
        "gen_random_uuid(), workspace_id, id, trading_account_id, 1, 'ENABLED', 'forged', "
        "'forged', %s, gen_random_uuid() FROM bot_instance",
        (M1,),
    ),
    ("UPDATE bot_runtime_lease SET bot_id = bot_id", ()),
    ("DELETE FROM bot_runtime_lease", ()),
    ("DELETE FROM bot_runtime_state", ()),
    ("UPDATE bot_runtime_state SET account_id = account_id", ()),
    ("UPDATE account_activity_incident SET status = 'RESOLVED'", ()),
    ("DELETE FROM account_activity_incident", ()),
    ("UPDATE bot_decision_job SET evidence_hash = evidence_hash", ()),
    ("DELETE FROM bot_decision_job", ()),
)
ADMIN_REGISTRY_ATTACKS = (
    "INSERT INTO bot_runtime_lease (id, workspace_id, bot_id, account_id, owner_id, "
    "fencing_token, expires_at) SELECT gen_random_uuid(), workspace_id, id, trading_account_id, "
    "'admin', 1, now() + interval '1 minute' FROM bot_instance",
    "UPDATE bot_runtime_lease SET fencing_token = fencing_token + 1",
    "INSERT INTO bot_runtime_state (id, workspace_id, bot_id, account_id, readiness, "
    "heartbeat_at, fencing_token) SELECT gen_random_uuid(), workspace_id, id, "
    "trading_account_id, 'READY', now(), 1 FROM bot_instance",
    "UPDATE bot_runtime_state SET readiness = 'READY'",
    "UPDATE workspace_runtime_control SET kill_state = 'ACTIVE'",
    "DELETE FROM workspace_runtime_control",
    "UPDATE bot_runtime_control SET entry_state = 'ENABLED'",
    "DELETE FROM bot_runtime_control",
    "UPDATE account_activity_incident SET fingerprint = fingerprint",
    "DELETE FROM account_activity_incident",
)


def worker(connection, role):
    assert connection.execute("SELECT current_user").fetchone()[0] == role
    for statement, params in IDENTITY_ATTACKS:
        denied(connection, statement, params)
    for table in (
        "trading_account",
        "workspace_settings_version",
        "strategy_release",
        "risk_profile_version",
        "config_version",
        "approval_request",
        "workspace",
    ):
        denied(connection, sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
    denied(connection, "SET LOCAL ROLE crypidian_app")
    denied(connection, "SELECT count(*) FROM outbox_event")
    own_job = {
        INGESTION: "SELECT count(*) FROM market_bar",
        TRAINER: "SELECT count(*) FROM backtest_run",
        GATEWAY: "SELECT has_table_privilege(current_user, 'audit_event', 'INSERT')",
    }[role]
    assert scalar(connection, own_job) is not None
    return {"role": role, "denied": len(IDENTITY_ATTACKS) + 9}


def runtime(connection):
    assert connection.execute("SELECT current_user").fetchone()[0] == RUNTIME
    for statement, params in IDENTITY_ATTACKS:
        denied(connection, statement, params)
    denied(connection, "UPDATE trading_account SET mode = 'PAPER' WHERE id = %s", (A1,))
    denied(
        connection,
        "INSERT INTO config_version (id, workspace_id, bot_id, trading_account_id, "
        "version, config, config_hash) SELECT gen_random_uuid(), workspace_id, id, "
        "trading_account_id, 9, '{}', repeat('f', 64) FROM bot_instance",
    )
    # MB-05: runtime không ghi control/resolution, không xóa/sửa identity lease/state/job.
    for statement, params in RUNTIME_REGISTRY_ATTACKS:
        denied(connection, statement, params)
    # Group crypidian_app không còn quyền identity: SET ROLE không mở lại đường ghi.
    with connection.transaction():
        scope(connection)
        connection.execute("SET LOCAL ROLE crypidian_app")
        try:
            with connection.transaction():
                connection.execute("SELECT count(*) FROM session")
        except psycopg.Error as error:
            if error.sqlstate != "42501":
                raise AssertionError(str(error)) from error
        else:
            raise AssertionError("crypidian_app vẫn đọc được session")
    # Đường code runtime vẫn chạy và RLS workspace không đổi.
    assert scalar(connection, "SELECT count(*) FROM trading_account") == 1
    assert scalar(connection, "SELECT count(*) FROM trading_account", workspace=W2) == 1
    assert scalar(connection, "SELECT count(*) FROM trading_account", workspace="") == 0
    for table in ("strategy_release", "risk_profile_version", "workspace_settings_version"):
        query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        assert scalar(connection, query) == 1, table
    assert scalar(connection, "SELECT count(*) FROM approval_request") == 1
    assert scalar(connection, "SELECT count(*) FROM workspace") == 2
    with connection.transaction():
        scope(connection)
        locked = connection.execute(
            "SELECT id FROM trading_account WHERE id = %s FOR UPDATE", (A1,)
        ).fetchone()
        assert locked == (A1,)
        touched = connection.execute(
            "UPDATE trading_account SET updated_at = now() WHERE id = %s", (A1,)
        ).rowcount
        assert touched == 1
        connection.execute(
            "INSERT INTO trading_account (id, workspace_id, name, venue, mode, environment, "
            "status, max_notional) VALUES (%s, %s, 'runtime-paper', 'binance_usdm', 'PAPER', "
            "'local', 'ENABLED', 1000)",
            (identity("runtime-account"), W1),
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM bot_instance WHERE trading_account_id = %s",
                (identity("runtime-account"),),
            ).fetchone()[0]
            == 1
        )
    return {"role": RUNTIME, "denied": len(IDENTITY_ATTACKS) + 3 + len(RUNTIME_REGISTRY_ATTACKS)}


def admin(connection):
    assert connection.execute("SELECT current_user").fetchone()[0] == ADMIN
    for statement in (
        "UPDATE session SET expires_at = now() + interval '10 years'",
        "UPDATE session SET token_hash = 'stolen'",
        "UPDATE mfa_factor SET secret_reference = 'swapped'",
        "UPDATE trading_account SET mode = 'PAPER'",
        "UPDATE trading_account SET environment = 'production'",
        "UPDATE user_membership SET user_id = user_id",
        # Fix round 1: không code nào sửa status membership (predicate RLS MB-01) hay cap account.
        "UPDATE user_membership SET status = 'SUSPENDED'",
        "UPDATE trading_account SET max_notional = 999999",
        "UPDATE trading_account SET status = 'DISABLED'",
        "UPDATE trading_account SET name = 'renamed'",
        "UPDATE role_binding SET role = 'owner'",
        "DELETE FROM role_binding",
        "DELETE FROM session",
        "DELETE FROM mfa_factor",
        "DELETE FROM user_membership",
        "DELETE FROM trading_account",
        "DELETE FROM workspace_settings_version",
        "INSERT INTO telegram_binding (id, workspace_id, membership_id, telegram_user_id) "
        "SELECT gen_random_uuid(), workspace_id, id, 'forged' FROM user_membership",
        "UPDATE approval_request SET status = 'APPROVED'",
        "INSERT INTO strategy_release (id, workspace_id, name, version, status, recipe, "
        "content_hash) VALUES (gen_random_uuid(), "
        "current_setting('app.workspace_id')::uuid, 'evil', 9, 'PUBLISHED', '{}', "
        "repeat('e', 64))",
        *ADMIN_REGISTRY_ATTACKS,
    ):
        denied(connection, statement)
    with connection.transaction():
        scope(connection, member=M1)
        for table in (
            "session",
            "mfa_factor",
            "secret_reference",
            "user_membership",
            "role_binding",
            "telegram_binding",
            "watchlist_item",
        ):
            query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            assert connection.execute(query).fetchone()[0] == 1, table
        # Luồng admin thật: khóa/sửa đúng cột code dùng, tạo binding, secret, risk profile, draft.
        connection.execute("SELECT id FROM user_membership WHERE id = %s FOR UPDATE", (M1,))
        connection.execute("SELECT id FROM mfa_factor FOR UPDATE")
        assert (
            connection.execute(
                "UPDATE session SET revoked_at = now(), csrf_token_hash = 'x', "
                "mfa_verified_at = now()"
            ).rowcount
            == 1
        )
        assert (
            connection.execute(
                "UPDATE mfa_factor SET last_used_counter = 7, verified_at = now()"
            ).rowcount
            == 1
        )
        connection.execute(
            "INSERT INTO role_binding (id, workspace_id, membership_id, role) "
            "VALUES (gen_random_uuid(), %s, %s, 'analyst_reviewer')",
            (W1, M1),
        )
        connection.execute(
            "INSERT INTO secret_reference (id, workspace_id, provider, reference_key, status) "
            "VALUES (gen_random_uuid(), %s, 'local_mfa', 'ref-admin', 'ACTIVE') RETURNING id",
            (W1,),
        )
        connection.execute(
            "INSERT INTO risk_profile_version (id, workspace_id, name, version, "
            "max_risk_fraction, settings, is_active) VALUES (gen_random_uuid(), %s, 'base', 2, "
            "0.01, '{}', false) RETURNING created_at",
            (W1,),
        )
        draft = identity("admin-draft")
        connection.execute(
            "INSERT INTO workspace_settings_version (id, workspace_id, version, schema_version, "
            "settings_json, settings_hash, status, revision, created_by) VALUES "
            "(%s, %s, 2, 1, '{}', repeat('d', 64), 'DRAFT', 1, %s)",
            (draft, W1, M1),
        )
        assert (
            connection.execute(
                "UPDATE workspace_settings_version SET status = 'PUBLISHED', approved_by = %s, "
                "published_at = now(), effective_at = now() WHERE id = %s",
                (M1, draft),
            ).rowcount
            == 1
        )
        # MB-02 tạo bot bằng INSERT account PAPER (trigger tạo bot) rồi khóa account FOR UPDATE.
        connection.execute(
            "INSERT INTO trading_account (id, workspace_id, name, venue, mode, environment, "
            "status, max_notional) VALUES (%s, %s, 'admin-paper', 'binance_usdm', 'PAPER', "
            "'local', 'DISABLED', 1000) RETURNING created_at",
            (identity("admin-account"), W1),
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM bot_instance WHERE trading_account_id = %s",
                (identity("admin-account"),),
            ).fetchone()[0]
            == 1
        )
        assert connection.execute(
            "SELECT id FROM trading_account WHERE id = %s FOR UPDATE", (A1,)
        ).fetchone() == (A1,)
        connection.execute("SELECT id FROM workspace WHERE id = %s FOR UPDATE", (W1,))
    # RLS workspace của admin không đổi: GUC W2 không thấy dữ liệu W1.
    assert (
        scalar(
            connection,
            "SELECT count(*) FROM session WHERE workspace_id = %s",
            (W1,),
            workspace=W2,
            member=M2,
        )
        == 0
    )
    assert scalar(connection, "SELECT count(*) FROM mfa_factor", workspace=W2, member=M2) == 1
    assert scalar(connection, "SELECT count(*) FROM mfa_factor", workspace="") == 0
    return {"role": ADMIN, "denied": 20 + len(ADMIN_REGISTRY_ATTACKS)}


password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
with psycopg.connect(
    host="postgres",
    dbname="crypidian",
    user=os.environ["TRADING_BOT_DATABASE_USER"],
    password=password,
    autocommit=True,
) as database:
    phase = os.environ["SEC01_PHASE"]
    if phase == "acl_snapshot":
        result = acl_state(database)
    elif phase == "acl_compare":
        before, after = json.loads(os.environ["SEC01_SNAPSHOT"]), acl_state(database)
        difference = {
            key: {
                "missing": [row for row in before[key] if row not in after[key]],
                "extra": [row for row in after[key] if row not in before[key]],
            }
            for key in before
            if before[key] != after[key]
        }
        assert not difference, difference
        result = {"acl_restored": True}
    elif phase == "seed":
        with database.transaction():
            result = seed(database)
    elif phase == "matrix":
        result = check_matrix(database)
    elif phase == "worker":
        result = worker(database, os.environ["TRADING_BOT_DATABASE_USER"])
    elif phase == "runtime":
        result = runtime(database)
    elif phase == "admin":
        result = admin(database)
    else:
        raise AssertionError(phase)
    print(json.dumps(result, default=str))

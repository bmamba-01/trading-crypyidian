"""WP06.14: tách login admin-web khỏi quyền kế thừa của role ứng dụng.

Revision ID: f0a2c4e6b8d4
Revises: f0a2c4e6b8d3
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "f0a2c4e6b8d4"
down_revision = "f0a2c4e6b8d3"
branch_labels = None
depends_on = None

ADMIN = "crypidian_admin_web"
APP_ROLE = "crypidian_app"
SAFE_ROLE = "crypidian_admin_web_safe"
REPORT_READER = "crypidian_admin_report_reader"
MANUAL_REPORT_READER = "crypidian_report_reader"
TELEMETRY_READER = "crypidian_admin_telemetry_reader"
REPORT_SCOPE_ROLE = "crypidian_admin_report_scope_owner"
SNAPSHOT_TABLE = "wp06_admin_web_acl_snapshot"
ACL_MANIFEST_PATH = Path(__file__).parents[1] / "wp06_admin_safe_acl_manifest.json"
_MANIFEST_ROWS = json.loads(ACL_MANIFEST_PATH.read_text(encoding="utf-8"))
ADMIN_SAFE_ACL_MANIFEST = {
    entry["key"]: entry for entry in _MANIFEST_ROWS if entry["kind"] != "membership"
}
(MEMBERSHIP_SNAPSHOT,) = (row for row in _MANIFEST_ROWS if row["kind"] == "membership")
_SNAPSHOT_FIELDS = (
    "kind",
    "object_identity",
    "grantor_name",
    "consumer",
    "operation",
    "disposition",
    "grant_sql",
    "safe_grant_sql",
    "safe_revoke_sql",
)

REPORT_READER_RELATIONS = frozenset(
    {
        "bot_instance",
        "paper_account_seed",
        "trade",
        "trade_cost",
        "trade_plan",
        "order_intent",
        "fill",
        "position_event",
        "risk_reservation",
        "bot_decision",
        "proposal_backtest",
        "shared_signal_evidence",
        "analyst_run",
        "run_report",
        "bot_runtime_state",
        "outbox_event",
        "market_instrument",
        "market_bar",
        "market_mark_price",
        "market_live_quote",
        "ingestion_gap",
    }
)

_REPORT_WORKSPACE_RELATIONS = frozenset(
    {
        "market_instrument",
        "market_bar",
        "market_mark_price",
        "market_live_quote",
        "ingestion_gap",
    }
)
_REPORT_WORKSPACE_READ_RELATIONS = frozenset({"analyst_run", "run_report"})
_REPORT_BOT_RELATIONS = REPORT_READER_RELATIONS - {
    "bot_instance",
    "shared_signal_evidence",
    "outbox_event",
    *_REPORT_WORKSPACE_RELATIONS,
    *_REPORT_WORKSPACE_READ_RELATIONS,
}
_OUTBOX_READER_COLUMNS = (
    "id, workspace_id, aggregate_id, event_type, status, available_at, created_at, occurred_at"
)
_ANALYST_RUN_READER_COLUMNS = (
    "id, workspace_id, created_at, topic_key, engine, model, cli_version, session_id, resumed, "
    "prompt_version, output_version, run_report_hash, input_sha256, valid, error, exit_code, "
    "cost_usd, usage, validation_errors, structured_output, context"
)
_RUN_REPORT_READER_COLUMNS = (
    "id, workspace_id, report_hash, report_version, run_label, window_start, window_end, "
    "bot_count, payload, created_at"
)
_MANUAL_REPORT_READER_ACCOUNT_COLUMNS = "workspace_id, id, status"
_MANUAL_REPORT_READER_ACCOUNT_POLICY_SQL = f"""
CREATE POLICY wp06_manual_report_reader_account_select ON public.trading_account
FOR SELECT TO {MANUAL_REPORT_READER}
USING (
    workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
)
"""

# Những ledger admin không được truy vấn trực tiếp. Quyền đọc hợp lệ sẽ đi qua reader/gateway
# kiểm tra workspace và bot; không chuyển tiếp ACL cũ của chúng sang nhóm admin-safe.
RAW_LEDGER_RELATIONS = frozenset(
    {
        "trade",
        "trade_cost",
        "trade_plan",
        "order_intent",
        "exchange_order",
        "fill",
        "position_event",
        "risk_decision",
        "risk_reservation",
        "paper_candidate_outcome",
        "paper_execution_checkpoint",
        "bot_decision",
        "bot_decision_job",
        "shared_signal_evidence",
        "feature_snapshot",
        "market_bar",
        "learning_score_event",
        "signal_outcome",
        "venue_order_intent",
        "venue_order_transition",
        "run_report",
        "analyst_run",
        "proposal_backtest",
        "telegram_trade_notification",
        "telegram_signal_delivery",
    }
)

RAW_LEDGER_COLUMNS: dict[str, frozenset[str]] = {
    "trade": frozenset({"gross_pnl", "fee_total", "funding_total", "slippage_total", "net_pnl"}),
    "order_intent": frozenset({"quantity", "filled_quantity", "evidence"}),
    "bot_decision": frozenset({"payload", "decision_hash"}),
    "bot_decision_job": frozenset({"context_payload"}),
    "shared_signal_evidence": frozenset({"canonical_json", "source_refs"}),
    "learning_score_event": frozenset({"class_probabilities", "canonical_event"}),
}


def _upgrade_statements() -> tuple[str, ...]:
    """DDL tường minh để unit test khóa role membership và snapshot bất biến theo revision."""
    return (
        f"CREATE ROLE {SAFE_ROLE} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOREPLICATION NOBYPASSRLS",
        f"CREATE ROLE {TELEMETRY_READER} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOREPLICATION NOBYPASSRLS",
        f"CREATE ROLE {REPORT_SCOPE_ROLE} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOREPLICATION NOBYPASSRLS",
        f"GRANT {SAFE_ROLE} TO {ADMIN} WITH INHERIT TRUE, SET FALSE, ADMIN FALSE",
        f"REVOKE {APP_ROLE} FROM {ADMIN}",
        f"CREATE TABLE public.{SNAPSHOT_TABLE} ("
        "kind text NOT NULL, "
        "object_identity text NOT NULL, grantor_name text NOT NULL, consumer text NOT NULL, "
        "operation text NOT NULL, disposition text NOT NULL, "
        "grant_sql text PRIMARY KEY, safe_grant_sql text, safe_revoke_sql text)",
    )


def _downgrade_statements() -> tuple[str, ...]:
    """DDL cuối của downgrade; ACL được phục hồi từ snapshot trước khi xóa group role."""
    return (
        f"REVOKE {SAFE_ROLE} FROM {ADMIN}",
        f"DROP ROLE {SAFE_ROLE}",
        f"DROP ROLE {TELEMETRY_READER}",
        f"DROP ROLE {REPORT_SCOPE_ROLE}",
        f"DROP TABLE public.{SNAPSHOT_TABLE}",
    )


_REPORT_SCOPE_HELPER_SQL = """
CREATE FUNCTION public.wp06_admin_report_reader_can_access(
    p_workspace_id uuid, p_bot_id uuid
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $function$
    SELECT p_workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
       AND NULLIF(current_setting('app.membership_id', true), '') IS NOT NULL
       AND EXISTS (
            SELECT 1 FROM public.user_membership AS m
            WHERE m.workspace_id = p_workspace_id
              AND m.id = NULLIF(current_setting('app.membership_id', true), '')::uuid
              AND m.status = 'ACTIVE'
       )
       AND (
            EXISTS (
                SELECT 1 FROM public.role_binding AS r
                WHERE r.workspace_id = p_workspace_id
                  AND r.membership_id = NULLIF(current_setting('app.membership_id', true), '')::uuid
                  AND r.role IN ('owner', 'strategy_admin', 'risk_admin')
            )
            OR (
                p_bot_id IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM public.role_binding AS r
                    WHERE r.workspace_id = p_workspace_id
                      AND r.membership_id = NULLIF(
                          current_setting('app.membership_id', true), ''
                      )::uuid
                      AND r.role IN ('operator', 'analyst_reviewer')
                )
                AND EXISTS (
                    SELECT 1 FROM public.bot_grant AS g
                    WHERE g.workspace_id = p_workspace_id
                      AND g.bot_id = p_bot_id
                      AND g.membership_id = NULLIF(
                          current_setting('app.membership_id', true), ''
                      )::uuid
                      AND g.permission = 'bot:report:read'
                      AND g.revoked_at IS NULL
                      AND (g.expires_at IS NULL OR g.expires_at > clock_timestamp())
                )
            )
       )
$function$
"""

_REPORT_WORKSPACE_HELPER_SQL = """
CREATE FUNCTION public.wp06_admin_report_reader_can_read_workspace(
    p_workspace_id uuid
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $function$
    SELECT p_workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
       AND EXISTS (
            SELECT 1 FROM public.user_membership AS m
            WHERE m.workspace_id = p_workspace_id
              AND m.id = NULLIF(current_setting('app.membership_id', true), '')::uuid
              AND m.status = 'ACTIVE'
       )
       AND (
            EXISTS (
                SELECT 1 FROM public.role_binding AS r
                WHERE r.workspace_id = p_workspace_id
                  AND r.membership_id = NULLIF(
                      current_setting('app.membership_id', true), ''
                  )::uuid
                  AND r.role IN ('owner', 'strategy_admin', 'risk_admin')
            )
            OR (
                EXISTS (
                    SELECT 1 FROM public.role_binding AS r
                    WHERE r.workspace_id = p_workspace_id
                      AND r.membership_id = NULLIF(
                          current_setting('app.membership_id', true), ''
                      )::uuid
                      AND r.role IN ('operator', 'analyst_reviewer')
                )
                AND EXISTS (
                    SELECT 1 FROM public.bot_grant AS g
                    WHERE g.workspace_id = p_workspace_id
                      AND g.membership_id = NULLIF(
                          current_setting('app.membership_id', true), ''
                      )::uuid
                      AND g.permission = 'bot:report:read'
                      AND g.revoked_at IS NULL
                      AND (g.expires_at IS NULL OR g.expires_at > clock_timestamp())
                )
            )
       )
$function$
"""

_REPORT_WORKSPACE_READ_HELPER_SQL = """
CREATE FUNCTION public.wp06_admin_report_reader_can_read_workspace_page(
    p_workspace_id uuid
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $function$
    SELECT p_workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
       AND EXISTS (
            SELECT 1 FROM public.user_membership AS m
            WHERE m.workspace_id = p_workspace_id
              AND m.id = NULLIF(current_setting('app.membership_id', true), '')::uuid
              AND m.status = 'ACTIVE'
       )
       AND EXISTS (
            SELECT 1 FROM public.role_binding AS r
            WHERE r.workspace_id = p_workspace_id
              AND r.membership_id = NULLIF(
                  current_setting('app.membership_id', true), ''
              )::uuid
              AND r.role IN ('owner', 'strategy_admin', 'risk_admin')
       )
$function$
"""


_RELATION_ACL_SQL = sa.text(
    """
    SELECT
        'relation' AS kind,
        c.relname AS object_name,
        n.nspname AS schema_name,
        c.relname AS object_identity,
        c.relkind AS object_subtype,
        x.privilege_type AS privilege_type,
        x.is_grantable AS is_grantable,
        grantor.rolname AS grantor_name,
        pg_catalog.format(
            'GRANT %s ON %s %I.%I TO %I', x.privilege_type,
            CASE WHEN c.relkind = 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
            n.nspname, c.relname, CAST(:admin_role AS text)
        ) AS grant_sql,
        pg_catalog.format(
            'REVOKE %s ON %s %I.%I FROM %I', x.privilege_type,
            CASE WHEN c.relkind = 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
            n.nspname, c.relname, CAST(:admin_role AS text)
        ) AS revoke_sql,
        pg_catalog.format(
            'GRANT %s ON %s %I.%I TO %I', x.privilege_type,
            CASE WHEN c.relkind = 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
            n.nspname, c.relname, CAST(:safe_role AS text)
        ) AS safe_grant_sql,
        pg_catalog.format(
            'REVOKE %s ON %s %I.%I FROM %I', x.privilege_type,
            CASE WHEN c.relkind = 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
            n.nspname, c.relname, CAST(:safe_role AS text)
        ) AS safe_revoke_sql
    FROM pg_catalog.pg_class AS c
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) AS x
    JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = x.grantor
    WHERE x.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
    ORDER BY n.nspname, c.relname, x.privilege_type
    """
)

_COLUMN_ACL_SQL = sa.text(
    """
    SELECT
        'column' AS kind,
        c.relname AS object_name,
        n.nspname AS schema_name,
        c.relname || '.' || a.attname AS object_identity,
        c.relkind AS object_subtype,
        a.attname AS column_name,
        x.privilege_type AS privilege_type,
        x.is_grantable AS is_grantable,
        grantor.rolname AS grantor_name,
        pg_catalog.format(
            'GRANT %s (%I) ON TABLE %I.%I TO %I',
            x.privilege_type, a.attname, n.nspname, c.relname, CAST(:admin_role AS text)
        ) AS grant_sql,
        pg_catalog.format(
            'REVOKE %s (%I) ON TABLE %I.%I FROM %I',
            x.privilege_type, a.attname, n.nspname, c.relname, CAST(:admin_role AS text)
        ) AS revoke_sql,
        pg_catalog.format(
            'GRANT %s (%I) ON TABLE %I.%I TO %I',
            x.privilege_type, a.attname, n.nspname, c.relname, CAST(:safe_role AS text)
        ) AS safe_grant_sql,
        pg_catalog.format(
            'REVOKE %s (%I) ON TABLE %I.%I FROM %I',
            x.privilege_type, a.attname, n.nspname, c.relname, CAST(:safe_role AS text)
        ) AS safe_revoke_sql
    FROM pg_catalog.pg_attribute AS a
    JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) AS x
    JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = x.grantor
    WHERE x.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
      AND a.attnum <> 0
    ORDER BY n.nspname, c.relname, a.attnum, x.privilege_type
    """
)

_FUNCTION_ACL_SQL = sa.text(
    """
    SELECT
        'routine' AS kind,
        p.proname AS object_name,
        n.nspname AS schema_name,
        p.proname || '(' || pg_catalog.pg_get_function_identity_arguments(p.oid) || ')'
            AS object_identity,
        p.prokind AS object_subtype,
        x.privilege_type AS privilege_type,
        x.is_grantable AS is_grantable,
        grantor.rolname AS grantor_name,
        pg_catalog.format(
            'GRANT EXECUTE ON %s %s TO %I',
            CASE WHEN p.prokind = 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
            pg_catalog.format(
                '%I.%I(%s)', n.nspname, p.proname,
                pg_catalog.pg_get_function_identity_arguments(p.oid)
            ), CAST(:admin_role AS text)
        ) AS grant_sql,
        pg_catalog.format(
            'REVOKE EXECUTE ON %s %s FROM %I',
            CASE WHEN p.prokind = 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
            pg_catalog.format(
                '%I.%I(%s)', n.nspname, p.proname,
                pg_catalog.pg_get_function_identity_arguments(p.oid)
            ), CAST(:admin_role AS text)
        ) AS revoke_sql,
        pg_catalog.format(
            'GRANT EXECUTE ON %s %s TO %I',
            CASE WHEN p.prokind = 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
            pg_catalog.format(
                '%I.%I(%s)', n.nspname, p.proname,
                pg_catalog.pg_get_function_identity_arguments(p.oid)
            ), CAST(:safe_role AS text)
        ) AS safe_grant_sql,
        pg_catalog.format(
            'REVOKE EXECUTE ON %s %s FROM %I',
            CASE WHEN p.prokind = 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
            pg_catalog.format(
                '%I.%I(%s)', n.nspname, p.proname,
                pg_catalog.pg_get_function_identity_arguments(p.oid)
            ), CAST(:safe_role AS text)
        ) AS safe_revoke_sql
    FROM pg_catalog.pg_proc AS p
    JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
    CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) AS x
    JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = x.grantor
    WHERE x.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
    ORDER BY n.nspname, p.proname, pg_catalog.pg_get_function_identity_arguments(p.oid)
    """
)

_SCHEMA_ACL_SQL = sa.text(
    """
    SELECT
        'schema' AS kind,
        n.nspname AS object_name,
        n.nspname AS schema_name,
        n.nspname AS object_identity,
        ''::text AS object_subtype,
        x.privilege_type AS privilege_type,
        x.is_grantable AS is_grantable,
        grantor.rolname AS grantor_name,
        pg_catalog.format('GRANT %s ON SCHEMA %I TO %I', x.privilege_type,
                          n.nspname, CAST(:admin_role AS text)) AS grant_sql,
        pg_catalog.format('REVOKE %s ON SCHEMA %I FROM %I', x.privilege_type,
                          n.nspname, CAST(:admin_role AS text)) AS revoke_sql,
        CASE WHEN x.privilege_type = 'USAGE'
             THEN pg_catalog.format(
                 'GRANT USAGE ON SCHEMA %I TO %I', n.nspname, CAST(:safe_role AS text)
             )
             ELSE NULL END AS safe_grant_sql,
        CASE WHEN x.privilege_type = 'USAGE'
             THEN pg_catalog.format(
                 'REVOKE USAGE ON SCHEMA %I FROM %I', n.nspname, CAST(:safe_role AS text)
             )
             ELSE NULL END AS safe_revoke_sql
    FROM pg_catalog.pg_namespace AS n
    CROSS JOIN LATERAL pg_catalog.aclexplode(n.nspacl) AS x
    JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = x.grantor
    WHERE x.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
    ORDER BY n.nspname, x.privilege_type
    """
)

_TYPE_ACL_SQL = sa.text(
    """
    SELECT
        'type' AS kind,
        t.typname AS object_name,
        n.nspname AS schema_name,
        t.typname AS object_identity,
        t.typtype AS object_subtype,
        x.privilege_type AS privilege_type,
        x.is_grantable AS is_grantable,
        grantor.rolname AS grantor_name,
        pg_catalog.format('GRANT USAGE ON TYPE %I.%I TO %I', n.nspname,
                          t.typname, CAST(:admin_role AS text)) AS grant_sql,
        pg_catalog.format('REVOKE USAGE ON TYPE %I.%I FROM %I', n.nspname,
                          t.typname, CAST(:admin_role AS text)) AS revoke_sql,
        pg_catalog.format('GRANT USAGE ON TYPE %I.%I TO %I', n.nspname,
                          t.typname, CAST(:safe_role AS text)) AS safe_grant_sql,
        pg_catalog.format('REVOKE USAGE ON TYPE %I.%I FROM %I', n.nspname,
                          t.typname, CAST(:safe_role AS text)) AS safe_revoke_sql
    FROM pg_catalog.pg_type AS t
    JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
    CROSS JOIN LATERAL pg_catalog.aclexplode(t.typacl) AS x
    JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = x.grantor
    WHERE x.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
    ORDER BY n.nspname, t.typname, x.privilege_type
    """
)

_DATABASE_ACL_SQL = sa.text(
    """
    SELECT
        'database' AS kind,
        d.datname AS object_name,
        ''::text AS schema_name,
        d.datname AS object_identity,
        ''::text AS object_subtype,
        x.privilege_type AS privilege_type,
        x.is_grantable AS is_grantable,
        grantor.rolname AS grantor_name,
        pg_catalog.format('GRANT %s ON DATABASE %I TO %I', x.privilege_type,
                          d.datname, CAST(:admin_role AS text)) AS grant_sql,
        pg_catalog.format('REVOKE %s ON DATABASE %I FROM %I', x.privilege_type,
                          d.datname, CAST(:admin_role AS text)) AS revoke_sql,
        CASE WHEN x.privilege_type IN ('CONNECT', 'TEMPORARY')
             THEN pg_catalog.format('GRANT %s ON DATABASE %I TO %I', x.privilege_type,
                                    d.datname, CAST(:safe_role AS text))
             ELSE NULL END AS safe_grant_sql,
        CASE WHEN x.privilege_type IN ('CONNECT', 'TEMPORARY')
             THEN pg_catalog.format('REVOKE %s ON DATABASE %I FROM %I', x.privilege_type,
                                    d.datname, CAST(:safe_role AS text))
             ELSE NULL END AS safe_revoke_sql
    FROM pg_catalog.pg_database AS d
    CROSS JOIN LATERAL pg_catalog.aclexplode(d.datacl) AS x
    JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = x.grantor
    WHERE x.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
    ORDER BY x.privilege_type
    """
)

_LARGE_OBJECT_ACL_SQL = sa.text(
    """
    SELECT 'large_object' AS kind, m.oid::text AS object_name,
           ''::text AS schema_name, m.oid::text AS object_identity,
           ''::text AS object_subtype, x.privilege_type, x.is_grantable,
           grantor.rolname AS grantor_name,
           pg_catalog.format('GRANT %s ON LARGE OBJECT %s TO %I', x.privilege_type,
                             m.oid, CAST(:admin_role AS text)) AS grant_sql,
           pg_catalog.format('REVOKE %s ON LARGE OBJECT %s FROM %I', x.privilege_type,
                             m.oid, CAST(:admin_role AS text)) AS revoke_sql,
           pg_catalog.format('GRANT %s ON LARGE OBJECT %s TO %I', x.privilege_type,
                             m.oid, CAST(:safe_role AS text)) AS safe_grant_sql,
           pg_catalog.format('REVOKE %s ON LARGE OBJECT %s FROM %I', x.privilege_type,
                             m.oid, CAST(:safe_role AS text)) AS safe_revoke_sql
    FROM pg_catalog.pg_largeobject_metadata AS m
    CROSS JOIN LATERAL pg_catalog.aclexplode(m.lomacl) AS x
    JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = x.grantor
    WHERE x.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
    ORDER BY m.oid, x.privilege_type
    """
)

_LANGUAGE_ACL_SQL = sa.text(
    """
    SELECT 'language' AS kind, l.lanname AS object_name,
           ''::text AS schema_name, l.lanname AS object_identity,
           ''::text AS object_subtype, x.privilege_type, x.is_grantable,
           grantor.rolname AS grantor_name,
           pg_catalog.format('GRANT %s ON LANGUAGE %I TO %I', x.privilege_type,
                             l.lanname, CAST(:admin_role AS text)) AS grant_sql,
           pg_catalog.format('REVOKE %s ON LANGUAGE %I FROM %I', x.privilege_type,
                             l.lanname, CAST(:admin_role AS text)) AS revoke_sql,
           pg_catalog.format('GRANT %s ON LANGUAGE %I TO %I', x.privilege_type,
                             l.lanname, CAST(:safe_role AS text)) AS safe_grant_sql,
           pg_catalog.format('REVOKE %s ON LANGUAGE %I FROM %I', x.privilege_type,
                             l.lanname, CAST(:safe_role AS text)) AS safe_revoke_sql
    FROM pg_catalog.pg_language AS l
    CROSS JOIN LATERAL pg_catalog.aclexplode(l.lanacl) AS x
    JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = x.grantor
    WHERE x.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
    ORDER BY l.lanname, x.privilege_type
    """
)

_FOREIGN_DATA_WRAPPER_ACL_SQL = sa.text(
    """
    SELECT 'foreign_data_wrapper' AS kind, f.fdwname AS object_name,
           ''::text AS schema_name, f.fdwname AS object_identity,
           ''::text AS object_subtype, x.privilege_type, x.is_grantable,
           grantor.rolname AS grantor_name,
           pg_catalog.format('GRANT %s ON FOREIGN DATA WRAPPER %I TO %I',
                             x.privilege_type, f.fdwname, CAST(:admin_role AS text)) AS grant_sql,
           pg_catalog.format('REVOKE %s ON FOREIGN DATA WRAPPER %I FROM %I',
                             x.privilege_type, f.fdwname, CAST(:admin_role AS text)) AS revoke_sql,
           pg_catalog.format(
               'GRANT %s ON FOREIGN DATA WRAPPER %I TO %I', x.privilege_type,
               f.fdwname, CAST(:safe_role AS text)
           ) AS safe_grant_sql,
           pg_catalog.format(
               'REVOKE %s ON FOREIGN DATA WRAPPER %I FROM %I', x.privilege_type,
               f.fdwname, CAST(:safe_role AS text)
           ) AS safe_revoke_sql
    FROM pg_catalog.pg_foreign_data_wrapper AS f
    CROSS JOIN LATERAL pg_catalog.aclexplode(f.fdwacl) AS x
    JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = x.grantor
    WHERE x.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
    ORDER BY f.fdwname, x.privilege_type
    """
)

_FOREIGN_SERVER_ACL_SQL = sa.text(
    """
    SELECT 'foreign_server' AS kind, s.srvname AS object_name,
           ''::text AS schema_name, s.srvname AS object_identity,
           ''::text AS object_subtype, x.privilege_type, x.is_grantable,
           grantor.rolname AS grantor_name,
           pg_catalog.format('GRANT %s ON FOREIGN SERVER %I TO %I', x.privilege_type,
                             s.srvname, CAST(:admin_role AS text)) AS grant_sql,
           pg_catalog.format('REVOKE %s ON FOREIGN SERVER %I FROM %I', x.privilege_type,
                             s.srvname, CAST(:admin_role AS text)) AS revoke_sql,
           pg_catalog.format('GRANT %s ON FOREIGN SERVER %I TO %I', x.privilege_type,
                             s.srvname, CAST(:safe_role AS text)) AS safe_grant_sql,
           pg_catalog.format('REVOKE %s ON FOREIGN SERVER %I FROM %I', x.privilege_type,
                             s.srvname, CAST(:safe_role AS text)) AS safe_revoke_sql
    FROM pg_catalog.pg_foreign_server AS s
    CROSS JOIN LATERAL pg_catalog.aclexplode(s.srvacl) AS x
    JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = x.grantor
    WHERE x.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
    ORDER BY s.srvname, x.privilege_type
    """
)

_TABLESPACE_ACL_SQL = sa.text(
    """
    SELECT 'tablespace' AS kind, t.spcname AS object_name,
           ''::text AS schema_name, t.spcname AS object_identity,
           ''::text AS object_subtype, x.privilege_type, x.is_grantable,
           grantor.rolname AS grantor_name,
           pg_catalog.format('GRANT %s ON TABLESPACE %I TO %I', x.privilege_type,
                             t.spcname, CAST(:admin_role AS text)) AS grant_sql,
           pg_catalog.format('REVOKE %s ON TABLESPACE %I FROM %I', x.privilege_type,
                             t.spcname, CAST(:admin_role AS text)) AS revoke_sql,
           pg_catalog.format('GRANT %s ON TABLESPACE %I TO %I', x.privilege_type,
                             t.spcname, CAST(:safe_role AS text)) AS safe_grant_sql,
           pg_catalog.format('REVOKE %s ON TABLESPACE %I FROM %I', x.privilege_type,
                             t.spcname, CAST(:safe_role AS text)) AS safe_revoke_sql
    FROM pg_catalog.pg_tablespace AS t
    CROSS JOIN LATERAL pg_catalog.aclexplode(t.spcacl) AS x
    JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = x.grantor
    WHERE x.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
    ORDER BY t.spcname, x.privilege_type
    """
)

_PARAMETER_ACL_SQL = sa.text(
    """
    SELECT 'parameter' AS kind, p.parname AS object_name,
           ''::text AS schema_name, p.parname AS object_identity,
           ''::text AS object_subtype, x.privilege_type, x.is_grantable,
           grantor.rolname AS grantor_name,
           pg_catalog.format('GRANT %s ON PARAMETER %I TO %I', x.privilege_type,
                             p.parname, CAST(:admin_role AS text)) AS grant_sql,
           pg_catalog.format('REVOKE %s ON PARAMETER %I FROM %I', x.privilege_type,
                             p.parname, CAST(:admin_role AS text)) AS revoke_sql,
           pg_catalog.format('GRANT %s ON PARAMETER %I TO %I', x.privilege_type,
                             p.parname, CAST(:safe_role AS text)) AS safe_grant_sql,
           pg_catalog.format('REVOKE %s ON PARAMETER %I FROM %I', x.privilege_type,
                             p.parname, CAST(:safe_role AS text)) AS safe_revoke_sql
    FROM pg_catalog.pg_parameter_acl AS p
    CROSS JOIN LATERAL pg_catalog.aclexplode(p.paracl) AS x
    JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = x.grantor
    WHERE x.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
    ORDER BY p.parname, x.privilege_type
    """
)

_DEFAULT_ACL_QUERY = sa.text(
    """
    SELECT owner.rolname AS owner_role,
           COALESCE(n.nspname, '') AS schema_name,
           d.defaclobjtype AS object_type,
           grantor.rolname AS grantor_name,
           grantee.rolname AS grantee_name,
           x.privilege_type,
           x.is_grantable
    FROM pg_catalog.pg_default_acl AS d
    LEFT JOIN pg_catalog.pg_namespace AS n ON n.oid = d.defaclnamespace
    LEFT JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) AS x ON true
    JOIN pg_catalog.pg_roles AS owner ON owner.oid = d.defaclrole
    LEFT JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = x.grantor
    LEFT JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = x.grantee
    WHERE d.defaclrole = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
       OR x.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
    ORDER BY owner.rolname, n.nspname, d.defaclobjtype, grantee.rolname, x.privilege_type
    """
)

_OWNED_OBJECT_QUERY = sa.text(
    """
    SELECT EXISTS (
        SELECT FROM pg_catalog.pg_shdepend
        WHERE refclassid = 'pg_catalog.pg_authid'::regclass
          AND refobjid = :admin_oid AND deptype = 'o'
    )
    """
)

_SNAPSHOT_DEFAULT_ACL_QUERY = sa.text(
    """
    SELECT EXISTS (
        SELECT FROM pg_catalog.pg_default_acl d,
                    pg_catalog.aclexplode(d.defaclacl) x
        WHERE d.defaclrole = CURRENT_USER::regrole
          AND d.defaclnamespace IN (0, 'public'::regnamespace)
          AND d.defaclobjtype = 'r'
          AND x.grantee <> d.defaclrole AND x.privilege_type <> 'SELECT'
    )
    """
)


def _lock_acl_catalogs(connection: sa.Connection) -> None:
    """Chặn ghi catalog theo thứ tự cố định rồi lấy snapshot đọc mới sau hàng rào."""
    isolation: str = connection.exec_driver_sql("SHOW transaction_isolation").scalar_one()
    if isolation not in {"read committed", "read uncommitted"}:
        raise RuntimeError("WP06_CATALOG_READ_COMMITTED_REQUIRED")
    connection.exec_driver_sql(
        "LOCK TABLE pg_catalog.pg_attribute, pg_catalog.pg_auth_members, pg_catalog.pg_authid, "
        "pg_catalog.pg_class, pg_catalog.pg_database, pg_catalog.pg_default_acl, "
        "pg_catalog.pg_foreign_data_wrapper, pg_catalog.pg_foreign_server, pg_catalog.pg_language, "
        "pg_catalog.pg_largeobject_metadata, pg_catalog.pg_namespace, pg_catalog.pg_parameter_acl, "
        "pg_catalog.pg_proc, pg_catalog.pg_shdepend, pg_catalog.pg_tablespace, pg_catalog.pg_type "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    # PostgreSQL có thể nhả RowExclusiveLock của GRANT trước COMMIT. XID ghi vẫn giữ
    # ExclusiveLock, kể cả transaction đã PREPARE (pid NULL). Chỉ đợi tập XID hiện tại.
    connection.exec_driver_sql(
        """
        DO $barrier$
        DECLARE
            writers xid[];
            deadline timestamptz := pg_catalog.clock_timestamp() + interval '2 seconds';
        BEGIN
            SELECT array_agg(transactionid) INTO writers
            FROM pg_catalog.pg_locks
            WHERE locktype = 'transactionid' AND mode = 'ExclusiveLock' AND granted
              AND pid IS DISTINCT FROM pg_catalog.pg_backend_pid();
            WHILE EXISTS (
                SELECT FROM pg_catalog.pg_locks
                WHERE locktype = 'transactionid' AND mode = 'ExclusiveLock' AND granted
                  AND transactionid = ANY(writers)
            ) LOOP
                IF pg_catalog.clock_timestamp() >= deadline THEN
                    RAISE EXCEPTION 'WP06_CATALOG_WRITER_IN_PROGRESS';
                END IF;
                PERFORM pg_catalog.pg_sleep(0.05);
            END LOOP;
        END
        $barrier$
        """
    )


def _assert_no_global_snapshot_writer(connection: sa.Connection) -> None:
    has_global_writer: bool = connection.exec_driver_sql(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_roles AS r
            WHERE NOT r.rolsuper
              AND r.oid <> CURRENT_USER::regrole
              AND r.oid <> 'pg_write_all_data'::regrole
              AND pg_catalog.pg_has_role(r.oid, 'pg_write_all_data', 'MEMBER')
        )
        """
    ).scalar_one()
    if has_global_writer:
        raise RuntimeError("WP06_SNAPSHOT_WRITE_ACL_UNEXPECTED")


def _assert_snapshot_write_acl(connection: sa.Connection) -> None:
    _assert_no_global_snapshot_writer(connection)
    private: bool = connection.exec_driver_sql(
        f"""
        SELECT c.relowner = CURRENT_USER::regrole AND NOT EXISTS (
            SELECT FROM (
                SELECT x.grantee, x.privilege_type FROM pg_catalog.aclexplode(c.relacl) x
                UNION ALL
                SELECT x.grantee, x.privilege_type
                FROM pg_catalog.pg_attribute a, pg_catalog.aclexplode(a.attacl) x
                WHERE a.attrelid = c.oid
            ) acl
            WHERE acl.grantee <> c.relowner AND acl.privilege_type <> 'SELECT'
        )
        FROM pg_catalog.pg_class c WHERE c.oid = 'public.{SNAPSHOT_TABLE}'::regclass
        """
    ).scalar_one()
    if not private:
        raise RuntimeError("WP06_SNAPSHOT_WRITE_ACL_UNEXPECTED")


_MEMBERSHIP_QUERY = sa.text(
    """
    SELECT
        r.rolname,
        m.admin_option,
        m.inherit_option,
        m.set_option,
        grantor.rolname AS grantor_name,
        pg_catalog.format(
            'GRANT %I TO %I WITH ADMIN %s, INHERIT %s, SET %s',
            r.rolname, CAST(:admin_role AS text),
            CASE WHEN m.admin_option THEN 'TRUE' ELSE 'FALSE' END,
            CASE WHEN m.inherit_option THEN 'TRUE' ELSE 'FALSE' END,
            CASE WHEN m.set_option THEN 'TRUE' ELSE 'FALSE' END
        ) AS grant_sql
    FROM pg_catalog.pg_auth_members AS m
    JOIN pg_catalog.pg_roles AS r ON r.oid = m.roleid
    JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = m.grantor
    WHERE m.member = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
    ORDER BY r.rolname
    """
)


def _acl_queries() -> tuple[sa.TextClause, ...]:
    return (
        _RELATION_ACL_SQL,
        _COLUMN_ACL_SQL,
        _FUNCTION_ACL_SQL,
        _SCHEMA_ACL_SQL,
        _TYPE_ACL_SQL,
        _DATABASE_ACL_SQL,
        _LARGE_OBJECT_ACL_SQL,
        _LANGUAGE_ACL_SQL,
        _FOREIGN_DATA_WRAPPER_ACL_SQL,
        _FOREIGN_SERVER_ACL_SQL,
        _TABLESPACE_ACL_SQL,
        _PARAMETER_ACL_SQL,
    )


def _assert_expected_roles(connection: sa.Connection) -> None:
    roles = connection.execute(
        sa.text(
            """
            SELECT
                (SELECT rolcanlogin FROM pg_catalog.pg_roles WHERE rolname = :admin_role)
                    AS admin_can_login,
                (SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
                 FROM pg_catalog.pg_roles WHERE rolname = :admin_role) AS admin_unsafe,
                (SELECT rolcanlogin FROM pg_catalog.pg_roles WHERE rolname = :report_reader)
                    AS reader_can_login,
                (SELECT rolsuper OR rolinherit OR rolcreatedb OR rolcreaterole OR rolreplication
                        OR rolbypassrls
                 FROM pg_catalog.pg_roles WHERE rolname = :report_reader) AS reader_unsafe
            """
        ),
        {"admin_role": ADMIN, "report_reader": REPORT_READER},
    ).one()
    if (
        not roles.admin_can_login
        or roles.admin_unsafe
        or not roles.reader_can_login
        or roles.reader_unsafe
    ):
        raise RuntimeError("WP06_ADMIN_ROLE_ATTRIBUTES_UNEXPECTED")

    memberships = tuple(
        dict(row) for row in connection.execute(_MEMBERSHIP_QUERY, {"admin_role": ADMIN}).mappings()
    )
    _assert_expected_membership_options(memberships)
    if memberships[0]["grant_sql"] != MEMBERSHIP_SNAPSHOT["grant_sql"]:
        raise RuntimeError("WP06_ADMIN_ROLE_MEMBERSHIP_OPTIONS_UNEXPECTED")
    if connection.execute(_SNAPSHOT_DEFAULT_ACL_QUERY).scalar_one():
        raise RuntimeError("WP06_SNAPSHOT_DEFAULT_ACL_UNEXPECTED")

    default_acls = connection.execute(_DEFAULT_ACL_QUERY, {"admin_role": ADMIN}).mappings().all()
    if default_acls:
        raise RuntimeError("WP06_ADMIN_DEFAULT_ACL_UNEXPECTED")

    admin_oid: int = connection.execute(
        sa.text("SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :role"),
        {"role": ADMIN},
    ).scalar_one()
    if connection.execute(_OWNED_OBJECT_QUERY, {"admin_oid": admin_oid}).scalar_one():
        raise RuntimeError("WP06_ADMIN_OWNS_OBJECTS_UNEXPECTED")

    foreign_acl: bool = connection.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_shdepend
                WHERE refclassid = 'pg_catalog.pg_authid'::regclass
                  AND refobjid = :admin_oid
                  AND deptype = 'a'
                  AND dbid NOT IN (
                      0,
                      (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database())
                  )
            )
            """
        ),
        {"admin_oid": admin_oid},
    ).scalar_one()
    if foreign_acl:
        raise RuntimeError("WP06_ADMIN_FOREIGN_OBJECT_ACL_UNEXPECTED")

    _assert_no_global_snapshot_writer(connection)

    reader = connection.execute(
        sa.text(
            """
            SELECT count(*)
            FROM pg_catalog.pg_auth_members AS m
            JOIN pg_catalog.pg_roles AS r ON r.oid IN (m.member, m.roleid)
            WHERE r.rolname = :reader_role
            """
        ),
        {"reader_role": REPORT_READER},
    ).scalar_one()
    if reader:
        raise RuntimeError("WP06_ADMIN_REPORT_READER_MEMBERSHIP_UNEXPECTED")


def _assert_expected_membership_options(rows: Iterable[Mapping[str, Any]]) -> None:
    memberships = tuple(rows)
    expected = (
        APP_ROLE,
        False,
        True,
        True,
        "crypidian_admin",
    )
    actual = tuple(
        (
            row["rolname"],
            row["admin_option"],
            row["inherit_option"],
            row["set_option"],
            row.get("grantor_name"),
        )
        for row in memberships
    )
    if actual != (expected,):
        raise RuntimeError("WP06_ADMIN_ROLE_MEMBERSHIP_OPTIONS_UNEXPECTED")


def _admin_acl_manifest_key(row: Mapping[str, Any]) -> str:
    fields = (
        "kind",
        "schema_name",
        "object_identity",
        "object_subtype",
        "privilege_type",
        "grantor_name",
    )
    return "|".join(str(row.get(field) or "") for field in fields)


def _validate_admin_acl_rows(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    observed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        key = _admin_acl_manifest_key(row)
        entry = ADMIN_SAFE_ACL_MANIFEST.get(key)
        if entry is None:
            raise RuntimeError(f"WP06_ADMIN_ACL_UNCLASSIFIED:{key}")
        if key in observed:
            raise RuntimeError(f"WP06_ADMIN_ACL_DUPLICATE:{key}")
        if row.get("is_grantable"):
            raise RuntimeError(f"WP06_ADMIN_GRANT_OPTION_UNEXPECTED:{key}")
        if entry.get("is_grantable") is not False:
            raise RuntimeError(f"WP06_ADMIN_ACL_MANIFEST_MISMATCH:{key}:is_grantable")
        for field in ("grant_sql", "revoke_sql"):
            if row.get(field) != entry.get(field):
                raise RuntimeError(f"WP06_ADMIN_ACL_MANIFEST_MISMATCH:{key}:{field}")
        if entry.get("operation") != row.get("privilege_type"):
            raise RuntimeError(f"WP06_ADMIN_ACL_MANIFEST_MISMATCH:{key}:operation")
        if entry.get("disposition") not in {"transfer", "revoke_only"}:
            raise RuntimeError(f"WP06_ADMIN_ACL_DISPOSITION_UNEXPECTED:{key}")
        if entry.get("disposition") == "revoke_only" and (
            entry.get("safe_grant_sql") is not None or entry.get("safe_revoke_sql") is not None
        ):
            raise RuntimeError(f"WP06_ADMIN_ACL_DENY_HAS_TRANSFER_SQL:{key}")
        if entry.get("disposition") == "transfer" and (
            not row.get("safe_grant_sql") or not row.get("safe_revoke_sql")
        ):
            raise RuntimeError(f"WP06_ADMIN_ACL_TRANSFER_SQL_MISSING:{key}")
        if entry.get("disposition") == "transfer":
            for field in ("safe_grant_sql", "safe_revoke_sql"):
                if row.get(field) != entry.get(field):
                    raise RuntimeError(f"WP06_ADMIN_ACL_MANIFEST_MISMATCH:{key}:{field}")
        observed[key] = row

    missing = ADMIN_SAFE_ACL_MANIFEST.keys() - observed.keys()
    if missing:
        raise RuntimeError(f"WP06_ADMIN_ACL_MANIFEST_MISSING:{sorted(missing)[0]}")
    return tuple(observed.values())


def _assert_admin_acl_manifest(connection: sa.Connection) -> tuple[Mapping[str, Any], ...]:
    parameters = {"admin_role": ADMIN, "safe_role": SAFE_ROLE}
    rows = tuple(
        dict(row)
        for query in _acl_queries()
        for row in connection.execute(query, parameters).mappings().all()
    )
    return _validate_admin_acl_rows(rows)


def _configure_report_reader(connection: sa.Connection) -> None:
    connection.exec_driver_sql(_REPORT_SCOPE_HELPER_SQL)
    connection.exec_driver_sql(_REPORT_WORKSPACE_HELPER_SQL)
    connection.exec_driver_sql(_REPORT_WORKSPACE_READ_HELPER_SQL)
    op.execute(
        "ALTER FUNCTION public.wp06_admin_report_reader_can_access(uuid, uuid) "
        f"OWNER TO {REPORT_SCOPE_ROLE}"
    )
    op.execute(
        "ALTER FUNCTION public.wp06_admin_report_reader_can_read_workspace(uuid) "
        f"OWNER TO {REPORT_SCOPE_ROLE}"
    )
    op.execute(
        "ALTER FUNCTION public.wp06_admin_report_reader_can_read_workspace_page(uuid) "
        f"OWNER TO {REPORT_SCOPE_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_admin_report_reader_can_access(uuid, uuid) FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_admin_report_reader_can_read_workspace(uuid) "
        "FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_admin_report_reader_can_read_workspace_page(uuid) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_admin_report_reader_can_access(uuid, uuid) "
        f"TO {REPORT_READER}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_admin_report_reader_can_read_workspace(uuid) "
        f"TO {REPORT_READER}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_admin_report_reader_can_read_workspace_page(uuid) "
        f"TO {REPORT_READER}"
    )
    op.execute(
        f"GRANT SELECT (workspace_id, id, status) ON public.user_membership TO {REPORT_SCOPE_ROLE}"
    )
    op.execute(
        "GRANT SELECT (workspace_id, membership_id, role) ON public.role_binding "
        f"TO {REPORT_SCOPE_ROLE}"
    )
    op.execute(
        "GRANT SELECT (workspace_id, bot_id, membership_id, permission, revoked_at, expires_at) "
        f"ON public.bot_grant TO {REPORT_SCOPE_ROLE}"
    )
    op.execute(
        "CREATE POLICY wp06_admin_report_scope_membership_read ON public.user_membership "
        f"FOR SELECT TO {REPORT_SCOPE_ROLE} USING (workspace_id = NULLIF("
        "current_setting('app.workspace_id', true), '')::uuid AND id = NULLIF("
        "current_setting('app.membership_id', true), '')::uuid)"
    )
    op.execute(
        "CREATE POLICY wp06_admin_report_scope_role_read ON public.role_binding "
        f"FOR SELECT TO {REPORT_SCOPE_ROLE} USING (workspace_id = NULLIF("
        "current_setting('app.workspace_id', true), '')::uuid AND membership_id = NULLIF("
        "current_setting('app.membership_id', true), '')::uuid)"
    )
    op.execute(
        "CREATE POLICY wp06_admin_report_scope_grant_read ON public.bot_grant "
        f"FOR SELECT TO {REPORT_SCOPE_ROLE} USING (workspace_id = NULLIF("
        "current_setting('app.workspace_id', true), '')::uuid AND membership_id = NULLIF("
        "current_setting('app.membership_id', true), '')::uuid)"
    )

    for table in sorted(REPORT_READER_RELATIONS):
        if table == "outbox_event":
            op.execute(
                f"GRANT SELECT ({_OUTBOX_READER_COLUMNS}) ON public.outbox_event TO {REPORT_READER}"
            )
        elif table == "proposal_backtest":
            op.execute(
                "GRANT SELECT (workspace_id, id, analyst_run_id, proposal_index, bot_id, "
                "bot_name, created_at, source, verdict, proposal, result) "
                f"ON public.proposal_backtest TO {REPORT_READER}"
            )
        elif table == "analyst_run":
            op.execute(
                f"GRANT SELECT ({_ANALYST_RUN_READER_COLUMNS}) "
                f"ON public.analyst_run TO {REPORT_READER}"
            )
        elif table == "run_report":
            op.execute(
                f"GRANT SELECT ({_RUN_REPORT_READER_COLUMNS}) "
                f"ON public.run_report TO {REPORT_READER}"
            )
        else:
            op.execute(f"GRANT SELECT ON public.{table} TO {REPORT_READER}")
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        if table == "bot_instance":
            predicate = "public.wp06_admin_report_reader_can_access(workspace_id, id)"
        elif table == "shared_signal_evidence":
            predicate = (
                "EXISTS (SELECT 1 FROM public.bot_decision AS d "
                "WHERE d.workspace_id = shared_signal_evidence.workspace_id "
                "AND d.evidence_id = shared_signal_evidence.id "
                "AND public.wp06_admin_report_reader_can_access(d.workspace_id, d.bot_id))"
            )
        elif table in _REPORT_WORKSPACE_RELATIONS:
            predicate = "public.wp06_admin_report_reader_can_read_workspace(workspace_id)"
        elif table in _REPORT_WORKSPACE_READ_RELATIONS:
            predicate = "public.wp06_admin_report_reader_can_read_workspace_page(workspace_id)"
        elif table == "outbox_event":
            predicate = (
                "(event_type IN ('signal.evidence.recorded.v1', 'research.rights_changed.v1') "
                "AND public.wp06_admin_report_reader_can_read_workspace(workspace_id)) OR "
                "(event_type = 'paper.order_intent.expired' AND EXISTS ("
                "SELECT 1 FROM public.order_intent AS i "
                "WHERE i.workspace_id = outbox_event.workspace_id "
                "AND i.id = outbox_event.aggregate_id "
                "AND public.wp06_admin_report_reader_can_access(i.workspace_id, i.bot_id)))"
            )
        else:
            predicate = "public.wp06_admin_report_reader_can_access(workspace_id, bot_id)"
        op.execute(
            f"CREATE POLICY wp06_admin_report_reader_{table}_select ON public.{table} "
            f"FOR SELECT TO {REPORT_READER} USING ({predicate})"
        )


def _snapshot_and_move_direct_grants(
    connection: sa.Connection, rows: Iterable[Mapping[str, Any]]
) -> None:
    insert = sa.text(
        f"INSERT INTO public.{SNAPSHOT_TABLE} ({', '.join(_SNAPSHOT_FIELDS)}) "
        f"VALUES ({', '.join(':' + field for field in _SNAPSHOT_FIELDS)})"
    )
    for row in rows:
        entry = ADMIN_SAFE_ACL_MANIFEST[_admin_acl_manifest_key(row)]
        connection.execute(insert, entry)
        if entry["safe_grant_sql"] is not None:
            connection.exec_driver_sql(entry["safe_grant_sql"])
        connection.exec_driver_sql(entry["revoke_sql"])
    connection.execute(insert, MEMBERSHIP_SNAPSHOT)


def _assert_denied_raw_admin_reads(connection: sa.Connection) -> None:
    result: bool = connection.execute(
        sa.text(
            """
            SELECT
                pg_catalog.pg_has_role(:admin_role, :app_role, 'USAGE')
                OR has_table_privilege(:admin_role, 'public.trade', 'SELECT')
                OR has_column_privilege(:admin_role, 'public.trade', 'net_pnl', 'SELECT')
                OR has_table_privilege(:admin_role, 'public.order_intent', 'SELECT')
                OR has_table_privilege(
                    :admin_role, 'public.paper_execution_checkpoint', 'SELECT'
                )
                OR has_table_privilege(:admin_role, 'public.bot_decision', 'SELECT')
                OR has_column_privilege(:admin_role, 'public.bot_decision', 'payload', 'SELECT')
                OR has_table_privilege(:admin_role, 'public.shared_signal_evidence', 'SELECT')
                OR has_table_privilege(
                    :admin_role, 'public.telegram_signal_delivery', 'SELECT'
                )
                OR has_table_privilege(
                    :admin_role, 'public.telegram_signal_delivery', 'INSERT'
                )
                OR has_table_privilege(
                    :admin_role, 'public.telegram_signal_delivery', 'UPDATE'
                )
                OR has_table_privilege(
                    :admin_role, 'public.telegram_signal_delivery', 'DELETE'
                )
                OR has_table_privilege(:admin_role, 'public.run_report', 'SELECT')
                OR has_column_privilege(
                    :admin_role, 'public.learning_score_event', 'class_probabilities', 'SELECT'
                )
            """
        ),
        {"admin_role": ADMIN, "app_role": APP_ROLE},
    ).scalar_one()
    if result:
        raise RuntimeError("WP06_ADMIN_RAW_LEDGER_PRIVILEGE_REMAINS")


def upgrade() -> None:
    connection = op.get_bind()
    _lock_acl_catalogs(connection)
    _assert_expected_roles(connection)
    rows = _assert_admin_acl_manifest(connection)
    op.execute(_upgrade_statements()[0])
    op.execute(_upgrade_statements()[1])
    op.execute(_upgrade_statements()[2])
    op.execute(_upgrade_statements()[5])

    _assert_snapshot_write_acl(connection)
    _snapshot_and_move_direct_grants(connection, rows)
    op.execute(f"GRANT {SAFE_ROLE} TO {ADMIN} WITH INHERIT TRUE, SET FALSE, ADMIN FALSE")
    op.execute(f"REVOKE {APP_ROLE} FROM {ADMIN}")
    op.execute(
        f"ALTER ROLE {REPORT_READER} NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
    )
    _configure_report_reader(connection)
    op.execute(
        f"GRANT SELECT ({_MANUAL_REPORT_READER_ACCOUNT_COLUMNS}) "
        f"ON public.trading_account TO {MANUAL_REPORT_READER}"
    )
    op.execute(_MANUAL_REPORT_READER_ACCOUNT_POLICY_SQL)

    _assert_denied_raw_admin_reads(connection)
    _assert_snapshot_write_acl(connection)
    _assert_snapshot_manifest(connection)


def _validate_snapshot_rows(rows: Iterable[Mapping[str, Any]]) -> None:
    def contents(entries: Iterable[Mapping[str, Any]]) -> Counter[tuple[Any, ...]]:
        return Counter(tuple(row[field] for field in _SNAPSHOT_FIELDS) for row in entries)

    if contents(rows) != contents(_MANIFEST_ROWS):
        raise RuntimeError("WP06_SNAPSHOT_MANIFEST_UNEXPECTED")


def _assert_snapshot_manifest(connection: sa.Connection) -> None:
    rows = connection.exec_driver_sql(
        f"SELECT {', '.join(_SNAPSHOT_FIELDS)} FROM public.{SNAPSHOT_TABLE}"
    ).mappings()
    _validate_snapshot_rows(dict(row) for row in rows)


def downgrade() -> None:
    connection = op.get_bind()
    _lock_acl_catalogs(connection)
    connection.exec_driver_sql(f"LOCK TABLE public.{SNAPSHOT_TABLE} IN ACCESS EXCLUSIVE MODE")
    _assert_snapshot_write_acl(connection)
    _assert_snapshot_manifest(connection)
    op.execute("DROP POLICY wp06_manual_report_reader_account_select ON public.trading_account")
    op.execute(
        f"REVOKE SELECT ({_MANUAL_REPORT_READER_ACCOUNT_COLUMNS}) "
        f"ON public.trading_account FROM {MANUAL_REPORT_READER}"
    )
    op.execute("DROP POLICY wp06_admin_report_scope_grant_read ON public.bot_grant")
    op.execute("DROP POLICY wp06_admin_report_scope_role_read ON public.role_binding")
    op.execute("DROP POLICY wp06_admin_report_scope_membership_read ON public.user_membership")
    for table in sorted(REPORT_READER_RELATIONS, reverse=True):
        op.execute(f"DROP POLICY wp06_admin_report_reader_{table}_select ON public.{table}")
        if table == "outbox_event":
            op.execute(
                f"REVOKE SELECT ({_OUTBOX_READER_COLUMNS}) ON public.outbox_event "
                f"FROM {REPORT_READER}"
            )
        elif table == "proposal_backtest":
            op.execute(
                "REVOKE SELECT (workspace_id, id, analyst_run_id, proposal_index, bot_id, "
                "bot_name, created_at, source, verdict, proposal, result) "
                f"ON public.proposal_backtest FROM {REPORT_READER}"
            )
        elif table == "analyst_run":
            op.execute(
                f"REVOKE SELECT ({_ANALYST_RUN_READER_COLUMNS}) "
                f"ON public.analyst_run FROM {REPORT_READER}"
            )
        elif table == "run_report":
            op.execute(
                f"REVOKE SELECT ({_RUN_REPORT_READER_COLUMNS}) "
                f"ON public.run_report FROM {REPORT_READER}"
            )
        else:
            op.execute(f"REVOKE SELECT ON public.{table} FROM {REPORT_READER}")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_admin_report_reader_can_access(uuid, uuid) "
        f"FROM {REPORT_READER}"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_admin_report_reader_can_read_workspace(uuid) "
        f"FROM {REPORT_READER}"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_admin_report_reader_can_read_workspace_page(uuid) "
        f"FROM {REPORT_READER}"
    )
    op.execute("DROP FUNCTION public.wp06_admin_report_reader_can_access(uuid, uuid)")
    op.execute("DROP FUNCTION public.wp06_admin_report_reader_can_read_workspace(uuid)")
    op.execute("DROP FUNCTION public.wp06_admin_report_reader_can_read_workspace_page(uuid)")
    op.execute(
        "REVOKE SELECT (workspace_id, id, status) ON public.user_membership "
        f"FROM {REPORT_SCOPE_ROLE}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id, membership_id, role) ON public.role_binding "
        f"FROM {REPORT_SCOPE_ROLE}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id, bot_id, membership_id, permission, revoked_at, expires_at) "
        f"ON public.bot_grant FROM {REPORT_SCOPE_ROLE}"
    )
    for entry in reversed(_MANIFEST_ROWS):
        if entry["safe_revoke_sql"] is not None:
            connection.exec_driver_sql(entry["safe_revoke_sql"])

    op.execute(f"REVOKE {SAFE_ROLE} FROM {ADMIN}")
    for entry in _MANIFEST_ROWS:
        connection.exec_driver_sql(entry["grant_sql"])
    _assert_admin_acl_manifest(connection)

    op.execute(f"DROP ROLE {SAFE_ROLE}")
    op.execute(f"DROP ROLE {TELEMETRY_READER}")
    op.execute(f"DROP ROLE {REPORT_SCOPE_ROLE}")
    op.drop_table(SNAPSHOT_TABLE, schema="public")

"""QA-only probe cho ACL và fail-closed của projection risk admin."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg


def _connect(username: str, password_env: str) -> psycopg.Connection:
    password = Path(os.environ[password_env]).read_text(encoding="utf-8").strip()
    return psycopg.connect(
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        user=username,
        password=password,
        autocommit=True,
    )


def main() -> None:
    admin = _connect("crypidian_admin_web", "WP06_ADMIN_PASSWORD_FILE")
    fixture = _connect("crypidian_admin", "WP06_DATABASE_ADMIN_PASSWORD_FILE")
    workspace_id = uuid4()
    membership_id = uuid4()
    user_id = uuid4()
    binding_id = uuid4()

    with fixture.cursor() as cursor:
        cursor.execute(
            "INSERT INTO public.workspace (id, name, slug, mode, status) "
            "VALUES (%s, 'Risk projection QA', %s, 'PAPER', 'ACTIVE')",
            (workspace_id, f"risk-projection-{workspace_id.hex}"),
        )
        cursor.execute(
            "INSERT INTO public.user_account (id, email_normalized, password_hash, status) "
            "VALUES (%s, %s, 'qa-only-not-a-login', 'ACTIVE')",
            (user_id, f"risk-projection-{user_id.hex}@qa.invalid"),
        )
        cursor.execute(
            "INSERT INTO public.user_membership (id, workspace_id, user_id, status) "
            "VALUES (%s, %s, %s, 'ACTIVE')",
            (membership_id, workspace_id, user_id),
        )
        cursor.execute(
            "INSERT INTO public.role_binding (id, workspace_id, membership_id, role) "
            "VALUES (%s, %s, %s, 'owner')",
            (binding_id, workspace_id, membership_id),
        )

    with admin.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.workspace_id', %s, false), "
            "set_config('app.membership_id', %s, false)",
            (str(workspace_id), str(membership_id)),
        )
        acl = cursor.execute(
            """
            SELECT
                pg_catalog.has_function_privilege(
                    'crypidian_admin_web',
                    'public.wp06_admin_risk_capacity_snapshot(uuid,uuid,timestamptz,integer,text,text[])',
                    'EXECUTE'),
                NOT pg_catalog.has_function_privilege(
                    'crypidian_admin_web',
                    'public.wp06_admin_risk_projection_assert_scope(uuid,uuid)', 'EXECUTE'),
                NOT pg_catalog.has_table_privilege(
                    'crypidian_admin_web', 'public.trade', 'SELECT'),
                NOT pg_catalog.has_column_privilege(
                    'crypidian_admin_web', 'public.trade', 'net_pnl', 'SELECT'),
                NOT pg_catalog.has_table_privilege(
                    'crypidian_admin_web', 'public.order_intent', 'SELECT'),
                NOT pg_catalog.has_column_privilege(
                    'crypidian_admin_web', 'public.risk_reservation', 'reserved_risk', 'SELECT'),
                pg_catalog.pg_get_userbyid(capacity.proowner),
                capacity.prosecdef,
                NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_roles AS role
                    WHERE role.rolname = 'crypidian_risk_aggregator'
                      AND (role.rolcanlogin OR role.rolsuper OR role.rolbypassrls)
                )
            FROM pg_catalog.pg_proc AS capacity
            WHERE capacity.oid =
                'public.wp06_admin_risk_capacity_snapshot('
                'uuid,uuid,timestamptz,integer,text,text[])'::regprocedure
            """
        ).fetchone()
        capacity_rows = cursor.execute(
            "SELECT * FROM public.wp06_admin_risk_capacity_snapshot(%s,%s,%s,300,%s,%s)",
            (workspace_id, membership_id, "2026-11-25T00:00:00Z", "BINANCE_USDM", ["5m"]),
        )
        capacity_fields = tuple(column.name for column in capacity_rows.description)
        capacity_data = capacity_rows.fetchall()
        paper = cursor.execute(
            "SELECT * FROM public.wp06_admin_paper_policy_snapshot(%s,%s)",
            (workspace_id, membership_id),
        ).fetchone()
        try:
            cursor.execute(
                "SELECT * FROM public.wp06_admin_risk_capacity_snapshot(%s,%s,%s,300,%s,%s)",
                (uuid4(), membership_id, "2026-11-25T00:00:00Z", "BINANCE_USDM", ["5m"]),
            )
            wrong_workspace_denied = False
        except psycopg.Error as error:
            wrong_workspace_denied = "WP06_ADMIN_RISK_PROJECTION_SCOPE_DENIED" in str(error)

    with fixture.cursor() as cursor:
        cursor.execute(
            "UPDATE public.role_binding SET role = 'operator' WHERE workspace_id = %s AND id = %s",
            (workspace_id, binding_id),
        )
    try:
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM public.wp06_admin_paper_policy_snapshot(%s,%s)",
                (workspace_id, membership_id),
            )
        operator_denied = False
    except psycopg.Error as error:
        operator_denied = "WP06_ADMIN_RISK_PROJECTION_PERMISSION_DENIED" in str(error)

    with fixture.cursor() as cursor:
        cursor.execute(
            "UPDATE public.role_binding SET role = 'owner' WHERE workspace_id = %s AND id = %s",
            (workspace_id, binding_id),
        )
        cursor.execute(
            "UPDATE public.user_membership SET status = 'SUSPENDED' "
            "WHERE workspace_id = %s AND id = %s",
            (workspace_id, membership_id),
        )
    try:
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM public.wp06_admin_risk_capacity_snapshot(%s,%s,%s,300,%s,%s)",
                (workspace_id, membership_id, "2026-11-25T00:00:00Z", "BINANCE_USDM", ["5m"]),
            )
        inactive_membership_denied = False
    except psycopg.Error as error:
        inactive_membership_denied = "WP06_ADMIN_RISK_PROJECTION_PERMISSION_DENIED" in str(error)

    admin.close()
    fixture.close()
    outcome = {
        "admin_can_execute_capacity_projection": bool(acl[0]),
        "admin_cannot_execute_scope_helper": bool(acl[1]),
        "admin_cannot_select_trade": bool(acl[2]),
        "admin_cannot_select_trade_pnl": bool(acl[3]),
        "admin_cannot_select_order_intent": bool(acl[4]),
        "admin_cannot_select_reservation_risk": bool(acl[5]),
        "capacity_owner_is_risk_aggregator": acl[6] == "crypidian_risk_aggregator",
        "capacity_is_security_definer": bool(acl[7]),
        "risk_aggregator_is_nonlogin_safe": bool(acl[8]),
        "capacity_returns_only_aggregate_fields": capacity_fields
        == (
            "bot_id",
            "account_id",
            "is_enabled",
            "equity_usdt",
            "has_unknown_equity",
            "open_risk_usdt",
            "pending_risk_usdt",
            "has_unknown_exposure",
        ),
        "empty_workspace_returns_no_account_rows": capacity_data == [],
        "paper_policy_counts_are_zero": paper == (0, 0, 0),
        "workspace_mismatch_denied": wrong_workspace_denied,
        "non_workspace_role_denied": operator_denied,
        "inactive_membership_denied": inactive_membership_denied,
    }
    print(json.dumps(outcome, sort_keys=True))


if __name__ == "__main__":
    main()

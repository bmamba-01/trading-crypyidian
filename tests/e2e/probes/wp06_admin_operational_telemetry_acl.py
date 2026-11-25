"""QA-only PostgreSQL probe cho ACL và fail-closed của telemetry admin."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError


def _engine(username: str, secret_file: str) -> Engine:
    password = Path(os.environ[secret_file]).read_text(encoding="utf-8").strip()
    return create_engine(
        URL.create(
            "postgresql+psycopg",
            username=username,
            password=password,
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )


def main() -> None:
    engine = _engine("crypidian_admin_web", "WP06_ADMIN_PASSWORD_FILE")
    fixture_engine = _engine("crypidian_admin", "WP06_DATABASE_ADMIN_PASSWORD_FILE")
    workspace_id = uuid4()
    membership_id = uuid4()
    user_id = uuid4()
    binding_id = uuid4()

    with fixture_engine.begin() as seed:
        seed.execute(
            text(
                "INSERT INTO public.workspace (id, name, slug, mode, status) "
                "VALUES (:id, 'Telemetry QA', :slug, 'PAPER', 'ACTIVE')"
            ),
            {"id": workspace_id, "slug": f"telemetry-{workspace_id.hex}"},
        )
        seed.execute(
            text(
                "INSERT INTO public.user_account (id, email_normalized, password_hash, status) "
                "VALUES (:id, :email, 'qa-only-not-a-login', 'ACTIVE')"
            ),
            {"id": user_id, "email": f"telemetry-{user_id.hex}@qa.invalid"},
        )
        seed.execute(
            text(
                "INSERT INTO public.user_membership (id, workspace_id, user_id, status) "
                "VALUES (:id, :workspace_id, :user_id, 'ACTIVE')"
            ),
            {"id": membership_id, "workspace_id": workspace_id, "user_id": user_id},
        )
        seed.execute(
            text(
                "INSERT INTO public.role_binding (id, workspace_id, membership_id, role) "
                "VALUES (:id, :workspace_id, :membership_id, 'owner')"
            ),
            {"id": binding_id, "workspace_id": workspace_id, "membership_id": membership_id},
        )

    as_of = datetime.now(UTC)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        acl = connection.execute(
            text(
                """
                SELECT
                    (SELECT NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls AND NOT rolinherit
                       FROM pg_catalog.pg_roles WHERE rolname = 'crypidian_admin_telemetry_reader'),
                    NOT EXISTS (
                        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                         JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
                         JOIN pg_catalog.pg_roles AS granted ON granted.oid = membership.roleid
                        WHERE member.rolname = 'crypidian_admin_telemetry_reader'),
                    pg_catalog.pg_get_userbyid(proc_row.proowner),
                    pg_catalog.has_function_privilege(
                        'crypidian_admin_web',
                        'public.wp06_admin_operational_telemetry_snapshot(uuid,uuid,uuid,timestamptz,integer,text)',
                        'EXECUTE'),
                    NOT EXISTS (
                        SELECT 1 FROM pg_catalog.pg_proc AS routine
                         CROSS JOIN LATERAL pg_catalog.aclexplode(
                             COALESCE(routine.proacl,
                                      pg_catalog.acldefault('f', routine.proowner))) AS acl_entry
                        WHERE routine.oid = proc_row.oid AND acl_entry.grantee = 0
                          AND acl_entry.privilege_type = 'EXECUTE'),
                    NOT pg_catalog.has_column_privilege(
                        'crypidian_admin_web', 'public.trade', 'net_pnl', 'SELECT'),
                    NOT pg_catalog.has_table_privilege(
                        'crypidian_admin_web', 'public.order_intent', 'SELECT'),
                    pg_catalog.has_column_privilege(
                        'crypidian_admin_telemetry_reader', 'public.trade', 'net_pnl', 'SELECT'),
                    NOT pg_catalog.has_column_privilege(
                        'crypidian_admin_telemetry_reader', 'public.trade', 'gross_pnl', 'SELECT')
                FROM pg_catalog.pg_proc AS proc_row
                WHERE proc_row.oid =
                    'public.wp06_admin_operational_telemetry_snapshot('
                    'uuid,uuid,uuid,timestamptz,integer,text)'::regprocedure
                """
            )
        ).one()

        connection.execute(
            text("SELECT pg_catalog.set_config('app.workspace_id', :value, false)"),
            {"value": str(workspace_id)},
        )
        connection.execute(
            text("SELECT pg_catalog.set_config('app.membership_id', :value, false)"),
            {"value": str(membership_id)},
        )
        parameters = {
            "workspace_id": str(workspace_id),
            "membership_id": str(membership_id),
            "as_of": as_of,
        }
        query = text(
            """
            SELECT * FROM public.wp06_admin_operational_telemetry_snapshot(
                CAST(:workspace_id AS uuid), CAST(:membership_id AS uuid), NULL,
                CAST(:as_of AS timestamptz), 3, :operation)
            """
        )
        active_workspace_allowed = False
        aggregate_fields_only = False
        try:
            row = (
                connection.execute(query, parameters | {"operation": "health_read"})
                .mappings()
                .one()
            )
            active_workspace_allowed = True
            aggregate_fields_only = (
                set(row)
                == {
                    "feed_age_seconds",
                    "daily_loss",
                    "drawdown_pct",
                    "consecutive_api_errors",
                    "outbox_lag_seconds",
                    "clock_skew_ms",
                    "unprotected_exposure_count",
                    "reconciliation_discrepancy_count",
                    "unknown_order_count",
                    "daily_window_start",
                    "daily_window_end",
                    "rolling_window_start",
                    "realized_loss_today",
                    "realized_loss_rolling",
                    "gross_loss_today",
                    "gross_loss_rolling",
                    "trade_count_today",
                    "trade_count_rolling",
                    "timezone_name",
                }
                and row["daily_loss"] == 0
                and row["consecutive_api_errors"] == 3
                and row["realized_loss_today"] == 0
                and row["trade_count_today"] == 0
                and row["unprotected_exposure_count"] == 0
                and row["unknown_order_count"] == 0
            )
        except DBAPIError as error:
            raise AssertionError(
                f"Workspace QA có membership ACTIVE phải nhận projection: {error.orig}"
            ) from error

        resume_query = text(
            """
            SELECT count(*) = 1
              FROM public.wp06_admin_operational_telemetry_snapshot(
                CAST(:workspace_id AS uuid), CAST(:membership_id AS uuid), NULL,
                CAST(:as_of AS timestamptz), 3, 'breaker_resume')
            """
        )
        resume_scope_allowed = bool(
            connection.execute(resume_query, parameters).scalar_one_or_none()
        )

    with fixture_engine.begin() as seed:
        seed.execute(
            text("UPDATE public.user_membership SET status = 'SUSPENDED' WHERE id = :id"),
            {"id": membership_id},
        )

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(
            text("SELECT pg_catalog.set_config('app.workspace_id', :value, false)"),
            {"value": str(workspace_id)},
        )
        connection.execute(
            text("SELECT pg_catalog.set_config('app.membership_id', :value, false)"),
            {"value": str(membership_id)},
        )
        suspended_membership_denied = False
        try:
            connection.execute(query, parameters | {"operation": "health_read"}).all()
        except DBAPIError as error:
            suspended_membership_denied = "WP06_ADMIN_TELEMETRY_PERMISSION_DENIED" in str(
                error.orig
            )

    engine.dispose()
    fixture_engine.dispose()
    observations = {
        "telemetry_owner_isolated_no_login_role": acl[0],
        "telemetry_owner_has_no_role_memberships": acl[1],
        "telemetry_function_owned_by_projection_role": (
            acl[2] == "crypidian_admin_telemetry_reader"
        ),
        "admin_safe_can_execute_projection": acl[3],
        "public_cannot_execute_projection": acl[4],
        "admin_cannot_read_raw_trade_or_order": acl[5] and acl[6],
        "projection_role_reads_only_required_trade_columns": acl[7] and acl[8],
        "active_workspace_returns_aggregate_only_snapshot": (
            active_workspace_allowed and aggregate_fields_only
        ),
        "resume_projection_allows_current_owner_in_same_scope": resume_scope_allowed,
        "suspended_membership_is_denied": suspended_membership_denied,
    }
    assert all(observations.values()), observations
    print(json.dumps(observations, sort_keys=True))


if __name__ == "__main__":
    main()

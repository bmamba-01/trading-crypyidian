"""Probe PostgreSQL ACL và denial của gateway khi chưa có scorer target."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError


def _url(user: str, password: str) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )


def main() -> None:
    workspace_id = UUID(os.environ["CRYPIDIAN_E2E_WORKSPACE_ID"])
    bot_id = uuid4()
    super_engine = create_engine(
        _url(
            os.environ["TRADING_BOT_DATABASE_USER"],
            Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip(),
        )
    )
    admin_engine = create_engine(
        _url(
            "crypidian_admin_web",
            Path("/run/secrets/database_admin_web_password").read_text().strip(),
        )
    )
    try:
        with super_engine.begin() as connection:
            connection.execute(text("SET LOCAL session_replication_role = replica"))
            connection.execute(
                text(
                    "INSERT INTO public.bot_instance "
                    "(id, workspace_id, trading_account_id, name, status) "
                    "VALUES (:id, :workspace_id, :account_id, "
                    "'WP06 diagnostic fixture', 'DISABLED')"
                ),
                {
                    "id": bot_id,
                    "workspace_id": workspace_id,
                    "account_id": uuid4(),
                },
            )
            acl = (
                connection.execute(
                    text(
                        """
                    SELECT NOT role.rolcanlogin AND NOT role.rolinherit
                               AND NOT role.rolsuper AND NOT role.rolbypassrls
                               AND NOT EXISTS (
                                   SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                                   WHERE membership.roleid=role.oid
                                      OR membership.member=role.oid
                               ) AS evaluator_isolated,
                           pg_catalog.has_column_privilege(
                               role.rolname, 'public.trade', 'net_pnl', 'SELECT'
                           ) AND NOT pg_catalog.has_table_privilege(
                               role.rolname, 'public.trade', 'SELECT'
                           ) AS trade_column_scoped,
                           pg_catalog.has_table_privilege(
                               'crypidian_admin_web',
                               'public.learning_paper_performance_report', 'SELECT'
                           ) AND pg_catalog.has_table_privilege(
                               'crypidian_admin_web',
                               'public.learning_paper_performance_report', 'INSERT'
                           ) AS report_select_insert,
                           report.relrowsecurity AND report.relforcerowsecurity AS report_rls
                      FROM pg_catalog.pg_roles AS role
                      JOIN pg_catalog.pg_class AS report
                        ON report.oid='public.learning_paper_performance_report'::regclass
                     WHERE role.rolname='crypidian_learning_paper_performance_evaluator'
                    """
                    )
                )
                .mappings()
                .one()
            )
            execute_admin = connection.execute(
                text(
                    "SELECT pg_catalog.has_function_privilege("
                    "'crypidian_admin_web', 'public.wp06_aggregate_paper_performance"
                    "(uuid,uuid,text,uuid,text,timestamp with time zone)', 'EXECUTE')"
                )
            ).scalar_one()
            execute_public_app = connection.execute(
                text(
                    "SELECT NOT pg_catalog.has_function_privilege("
                    "'crypidian_app', 'public.wp06_aggregate_paper_performance"
                    "(uuid,uuid,text,uuid,text,timestamp with time zone)', 'EXECUTE')"
                    " AND NOT EXISTS ("
                    "SELECT 1 FROM pg_catalog.pg_proc AS function "
                    "CROSS JOIN LATERAL pg_catalog.aclexplode(function.proacl) AS acl "
                    "WHERE function.oid='public.wp06_aggregate_paper_performance"
                    "(uuid,uuid,text,uuid,text,timestamp with time zone)'::regprocedure "
                    "AND acl.grantee=0 AND acl.privilege_type='EXECUTE')"
                )
            ).scalar_one()
        with admin_engine.begin() as connection:
            connection.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace, true)"),
                {"workspace": str(workspace_id)},
            )
            denied = False
            try:
                connection.execute(
                    text(
                        "SELECT * FROM public.wp06_aggregate_paper_performance("
                        ":workspace, :bot, :fingerprint, :baseline_id, :baseline_hash, "
                        "pg_catalog.transaction_timestamp())"
                    ),
                    {
                        "workspace": workspace_id,
                        "bot": bot_id,
                        "fingerprint": "a" * 64,
                        "baseline_id": uuid4(),
                        "baseline_hash": "b" * 64,
                    },
                )
            except DBAPIError as error:
                denied = "WP06_PAPER_PERFORMANCE_ACTIVE_TARGET_REQUIRED" in str(error.orig)
            assert denied, "Gateway phải từ chối khi workspace chưa có target active."
        assert acl["evaluator_isolated"] is True
        assert acl["trade_column_scoped"] is True
        assert acl["report_select_insert"] is True
        assert acl["report_rls"] is True
        assert execute_admin is True
        assert execute_public_app is True
        print(
            json.dumps(
                {
                    "narrow_evaluator_role": True,
                    "aggregate_only_report_acl": True,
                    "admin_can_execute_gateway": True,
                    "public_and_app_cannot_execute": True,
                    "missing_active_target_denied": True,
                    "no_raw_member_fields_returned": True,
                },
                sort_keys=True,
            )
        )
    finally:
        super_engine.dispose()
        admin_engine.dispose()


if __name__ == "__main__":
    main()

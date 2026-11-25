"""Kiểm ACL và denial fail-closed của aggregate PAPER latency trên PostgreSQL."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

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
    password_path = Path(os.environ["WP06_ADMIN_PASSWORD_FILE"])
    admin_password = password_path.read_text(encoding="utf-8").strip()
    engine = create_engine(_url("crypidian_admin_web", admin_password))
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        access = connection.execute(
            text(
                """
                SELECT
                    pg_catalog.has_function_privilege(
                        'crypidian_admin_web',
                        'public.wp06_aggregate_scorer_paper_action_latency(uuid,uuid,uuid,text,text,text,text,text)',
                        'EXECUTE'),
                    (SELECT NOT EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_proc AS proc_row
                          CROSS JOIN LATERAL pg_catalog.aclexplode(
                              COALESCE(
                                  proc_row.proacl,
                                  pg_catalog.acldefault('f', proc_row.proowner))
                          ) AS acl_entry
                         WHERE proc_row.oid =
                              'public.wp06_aggregate_scorer_paper_action_latency('
                              'uuid,uuid,uuid,text,text,text,text,text)'::regprocedure
                           AND acl_entry.grantee = 0
                           AND acl_entry.privilege_type = 'EXECUTE')),
                    pg_catalog.has_column_privilege(
                        'crypidian_learning_paper_performance_evaluator',
                        'public.shared_signal_evidence', 'available_at', 'SELECT'),
                    NOT pg_catalog.has_column_privilege(
                        'crypidian_learning_paper_performance_evaluator',
                        'public.shared_signal_evidence', 'canonical_json', 'SELECT'),
                    NOT pg_catalog.has_column_privilege(
                        'crypidian_scorer_history', 'public.order_intent', 'id', 'SELECT')
                    AND NOT pg_catalog.has_column_privilege(
                        'crypidian_scorer_history', 'public.bot_decision', 'evidence_id', 'SELECT')
                    AND NOT pg_catalog.has_column_privilege(
                        'crypidian_scorer_history', 'public.shared_signal_evidence',
                        'id', 'SELECT'),
                    (SELECT NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
                            AND NOT rolinherit
                       FROM pg_catalog.pg_roles
                      WHERE rolname = 'crypidian_learning_paper_performance_evaluator'),
                    (SELECT COALESCE(proc_row.proacl::text, '<default>')
                       FROM pg_catalog.pg_proc AS proc_row
                      WHERE proc_row.oid =
                              'public.wp06_aggregate_scorer_paper_action_latency('
                              'uuid,uuid,uuid,text,text,text,text,text)'::regprocedure)
                """
            )
        ).one()
        workspace_id = uuid4()
        connection.execute(
            text("SELECT pg_catalog.set_config('app.workspace_id', :workspace_id, false)"),
            {"workspace_id": str(workspace_id)},
        )
        missing_bot_denied = False
        try:
            connection.execute(
                text(
                    """
                    SELECT * FROM public.wp06_aggregate_scorer_paper_action_latency(
                        CAST(:workspace_id AS uuid), CAST(:bot_id AS uuid),
                        CAST(:report_id AS uuid), :artifact_hash, :report_hash,
                        :base_hash, :policy_hash, '7d')
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "bot_id": str(uuid4()),
                    "report_id": str(uuid4()),
                    "artifact_hash": "a" * 64,
                    "report_hash": "b" * 64,
                    "base_hash": "c" * 64,
                    "policy_hash": "d" * 64,
                },
            ).all()
        except DBAPIError as error:
            missing_bot_denied = "WP06_SCORER_ACTION_LATENCY_BOT_NOT_FOUND" in str(error.orig)

    engine.dispose()
    observations = {
        "admin_executes_aggregate_only_gateway": access[0],
        "public_cannot_execute_gateway": access[1],
        "evaluator_evidence_columns_allowlisted": access[2] and access[3],
        "history_role_cannot_read_raw_latency_sources": access[4],
        "evaluator_role_isolated": access[5],
        "function_acl": access[6],
        "aggregate_denies_missing_bot_without_rows": missing_bot_denied,
    }
    assert all(observations.values()), observations
    print(json.dumps(observations, sort_keys=True))


if __name__ == "__main__":
    main()

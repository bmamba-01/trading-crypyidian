"""Probe PostgreSQL để kiểm quyền tối thiểu của login admin-web."""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg


def main() -> None:
    password_file = Path(os.environ["WP06_ADMIN_PASSWORD_FILE"])
    connection = psycopg.connect(
        host="postgres",
        dbname="crypidian",
        user="crypidian_admin_web",
        password=password_file.read_text(encoding="utf-8").strip(),
    )
    try:
        row = connection.execute(
            """
            SELECT
                pg_has_role('crypidian_admin_web', 'crypidian_app', 'USAGE'),
                has_column_privilege(
                    'crypidian_admin_web', 'public.trade', 'net_pnl', 'SELECT'
                ),
                has_column_privilege(
                    'crypidian_admin_web', 'public.learning_score_event',
                    'class_probabilities', 'SELECT'
                ),
                has_table_privilege(
                    'crypidian_admin_web', 'public.user_account', 'SELECT'
                ),
                has_table_privilege(
                    'crypidian_admin_web', 'public.bot_config_version', 'SELECT'
                ),
                has_table_privilege(
                    'crypidian_admin_web', 'public.paper_execution_checkpoint', 'SELECT'
                )
            """
        ).fetchone()
        outcome = {
            "inherits_application_role": bool(row[0]),
            "can_read_trade_net_pnl": bool(row[1]),
            "can_read_score_probabilities": bool(row[2]),
            "can_read_auth_account": bool(row[3]),
            "can_read_bot_config": bool(row[4]),
            "can_read_paper_checkpoint": bool(row[5]),
        }
        print(json.dumps(outcome, sort_keys=True))
    finally:
        connection.close()

    if os.environ.get("WP06_EXPECT_RESTORED_ROLE") == "1":
        return

    reader_password_file = Path(os.environ["WP06_ADMIN_REPORT_READER_PASSWORD_FILE"])
    reader = psycopg.connect(
        host="postgres",
        dbname="crypidian",
        user="crypidian_admin_report_reader",
        password=reader_password_file.read_text(encoding="utf-8").strip(),
    )
    try:
        reader_row = reader.execute(
            """
            SELECT
                r.rolcanlogin,
                r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
                    OR r.rolbypassrls,
                pg_has_role(
                    'crypidian_admin_report_reader',
                    'crypidian_admin_report_scope_owner',
                    'USAGE'
                ),
                has_table_privilege(
                    'crypidian_admin_report_reader', 'public.trade', 'INSERT'
                ),
                has_column_privilege(
                    'crypidian_admin_report_reader', 'public.paper_execution_checkpoint',
                    'snapshot_payload', 'SELECT'
                ),
                has_table_privilege(
                    'crypidian_admin_report_reader', 'public.paper_execution_checkpoint', 'INSERT'
                ),
                public.wp06_admin_report_reader_can_access(
                    '00000000-0000-0000-0000-000000000001',
                    '00000000-0000-0000-0000-000000000002'
                ),
                (SELECT count(*) FROM public.trade),
                (SELECT count(*) FROM public.bot_decision),
                (SELECT count(*) FROM public.paper_execution_checkpoint),
                has_table_privilege(
                    current_user, 'public.signal_outcome', 'INSERT'
                ) OR has_any_column_privilege(
                    current_user, 'public.signal_outcome', 'INSERT'
                ),
                has_table_privilege(
                    current_user, 'public.signal_outcome', 'UPDATE'
                ) OR has_any_column_privilege(
                    current_user, 'public.signal_outcome', 'UPDATE'
                ),
                has_table_privilege(current_user, 'public.signal_outcome', 'DELETE'),
                has_table_privilege(current_user, 'public.signal_outcome', 'TRUNCATE'),
                has_table_privilege(
                    current_user, 'public.telegram_signal_delivery', 'INSERT'
                ) OR has_any_column_privilege(
                    current_user, 'public.telegram_signal_delivery', 'INSERT'
                ),
                has_table_privilege(
                    current_user, 'public.telegram_signal_delivery', 'UPDATE'
                ) OR has_any_column_privilege(
                    current_user, 'public.telegram_signal_delivery', 'UPDATE'
                ),
                has_table_privilege(
                    current_user, 'public.telegram_signal_delivery', 'DELETE'
                ),
                has_table_privilege(
                    current_user, 'public.telegram_signal_delivery', 'TRUNCATE'
                ),
                ARRAY(
                    SELECT relation_name
                    FROM unnest(ARRAY[
                        'bot_instance', 'paper_account_seed', 'trade', 'trade_cost',
                        'trade_plan', 'order_intent', 'fill', 'position_event',
                        'risk_reservation', 'bot_decision', 'proposal_backtest',
                        'shared_signal_evidence', 'analyst_run', 'run_report',
                        'bot_runtime_state', 'outbox_event', 'market_instrument',
                        'market_bar', 'market_mark_price', 'market_live_quote',
                        'ingestion_gap', 'paper_execution_checkpoint', 'signal_outcome',
                        'telegram_signal_delivery'
                    ]) AS relation_name
                    WHERE has_table_privilege(
                        current_user, 'public.' || relation_name, 'SELECT'
                    )
                    ORDER BY relation_name
                ),
                (
                    SELECT jsonb_object_agg(
                        relation_name,
                        COALESCE(readable_columns, ARRAY[]::text[])
                    )
                    FROM unnest(ARRAY[
                        'bot_instance', 'paper_account_seed', 'trade', 'trade_cost',
                        'trade_plan', 'order_intent', 'fill', 'position_event',
                        'risk_reservation', 'bot_decision', 'proposal_backtest',
                        'shared_signal_evidence', 'analyst_run', 'run_report',
                        'bot_runtime_state', 'outbox_event', 'market_instrument',
                        'market_bar', 'market_mark_price', 'market_live_quote',
                        'ingestion_gap', 'paper_execution_checkpoint', 'signal_outcome',
                        'telegram_signal_delivery'
                    ]) AS report_tables(relation_name)
                    LEFT JOIN LATERAL (
                        SELECT array_agg(a.attname ORDER BY a.attname) AS readable_columns
                        FROM pg_catalog.pg_attribute AS a
                        JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
                        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public'
                          AND c.relname = report_tables.relation_name
                          AND a.attnum > 0
                          AND NOT a.attisdropped
                          AND has_column_privilege(current_user, c.oid, a.attnum, 'SELECT')
                    ) AS column_acl ON true
                ),
                has_column_privilege(current_user, 'public.trade', 'net_pnl', 'SELECT'),
                has_column_privilege(current_user, 'public.order_intent', 'quantity', 'SELECT'),
                has_column_privilege(current_user, 'public.order_intent', 'evidence', 'SELECT'),
                has_column_privilege(
                    current_user, 'public.order_intent', 'filled_quantity', 'SELECT'
                ),
                has_column_privilege(current_user, 'public.bot_decision', 'payload', 'SELECT'),
                has_column_privilege(
                    current_user, 'public.bot_decision', 'decision_hash', 'SELECT'
                ),
                has_column_privilege(
                    current_user, 'public.shared_signal_evidence', 'symbol', 'SELECT'
                ),
                has_column_privilege(
                    current_user, 'public.shared_signal_evidence', 'observed_at', 'SELECT'
                ),
                has_column_privilege(
                    current_user, 'public.shared_signal_evidence', 'canonical_json', 'SELECT'
                ),
                has_column_privilege(
                    current_user, 'public.shared_signal_evidence', 'source_refs', 'SELECT'
                ),
                has_column_privilege(
                    current_user, 'public.position_event', 'payload', 'SELECT'
                )
            FROM pg_catalog.pg_roles AS r
            WHERE r.rolname = current_user
            """
        ).fetchone()
        print(
            json.dumps(
                {
                    "report_reader_can_login": bool(reader_row[0]),
                    "report_reader_has_unsafe_role_attributes": bool(reader_row[1]),
                    "report_reader_inherits_scope_owner": bool(reader_row[2]),
                    "report_reader_can_insert_trade": bool(reader_row[3]),
                    "report_reader_can_select_paper_checkpoint": bool(reader_row[4]),
                    "report_reader_can_insert_paper_checkpoint": bool(reader_row[5]),
                    "report_reader_scope_without_identity": bool(reader_row[6]),
                    "report_reader_unscoped_trade_rows": reader_row[7],
                    "report_reader_unscoped_decision_rows": reader_row[8],
                    "report_reader_unscoped_paper_checkpoint_rows": reader_row[9],
                    "report_reader_signal_outcome_dml": {
                        "insert": bool(reader_row[10]),
                        "update": bool(reader_row[11]),
                        "delete": bool(reader_row[12]),
                        "truncate": bool(reader_row[13]),
                    },
                    "report_reader_telegram_delivery_dml": {
                        "insert": bool(reader_row[14]),
                        "update": bool(reader_row[15]),
                        "delete": bool(reader_row[16]),
                        "truncate": bool(reader_row[17]),
                    },
                    "report_reader_table_wide_select": reader_row[18],
                    "report_reader_selectable_columns": reader_row[19],
                    "report_reader_columns": {
                        "trade.net_pnl": bool(reader_row[20]),
                        "order_intent.quantity": bool(reader_row[21]),
                        "order_intent.evidence": bool(reader_row[22]),
                        "order_intent.filled_quantity": bool(reader_row[23]),
                        "bot_decision.payload": bool(reader_row[24]),
                        "bot_decision.decision_hash": bool(reader_row[25]),
                        "shared_signal_evidence.symbol": bool(reader_row[26]),
                        "shared_signal_evidence.observed_at": bool(reader_row[27]),
                        "shared_signal_evidence.canonical_json": bool(reader_row[28]),
                        "shared_signal_evidence.source_refs": bool(reader_row[29]),
                        "position_event.payload": bool(reader_row[30]),
                    },
                },
                sort_keys=True,
            )
        )
    finally:
        reader.close()


if __name__ == "__main__":
    main()

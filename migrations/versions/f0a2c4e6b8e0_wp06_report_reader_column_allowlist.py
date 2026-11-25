"""WP06.15: thay SELECT table-wide của report-reader bằng allowlist cột."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f0a2c4e6b8e0"
down_revision: str | None = "f0a2c4e6b8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READER = "crypidian_admin_report_reader"

# Chỉ cột được query report, live-monitor, risk projection và flatten consistency hiện dùng.
# Cột chứa bản canonical/source refs không được đọc bởi admin flow; cột mới trong tương lai cũng
# không tự nhận quyền khi schema thay đổi.
COLUMN_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "bot_instance": ("workspace_id", "id", "trading_account_id", "name"),
    "paper_account_seed": (
        "bot_id",
        "workspace_id",
        "quote_asset",
        "initial_equity",
        "created_at",
    ),
    "trade": (
        "bot_id",
        "workspace_id",
        "account_id",
        "mode",
        "intent_id",
        "status",
        "symbol",
        "quote_asset",
        "side",
        "settings_version_id",
        "strategy_release_id",
        "strategy_release_hash",
        "feature_snapshot_id",
        "candidate_id",
        "risk_decision_id",
        "risk_reservation_id",
        "gross_pnl",
        "fee_total",
        "funding_total",
        "slippage_total",
        "total_cost",
        "net_pnl",
        "opened_at",
        "closed_at",
        "lineage",
        "id",
    ),
    "trade_cost": (
        "bot_id",
        "workspace_id",
        "account_id",
        "mode",
        "trade_id",
        "cost_type",
        "amount",
        "currency",
        "occurred_at",
        "source_event_id",
        "id",
        "created_at",
    ),
    "trade_plan": ("id", "bot_id", "workspace_id", "account_id", "plan_hash", "payload"),
    "order_intent": (
        "bot_id",
        "workspace_id",
        "account_id",
        "mode",
        "idempotency_key",
        "plan_hash",
        "settings_version_id",
        "risk_decision_id",
        "risk_reservation_id",
        "symbol",
        "quote_asset",
        "side",
        "strategy_release_id",
        "strategy_release_hash",
        "feature_snapshot_id",
        "candidate_id",
        "quantity",
        "filled_quantity",
        "status",
        "evidence",
        "id",
        "created_at",
    ),
    "fill": (
        "bot_id",
        "workspace_id",
        "account_id",
        "mode",
        "intent_id",
        "fill_role",
        "fill_key",
        "bar_key",
        "fill_policy_version",
        "quantity",
        "price",
        "fee_amount",
        "slippage_amount",
        "occurred_at",
        "lineage",
        "id",
        "created_at",
    ),
    "position_event": (
        "bot_id",
        "workspace_id",
        "account_id",
        "mode",
        "intent_id",
        "event_key",
        "event_type",
        "quantity",
        "occurred_at",
        "payload",
        "id",
        "created_at",
    ),
    "risk_reservation": (
        "bot_id",
        "workspace_id",
        "account_id",
        "risk_decision_id",
        "plan_hash",
        "symbol",
        "reserved_risk",
        "reserved_notional",
        "reserved_initial_margin",
        "expires_at",
        "status",
        "id",
        "created_at",
    ),
    "bot_decision": (
        "workspace_id",
        "bot_id",
        "account_id",
        "evidence_id",
        "evidence_hash",
        "bot_config_version_id",
        "bot_config_hash",
        "root_settings_version_id",
        "model_release_id",
        "evaluated_at",
        "status",
        "reason_code",
        "candidate_id",
        "decision_hash",
        "payload",
        "id",
        "created_at",
    ),
    "proposal_backtest": (
        "workspace_id",
        "id",
        "analyst_run_id",
        "proposal_index",
        "bot_id",
        "bot_name",
        "created_at",
        "source",
        "verdict",
        "proposal",
        "result",
    ),
    "shared_signal_evidence": ("workspace_id", "id", "symbol", "observed_at"),
    "analyst_run": (
        "id",
        "workspace_id",
        "created_at",
        "topic_key",
        "engine",
        "model",
        "cli_version",
        "session_id",
        "resumed",
        "prompt_version",
        "output_version",
        "run_report_hash",
        "input_sha256",
        "valid",
        "error",
        "exit_code",
        "cost_usd",
        "usage",
        "validation_errors",
        "structured_output",
        "context",
    ),
    "run_report": (
        "id",
        "workspace_id",
        "report_hash",
        "report_version",
        "run_label",
        "window_start",
        "window_end",
        "bot_count",
        "payload",
        "created_at",
    ),
    "bot_runtime_state": ("workspace_id", "bot_id", "heartbeat_at"),
    "outbox_event": (
        "id",
        "workspace_id",
        "aggregate_id",
        "event_type",
        "status",
        "available_at",
        "created_at",
        "occurred_at",
    ),
    "market_instrument": ("workspace_id", "id", "symbol"),
    "market_bar": (
        "workspace_id",
        "symbol",
        "timeframe",
        "source",
        "open_time",
        "close_time",
        "available_at",
        "is_closed",
        "revision",
        "checksum",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "taker_buy_base_volume",
        "id",
    ),
    "market_mark_price": (
        "workspace_id",
        "instrument_id",
        "source",
        "event_at",
        "available_at",
        "mark_price",
        "index_price",
        "funding_rate",
        "next_funding_time",
        "received_at",
        "id",
    ),
    "market_live_quote": (
        "workspace_id",
        "source",
        "instrument_id",
        "symbol",
        "bid_price",
        "bid_quantity",
        "ask_price",
        "ask_quantity",
        "event_at",
    ),
    "ingestion_gap": (
        "id",
        "workspace_id",
        "source",
        "symbol",
        "timeframe",
        "gap_start",
        "gap_end",
        "detected_at",
        "resolved_at",
    ),
    "paper_execution_checkpoint": (
        "bot_id",
        "workspace_id",
        "account_id",
        "revision",
        "schema_version",
        "policy_hash",
        "snapshot_hash",
        "snapshot_payload",
    ),
    "signal_outcome": (
        "id",
        "workspace_id",
        "bot_id",
        "decision_id",
        "method_version",
        "horizon_bars",
        "status",
        "symbol",
        "timeframe",
        "direction",
        "strategy_release_id",
        "signal_at",
        "first_touch",
        "same_bar_conflict",
        "seconds_to_first_touch",
        "mfe_r",
        "mae_r",
        "outcome_r",
        "label",
    ),
    "telegram_signal_delivery": ("workspace_id", "bot_id", "decision_id", "status"),
}

REPORT_TABLES = tuple(COLUMN_ALLOWLISTS)
_COLUMN_ACL_QUERY = sa.text(
    """
    SELECT a.attname AS column_name
      FROM pg_catalog.pg_attribute AS a
      JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
      JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
      CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) AS privilege
     WHERE n.nspname = 'public'
       AND c.relname = :table_name
       AND a.attnum > 0
       AND NOT a.attisdropped
       AND privilege.grantee = (
            SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :role_name
       )
       AND privilege.privilege_type = 'SELECT'
    """
)


def _revoke_reader_select(connection: sa.Connection) -> None:
    preparer = connection.dialect.identifier_preparer
    for table_name in REPORT_TABLES:
        connection.exec_driver_sql(f"REVOKE SELECT ON TABLE public.{table_name} FROM {READER}")
        columns = connection.execute(
            _COLUMN_ACL_QUERY, {"table_name": table_name, "role_name": READER}
        ).scalars()
        for column_name in columns:
            quoted_column = preparer.quote(column_name)
            connection.exec_driver_sql(
                f"REVOKE SELECT ({quoted_column}) ON TABLE public.{table_name} FROM {READER}"
            )


def _assert_no_effective_table_select(connection: sa.Connection) -> None:
    """Không tiếp tục nếu SELECT bảng còn đến từ PUBLIC hoặc role kế thừa khác."""
    for table_name in REPORT_TABLES:
        has_select = connection.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege(:role_name, "
                "pg_catalog.to_regclass(:relation_name), 'SELECT')"
            ),
            {"role_name": READER, "relation_name": f"public.{table_name}"},
        ).scalar_one()
        if has_select:
            raise RuntimeError("WP06_REPORT_READER_TABLE_SELECT_OUTSIDE_ALLOWLIST")


def _assert_no_effective_column_select_outside_allowlist(connection: sa.Connection) -> None:
    """Fail closed nếu một cột ngoài danh sách vẫn đọc được qua PUBLIC/quyền kế thừa."""
    for table_name, allowed_columns in COLUMN_ALLOWLISTS.items():
        rows = connection.execute(
            sa.text(
                """
                SELECT a.attname,
                       pg_catalog.has_column_privilege(
                           :role_name, c.oid, a.attnum, 'SELECT'
                       ) AS can_select
                  FROM pg_catalog.pg_attribute AS a
                  JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
                  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                   AND c.relname = :table_name
                   AND a.attnum > 0
                   AND NOT a.attisdropped
                """
            ),
            {"role_name": READER, "table_name": table_name},
        ).all()
        if any(
            can_select and column_name not in allowed_columns for column_name, can_select in rows
        ):
            raise RuntimeError("WP06_REPORT_READER_COLUMN_SELECT_OUTSIDE_ALLOWLIST")


def _grant_reader_columns(connection: sa.Connection) -> None:
    preparer = connection.dialect.identifier_preparer
    quoted_schema = preparer.quote_schema("public")
    for table_name, columns in COLUMN_ALLOWLISTS.items():
        quoted_table = preparer.quote(table_name)
        column_sql = ", ".join(preparer.quote(column) for column in columns)
        connection.exec_driver_sql(
            f"GRANT SELECT ({column_sql}) ON TABLE {quoted_schema}.{quoted_table} TO {READER}"
        )


def upgrade() -> None:
    """Thu hồi ACL rộng hiện có rồi cấp lại column allowlist đã kiểm kê."""
    connection = op.get_bind()
    _revoke_reader_select(connection)
    _assert_no_effective_table_select(connection)
    _grant_reader_columns(connection)
    _assert_no_effective_table_select(connection)
    _assert_no_effective_column_select_outside_allowlist(connection)


def downgrade() -> None:
    """Khôi phục chính xác grant của d4/d6/d8 trước khi áp dụng WP06.15."""
    _revoke_reader_select(op.get_bind())
    for table_name in REPORT_TABLES:
        if table_name in {"outbox_event", "proposal_backtest", "analyst_run", "run_report"}:
            continue
        if table_name in {"signal_outcome", "telegram_signal_delivery"}:
            continue
        op.execute(f"GRANT SELECT ON TABLE public.{table_name} TO {READER}")
    op.execute(
        "GRANT SELECT (id, workspace_id, aggregate_id, event_type, status, available_at, "
        "created_at, occurred_at) ON TABLE public.outbox_event TO crypidian_admin_report_reader"
    )
    op.execute(
        "GRANT SELECT (workspace_id, id, analyst_run_id, proposal_index, bot_id, bot_name, "
        "created_at, source, verdict, proposal, result) ON TABLE public.proposal_backtest "
        "TO crypidian_admin_report_reader"
    )
    op.execute(
        "GRANT SELECT (id, workspace_id, created_at, topic_key, engine, model, cli_version, "
        "session_id, resumed, prompt_version, output_version, run_report_hash, input_sha256, "
        "valid, error, exit_code, cost_usd, usage, validation_errors, structured_output, context) "
        "ON TABLE public.analyst_run TO crypidian_admin_report_reader"
    )
    op.execute(
        "GRANT SELECT (id, workspace_id, report_hash, report_version, run_label, window_start, "
        "window_end, bot_count, payload, created_at) ON TABLE public.run_report "
        "TO crypidian_admin_report_reader"
    )
    op.execute(
        "GRANT SELECT (id, workspace_id, bot_id, decision_id, method_version, horizon_bars, "
        "status, symbol, timeframe, direction, strategy_release_id, signal_at, first_touch, "
        "same_bar_conflict, seconds_to_first_touch, mfe_r, mae_r, outcome_r, label) "
        "ON TABLE public.signal_outcome TO crypidian_admin_report_reader"
    )
    op.execute(
        "GRANT SELECT (workspace_id, bot_id, decision_id, status) "
        "ON TABLE public.telegram_signal_delivery TO crypidian_admin_report_reader"
    )

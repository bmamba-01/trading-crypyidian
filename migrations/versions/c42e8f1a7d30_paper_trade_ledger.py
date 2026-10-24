"""Tạo sổ giao dịch PAPER có lineage và ràng buộc tenant.

Revision ID: c42e8f1a7d30
Revises: b61a35c79e20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c42e8f1a7d30"
down_revision: str | None = "b61a35c79e20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AMOUNT = sa.Numeric(38, 18, asdecimal=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def _lineage(columns: list[sa.Column[object]]) -> list[sa.Column[object]]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        *columns,
    ]


def upgrade() -> None:
    op.create_table(
        "order_intent",
        *_lineage(
            [
                sa.Column("workspace_id", sa.Uuid(), nullable=False),
                sa.Column("account_id", sa.Uuid(), nullable=False),
                sa.Column("mode", sa.String(16), server_default="PAPER", nullable=False),
                sa.Column("idempotency_key", sa.String(240), nullable=False),
                sa.Column("plan_hash", sa.String(64), nullable=False),
                sa.Column("settings_version_id", sa.Uuid(), nullable=False),
                sa.Column("strategy_release_id", sa.Uuid(), nullable=False),
                sa.Column("strategy_release_hash", sa.String(64), nullable=False),
                sa.Column("feature_snapshot_id", sa.String(64), nullable=False),
                sa.Column("candidate_id", sa.String(64), nullable=False),
                sa.Column("risk_decision_id", sa.Uuid(), nullable=False),
                sa.Column("risk_reservation_id", sa.Uuid(), nullable=False),
                sa.Column("symbol", sa.String(64), nullable=False),
                sa.Column("quote_asset", sa.String(16), nullable=False),
                sa.Column("side", sa.String(8), nullable=False),
                sa.Column("quantity", AMOUNT, nullable=False),
                sa.Column("filled_quantity", AMOUNT, nullable=False),
                sa.Column("status", sa.String(24), nullable=False),
                sa.Column("evidence", JSONB, nullable=False),
            ]
        ),
        sa.CheckConstraint("mode = 'PAPER'", name="order_intent_paper_only"),
        sa.CheckConstraint("side IN ('LONG', 'SHORT')", name="order_intent_side_valid"),
        sa.CheckConstraint("length(quote_asset) > 0", name="order_intent_quote_asset_nonempty"),
        sa.CheckConstraint(
            "quantity > 0 AND filled_quantity >= 0 AND filled_quantity <= quantity",
            name="order_intent_quantity_valid",
        ),
        sa.CheckConstraint(
            "status IN ('ACCEPTED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', "
            "'EXPIRED', 'UNKNOWN')",
            name="order_intent_status_valid",
        ),
        sa.CheckConstraint(
            "(status = 'ACCEPTED' AND filled_quantity = 0) OR "
            "(status = 'PARTIALLY_FILLED' AND filled_quantity > 0 "
            "AND filled_quantity < quantity) OR "
            "(status = 'FILLED' AND filled_quantity = quantity) OR "
            "status IN ('CANCELLED', 'EXPIRED', 'UNKNOWN')",
            name="order_intent_status_quantity_consistent",
        ),
        sa.CheckConstraint("length(candidate_id) = 64", name="order_intent_candidate_sha256"),
        sa.CheckConstraint(
            "length(strategy_release_hash) = 64", name="order_intent_release_sha256"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"], ["trading_account.workspace_id", "trading_account.id"]
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "settings_version_id"],
            ["workspace_settings_version.workspace_id", "workspace_settings_version.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "strategy_release_id"],
            ["strategy_release.workspace_id", "strategy_release.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "plan_hash"],
            ["trade_plan.workspace_id", "trade_plan.account_id", "trade_plan.plan_hash"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "risk_decision_id"],
            ["risk_decision.workspace_id", "risk_decision.account_id", "risk_decision.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "risk_reservation_id"],
            ["risk_reservation.workspace_id", "risk_reservation.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "feature_snapshot_id"],
            ["feature_snapshot.workspace_id", "feature_snapshot.snapshot_id"],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_intent")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_order_intent_workspace_id"),
        sa.UniqueConstraint("workspace_id", "account_id", "id", name="uq_order_intent_account_id"),
        sa.UniqueConstraint(
            "workspace_id", "account_id", "idempotency_key", name="uq_order_intent_idempotency"
        ),
    )
    op.create_index(
        "ix_order_intent_workspace_account_status",
        "order_intent",
        ["workspace_id", "account_id", "status"],
    )
    op.create_table(
        "exchange_order",
        *_lineage(
            [
                sa.Column("workspace_id", sa.Uuid(), nullable=False),
                sa.Column("account_id", sa.Uuid(), nullable=False),
                sa.Column("mode", sa.String(16), server_default="PAPER", nullable=False),
                sa.Column("intent_id", sa.Uuid(), nullable=False),
                sa.Column("client_order_id", sa.String(240), nullable=False),
                sa.Column("exchange_order_id", sa.String(240), nullable=True),
                sa.Column("status", sa.String(24), nullable=False),
                sa.Column("raw_reference", sa.Text(), nullable=True),
            ]
        ),
        sa.CheckConstraint("mode = 'PAPER'", name="exchange_order_paper_only"),
        sa.CheckConstraint(
            "status IN ('NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'EXPIRED', 'UNKNOWN')",
            name="exchange_order_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "intent_id"],
            ["order_intent.workspace_id", "order_intent.account_id", "order_intent.id"],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exchange_order")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_exchange_order_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "account_id", "client_order_id", name="uq_exchange_order_client_id"
        ),
    )
    op.create_table(
        "fill",
        *_lineage(
            [
                sa.Column("workspace_id", sa.Uuid(), nullable=False),
                sa.Column("account_id", sa.Uuid(), nullable=False),
                sa.Column("mode", sa.String(16), server_default="PAPER", nullable=False),
                sa.Column("intent_id", sa.Uuid(), nullable=False),
                sa.Column("fill_key", sa.String(240), nullable=False),
                sa.Column("bar_key", sa.String(240), nullable=False),
                sa.Column("fill_policy_version", sa.String(80), nullable=False),
                sa.Column("quantity", AMOUNT, nullable=False),
                sa.Column("price", AMOUNT, nullable=False),
                sa.Column("fee_amount", AMOUNT, nullable=False),
                sa.Column("slippage_amount", AMOUNT, nullable=False),
                sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("lineage", JSONB, nullable=False),
            ]
        ),
        sa.CheckConstraint("mode = 'PAPER'", name="fill_paper_only"),
        sa.CheckConstraint("quantity > 0 AND price > 0", name="fill_amounts_positive"),
        sa.CheckConstraint(
            "fee_amount >= 0 AND slippage_amount >= 0", name="fill_costs_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "intent_id"],
            ["order_intent.workspace_id", "order_intent.account_id", "order_intent.id"],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fill")),
        sa.UniqueConstraint("workspace_id", "account_id", "fill_key", name="uq_fill_idempotency"),
    )
    op.create_index(
        "ix_fill_workspace_account_time", "fill", ["workspace_id", "account_id", "occurred_at"]
    )
    op.create_table(
        "position_event",
        *_lineage(
            [
                sa.Column("workspace_id", sa.Uuid(), nullable=False),
                sa.Column("account_id", sa.Uuid(), nullable=False),
                sa.Column("mode", sa.String(16), server_default="PAPER", nullable=False),
                sa.Column("intent_id", sa.Uuid(), nullable=False),
                sa.Column("event_key", sa.String(240), nullable=False),
                sa.Column("event_type", sa.String(24), nullable=False),
                sa.Column("quantity", AMOUNT, nullable=False),
                sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("payload", JSONB, nullable=False),
                sa.Column("lineage", JSONB, nullable=False),
            ]
        ),
        sa.CheckConstraint("mode = 'PAPER'", name="position_event_paper_only"),
        sa.CheckConstraint(
            "event_type IN ('OPENED', 'INCREASED', 'REDUCED', 'CLOSED', 'STOP_UPDATED')",
            name="position_event_type_valid",
        ),
        sa.CheckConstraint("quantity >= 0", name="position_event_quantity_nonnegative"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "intent_id"],
            ["order_intent.workspace_id", "order_intent.account_id", "order_intent.id"],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_position_event")),
        sa.UniqueConstraint(
            "workspace_id", "account_id", "event_key", name="uq_position_event_key"
        ),
    )
    op.create_table(
        "trade",
        *_lineage(
            [
                sa.Column("workspace_id", sa.Uuid(), nullable=False),
                sa.Column("account_id", sa.Uuid(), nullable=False),
                sa.Column("mode", sa.String(16), server_default="PAPER", nullable=False),
                sa.Column("intent_id", sa.Uuid(), nullable=False),
                sa.Column("status", sa.String(16), nullable=False),
                sa.Column("symbol", sa.String(64), nullable=False),
                sa.Column("quote_asset", sa.String(16), nullable=False),
                sa.Column("side", sa.String(8), nullable=False),
                sa.Column("settings_version_id", sa.Uuid(), nullable=False),
                sa.Column("strategy_release_id", sa.Uuid(), nullable=False),
                sa.Column("strategy_release_hash", sa.String(64), nullable=False),
                sa.Column("feature_snapshot_id", sa.String(64), nullable=False),
                sa.Column("candidate_id", sa.String(64), nullable=False),
                sa.Column("risk_decision_id", sa.Uuid(), nullable=False),
                sa.Column("risk_reservation_id", sa.Uuid(), nullable=False),
                sa.Column("gross_pnl", AMOUNT, nullable=False),
                sa.Column("fee_total", AMOUNT, nullable=False),
                sa.Column("funding_total", AMOUNT, nullable=False),
                sa.Column("slippage_total", AMOUNT, nullable=False),
                sa.Column("total_cost", AMOUNT, nullable=False),
                sa.Column("net_pnl", AMOUNT, nullable=False),
                sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("lineage", JSONB, nullable=False),
            ]
        ),
        sa.CheckConstraint("mode = 'PAPER'", name="trade_paper_only"),
        sa.CheckConstraint("status IN ('OPEN', 'CLOSED')", name="trade_status_valid"),
        sa.CheckConstraint("side IN ('LONG', 'SHORT')", name="trade_side_valid"),
        sa.CheckConstraint("length(quote_asset) > 0", name="trade_quote_asset_nonempty"),
        sa.CheckConstraint(
            "(status = 'OPEN' AND closed_at IS NULL) OR "
            "(status = 'CLOSED' AND closed_at IS NOT NULL)",
            name="trade_status_close_time_consistent",
        ),
        sa.CheckConstraint(
            "total_cost = fee_total + funding_total + slippage_total", name="trade_cost_sum_exact"
        ),
        sa.CheckConstraint("net_pnl = gross_pnl - total_cost", name="trade_net_pnl_exact"),
        sa.CheckConstraint("fee_total >= 0 AND slippage_total >= 0", name="trade_cost_nonnegative"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "intent_id"],
            ["order_intent.workspace_id", "order_intent.account_id", "order_intent.id"],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trade")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_trade_workspace_id"),
        sa.UniqueConstraint("workspace_id", "account_id", "id", name="uq_trade_account_id"),
        sa.UniqueConstraint("workspace_id", "account_id", "intent_id", name="uq_trade_intent"),
    )
    op.create_table(
        "trade_cost",
        *_lineage(
            [
                sa.Column("workspace_id", sa.Uuid(), nullable=False),
                sa.Column("account_id", sa.Uuid(), nullable=False),
                sa.Column("mode", sa.String(16), server_default="PAPER", nullable=False),
                sa.Column("trade_id", sa.Uuid(), nullable=False),
                sa.Column("cost_key", sa.String(240), nullable=False),
                sa.Column("cost_type", sa.String(16), nullable=False),
                sa.Column("amount", AMOUNT, nullable=False),
                sa.Column("currency", sa.String(16), nullable=False),
                sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("source_event_id", sa.String(240), nullable=False),
                sa.Column("lineage", JSONB, nullable=False),
            ]
        ),
        sa.CheckConstraint("mode = 'PAPER'", name="trade_cost_paper_only"),
        sa.CheckConstraint(
            "cost_type IN ('FEE', 'FUNDING', 'SLIPPAGE')", name="trade_cost_type_valid"
        ),
        sa.CheckConstraint(
            "amount >= 0 OR cost_type = 'FUNDING'", name="trade_cost_amount_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "trade_id"],
            ["trade.workspace_id", "trade.account_id", "trade.id"],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trade_cost")),
        sa.UniqueConstraint("workspace_id", "account_id", "cost_key", name="uq_trade_cost_key"),
    )

    policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    for table in (
        "order_intent",
        "exchange_order",
        "fill",
        "position_event",
        "trade",
        "trade_cost",
    ):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY workspace_isolation ON "{table}" USING ({policy}) WITH CHECK ({policy})'
        )
        op.execute(f'GRANT SELECT, INSERT ON TABLE "{table}" TO crypidian_app')
        op.execute(f'GRANT SELECT, INSERT ON TABLE "{table}" TO crypidian_trading_runtime')
    op.execute(
        "GRANT UPDATE (status, filled_quantity) ON order_intent TO crypidian_trading_runtime"
    )
    op.execute("GRANT UPDATE (status) ON exchange_order TO crypidian_trading_runtime")
    op.execute(
        "GRANT UPDATE (status, closed_at, gross_pnl, fee_total, funding_total, slippage_total, "
        "total_cost, net_pnl) ON trade TO crypidian_trading_runtime"
    )

    op.execute(
        """
        CREATE FUNCTION reject_paper_ledger_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'paper ledger rows are append-only'; END; $$
        """
    )
    for table in ("fill", "position_event", "trade_cost"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION reject_paper_ledger_mutation()"
        )
    op.execute(
        """
        CREATE FUNCTION guard_paper_trade_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'paper trades cannot be deleted';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'OPEN' OR NEW.closed_at IS NOT NULL THEN
                    RAISE EXCEPTION 'paper trade must start OPEN';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status <> 'OPEN' OR NEW.status <> 'CLOSED' OR NEW.closed_at IS NULL
               OR (to_jsonb(NEW) - ARRAY['status','closed_at','gross_pnl','fee_total',
                   'funding_total','slippage_total','total_cost','net_pnl'])
                  <> (to_jsonb(OLD) - ARRAY['status','closed_at','gross_pnl','fee_total',
                      'funding_total','slippage_total','total_cost','net_pnl']) THEN
                RAISE EXCEPTION 'trade may transition from OPEN to CLOSED once';
            END IF;
            RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER trade_close_guard BEFORE INSERT OR UPDATE OR DELETE ON trade "
        "FOR EACH ROW EXECUTE FUNCTION guard_paper_trade_update()"
    )
    op.execute(
        """
        CREATE FUNCTION guard_paper_exchange_order_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR (to_jsonb(NEW) - 'status') <> (to_jsonb(OLD) - 'status')
               OR NOT (OLD.status = NEW.status
                 OR (OLD.status = 'NEW' AND NEW.status IN
                     ('PARTIALLY_FILLED','FILLED','CANCELLED','EXPIRED','UNKNOWN'))
                 OR (OLD.status = 'PARTIALLY_FILLED' AND NEW.status IN
                     ('PARTIALLY_FILLED','FILLED','CANCELLED','UNKNOWN'))
                 OR (OLD.status = 'UNKNOWN' AND NEW.status IN
                     ('PARTIALLY_FILLED','FILLED','CANCELLED','EXPIRED'))) THEN
                RAISE EXCEPTION 'invalid paper exchange order transition';
            END IF;
            RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER exchange_order_transition_guard BEFORE UPDATE OR DELETE ON exchange_order "
        "FOR EACH ROW EXECUTE FUNCTION guard_paper_exchange_order_update()"
    )
    op.execute(
        """
        CREATE FUNCTION guard_paper_trade_cost_currency() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE expected_currency text;
        BEGIN
            SELECT quote_asset INTO expected_currency FROM trade
             WHERE workspace_id = NEW.workspace_id AND account_id = NEW.account_id
               AND id = NEW.trade_id;
            IF expected_currency IS NULL OR NEW.currency <> expected_currency THEN
                RAISE EXCEPTION 'paper trade cost currency must match trade quote asset';
            END IF;
            RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER trade_cost_currency_guard BEFORE INSERT ON trade_cost "
        "FOR EACH ROW EXECUTE FUNCTION guard_paper_trade_cost_currency()"
    )
    op.execute(
        """
        CREATE FUNCTION guard_paper_intent_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'paper intents cannot be deleted'; END IF;
            IF (to_jsonb(NEW) - ARRAY['status','filled_quantity'])
                   <> (to_jsonb(OLD) - ARRAY['status','filled_quantity'])
               OR NEW.filled_quantity < OLD.filled_quantity OR NEW.filled_quantity > NEW.quantity
               OR NOT (OLD.status = NEW.status
                 OR (OLD.status = 'ACCEPTED' AND NEW.status IN
                     ('PARTIALLY_FILLED','FILLED','CANCELLED','EXPIRED','UNKNOWN'))
                 OR (OLD.status = 'PARTIALLY_FILLED' AND NEW.status IN
                     ('PARTIALLY_FILLED','FILLED','CANCELLED','UNKNOWN'))
                 OR (OLD.status = 'UNKNOWN' AND NEW.status IN
                     ('PARTIALLY_FILLED','FILLED','CANCELLED','EXPIRED'))) THEN
                RAISE EXCEPTION 'invalid paper intent transition';
            END IF;
            RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER order_intent_transition_guard BEFORE UPDATE OR DELETE ON order_intent "
        "FOR EACH ROW EXECUTE FUNCTION guard_paper_intent_update()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER order_intent_transition_guard ON order_intent")
    op.execute("DROP FUNCTION guard_paper_intent_update()")
    op.execute("DROP TRIGGER trade_cost_currency_guard ON trade_cost")
    op.execute("DROP FUNCTION guard_paper_trade_cost_currency()")
    op.execute("DROP TRIGGER exchange_order_transition_guard ON exchange_order")
    op.execute("DROP FUNCTION guard_paper_exchange_order_update()")
    op.execute("DROP TRIGGER trade_close_guard ON trade")
    op.execute("DROP FUNCTION guard_paper_trade_update()")
    for table in ("fill", "position_event", "trade_cost"):
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.execute("DROP FUNCTION reject_paper_ledger_mutation()")
    op.execute("REVOKE UPDATE ON order_intent FROM crypidian_trading_runtime")
    op.execute("REVOKE UPDATE ON trade FROM crypidian_trading_runtime")
    for table in (
        "trade_cost",
        "trade",
        "position_event",
        "fill",
        "exchange_order",
        "order_intent",
    ):
        op.execute(f'DROP POLICY workspace_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM crypidian_app')
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM crypidian_trading_runtime')
        op.drop_table(table)

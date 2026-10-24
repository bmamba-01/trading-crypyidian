"""Lưu reservation risk atomic theo workspace và tài khoản.

Revision ID: f31b7c9a24d1
Revises: e18f35a7c902
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f31b7c9a24d1"
down_revision: str | None = "e18f35a7c902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_plan",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("notional", sa.Numeric(38, 18), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(symbol) > 0", name="trade_plan_symbol_nonempty"),
        sa.CheckConstraint("notional > 0", name="trade_plan_notional_positive"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trade_plan")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_trade_plan_workspace_id"),
        sa.UniqueConstraint("workspace_id", "account_id", "plan_hash", name="uq_trade_plan_hash"),
    )
    op.create_index(
        "ix_trade_plan_workspace_account_created",
        "trade_plan",
        ["workspace_id", "account_id", "created_at"],
    )
    op.create_table(
        "risk_decision",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("decision_key", sa.String(length=64), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=False),
        sa.Column("detail_vi", sa.Text(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(reason_code) > 0", name="risk_decision_reason_nonempty"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "plan_hash"],
            ["trade_plan.workspace_id", "trade_plan.account_id", "trade_plan.plan_hash"],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_decision")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_risk_decision_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "account_id", "id", name="uq_risk_decision_workspace_account_id"
        ),
        sa.UniqueConstraint(
            "workspace_id", "account_id", "decision_key", name="uq_risk_decision_key"
        ),
    )
    op.create_index(
        "ix_risk_decision_workspace_account_created",
        "risk_decision",
        ["workspace_id", "account_id", "created_at"],
    )
    op.create_table(
        "risk_reservation",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("risk_decision_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=240), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("reserved_risk", sa.Numeric(38, 18), nullable=False),
        sa.Column("reserved_notional", sa.Numeric(38, 18), nullable=False),
        sa.Column("reserved_initial_margin", sa.Numeric(38, 18), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resolution_reason", sa.String(length=40), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("reserved_risk > 0", name="risk_reservation_risk_positive"),
        sa.CheckConstraint("reserved_notional > 0", name="risk_reservation_notional_positive"),
        sa.CheckConstraint(
            "reserved_initial_margin > 0", name="risk_reservation_initial_margin_positive"
        ),
        sa.CheckConstraint("length(symbol) > 0", name="risk_reservation_symbol_nonempty"),
        sa.CheckConstraint(
            "status IN ('RESERVED', 'UNKNOWN', 'RELEASED', 'TRANSFERRED')",
            name="risk_reservation_status_valid",
        ),
        sa.CheckConstraint(
            "resolution_reason IS NULL OR resolution_reason IN "
            "('EXPIRED_BEFORE_SUBMIT', 'CANCELLED_BEFORE_SUBMIT', "
            "'ORDER_ABSENT', 'OPEN_RISK_RECORDED')",
            name="risk_reservation_resolution_reason_valid",
        ),
        sa.CheckConstraint(
            "(status IN ('RESERVED', 'UNKNOWN') AND resolution_reason IS NULL) OR "
            "(status = 'RELEASED' AND resolution_reason IN "
            "('EXPIRED_BEFORE_SUBMIT', 'CANCELLED_BEFORE_SUBMIT', 'ORDER_ABSENT')) OR "
            "(status = 'TRANSFERRED' AND resolution_reason = 'OPEN_RISK_RECORDED')",
            name="risk_reservation_status_reason_consistent",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "risk_decision_id"],
            [
                "risk_decision.workspace_id",
                "risk_decision.account_id",
                "risk_decision.id",
            ],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_reservation")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_risk_reservation_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "idempotency_key",
            name="uq_risk_reservation_idempotency",
        ),
    )
    op.create_index(
        "ix_risk_reservation_active",
        "risk_reservation",
        ["workspace_id", "account_id", "status"],
    )

    policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    for table in ("trade_plan", "risk_decision", "risk_reservation"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY workspace_isolation ON "{table}" USING ({policy}) WITH CHECK ({policy})'
        )
    op.execute(
        """
        CREATE FUNCTION reject_risk_evaluation_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'trade plan and risk decision records are append-only';
        END;
        $$
        """
    )
    for table in ("trade_plan", "risk_decision"):
        op.execute(
            f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION reject_risk_evaluation_mutation()"
        )
    op.execute(
        """
        CREATE FUNCTION protect_risk_reservation_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'risk reservation cannot be deleted';
            END IF;
            IF (OLD.id, OLD.workspace_id, OLD.account_id, OLD.idempotency_key, OLD.plan_hash,
                OLD.risk_decision_id, OLD.symbol, OLD.reserved_risk, OLD.reserved_notional,
                OLD.reserved_initial_margin,
                OLD.created_at)
               IS DISTINCT FROM
               (NEW.id, NEW.workspace_id, NEW.account_id, NEW.idempotency_key, NEW.plan_hash,
                NEW.risk_decision_id, NEW.symbol, NEW.reserved_risk, NEW.reserved_notional,
                NEW.reserved_initial_margin,
                NEW.created_at)
            THEN
                RAISE EXCEPTION 'risk reservation identity and budget are immutable';
            END IF;
            IF OLD.status = NEW.status THEN
                IF OLD.resolution_reason IS DISTINCT FROM NEW.resolution_reason THEN
                    RAISE EXCEPTION 'risk reservation resolution reason is immutable';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status = 'RESERVED' AND NEW.status = 'UNKNOWN'
               AND NEW.resolution_reason IS NULL THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'RESERVED' AND NEW.status = 'RELEASED'
               AND NEW.resolution_reason IN (
                   'EXPIRED_BEFORE_SUBMIT', 'CANCELLED_BEFORE_SUBMIT'
               ) THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'UNKNOWN' AND NEW.status = 'RELEASED'
               AND NEW.resolution_reason = 'ORDER_ABSENT' THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'UNKNOWN' AND NEW.status = 'TRANSFERRED'
               AND NEW.resolution_reason = 'OPEN_RISK_RECORDED' THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'invalid risk reservation status transition';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER risk_reservation_transition_guard "
        "BEFORE UPDATE OR DELETE ON risk_reservation "
        "FOR EACH ROW EXECUTE FUNCTION protect_risk_reservation_transition()"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE risk_reservation TO crypidian_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE risk_reservation TO crypidian_trading_runtime"
    )
    op.execute("GRANT SELECT, INSERT ON TABLE trade_plan, risk_decision TO crypidian_app")
    op.execute(
        "GRANT SELECT, INSERT ON TABLE trade_plan, risk_decision TO crypidian_trading_runtime"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER risk_reservation_transition_guard ON risk_reservation")
    op.execute("DROP FUNCTION protect_risk_reservation_transition()")
    for table in ("trade_plan", "risk_decision"):
        op.execute(f'DROP TRIGGER {table}_immutable ON "{table}"')
    op.execute("DROP FUNCTION reject_risk_evaluation_mutation()")
    for table in ("risk_reservation", "risk_decision", "trade_plan"):
        op.execute(f'DROP POLICY workspace_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.execute("REVOKE ALL PRIVILEGES ON TABLE risk_reservation FROM crypidian_app")
    op.execute("REVOKE ALL PRIVILEGES ON TABLE risk_reservation FROM crypidian_trading_runtime")
    op.execute("REVOKE ALL PRIVILEGES ON TABLE trade_plan, risk_decision FROM crypidian_app")
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE trade_plan, risk_decision FROM crypidian_trading_runtime"
    )
    op.drop_index("ix_risk_reservation_active", table_name="risk_reservation")
    op.drop_table("risk_reservation")
    op.drop_index("ix_risk_decision_workspace_account_created", table_name="risk_decision")
    op.drop_table("risk_decision")
    op.drop_index("ix_trade_plan_workspace_account_created", table_name="trade_plan")
    op.drop_table("trade_plan")

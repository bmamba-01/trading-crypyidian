"""Lưu sensitivity scenario và trade outcomes bất biến của replay.

Revision ID: 8c4f2a6d9e31
Revises: 7a1d9e4c2b63
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8c4f2a6d9e31"
down_revision: str | None = "7a1d9e4c2b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def _common() -> list[sa.Column[object]]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "backtest_scenario",
        *_common(),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("trial_record_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("scenario_id", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("scenario_hash", sa.String(64), nullable=False),
        sa.Column("scenario", JSONB, nullable=False),
        sa.Column("canonical_scenario", sa.Text(), nullable=False),
        sa.CheckConstraint("length(manifest_hash) = 64", name="backtest_scenario_manifest_sha256"),
        sa.CheckConstraint("length(scenario_hash) = 64", name="backtest_scenario_sha256"),
        sa.CheckConstraint("kind IN ('BASE', 'COST', 'PARAMETER')", name="backtest_scenario_kind"),
        sa.CheckConstraint("jsonb_typeof(scenario) = 'object'", name="backtest_scenario_object"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["backtest_run.workspace_id", "backtest_run.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "trial_record_id"],
            ["backtest_trial.workspace_id", "backtest_trial.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_scenario")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_backtest_scenario_workspace_id"),
        sa.UniqueConstraint("workspace_id", "trial_record_id", name="uq_backtest_scenario_trial"),
    )
    op.create_index(
        "ix_backtest_scenario_workspace_run", "backtest_scenario", ["workspace_id", "run_id"]
    )
    op.create_table(
        "backtest_trade_outcome",
        *_common(),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("trial_record_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("trade_id", sa.String(240), nullable=False),
        sa.Column("outcome_hash", sa.String(64), nullable=False),
        sa.Column("outcome", JSONB, nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.CheckConstraint("length(manifest_hash) = 64", name="backtest_outcome_manifest_sha256"),
        sa.CheckConstraint("length(outcome_hash) = 64", name="backtest_outcome_sha256"),
        sa.CheckConstraint("ordinal >= 0", name="backtest_outcome_ordinal_nonnegative"),
        sa.CheckConstraint("jsonb_typeof(outcome) = 'object'", name="backtest_outcome_object"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["backtest_run.workspace_id", "backtest_run.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "trial_record_id"],
            ["backtest_trial.workspace_id", "backtest_trial.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_trade_outcome")),
        sa.UniqueConstraint(
            "workspace_id", "trial_record_id", "ordinal", name="uq_backtest_outcome_ordinal"
        ),
        sa.UniqueConstraint(
            "workspace_id", "trial_record_id", "trade_id", name="uq_backtest_outcome_trade"
        ),
    )
    op.create_index(
        "ix_backtest_outcome_workspace_trial",
        "backtest_trade_outcome",
        ["workspace_id", "trial_record_id", "ordinal"],
    )

    policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    for table in ("backtest_scenario", "backtest_trade_outcome"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY workspace_isolation ON "{table}" USING ({policy}) WITH CHECK ({policy})'
        )
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_backtest_mutation()"
        )
    op.execute(
        "GRANT SELECT, INSERT ON TABLE backtest_scenario, backtest_trade_outcome TO crypidian_app"
    )
    op.execute(
        "GRANT SELECT, INSERT ON TABLE backtest_scenario, backtest_trade_outcome "
        "TO crypidian_trainer_worker"
    )


def downgrade() -> None:
    for table in ("backtest_trade_outcome", "backtest_scenario"):
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
    for table in ("backtest_trade_outcome", "backtest_scenario"):
        op.execute(f'DROP POLICY workspace_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE backtest_scenario, backtest_trade_outcome "
        "FROM crypidian_app"
    )
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE backtest_scenario, backtest_trade_outcome "
        "FROM crypidian_trainer_worker"
    )
    op.drop_index("ix_backtest_outcome_workspace_trial", table_name="backtest_trade_outcome")
    op.drop_table("backtest_trade_outcome")
    op.drop_index("ix_backtest_scenario_workspace_run", table_name="backtest_scenario")
    op.drop_table("backtest_scenario")

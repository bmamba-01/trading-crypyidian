"""Lưu backtest run, trial và report bất biến theo workspace.

Revision ID: 9a7c2d5e1b40
Revises: c42e8f1a7d30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9a7c2d5e1b40"
down_revision: str | None = "c42e8f1a7d30"
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
        "backtest_run",
        *_common(),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest", JSONB, nullable=False),
        sa.Column("canonical_manifest", sa.Text(), nullable=False),
        sa.CheckConstraint("length(manifest_hash) = 64", name="backtest_run_manifest_sha256"),
        sa.CheckConstraint(
            "jsonb_typeof(manifest) = 'object'", name="backtest_run_manifest_object"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_run")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_backtest_run_workspace_id"),
        sa.UniqueConstraint("workspace_id", "manifest_hash", name="uq_backtest_run_manifest"),
    )
    op.create_index(
        "ix_backtest_run_workspace_created", "backtest_run", ["workspace_id", "created_at"]
    )
    op.create_table(
        "backtest_trial",
        *_common(),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("trial_id", sa.String(length=64), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("parameter_family_hash", sa.String(length=64), nullable=False),
        sa.Column("trial_index", sa.Integer(), nullable=False),
        sa.Column("trial_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("completed_observation_count", sa.Integer(), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("error_type", sa.String(length=120), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("canonical_trial", sa.Text(), nullable=False),
        sa.CheckConstraint("length(trial_id) = 64", name="backtest_trial_id_sha256"),
        sa.CheckConstraint("length(manifest_hash) = 64", name="backtest_trial_manifest_sha256"),
        sa.CheckConstraint(
            "length(parameter_family_hash) = 64", name="backtest_trial_parameter_sha256"
        ),
        sa.CheckConstraint("length(content_hash) = 64", name="backtest_trial_content_sha256"),
        sa.CheckConstraint(
            "trial_count >= 1 AND trial_index >= 0 AND trial_index < trial_count",
            name="backtest_trial_index_valid",
        ),
        sa.CheckConstraint(
            "observation_count >= 0 AND completed_observation_count >= 0 "
            "AND completed_observation_count <= observation_count AND trade_count >= 0",
            name="backtest_trial_counts_valid",
        ),
        sa.CheckConstraint(
            "status IN ('COMPLETED', 'REJECTED', 'FAILED')", name="backtest_trial_status_valid"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["backtest_run.workspace_id", "backtest_run.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_trial")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_backtest_trial_workspace_id"),
        sa.UniqueConstraint("workspace_id", "trial_id", name="uq_backtest_trial_trial_id"),
    )
    op.create_index(
        "ix_backtest_trial_workspace_run",
        "backtest_trial",
        ["workspace_id", "run_id", "trial_index"],
    )
    op.create_table(
        "backtest_report",
        *_common(),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("trial_record_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("report_hash", sa.String(length=64), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("usable_observation_count", sa.Integer(), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("coverage", sa.Float(), nullable=False),
        sa.Column("report", JSONB, nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.CheckConstraint("length(manifest_hash) = 64", name="backtest_report_manifest_sha256"),
        sa.CheckConstraint("length(report_hash) = 64", name="backtest_report_sha256"),
        sa.CheckConstraint(
            "observation_count >= 0 AND usable_observation_count >= 0 "
            "AND usable_observation_count <= observation_count AND trade_count >= 0",
            name="backtest_report_counts_valid",
        ),
        sa.CheckConstraint(
            "coverage >= 0 AND coverage <= 1", name="backtest_report_coverage_valid"
        ),
        sa.CheckConstraint("jsonb_typeof(report) = 'object'", name="backtest_report_object"),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_report")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_backtest_report_workspace_id"),
        sa.UniqueConstraint("workspace_id", "report_hash", name="uq_backtest_report_hash"),
        sa.UniqueConstraint("workspace_id", "trial_record_id", name="uq_backtest_report_trial"),
    )
    op.create_index(
        "ix_backtest_report_workspace_created", "backtest_report", ["workspace_id", "created_at"]
    )

    policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    for table in ("backtest_run", "backtest_trial", "backtest_report"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY workspace_isolation ON "{table}" USING ({policy}) WITH CHECK ({policy})'
        )
    op.execute(
        """
        CREATE FUNCTION reject_backtest_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'backtest records are append-only';
        END;
        $$
        """
    )
    for table in ("backtest_run", "backtest_trial", "backtest_report"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_backtest_mutation()"
        )
    op.execute(
        "GRANT SELECT, INSERT ON TABLE backtest_run, backtest_trial, backtest_report "
        "TO crypidian_app"
    )
    op.execute(
        "GRANT SELECT, INSERT ON TABLE backtest_run, backtest_trial, backtest_report "
        "TO crypidian_trainer_worker"
    )


def downgrade() -> None:
    for table in ("backtest_report", "backtest_trial", "backtest_run"):
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
    op.execute("DROP FUNCTION reject_backtest_mutation()")
    for table in ("backtest_report", "backtest_trial", "backtest_run"):
        op.execute(f'DROP POLICY workspace_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE backtest_run, backtest_trial, backtest_report "
        "FROM crypidian_app"
    )
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE backtest_run, backtest_trial, backtest_report "
        "FROM crypidian_trainer_worker"
    )
    op.drop_index("ix_backtest_report_workspace_created", table_name="backtest_report")
    op.drop_table("backtest_report")
    op.drop_index("ix_backtest_trial_workspace_run", table_name="backtest_trial")
    op.drop_table("backtest_trial")
    op.drop_index("ix_backtest_run_workspace_created", table_name="backtest_run")
    op.drop_table("backtest_run")

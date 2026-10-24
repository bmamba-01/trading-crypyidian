"""Persist complete PAPER gateway checkpoints by account revision.

Revision ID: a1f4c9d2e6b8
Revises: 8c4f2a6d9e31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1f4c9d2e6b8"
down_revision: str | None = "8c4f2a6d9e31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_execution_checkpoint",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=16), server_default="PAPER", nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(length=240), nullable=False),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column("event_scope", sa.String(length=120), nullable=True),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_payload", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("mode = 'PAPER'", name="paper_checkpoint_mode"),
        sa.CheckConstraint("revision > 0", name="paper_checkpoint_revision_positive"),
        sa.CheckConstraint("schema_version > 0", name="paper_checkpoint_schema_positive"),
        sa.CheckConstraint("length(event_key) > 0", name="paper_checkpoint_event_key_nonempty"),
        sa.CheckConstraint(
            "length(event_hash) = 64 AND event_hash = lower(event_hash) "
            "AND event_hash !~ '[^0-9a-f]'",
            name="paper_checkpoint_event_hash_sha256",
        ),
        sa.CheckConstraint(
            "length(policy_hash) = 64 AND policy_hash = lower(policy_hash) "
            "AND policy_hash !~ '[^0-9a-f]'",
            name="paper_checkpoint_policy_hash_sha256",
        ),
        sa.CheckConstraint(
            "length(snapshot_hash) = 64 AND snapshot_hash = lower(snapshot_hash) "
            "AND snapshot_hash !~ '[^0-9a-f]'",
            name="paper_checkpoint_snapshot_hash_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
            name="fk_paper_checkpoint_account",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paper_execution_checkpoint")),
        sa.UniqueConstraint(
            "workspace_id", "account_id", "revision", name="uq_paper_checkpoint_revision"
        ),
        sa.UniqueConstraint(
            "workspace_id", "account_id", "event_key", name="uq_paper_checkpoint_event"
        ),
    )
    op.create_index(
        "ix_paper_checkpoint_workspace_account_revision",
        "paper_execution_checkpoint",
        ["workspace_id", "account_id", "revision"],
    )
    policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    op.execute("ALTER TABLE paper_execution_checkpoint ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE paper_execution_checkpoint FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY workspace_isolation ON paper_execution_checkpoint "
        f"USING ({policy}) WITH CHECK ({policy})"
    )
    op.execute(
        "CREATE FUNCTION reject_paper_checkpoint_mutation() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'paper execution checkpoint is append only'; END $$"
    )
    op.execute(
        "CREATE TRIGGER paper_execution_checkpoint_immutable BEFORE UPDATE OR DELETE "
        "ON paper_execution_checkpoint FOR EACH ROW "
        "EXECUTE FUNCTION reject_paper_checkpoint_mutation()"
    )
    for role in ("crypidian_app", "crypidian_trading_runtime"):
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE paper_execution_checkpoint FROM {role}")
        op.execute("GRANT SELECT, INSERT ON TABLE paper_execution_checkpoint TO " + role)


def downgrade() -> None:
    op.execute("DROP TRIGGER paper_execution_checkpoint_immutable ON paper_execution_checkpoint")
    op.execute("DROP FUNCTION reject_paper_checkpoint_mutation()")
    op.execute("DROP POLICY workspace_isolation ON paper_execution_checkpoint")
    op.execute("ALTER TABLE paper_execution_checkpoint DISABLE ROW LEVEL SECURITY")
    for role in ("crypidian_app", "crypidian_trading_runtime"):
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE paper_execution_checkpoint FROM {role}")
    op.drop_index(
        "ix_paper_checkpoint_workspace_account_revision",
        table_name="paper_execution_checkpoint",
    )
    op.drop_table("paper_execution_checkpoint")

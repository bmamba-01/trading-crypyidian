"""Persist immutable initial funding for synthetic PAPER accounts.

Revision ID: 7a1d9e4c2b63
Revises: 6c2f91a4e7b3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a1d9e4c2b63"
down_revision: str | None = "6c2f91a4e7b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_account_seed",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("trading_account_id", sa.Uuid(), nullable=False),
        sa.Column("quote_asset", sa.String(length=16), nullable=False),
        sa.Column("initial_equity", sa.Numeric(38, 18), nullable=False),
        sa.Column("source_settings_version_id", sa.Uuid(), nullable=False),
        sa.Column("source_settings_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=240), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("initial_equity > 0", name="initial_equity_positive"),
        sa.CheckConstraint(
            "length(source_settings_hash) = 64 "
            "AND source_settings_hash = lower(source_settings_hash) "
            "AND source_settings_hash !~ '[^0-9a-f]'",
            name="source_settings_hash_sha256",
        ),
        sa.CheckConstraint(
            "quote_asset = upper(quote_asset) AND quote_asset ~ '^[A-Z0-9]{2,16}$'",
            name="quote_asset_valid",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "trading_account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
            name="fk_paper_account_seed_account",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_settings_version_id"],
            ["workspace_settings_version.workspace_id", "workspace_settings_version.id"],
            name="fk_paper_account_seed_settings",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paper_account_seed")),
        sa.UniqueConstraint(
            "workspace_id",
            "trading_account_id",
            name="uq_paper_account_seed_workspace_account",
        ),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_paper_account_seed_idempotency"
        ),
    )
    op.execute("ALTER TABLE paper_account_seed ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE paper_account_seed FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY workspace_isolation ON paper_account_seed "
        "USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)"
    )
    op.execute(
        "CREATE FUNCTION reject_paper_account_seed_mutation() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'paper account seed is immutable'; END $$"
    )
    op.execute(
        "CREATE TRIGGER paper_account_seed_immutable BEFORE UPDATE OR DELETE "
        "ON paper_account_seed FOR EACH ROW EXECUTE FUNCTION reject_paper_account_seed_mutation()"
    )
    op.execute("REVOKE ALL PRIVILEGES ON TABLE paper_account_seed FROM crypidian_app")
    op.execute("GRANT SELECT, INSERT ON TABLE paper_account_seed TO crypidian_app")
    op.execute("REVOKE ALL PRIVILEGES ON TABLE paper_account_seed FROM crypidian_trading_runtime")
    op.execute("GRANT SELECT, INSERT ON TABLE paper_account_seed TO crypidian_trading_runtime")


def downgrade() -> None:
    op.execute("DROP TRIGGER paper_account_seed_immutable ON paper_account_seed")
    op.execute("DROP FUNCTION reject_paper_account_seed_mutation()")
    op.execute("DROP POLICY workspace_isolation ON paper_account_seed")
    op.execute("ALTER TABLE paper_account_seed DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL PRIVILEGES ON TABLE paper_account_seed FROM crypidian_app")
    op.execute("REVOKE ALL PRIVILEGES ON TABLE paper_account_seed FROM crypidian_trading_runtime")
    op.drop_table("paper_account_seed")

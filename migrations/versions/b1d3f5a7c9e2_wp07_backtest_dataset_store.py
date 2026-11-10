"""WP-07C: lưu backtest dataset manifest bất biến theo workspace."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b1d3f5a7c9e2"
down_revision: str | None = "a2c4e6f8b0d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "backtest_dataset_manifest",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("interval", sa.String(8), nullable=False),
        sa.Column("warmup_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluation_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_fingerprint", sa.String(64), nullable=False),
        sa.Column("manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("canonical_manifest", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "dataset_hash ~ '^[0-9a-f]{64}$' AND policy_fingerprint ~ '^[0-9a-f]{64}$'",
            name="backtest_dataset_manifest_hashes_valid",
        ),
        sa.CheckConstraint(
            "status IN ('READY', 'INCOMPLETE')",
            name="backtest_dataset_manifest_status_valid",
        ),
        sa.CheckConstraint(
            "length(symbol) > 0 AND length(interval) > 0",
            name="backtest_dataset_manifest_identity_valid",
        ),
        sa.CheckConstraint(
            "warmup_start <= evaluation_start AND evaluation_start < range_end",
            name="backtest_dataset_manifest_range_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(manifest_json) = 'object'",
            name="backtest_dataset_manifest_json_object",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_dataset_manifest")),
        sa.UniqueConstraint(
            "workspace_id", "dataset_hash", name="uq_backtest_dataset_manifest_hash"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_backtest_dataset_manifest_workspace_id"),
    )
    op.create_index(
        "ix_backtest_dataset_manifest_workspace_created",
        "backtest_dataset_manifest",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_backtest_dataset_manifest_workspace_symbol_range",
        "backtest_dataset_manifest",
        ["workspace_id", "symbol", "range_end"],
    )
    op.execute('ALTER TABLE public."backtest_dataset_manifest" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE public."backtest_dataset_manifest" FORCE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY workspace_isolation ON public."backtest_dataset_manifest" '
        f"TO {ADMIN} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        """
        CREATE FUNCTION public.reject_backtest_dataset_manifest_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'WP07_BACKTEST_DATASET_MANIFEST_IMMUTABLE';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER backtest_dataset_manifest_immutable "
        'BEFORE UPDATE OR DELETE ON public."backtest_dataset_manifest" '
        "FOR EACH ROW EXECUTE FUNCTION public.reject_backtest_dataset_manifest_mutation()"
    )
    op.execute(
        f"REVOKE ALL PRIVILEGES ON TABLE public.backtest_dataset_manifest FROM PUBLIC, {ADMIN}"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.backtest_dataset_manifest TO {ADMIN}")


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM public.backtest_dataset_manifest) THEN "
        "RAISE EXCEPTION 'WP07_BACKTEST_DATASET_MANIFEST_DOWNGRADE_REQUIRES_EMPTY_TABLE'; "
        "END IF; END $$"
    )
    op.execute(
        'DROP TRIGGER backtest_dataset_manifest_immutable ON public."backtest_dataset_manifest"'
    )
    op.execute("DROP FUNCTION public.reject_backtest_dataset_manifest_mutation()")
    op.execute('DROP POLICY workspace_isolation ON public."backtest_dataset_manifest"')
    op.execute('ALTER TABLE public."backtest_dataset_manifest" DISABLE ROW LEVEL SECURITY')
    op.execute(
        f"REVOKE ALL PRIVILEGES ON TABLE public.backtest_dataset_manifest FROM PUBLIC, {ADMIN}"
    )
    op.drop_index(
        "ix_backtest_dataset_manifest_workspace_symbol_range",
        table_name="backtest_dataset_manifest",
    )
    op.drop_index(
        "ix_backtest_dataset_manifest_workspace_created",
        table_name="backtest_dataset_manifest",
    )
    op.drop_table("backtest_dataset_manifest")

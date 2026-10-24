"""Lưu feature snapshot bất biến theo workspace và point-in-time.

Revision ID: b61a35c79e20
Revises: f31b7c9a24d1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b61a35c79e20"
down_revision: str | None = "f31b7c9a24d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    jsonb_type = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "feature_snapshot",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_hash", sa.String(length=64), nullable=False),
        sa.Column("formula_hash", sa.String(length=64), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("input_bar_ids", jsonb_type, nullable=False),
        sa.Column("input_event_ids", jsonb_type, nullable=False),
        sa.Column("features", jsonb_type, nullable=False),
        sa.Column("warmup", jsonb_type, nullable=False),
        sa.Column("quality_flags", jsonb_type, nullable=False),
        sa.Column("data_freshness", sa.String(length=16), nullable=False),
        sa.Column("data_coverage", sa.Float(), nullable=False),
        sa.Column("regime", jsonb_type, nullable=False),
        sa.Column("is_usable", sa.Boolean(), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(snapshot_id) = 64", name="feature_snapshot_id_sha256"),
        sa.CheckConstraint("length(content_hash) = 64", name="feature_snapshot_content_sha256"),
        sa.CheckConstraint(
            "snapshot_id = content_hash", name="feature_snapshot_identity_matches_hash"
        ),
        sa.CheckConstraint("length(schema_hash) = 64", name="feature_snapshot_schema_sha256"),
        sa.CheckConstraint("length(formula_hash) = 64", name="feature_snapshot_formula_sha256"),
        sa.CheckConstraint("length(code_hash) = 64", name="feature_snapshot_code_sha256"),
        sa.CheckConstraint("as_of <= computed_at", name="feature_snapshot_time_order"),
        sa.CheckConstraint(
            "data_coverage >= 0 AND data_coverage <= 1", name="feature_snapshot_coverage"
        ),
        sa.CheckConstraint(
            "data_freshness IN ('FRESH', 'STALE', 'UNKNOWN')",
            name="feature_snapshot_freshness_valid",
        ),
        sa.CheckConstraint(
            "regime ->> 'label' IN ('TREND', 'RANGE', 'HIGH_VOLATILITY', 'UNKNOWN')",
            name="feature_snapshot_regime_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(input_bar_ids) = 'array' AND "
            "jsonb_typeof(input_event_ids) = 'array' AND "
            "jsonb_array_length(input_bar_ids) = jsonb_array_length(input_event_ids)",
            name="feature_snapshot_lineage_arrays_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_snapshot")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_feature_snapshot_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "snapshot_id", name="uq_feature_snapshot_workspace_snapshot"
        ),
        sa.UniqueConstraint(
            "workspace_id", "content_hash", name="uq_feature_snapshot_workspace_content_hash"
        ),
    )
    op.create_index(
        "ix_feature_snapshot_asof",
        "feature_snapshot",
        ["workspace_id", "symbol", "timeframe", "as_of", "computed_at"],
    )
    op.create_index(
        "ix_feature_snapshot_schema",
        "feature_snapshot",
        ["workspace_id", "schema_hash", "formula_hash", "code_hash"],
    )
    policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    op.execute('ALTER TABLE "feature_snapshot" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "feature_snapshot" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY workspace_isolation ON "feature_snapshot" '
        f"USING ({policy}) WITH CHECK ({policy})"
    )
    op.execute(
        """
        CREATE FUNCTION reject_feature_snapshot_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'feature snapshots are append-only';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER feature_snapshot_immutable BEFORE UPDATE OR DELETE ON feature_snapshot "
        "FOR EACH ROW EXECUTE FUNCTION reject_feature_snapshot_mutation()"
    )
    op.execute("GRANT SELECT, INSERT ON TABLE feature_snapshot TO crypidian_app")
    op.execute("GRANT SELECT, INSERT ON TABLE feature_snapshot TO crypidian_trading_runtime")


def downgrade() -> None:
    op.execute("DROP TRIGGER feature_snapshot_immutable ON feature_snapshot")
    op.execute("DROP FUNCTION reject_feature_snapshot_mutation()")
    op.execute('DROP POLICY workspace_isolation ON "feature_snapshot"')
    op.execute('ALTER TABLE "feature_snapshot" DISABLE ROW LEVEL SECURITY')
    op.execute("REVOKE ALL PRIVILEGES ON TABLE feature_snapshot FROM crypidian_app")
    op.execute("REVOKE ALL PRIVILEGES ON TABLE feature_snapshot FROM crypidian_trading_runtime")
    op.drop_index("ix_feature_snapshot_schema", table_name="feature_snapshot")
    op.drop_index("ix_feature_snapshot_asof", table_name="feature_snapshot")
    op.drop_table("feature_snapshot")

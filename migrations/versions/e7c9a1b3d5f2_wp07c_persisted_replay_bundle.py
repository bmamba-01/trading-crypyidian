"""WP-07C: lưu bundle replay và biên nhận quyền bất biến theo lần chạy."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7c9a1b3d5f2"
down_revision: str | None = "c9d7e5f3a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
TRAINER = "crypidian_trainer_worker"
_TABLES = ("backtest_dataset_replay_bundle", "backtest_run_dataset_rights")
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def _identity_columns() -> tuple[sa.Column, ...]:
    """Tạo cột định danh mới cho từng bảng append-only."""
    return (
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "backtest_dataset_replay_bundle",
        *_identity_columns(),
        sa.Column("bundle_hash", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("evaluation_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rights_provider", sa.String(64), nullable=False),
        sa.Column("rights_purpose", sa.String(64), nullable=False),
        sa.Column("policy_fingerprint", sa.String(64), nullable=False),
        sa.Column("bundle_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("canonical_bundle", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_dataset_replay_bundle")),
        sa.UniqueConstraint("workspace_id", "bundle_hash", name="uq_backtest_replay_bundle_hash"),
        sa.CheckConstraint(
            "bundle_hash ~ '^[0-9a-f]{64}$' AND policy_fingerprint ~ '^[0-9a-f]{64}$'",
            name="backtest_replay_bundle_hashes_valid",
        ),
        sa.CheckConstraint("schema_version = 1", name="backtest_replay_bundle_schema_valid"),
        sa.CheckConstraint(
            "length(symbol) > 0 AND length(rights_provider) > 0 AND length(rights_purpose) > 0",
            name="backtest_replay_bundle_scope_valid",
        ),
        sa.CheckConstraint(
            "evaluation_start < range_end", name="backtest_replay_bundle_range_valid"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(bundle_json) = 'object'", name="backtest_replay_bundle_json_object"
        ),
    )
    op.create_index(
        "ix_backtest_replay_bundle_workspace_created",
        "backtest_dataset_replay_bundle",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_backtest_replay_bundle_workspace_symbol_range",
        "backtest_dataset_replay_bundle",
        ["workspace_id", "symbol", "range_end"],
    )
    op.create_table(
        "backtest_run_dataset_rights",
        *_identity_columns(),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("bundle_hash", sa.String(64), nullable=False),
        sa.Column("child_dataset_hash", sa.String(64), nullable=False),
        sa.Column("interval", sa.String(8), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("terms_url", sa.Text(), nullable=False),
        sa.Column("policy_fingerprint", sa.String(64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("canonical_evidence", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_run_dataset_rights")),
        sa.UniqueConstraint(
            "workspace_id",
            "run_id",
            "child_dataset_hash",
            name="uq_backtest_run_dataset_rights_child",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["backtest_run.workspace_id", "backtest_run.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "bundle_hash ~ '^[0-9a-f]{64}$' AND child_dataset_hash ~ '^[0-9a-f]{64}$' "
            "AND policy_fingerprint ~ '^[0-9a-f]{64}$' AND evidence_hash ~ '^[0-9a-f]{64}$'",
            name="backtest_run_dataset_rights_hashes_valid",
        ),
        sa.CheckConstraint(
            "length(interval) > 0 AND length(provider) > 0 AND length(purpose) > 0 "
            "AND length(terms_url) > 0",
            name="backtest_run_dataset_rights_scope_valid",
        ),
        sa.CheckConstraint(
            "verified_at <= checked_at AND checked_at < valid_until",
            name="backtest_run_dataset_rights_lease_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_json) = 'object'", name="backtest_run_dataset_rights_json_object"
        ),
    )
    op.create_index(
        "ix_backtest_run_dataset_rights_workspace_created",
        "backtest_run_dataset_rights",
        ["workspace_id", "created_at"],
    )
    op.execute(
        """
        CREATE FUNCTION public.reject_wp07c_replay_bundle_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'WP07C_REPLAY_BUNDLE_OR_RIGHTS_IMMUTABLE';
        END;
        $$
        """
    )
    for table in _TABLES:
        roles = ADMIN if table == "backtest_dataset_replay_bundle" else f"{ADMIN}, {TRAINER}"
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY workspace_isolation ON public."{table}" '
            f"TO {roles} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
        )
        op.execute(
            f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON public."{table}" '
            "FOR EACH ROW EXECUTE FUNCTION public.reject_wp07c_replay_bundle_mutation()"
        )
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM PUBLIC, {ADMIN}, {TRAINER}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.backtest_dataset_replay_bundle TO {ADMIN}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.backtest_run_dataset_rights TO {TRAINER}")
    op.execute(f"GRANT SELECT ON TABLE public.backtest_run_dataset_rights TO {ADMIN}")


def downgrade() -> None:
    # Chặn ghi đồng thời và từ chối nếu RLS có thể che dữ liệu khỏi kiểm tra guard.
    op.execute(
        "LOCK TABLE public.backtest_dataset_replay_bundle, public.backtest_run_dataset_rights "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM public.backtest_dataset_replay_bundle) "
        "OR EXISTS (SELECT 1 FROM public.backtest_run_dataset_rights) THEN "
        "RAISE EXCEPTION 'WP07C_REPLAY_BUNDLE_DOWNGRADE_REQUIRES_EMPTY_TABLES'; "
        "END IF; END $$"
    )
    for table in reversed(_TABLES):
        op.execute(f'DROP TRIGGER {table}_immutable ON public."{table}"')
        op.execute(f'DROP POLICY workspace_isolation ON public."{table}"')
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM PUBLIC, {ADMIN}, {TRAINER}")
        op.drop_table(table)
    op.execute("DROP FUNCTION public.reject_wp07c_replay_bundle_mutation()")

"""WP06: phê duyệt split bất biến trước khi niêm phong manifest."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9b1d3e5a7c9"
down_revision: str | None = "f5a7c9e1b3d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "learning_split_approval"
ADMIN = "crypidian_admin_web"
APP = "crypidian_app"
TRAINER = "crypidian_trainer_worker"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
IMMUTABLE_FUNCTION = "public.reject_learning_split_approval_mutation"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_membership_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("split_policy_version", sa.String(128), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mfa_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approval_hash", sa.String(64), nullable=False),
        sa.Column("canonical_approval", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND approval_hash ~ '^[0-9a-f]{64}$'",
            name="learning_split_approval_hashes_valid",
        ),
        sa.CheckConstraint(
            "length(btrim(split_policy_version)) BETWEEN 1 AND 128",
            name="learning_split_approval_policy_present",
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) BETWEEN 12 AND 500",
            name="learning_split_approval_reason_valid",
        ),
        sa.CheckConstraint(
            "mfa_verified_at <= approved_at AND "
            "approved_at - mfa_verified_at <= interval '15 minutes'",
            name="learning_split_approval_mfa_recent",
        ),
        sa.CheckConstraint(
            "length(canonical_approval) > 0",
            name="learning_split_approval_canonical_present",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "actor_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_learning_split_approval_actor_membership",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_split_approval")),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_split_approval_idempotency"
        ),
        sa.UniqueConstraint(
            "workspace_id", "manifest_hash", name="uq_learning_split_approval_manifest"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_learning_split_approval_workspace_id"),
    )
    op.create_index(
        "ix_learning_split_approval_workspace_approved",
        TABLE,
        ["workspace_id", "approved_at"],
    )
    op.execute(f'ALTER TABLE public."{TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE public."{TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY learning_split_approval_workspace ON public."{TABLE}" '
        f"FOR ALL TO {ADMIN} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f"CREATE FUNCTION {IMMUTABLE_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'WP06_LEARNING_SPLIT_APPROVAL_IMMUTABLE'; END; $$"
    )
    op.execute(
        f"CREATE TRIGGER learning_split_approval_immutable BEFORE UPDATE OR DELETE "
        f'ON public."{TABLE}" FOR EACH ROW EXECUTE FUNCTION {IMMUTABLE_FUNCTION}()'
    )
    op.execute(
        f"REVOKE ALL PRIVILEGES ON TABLE public.{TABLE} FROM PUBLIC, {APP}, {ADMIN}, {TRAINER}"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{TABLE} TO {ADMIN}")
    op.execute(f"REVOKE ALL ON FUNCTION {IMMUTABLE_FUNCTION}() FROM PUBLIC")


def downgrade() -> None:
    op.execute(
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.{TABLE}) THEN "
        "RAISE EXCEPTION 'WP06_SPLIT_APPROVAL_DOWNGRADE_REQUIRES_EMPTY_TABLE'; "
        "END IF; END $$"
    )
    op.execute(f"DROP TRIGGER learning_split_approval_immutable ON public.{TABLE}")
    op.execute(f"DROP FUNCTION {IMMUTABLE_FUNCTION}()")
    op.execute(f"DROP POLICY learning_split_approval_workspace ON public.{TABLE}")
    op.execute(f"ALTER TABLE public.{TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{TABLE} FROM {ADMIN}")
    op.drop_index("ix_learning_split_approval_workspace_approved", table_name=TABLE)
    op.drop_table(TABLE)

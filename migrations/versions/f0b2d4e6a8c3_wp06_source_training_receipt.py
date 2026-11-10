"""WP06: receipt metadata quyền TRAINING cho source-dependent normalized rows."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f0b2d4e6a8c3"
down_revision: str | None = "e1a2c4e6f8b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BUILDER = "crypidian_candidate_builder"
APP = "crypidian_app"
ADMIN = "crypidian_admin_web"
TRAINER = "crypidian_trainer_worker"
INGESTION = "crypidian_ingestion_worker"
REPLAY_WRITER = "crypidian_replay_writer"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
RIGHTS_EVENT = "research.rights_changed.v1"
RIGHTS_CONSUMER = "wp06.dataset-rights-eligibility.v1"
TABLE = "learning_source_training_receipt"


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
        sa.Column("normalized_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("sample_id", sa.String(128), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("revision_id", sa.String(128), nullable=False),
        sa.Column("policy_id", sa.String(160), nullable=False),
        sa.Column("policy_sequence", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("rights_hash", sa.String(64), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("canonical_evidence", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND rights_hash ~ '^[0-9a-f]{64}$' AND evidence_hash ~ '^[0-9a-f]{64}$'",
            name="learning_source_training_receipt_hashes_valid",
        ),
        sa.CheckConstraint(
            "purpose = 'training' AND policy_sequence > 0",
            name="learning_source_training_receipt_scope_valid",
        ),
        sa.CheckConstraint(
            "length(sample_id) BETWEEN 1 AND 128 AND length(source_id) BETWEEN 1 AND 128 "
            "AND length(item_id) BETWEEN 1 AND 128 AND length(revision_id) BETWEEN 1 AND 128 "
            "AND length(policy_id) BETWEEN 1 AND 160",
            name="learning_source_training_receipt_identifiers_valid",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_hash", "fold_plan_hash", "normalized_dataset_id"],
            [
                "learning_normalized_dataset.workspace_id",
                "learning_normalized_dataset.manifest_hash",
                "learning_normalized_dataset.fold_plan_hash",
                "learning_normalized_dataset.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_source_training_receipt_dataset",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_source_training_receipt")),
        sa.UniqueConstraint(
            "workspace_id",
            "normalized_dataset_id",
            "sample_id",
            "source_id",
            "item_id",
            "revision_id",
            "policy_id",
            name="uq_learning_source_training_receipt_reference",
        ),
    )
    op.create_index(
        "ix_learning_source_training_receipt_manifest",
        TABLE,
        ["workspace_id", "manifest_hash", "fold_plan_hash"],
    )
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY candidate_builder_workspace_select ON public.{TABLE} "
        f"FOR SELECT TO {BUILDER} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY candidate_builder_workspace_insert ON public.{TABLE} "
        f"FOR INSERT TO {BUILDER} WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f"REVOKE ALL ON TABLE public.{TABLE} FROM PUBLIC, {APP}, {ADMIN}, {TRAINER}, "
        f"{INGESTION}, {REPLAY_WRITER}, {BUILDER}"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{TABLE} TO {BUILDER}")
    op.execute(
        f"CREATE TRIGGER {TABLE}_immutable BEFORE UPDATE OR DELETE ON public.{TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION public.reject_learning_normalized_mutation()"
    )

    for table in ("source_item_revision", "source_item_observation", "source_item_takedown"):
        op.execute(
            f"CREATE POLICY candidate_builder_workspace_select ON public.{table} "
            f"FOR SELECT TO {BUILDER} USING ({WORKSPACE})"
        )
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {BUILDER}")
    op.execute(
        "CREATE POLICY candidate_builder_workspace_select "
        "ON public.dataset_manifest_eligibility_event "
        f"FOR SELECT TO {BUILDER} USING ({WORKSPACE})"
    )
    op.execute("GRANT SELECT ON TABLE public.dataset_manifest_eligibility_event TO " + BUILDER)
    op.execute(
        f"CREATE POLICY candidate_builder_rights_event_select ON public.outbox_event "
        f"FOR SELECT TO {BUILDER} USING ({WORKSPACE} AND event_type = '{RIGHTS_EVENT}')"
    )
    op.execute(
        "GRANT SELECT (id, workspace_id, event_type, payload) ON TABLE public.outbox_event TO "
        + BUILDER
    )
    op.execute(
        "CREATE POLICY candidate_builder_rights_receipt_select ON public.consumer_receipt "
        f"FOR SELECT TO {BUILDER} USING ({WORKSPACE} AND consumer_name = '{RIGHTS_CONSUMER}')"
    )
    op.execute(
        "GRANT SELECT (workspace_id, outbox_event_id, consumer_name) "
        "ON TABLE public.consumer_receipt TO " + BUILDER
    )


def downgrade() -> None:
    op.execute(f"LOCK TABLE public.{TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.{TABLE}) THEN "
        "RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RECEIPT_DOWNGRADE_BLOCKED'; END IF; END $$"
    )
    for table, policy in (
        ("consumer_receipt", "candidate_builder_rights_receipt_select"),
        ("outbox_event", "candidate_builder_rights_event_select"),
        ("dataset_manifest_eligibility_event", "candidate_builder_workspace_select"),
    ):
        op.execute(f"DROP POLICY {policy} ON public.{table}")
    op.execute(
        "REVOKE SELECT (workspace_id, outbox_event_id, consumer_name) "
        "ON TABLE public.consumer_receipt FROM " + BUILDER
    )
    op.execute(
        "REVOKE SELECT (id, workspace_id, event_type, payload) ON TABLE public.outbox_event FROM "
        + BUILDER
    )
    for table in ("source_item_takedown", "source_item_observation", "source_item_revision"):
        op.execute(f"DROP POLICY candidate_builder_workspace_select ON public.{table}")
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {BUILDER}")
    op.execute(f"REVOKE SELECT ON TABLE public.dataset_manifest_eligibility_event FROM {BUILDER}")
    op.execute(f"DROP POLICY candidate_builder_workspace_insert ON public.{TABLE}")
    op.execute(f"DROP POLICY candidate_builder_workspace_select ON public.{TABLE}")
    op.execute(f"REVOKE ALL ON TABLE public.{TABLE} FROM {BUILDER}")
    op.drop_index("ix_learning_source_training_receipt_manifest", table_name=TABLE)
    op.drop_table(TABLE)

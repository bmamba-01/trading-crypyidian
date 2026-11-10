"""WP06G.2: append-only manifest invalidation và quyền live-check cho trainer."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2f4a6c8e0b1"
down_revision: str | None = "c1e3f5a7b9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP = "crypidian_app"
ADMIN = "crypidian_admin_web"
TRAINER = "crypidian_trainer_worker"
INGESTION = "crypidian_ingestion_worker"
_WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
_RIGHTS_EVENT = "research.rights_changed.v1"
_RIGHTS_CONSUMER = "wp06.dataset-rights-eligibility.v1"
_SOURCE_TABLES = (
    "source_policy_revision",
    "source_policy_observation",
    "source_item_revision",
    "source_item_observation",
    "source_item_takedown",
)


def upgrade() -> None:
    op.create_table(
        "dataset_manifest_eligibility_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_id", sa.Uuid(), nullable=False),
        sa.Column("sample_id", sa.Text(), nullable=False),
        sa.Column("outbox_event_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("revision_id", sa.String(128), nullable=False),
        sa.Column("policy_id", sa.String(160), nullable=False),
        sa.Column("policy_sequence", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("rights_hash", sa.String(64), nullable=False),
        sa.Column("change_type", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "item_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "revision_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "policy_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$'",
            name="dataset_manifest_eligibility_event_identifiers_valid",
        ),
        sa.CheckConstraint(
            "policy_sequence > 0", name="dataset_manifest_eligibility_sequence_valid"
        ),
        sa.CheckConstraint(
            "purpose = 'training'", name="dataset_manifest_eligibility_purpose_valid"
        ),
        sa.CheckConstraint(
            "rights_hash ~ '^[0-9a-f]{64}$'",
            name="dataset_manifest_eligibility_hash_valid",
        ),
        sa.CheckConstraint(
            "change_type IN ('policy_revision', 'policy_expired', 'item_revision', "
            "'item_tombstoned', 'item_takedown')",
            name="dataset_manifest_eligibility_change_type_valid",
        ),
        sa.CheckConstraint(
            "reason_code IN ('source_policy_changed', 'source_policy_expired', "
            "'source_item_superseded', 'source_item_tombstoned', 'source_item_takedown')",
            name="dataset_manifest_eligibility_reason_valid",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_id"],
            ["dataset_manifest.workspace_id", "dataset_manifest.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "outbox_event_id"],
            ["outbox_event.workspace_id", "outbox_event.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_manifest_eligibility_event")),
        sa.UniqueConstraint(
            "workspace_id",
            "outbox_event_id",
            "manifest_id",
            "sample_id",
            "source_id",
            "item_id",
            "revision_id",
            "policy_id",
            "policy_sequence",
            "purpose",
            name="uq_dataset_manifest_eligibility_event_identity",
        ),
    )
    op.create_index(
        "ix_dataset_manifest_eligibility_latest",
        "dataset_manifest_eligibility_event",
        ["workspace_id", "manifest_id", "sample_id", "created_at"],
    )
    op.create_index(
        "ix_dataset_manifest_eligibility_reference",
        "dataset_manifest_eligibility_event",
        [
            "workspace_id",
            "source_id",
            "item_id",
            "revision_id",
            "policy_id",
            "policy_sequence",
            "purpose",
        ],
    )

    op.execute('ALTER TABLE public."dataset_manifest_eligibility_event" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE public."dataset_manifest_eligibility_event" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY workspace_isolation ON public."dataset_manifest_eligibility_event" '
        f"TO {APP}, {ADMIN}, {TRAINER} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        "CREATE TRIGGER dataset_manifest_eligibility_append_only "
        'BEFORE UPDATE OR DELETE ON public."dataset_manifest_eligibility_event" '
        "FOR EACH ROW EXECUTE FUNCTION public.reject_dataset_manifest_mutation()"
    )
    op.execute(
        'REVOKE ALL PRIVILEGES ON TABLE public."dataset_manifest_eligibility_event" '
        f"FROM PUBLIC, {APP}, {ADMIN}, {TRAINER}, {INGESTION}, "
        "crypidian_trading_runtime, crypidian_execution_gateway"
    )
    op.execute(
        f'GRANT SELECT, INSERT ON TABLE public."dataset_manifest_eligibility_event" TO {APP}'
    )
    op.execute(
        f'GRANT SELECT ON TABLE public."dataset_manifest_eligibility_event" TO {ADMIN}, {TRAINER}'
    )

    # Cho consumer app truy vấn reverse index/manifest trong workspace hiện hành.
    op.execute('DROP POLICY workspace_isolation ON public."dataset_manifest"')
    op.execute(
        f'CREATE POLICY workspace_isolation ON public."dataset_manifest" '
        f"TO {APP}, {ADMIN}, {TRAINER} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(f'GRANT SELECT ON TABLE public."dataset_manifest" TO {APP}')
    op.execute(
        "GRANT SELECT (workspace_id, id, manifest_hash) "
        f'ON TABLE public."dataset_manifest" TO {TRAINER}'
    )
    op.execute('DROP POLICY workspace_isolation ON public."dataset_manifest_source_ref"')
    op.execute(
        f'CREATE POLICY workspace_isolation ON public."dataset_manifest_source_ref" '
        f"TO {APP}, {ADMIN}, {TRAINER} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(f'GRANT SELECT ON TABLE public."dataset_manifest_source_ref" TO {APP}')

    # Trainer chỉ nhìn rights event và receipt của consumer này, không đọc outbox chung.
    op.execute("DROP POLICY workspace_isolation ON public.outbox_event")
    op.execute(
        f"CREATE POLICY app_workspace_access ON public.outbox_event TO {APP} "
        f"USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY trainer_rights_event_read ON public.outbox_event FOR SELECT TO {TRAINER} "
        f"USING ({_WORKSPACE} AND event_type = '{_RIGHTS_EVENT}')"
    )
    op.execute(f"GRANT SELECT ON TABLE public.outbox_event TO {TRAINER}")
    op.execute("DROP POLICY workspace_isolation ON public.consumer_receipt")
    op.execute(
        f"CREATE POLICY app_workspace_access ON public.consumer_receipt TO {APP} "
        f"USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY trainer_rights_receipt_read ON public.consumer_receipt "
        f"FOR SELECT TO {TRAINER} "
        f"USING ({_WORKSPACE} AND consumer_name = '{_RIGHTS_CONSUMER}')"
    )
    op.execute(f"GRANT SELECT ON TABLE public.consumer_receipt TO {TRAINER}")

    # Live access gate cần đọc policy/revision/receipt/takedown, trainer không ghi các bảng này.
    for table_name in _SOURCE_TABLES:
        if table_name == "source_policy_revision":
            op.execute('DROP POLICY workspace_isolation ON public."source_policy_revision"')
            op.execute(
                'CREATE POLICY workspace_isolation ON public."source_policy_revision" '
                f"TO {ADMIN}, {INGESTION}, {TRAINER} USING ({_WORKSPACE}) "
                f"WITH CHECK ({_WORKSPACE})"
            )
        else:
            op.execute(f'DROP POLICY workspace_select ON public."{table_name}"')
            op.execute(
                f'CREATE POLICY workspace_select ON public."{table_name}" FOR SELECT '
                f"TO {ADMIN}, {INGESTION}, {TRAINER} USING ({_WORKSPACE})"
            )
        op.execute(f'GRANT SELECT ON TABLE public."{table_name}" TO {TRAINER}')


def downgrade() -> None:
    op.execute(
        'DO $$ BEGIN IF EXISTS (SELECT 1 FROM public."dataset_manifest_eligibility_event") '
        "THEN RAISE EXCEPTION 'WP06_RIGHTS_INVALIDATION_DOWNGRADE_REQUIRES_EMPTY_TABLE'; "
        "END IF; END $$"
    )
    op.execute(
        "DROP TRIGGER dataset_manifest_eligibility_append_only "
        'ON public."dataset_manifest_eligibility_event"'
    )
    op.execute('DROP POLICY workspace_isolation ON public."dataset_manifest_eligibility_event"')
    op.execute('ALTER TABLE public."dataset_manifest_eligibility_event" DISABLE ROW LEVEL SECURITY')
    op.execute(
        'REVOKE ALL PRIVILEGES ON TABLE public."dataset_manifest_eligibility_event" '
        f"FROM PUBLIC, {APP}, {ADMIN}, {TRAINER}"
    )
    op.drop_index(
        "ix_dataset_manifest_eligibility_reference",
        table_name="dataset_manifest_eligibility_event",
    )
    op.drop_index(
        "ix_dataset_manifest_eligibility_latest",
        table_name="dataset_manifest_eligibility_event",
    )
    op.drop_table("dataset_manifest_eligibility_event")

    for table_name in _SOURCE_TABLES:
        if table_name == "source_policy_revision":
            op.execute('DROP POLICY workspace_isolation ON public."source_policy_revision"')
            op.execute(
                'CREATE POLICY workspace_isolation ON public."source_policy_revision" '
                f"USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
            )
        else:
            op.execute(f'DROP POLICY workspace_select ON public."{table_name}"')
            op.execute(
                f'CREATE POLICY workspace_select ON public."{table_name}" FOR SELECT '
                f"TO {ADMIN}, {INGESTION} USING ({_WORKSPACE})"
            )
        op.execute(f'REVOKE SELECT ON TABLE public."{table_name}" FROM {TRAINER}')

    op.execute("DROP POLICY trainer_rights_receipt_read ON public.consumer_receipt")
    op.execute("DROP POLICY app_workspace_access ON public.consumer_receipt")
    op.execute(
        "CREATE POLICY workspace_isolation ON public.consumer_receipt "
        f"USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute("REVOKE SELECT ON TABLE public.consumer_receipt FROM crypidian_trainer_worker")
    op.execute("DROP POLICY trainer_rights_event_read ON public.outbox_event")
    op.execute("DROP POLICY app_workspace_access ON public.outbox_event")
    op.execute(
        "CREATE POLICY workspace_isolation ON public.outbox_event "
        f"USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute("REVOKE SELECT ON TABLE public.outbox_event FROM crypidian_trainer_worker")

    op.execute('DROP POLICY workspace_isolation ON public."dataset_manifest_source_ref"')
    op.execute(
        'CREATE POLICY workspace_isolation ON public."dataset_manifest_source_ref" '
        f"TO {ADMIN}, {TRAINER} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute('REVOKE SELECT ON TABLE public."dataset_manifest_source_ref" FROM crypidian_app')
    op.execute('DROP POLICY workspace_isolation ON public."dataset_manifest"')
    op.execute(
        'CREATE POLICY workspace_isolation ON public."dataset_manifest" '
        f"TO {ADMIN}, {TRAINER} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute('REVOKE SELECT ON TABLE public."dataset_manifest" FROM crypidian_app')
    op.execute(
        "REVOKE SELECT (workspace_id, id, manifest_hash) "
        'ON TABLE public."dataset_manifest" FROM crypidian_trainer_worker'
    )

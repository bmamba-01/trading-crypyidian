"""WP06.3E: receipt giao dịch và payload gateway một lần cho evaluator."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8b0d2e4a6c8"
down_revision: str | None = "f4b6d8e0a2c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVALUATOR = "crypidian_learning_evaluator"
READER = "crypidian_learning_evaluation_reader"
ADMIN = "crypidian_admin_web"
APP = "crypidian_app"
TRAINER = "crypidian_trainer_worker"
BUILDER = "crypidian_candidate_builder"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
OPEN_FUNCTION = "public.wp06_open_learning_evaluation(uuid, text, uuid, text)"
OUTCOME_FUNCTION = (
    "public.wp06_read_learning_fold_outcomes(uuid, uuid, text, text, text, text, uuid[])"
)
OPEN_FUNCTION_NAME = "public.wp06_open_learning_evaluation"
OUTCOME_FUNCTION_NAME = "public.wp06_read_learning_fold_outcomes"
MANIFEST_METADATA_COLUMNS = (
    "workspace_id",
    "id",
    "created_at",
    "manifest_hash",
    "query_hash",
    "manifest_version",
    "data_start",
    "data_end",
    "as_of",
    "feature_schema_version",
    "feature_version",
    "label_version",
    "sample_schema_version",
    "split_policy_version",
    "sample_count",
    "exclusion_count",
    "holdout_sealed_at",
    "retention_policy_ref",
)
READER_TABLES = (
    "dataset_manifest",
    "learning_split_approval",
    "learning_normalized_dataset",
    "learning_normalized_sample",
    "learning_fold_assignment",
    "backtest_run",
    "backtest_trial",
    "backtest_report",
    "backtest_trade_outcome",
)


def _reader_policy(table: str) -> None:
    op.execute(
        f"CREATE POLICY learning_evaluation_reader_select ON public.{table} "
        f"FOR SELECT TO {READER} USING ({WORKSPACE})"
    )


def upgrade() -> None:
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{READER}') THEN
            CREATE ROLE {READER} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSIF EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE rolname='{READER}' AND
            (rolcanlogin OR rolsuper OR rolbypassrls OR rolinherit OR rolcreatedb OR
            rolcreaterole OR rolreplication OR EXISTS (SELECT 1 FROM
                pg_catalog.pg_auth_members m WHERE m.roleid=r.oid OR m.member=r.oid))) THEN
            RAISE EXCEPTION 'WP06_EVALUATION_READER_ROLE_UNSAFE';
        END IF;
    END $$""")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {READER}, {EVALUATOR}")

    op.create_table(
        "learning_evaluation_access_receipt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("split_policy_version", sa.String(128), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("approval_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("actor_principal", sa.String(80), nullable=False),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND approval_hash ~ '^[0-9a-f]{64}$' AND "
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="learning_evaluation_access_receipt_hashes_valid",
        ),
        sa.CheckConstraint(
            "purpose = 'diagnostic-evaluation' AND "
            "actor_principal = 'crypidian_learning_evaluator'",
            name="learning_evaluation_access_receipt_actor_purpose_valid",
        ),
        sa.CheckConstraint(
            "length(split_policy_version) BETWEEN 1 AND 128",
            name="learning_evaluation_access_receipt_policy_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_hash"],
            ["dataset_manifest.workspace_id", "dataset_manifest.manifest_hash"],
            name="fk_learning_evaluation_access_receipt_manifest",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_evaluation_access_receipt")),
        sa.UniqueConstraint(
            "workspace_id",
            "evaluation_run_id",
            name="uq_learning_evaluation_access_receipt_run",
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_learning_evaluation_access_receipt_workspace"
        ),
    )
    op.create_index(
        "ix_learning_evaluation_access_receipt_manifest",
        "learning_evaluation_access_receipt",
        ["workspace_id", "manifest_hash", "created_at"],
    )
    op.create_table(
        "learning_evaluation_access_fold",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("fold_id", sa.String(64), nullable=False),
        sa.Column("payload_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "fold_id ~ '^[0-9a-f]{64}$' AND payload_count > 0",
            name="learning_evaluation_access_fold_values_valid",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "receipt_id"],
            [
                "learning_evaluation_access_receipt.workspace_id",
                "learning_evaluation_access_receipt.id",
            ],
            name="fk_learning_evaluation_access_fold_receipt",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_evaluation_access_fold")),
        sa.UniqueConstraint(
            "workspace_id",
            "receipt_id",
            "fold_id",
            name="uq_learning_evaluation_access_fold_once",
        ),
    )

    op.execute("""CREATE FUNCTION public.reject_learning_evaluation_access_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$ BEGIN
        RAISE EXCEPTION 'WP06_EVALUATION_ACCESS_RECEIPT_IMMUTABLE'; END $$""")
    op.execute(
        "REVOKE ALL ON FUNCTION public.reject_learning_evaluation_access_mutation() FROM PUBLIC"
    )

    for table in (
        "learning_evaluation_access_receipt",
        "learning_evaluation_access_fold",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_reader_select ON public.{table} "
            f"FOR SELECT TO {READER} USING ({WORKSPACE})"
        )
        op.execute(
            f"CREATE POLICY {table}_reader_insert ON public.{table} "
            f"FOR INSERT TO {READER} WITH CHECK ({WORKSPACE})"
        )
        op.execute(
            f"CREATE POLICY {table}_evaluator_read ON public.{table} "
            f"FOR SELECT TO {EVALUATOR} USING ({WORKSPACE})"
        )
        op.execute(
            f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, {ADMIN}, {APP}, "
            f"{TRAINER}, {BUILDER}, {EVALUATOR}, {READER}"
        )
        op.execute(f"GRANT SELECT, INSERT ON TABLE public.{table} TO {READER}")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {EVALUATOR}")
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.reject_learning_evaluation_access_mutation()"
        )

    for table in READER_TABLES:
        _reader_policy(table)
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {READER}")

    # Evaluator nhận metadata cần cho lineage, payload chỉ qua SECURITY DEFINER.
    op.execute(f'REVOKE SELECT ON TABLE public."dataset_manifest" FROM {EVALUATOR}')
    op.execute(
        f"GRANT SELECT ({', '.join(MANIFEST_METADATA_COLUMNS)}) "
        f'ON TABLE public."dataset_manifest" TO {EVALUATOR}'
    )
    op.execute(f"REVOKE SELECT ON TABLE public.backtest_trade_outcome FROM {EVALUATOR}")
    op.execute(
        f"REVOKE SELECT (workspace_id, id, trial_record_id, run_id, manifest_hash, ordinal, "
        f"outcome_hash, trade_id, canonical_json, outcome) "
        f"ON TABLE public.backtest_trade_outcome FROM {EVALUATOR}"
    )
    op.execute(
        "GRANT SELECT (workspace_id, id, trial_record_id, ordinal) "
        f"ON TABLE public.backtest_trade_outcome TO {EVALUATOR}"
    )
    op.execute(
        f"REVOKE SELECT ON TABLE public.backtest_report, public.backtest_scenario FROM {EVALUATOR}"
    )
    op.execute(
        "GRANT SELECT (workspace_id, run_id, trial_record_id, manifest_hash) "
        f"ON TABLE public.backtest_report TO {EVALUATOR}"
    )

    op.execute(f"GRANT CREATE ON SCHEMA public TO {READER}")
    op.execute(f"""CREATE FUNCTION {OPEN_FUNCTION_NAME}(
        target_workspace uuid, target_manifest_hash text, target_evaluation_run_id uuid,
        target_request_fingerprint text)
        RETURNS TABLE(receipt_id uuid, manifest jsonb, canonical_manifest text,
                      split_policy_version text, fold_plan_hash text, approval_hash text)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            stored_manifest jsonb;
            stored_canonical text;
            stored_policy text;
            stored_fold_hash text;
            stored_approval_hash text;
            new_receipt_id uuid;
        BEGIN
            IF session_user <> '{EVALUATOR}' THEN
                RAISE EXCEPTION 'WP06_EVALUATOR_CALLER_DENIED';
            END IF;
            IF target_workspace IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_EVALUATOR_WORKSPACE_DENIED';
            END IF;
            IF target_request_fingerprint !~ '^[0-9a-f]{{64}}$' THEN
                RAISE EXCEPTION 'WP06_EVALUATOR_REQUEST_FINGERPRINT_INVALID';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'wp06-evaluation:' || target_workspace::text || ':' || target_manifest_hash, 0));
            SELECT m.manifest, m.canonical_manifest, m.split_policy_version
              INTO stored_manifest, stored_canonical, stored_policy
              FROM public.dataset_manifest AS m
             WHERE m.workspace_id = target_workspace AND m.manifest_hash = target_manifest_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_EVALUATION_MANIFEST_NOT_FOUND';
            END IF;
            IF EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_array_elements(
                    COALESCE(stored_manifest->'samples', '[]'::jsonb)
                ) AS sample(value)
                WHERE COALESCE((sample.value->>'source_dependent')::boolean, false)
                   OR pg_catalog.jsonb_array_length(
                       COALESCE(sample.value->'source_references', '[]'::jsonb)) > 0
            ) THEN
                RAISE EXCEPTION 'WP06_EVALUATION_SOURCE_RIGHTS_FAIL_CLOSED';
            END IF;
            SELECT approval.fold_plan_hash, approval.approval_hash
              INTO stored_fold_hash, stored_approval_hash
              FROM public.learning_split_approval AS approval
             WHERE approval.workspace_id = target_workspace
               AND approval.manifest_hash = target_manifest_hash
               AND approval.split_policy_version = stored_policy;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_EVALUATION_APPROVAL_NOT_FOUND';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.learning_normalized_dataset AS dataset
                 WHERE dataset.workspace_id = target_workspace
                   AND dataset.manifest_hash = target_manifest_hash
                   AND dataset.split_policy_version = stored_policy
                   AND dataset.fold_plan_hash = stored_fold_hash
                   AND dataset.approval_hash = stored_approval_hash
            ) THEN
                RAISE EXCEPTION 'WP06_EVALUATION_NORMALIZED_DATASET_NOT_FOUND';
            END IF;
            INSERT INTO public.learning_evaluation_access_receipt(
                id, workspace_id, evaluation_run_id, manifest_hash, split_policy_version,
                fold_plan_hash, approval_hash, request_fingerprint, purpose, actor_principal)
            VALUES (
                pg_catalog.gen_random_uuid(), target_workspace, target_evaluation_run_id,
                target_manifest_hash, stored_policy, stored_fold_hash, stored_approval_hash,
                target_request_fingerprint, 'diagnostic-evaluation', '{EVALUATOR}')
            RETURNING id INTO new_receipt_id;
            RETURN QUERY SELECT new_receipt_id, stored_manifest, stored_canonical, stored_policy,
                                stored_fold_hash, stored_approval_hash;
        END $$""")
    op.execute(f"ALTER FUNCTION {OPEN_FUNCTION} OWNER TO {READER}")
    op.execute(f"""CREATE FUNCTION {OUTCOME_FUNCTION_NAME}(
        target_receipt_id uuid, target_workspace uuid, target_manifest_hash text,
        target_split_policy_version text, target_fold_plan_hash text, target_fold_id text,
        target_outcome_ids uuid[])
        RETURNS SETOF public.backtest_trade_outcome
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            receipt public.learning_evaluation_access_receipt%ROWTYPE;
            assigned_ids uuid[];
        BEGIN
            IF session_user <> '{EVALUATOR}' THEN
                RAISE EXCEPTION 'WP06_EVALUATOR_CALLER_DENIED';
            END IF;
            IF target_workspace IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_EVALUATOR_WORKSPACE_DENIED';
            END IF;
            SELECT access.* INTO receipt
              FROM public.learning_evaluation_access_receipt AS access
             WHERE access.id = target_receipt_id
               AND access.workspace_id = target_workspace
               AND access.manifest_hash = target_manifest_hash
               AND access.split_policy_version = target_split_policy_version
               AND access.fold_plan_hash = target_fold_plan_hash
               AND access.actor_principal = '{EVALUATOR}'
               AND access.purpose = 'diagnostic-evaluation';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_EVALUATION_RECEIPT_NOT_FOUND';
            END IF;
            SELECT COALESCE(pg_catalog.array_agg(sample.outcome_id::uuid
                       ORDER BY sample.outcome_id::uuid), ARRAY[]::uuid[])
              INTO assigned_ids
              FROM public.learning_fold_assignment AS assignment
              JOIN public.learning_normalized_sample AS sample
                ON sample.workspace_id = assignment.workspace_id
               AND sample.manifest_hash = assignment.manifest_hash
               AND sample.fold_plan_hash = assignment.fold_plan_hash
               AND sample.sample_id = assignment.sample_id
             WHERE assignment.workspace_id = target_workspace
               AND assignment.manifest_hash = target_manifest_hash
               AND assignment.fold_plan_hash = target_fold_plan_hash
               AND assignment.fold_id = target_fold_id
               AND assignment.role IN ('train', 'validation', 'test');
            IF pg_catalog.cardinality(assigned_ids) = 0
               OR target_outcome_ids IS DISTINCT FROM assigned_ids THEN
                RAISE EXCEPTION 'WP06_EVALUATION_FOLD_MEMBERSHIP_MISMATCH';
            END IF;
            INSERT INTO public.learning_evaluation_access_fold(
                id, workspace_id, receipt_id, fold_id, payload_count)
            VALUES (pg_catalog.gen_random_uuid(), target_workspace, target_receipt_id,
                    target_fold_id, pg_catalog.cardinality(assigned_ids));
            RETURN QUERY
                SELECT outcome.* FROM public.backtest_trade_outcome AS outcome
                 WHERE outcome.workspace_id = target_workspace
                   AND outcome.id = ANY(assigned_ids)
                 ORDER BY outcome.ordinal;
        END $$""")
    op.execute(f"ALTER FUNCTION {OUTCOME_FUNCTION} OWNER TO {READER}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {READER}")
    op.execute(f"REVOKE ALL ON FUNCTION {OPEN_FUNCTION} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {OUTCOME_FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {OPEN_FUNCTION} TO {EVALUATOR}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {OUTCOME_FUNCTION} TO {EVALUATOR}")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.learning_evaluation_access_fold)
           OR EXISTS (SELECT 1 FROM public.learning_evaluation_access_receipt) THEN
            RAISE EXCEPTION 'WP06_EVALUATION_ACCESS_RECEIPT_DOWNGRADE_REQUIRES_EMPTY_TABLES';
        END IF;
    END $$""")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {OUTCOME_FUNCTION} FROM {EVALUATOR}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {OPEN_FUNCTION} FROM {EVALUATOR}")
    op.execute(f"DROP FUNCTION {OUTCOME_FUNCTION}")
    op.execute(f"DROP FUNCTION {OPEN_FUNCTION}")
    for table in ("learning_evaluation_access_fold", "learning_evaluation_access_receipt"):
        op.execute(f"DROP TRIGGER {table}_immutable ON public.{table}")
        op.execute(f"DROP POLICY {table}_evaluator_read ON public.{table}")
        op.execute(f"DROP POLICY {table}_reader_insert ON public.{table}")
        op.execute(f"DROP POLICY {table}_reader_select ON public.{table}")
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM {EVALUATOR}, {READER}")
    op.execute("DROP FUNCTION public.reject_learning_evaluation_access_mutation()")
    op.drop_table("learning_evaluation_access_fold")
    op.drop_index(
        "ix_learning_evaluation_access_receipt_manifest",
        table_name="learning_evaluation_access_receipt",
    )
    op.drop_table("learning_evaluation_access_receipt")
    for table in READER_TABLES:
        op.execute(f"DROP POLICY learning_evaluation_reader_select ON public.{table}")
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {READER}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {READER}, {EVALUATOR}")
    op.execute(f"DROP ROLE {READER}")
    op.execute(f"GRANT SELECT ON TABLE public.dataset_manifest TO {EVALUATOR}")
    op.execute(f"GRANT SELECT ON TABLE public.backtest_trade_outcome TO {EVALUATOR}")
    op.execute(
        f"GRANT SELECT ON TABLE public.backtest_run, public.backtest_trial, "
        f"public.backtest_report, public.backtest_scenario TO {EVALUATOR}"
    )

"""WP06.3F: cấp batch train/validation qua attempt và receipt một lần."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c4e6f8a0d3"
down_revision: str | None = "a0c2e4f6b8d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRAINER = "crypidian_trainer_worker"
ADMIN = "crypidian_admin_web"
READER = "crypidian_learning_batch_reader"
RECEIPT = "learning_training_access_receipt"
FUNCTION = "public.wp06_claim_learning_training_batch(uuid, uuid)"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{READER}') THEN
            CREATE ROLE {READER} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSIF EXISTS (SELECT 1 FROM pg_catalog.pg_roles AS role WHERE role.rolname='{READER}'
            AND (role.rolcanlogin OR role.rolsuper OR role.rolbypassrls OR role.rolinherit
                 OR role.rolcreatedb OR role.rolcreaterole OR role.rolreplication
                 OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                            WHERE membership.roleid=role.oid OR membership.member=role.oid))) THEN
            RAISE EXCEPTION 'WP06_TRAINER_BATCH_READER_ROLE_UNSAFE';
        END IF;
    END $$""")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {READER}")
    op.create_table(
        RECEIPT,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("fold_id", sa.String(64), nullable=False),
        sa.Column("approval_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("actor_principal", sa.String(80), nullable=False),
        sa.Column("training_samples", sa.Integer(), nullable=False),
        sa.Column("validation_samples", sa.Integer(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_id ~ '^[0-9a-f]{64}$' AND approval_hash ~ '^[0-9a-f]{64}$' "
            "AND request_fingerprint ~ '^[0-9a-f]{64}$' AND profile_hash ~ '^[0-9a-f]{64}$'",
            name="learning_training_access_receipt_hashes_valid",
        ),
        sa.CheckConstraint(
            "purpose = 'trainer-input' AND actor_principal = 'crypidian_trainer_worker'",
            name="learning_training_access_receipt_actor_purpose_valid",
        ),
        sa.CheckConstraint(
            "training_samples > 0 AND validation_samples > 0 "
            "AND sample_count = training_samples + validation_samples",
            name="learning_training_access_receipt_counts_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_training_attempt.workspace_id", "learning_training_attempt.id"],
            name="fk_learning_training_access_receipt_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "normalized_dataset_id", "fold_id"],
            [
                "learning_fold_resource_profile.workspace_id",
                "learning_fold_resource_profile.normalized_dataset_id",
                "learning_fold_resource_profile.fold_id",
            ],
            name="fk_learning_training_access_receipt_profile",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_training_access_receipt")),
        sa.UniqueConstraint(
            "workspace_id",
            "attempt_id",
            name="uq_learning_training_access_receipt_attempt",
        ),
    )
    op.create_index(
        "ix_learning_training_access_receipt_manifest",
        RECEIPT,
        ["workspace_id", "manifest_hash", "fold_plan_hash"],
    )
    op.execute("""
        CREATE FUNCTION public.reject_learning_training_access_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN RAISE EXCEPTION 'WP06_TRAINING_ACCESS_RECEIPT_APPEND_ONLY'; END $$
    """)
    op.execute(
        f"CREATE TRIGGER {RECEIPT}_immutable BEFORE UPDATE OR DELETE ON public.{RECEIPT} "
        "FOR EACH ROW EXECUTE FUNCTION public.reject_learning_training_access_mutation()"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.reject_learning_training_access_mutation() FROM PUBLIC"
    )

    op.execute(f"ALTER TABLE public.{RECEIPT} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{RECEIPT} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"REVOKE ALL PRIVILEGES ON TABLE public.{RECEIPT} FROM PUBLIC, {TRAINER}, {ADMIN}, {READER}"
    )
    op.execute(
        f"CREATE POLICY learning_training_access_admin_read ON public.{RECEIPT} "
        f"FOR SELECT TO {ADMIN} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_training_access_reader_scope ON public.{RECEIPT} "
        f"FOR ALL TO {READER} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(f"GRANT SELECT ON TABLE public.{RECEIPT} TO {ADMIN}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{RECEIPT} TO {READER}")

    reader_tables = (
        "dataset_manifest",
        "learning_split_approval",
        "learning_normalized_dataset",
        "learning_normalized_sample",
        "learning_fold_assignment",
        "learning_fold_resource_profile",
        "learning_training_attempt",
        "learning_attempt_event",
    )
    for table in reader_tables:
        if table == "learning_fold_assignment":
            continue
        op.execute(
            f"CREATE POLICY learning_training_batch_reader_scope ON public.{table} "
            f"FOR SELECT TO {READER} USING ({WORKSPACE})"
        )
    op.execute(
        "CREATE POLICY learning_training_batch_reader_roles ON public.learning_fold_assignment "
        f"FOR SELECT TO {READER} USING ({WORKSPACE} AND role IN ('train', 'validation'))"
    )
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {READER}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {READER}")
    op.execute(f"GRANT CREATE ON SCHEMA public TO {READER}")
    op.execute("""
        GRANT SELECT (workspace_id, manifest_hash, manifest)
            ON public.dataset_manifest TO crypidian_learning_batch_reader;
        GRANT SELECT (workspace_id, manifest_hash, split_policy_version, fold_plan_hash,
                      approval_hash)
            ON public.learning_split_approval TO crypidian_learning_batch_reader;
        GRANT SELECT (workspace_id, manifest_hash, split_policy_version, fold_plan_hash,
                      id, approval_hash)
            ON public.learning_normalized_dataset TO crypidian_learning_batch_reader;
        GRANT SELECT (workspace_id, manifest_hash, fold_plan_hash, sample_id, outcome_id,
                      outcome_hash, feature_snapshot_hash, feature_schema_hash, label_hash,
                      label_value, features, sample_hash)
            ON public.learning_normalized_sample TO crypidian_learning_batch_reader;
        GRANT SELECT (workspace_id, manifest_hash, fold_plan_hash, fold_id, sample_id, role)
            ON public.learning_fold_assignment TO crypidian_learning_batch_reader;
        GRANT SELECT (workspace_id, normalized_dataset_id, manifest_hash, fold_plan_hash,
                      fold_id, training_samples, validation_samples, feature_count,
                      feature_schema_hash, profile_hash)
            ON public.learning_fold_resource_profile TO crypidian_learning_batch_reader;
        GRANT SELECT ON public.learning_training_attempt TO crypidian_learning_batch_reader;
        GRANT SELECT (workspace_id, attempt_id, sequence, state, actor_principal)
            ON public.learning_attempt_event TO crypidian_learning_batch_reader;
    """)

    op.execute(f"""
        CREATE FUNCTION public.wp06_claim_learning_training_batch(
            target_workspace uuid, target_attempt_id uuid
        ) RETURNS TABLE (
            attempt_id uuid, workspace_id uuid, normalized_dataset_id uuid,
            manifest_hash text, split_policy_version text, fold_plan_hash text,
            approval_hash text, fold_id text, seed integer, trainer_version text,
            dependency_fingerprint text, sample_id text, role text, outcome_id text,
            outcome_hash text, feature_snapshot_hash text, feature_schema_hash text,
            label_hash text, label_value integer, features jsonb, sample_hash text
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            attempt_row public.learning_training_attempt%ROWTYPE;
            stored_manifest jsonb;
            latest_state text;
            latest_actor text;
            latest_sequence integer;
            approved_hash text;
            stored_dataset_id uuid;
            train_count integer;
            validation_count integer;
            invalid_count integer;
            profile_train_count integer;
            profile_validation_count integer;
            profile_feature_count integer;
            profile_schema_hash text;
            profile_hash text;
        BEGIN
            IF session_user <> 'crypidian_trainer_worker' THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_CALLER_DENIED';
            END IF;
            IF target_workspace IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_WORKSPACE_DENIED';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(target_attempt_id::text, 0)
            );
            SELECT attempt.* INTO attempt_row
              FROM public.learning_training_attempt AS attempt
             WHERE attempt.workspace_id = target_workspace AND attempt.id = target_attempt_id
            ;
            IF NOT FOUND OR attempt_row.initial_state <> 'queued' THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_ATTEMPT_NOT_FOUND';
            END IF;
            SELECT event.state, event.sequence, event.actor_principal
              INTO latest_state, latest_sequence, latest_actor
              FROM public.learning_attempt_event AS event
             WHERE event.workspace_id = target_workspace AND event.attempt_id = target_attempt_id
             ORDER BY event.sequence DESC LIMIT 1;
            IF NOT FOUND OR latest_state <> 'running' OR latest_sequence <> 2
                OR latest_actor <> 'crypidian_trainer_worker' THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_ATTEMPT_NOT_RUNNING';
            END IF;
            IF EXISTS (SELECT 1 FROM public.{RECEIPT} AS receipt
                WHERE receipt.workspace_id = target_workspace
                  AND receipt.attempt_id = target_attempt_id) THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_ALREADY_CONSUMED';
            END IF;
            SELECT manifest_record.manifest INTO stored_manifest
              FROM public.dataset_manifest AS manifest_record
             WHERE manifest_record.workspace_id = target_workspace
               AND manifest_record.manifest_hash = attempt_row.manifest_hash;
            IF NOT FOUND OR pg_catalog.jsonb_typeof(stored_manifest->'samples')
                    IS DISTINCT FROM 'array'
                OR EXISTS (
                    SELECT 1
                      FROM pg_catalog.jsonb_array_elements(
                          stored_manifest->'samples'
                      ) AS sample(value)
                    WHERE COALESCE((sample.value->>'source_dependent')::boolean, true)
                       OR pg_catalog.jsonb_array_length(
                            COALESCE(sample.value->'source_references', '[]'::jsonb)) <> 0
                       OR pg_catalog.jsonb_array_length(
                            COALESCE(sample.value->'source_rights_hashes', '[]'::jsonb)) <> 0
                       OR pg_catalog.jsonb_array_length(
                            COALESCE(sample.value->'source_revision_refs', '[]'::jsonb)) <> 0
                ) THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_SOURCE_RIGHTS_FAIL_CLOSED';
            END IF;
            SELECT approval.approval_hash INTO approved_hash
              FROM public.learning_split_approval AS approval
             WHERE approval.workspace_id = target_workspace
               AND approval.manifest_hash = attempt_row.manifest_hash
               AND approval.split_policy_version = attempt_row.split_policy_version
               AND approval.fold_plan_hash = attempt_row.fold_plan_hash;
            IF NOT FOUND OR approved_hash <> attempt_row.approval_hash THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_APPROVAL_MISMATCH';
            END IF;
            SELECT dataset.id INTO stored_dataset_id
              FROM public.learning_normalized_dataset AS dataset
             WHERE dataset.workspace_id = target_workspace
               AND dataset.id = attempt_row.normalized_dataset_id
               AND dataset.manifest_hash = attempt_row.manifest_hash
               AND dataset.split_policy_version = attempt_row.split_policy_version
               AND dataset.fold_plan_hash = attempt_row.fold_plan_hash
               AND dataset.approval_hash = attempt_row.approval_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_DATASET_MISMATCH';
            END IF;
            SELECT profile.training_samples, profile.validation_samples, profile.feature_count,
                   profile.feature_schema_hash, profile.profile_hash
              INTO profile_train_count, profile_validation_count, profile_feature_count,
                   profile_schema_hash, profile_hash
              FROM public.learning_fold_resource_profile AS profile
             WHERE profile.workspace_id = target_workspace
               AND profile.normalized_dataset_id = stored_dataset_id
               AND profile.manifest_hash = attempt_row.manifest_hash
               AND profile.fold_plan_hash = attempt_row.fold_plan_hash
               AND profile.fold_id = attempt_row.fold_id;
            IF NOT FOUND OR profile_train_count
                    <> (attempt_row.resource_request->>'training_samples')::integer
                OR profile_validation_count
                    <> (attempt_row.resource_request->>'validation_samples')::integer
                OR profile_feature_count
                    <> (attempt_row.resource_request->>'feature_count')::integer THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_PROFILE_MISMATCH';
            END IF;
            SELECT count(*) FILTER (WHERE assignment.role = 'train')::integer,
                   count(*) FILTER (WHERE assignment.role = 'validation')::integer,
                   count(*) FILTER (
                       WHERE sample.feature_schema_hash <> profile_schema_hash
                          OR pg_catalog.jsonb_typeof(sample.features) <> 'object'
                          OR sample.features = '{{}}'::jsonb
                          OR (SELECT count(*) FROM pg_catalog.jsonb_object_keys(sample.features))
                                <> profile_feature_count
                   )::integer
              INTO train_count, validation_count, invalid_count
              FROM public.learning_fold_assignment AS assignment
              JOIN public.learning_normalized_sample AS sample
                ON sample.workspace_id = assignment.workspace_id
               AND sample.manifest_hash = assignment.manifest_hash
               AND sample.fold_plan_hash = assignment.fold_plan_hash
               AND sample.sample_id = assignment.sample_id
             WHERE assignment.workspace_id = target_workspace
               AND assignment.manifest_hash = attempt_row.manifest_hash
               AND assignment.fold_plan_hash = attempt_row.fold_plan_hash
               AND assignment.fold_id = attempt_row.fold_id
               AND assignment.role IN ('train', 'validation');
            IF train_count IS DISTINCT FROM profile_train_count
                OR validation_count IS DISTINCT FROM profile_validation_count
                OR invalid_count <> 0 THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_COUNTS_INVALID';
            END IF;
            INSERT INTO public.{RECEIPT}(
                id, workspace_id, attempt_id, normalized_dataset_id, manifest_hash,
                fold_plan_hash, fold_id, approval_hash, request_fingerprint, profile_hash,
                purpose, actor_principal, training_samples, validation_samples, sample_count
            ) VALUES (
                pg_catalog.gen_random_uuid(), target_workspace, target_attempt_id,
                stored_dataset_id, attempt_row.manifest_hash, attempt_row.fold_plan_hash,
                attempt_row.fold_id, attempt_row.approval_hash, attempt_row.content_hash,
                profile_hash, 'trainer-input', 'crypidian_trainer_worker',
                train_count, validation_count, train_count + validation_count
            );
            RETURN QUERY
                SELECT attempt_row.id, attempt_row.workspace_id, attempt_row.normalized_dataset_id,
                       attempt_row.manifest_hash::text, attempt_row.split_policy_version::text,
                       attempt_row.fold_plan_hash::text, attempt_row.approval_hash::text,
                       attempt_row.fold_id::text, attempt_row.seed,
                       attempt_row.trainer_version::text,
                       attempt_row.dependency_fingerprint::text, sample.sample_id,
                       assignment.role::text, sample.outcome_id::text,
                       sample.outcome_hash::text, sample.feature_snapshot_hash::text,
                       sample.feature_schema_hash::text, sample.label_hash::text,
                       sample.label_value, sample.features, sample.sample_hash::text
                  FROM public.learning_fold_assignment AS assignment
                  JOIN public.learning_normalized_sample AS sample
                    ON sample.workspace_id = assignment.workspace_id
                   AND sample.manifest_hash = assignment.manifest_hash
                   AND sample.fold_plan_hash = assignment.fold_plan_hash
                   AND sample.sample_id = assignment.sample_id
                 WHERE assignment.workspace_id = target_workspace
                   AND assignment.manifest_hash = attempt_row.manifest_hash
                   AND assignment.fold_plan_hash = attempt_row.fold_plan_hash
                   AND assignment.fold_id = attempt_row.fold_id
                   AND assignment.role IN ('train', 'validation')
                 ORDER BY assignment.role, assignment.sample_id;
        END $$
    """)
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {READER}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {READER}")
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {TRAINER}")


def downgrade() -> None:
    op.execute(f"LOCK TABLE public.{RECEIPT} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.{RECEIPT}) THEN
            RAISE EXCEPTION 'WP06_TRAINING_ACCESS_RECEIPT_DOWNGRADE_REQUIRES_EMPTY_TABLE';
        END IF;
    END $$""")
    op.execute(f"DROP FUNCTION {FUNCTION}")
    op.execute(f"DROP POLICY learning_training_access_reader_scope ON public.{RECEIPT}")
    op.execute(f"DROP POLICY learning_training_access_admin_read ON public.{RECEIPT}")
    op.execute(f"ALTER TABLE public.{RECEIPT} DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE ALL ON TABLE public.{RECEIPT} FROM {ADMIN}, {READER}, {TRAINER}")
    op.execute(f"DROP TRIGGER {RECEIPT}_immutable ON public.{RECEIPT}")
    op.execute("DROP FUNCTION public.reject_learning_training_access_mutation()")
    op.drop_index("ix_learning_training_access_receipt_manifest", table_name=RECEIPT)
    op.drop_table(RECEIPT)
    for table in (
        "learning_attempt_event",
        "learning_training_attempt",
        "learning_fold_resource_profile",
        "learning_fold_assignment",
        "learning_normalized_sample",
        "learning_normalized_dataset",
        "learning_split_approval",
        "dataset_manifest",
    ):
        if table == "learning_fold_assignment":
            continue
        op.execute(f"DROP POLICY learning_training_batch_reader_scope ON public.{table}")
    op.execute(
        "DROP POLICY learning_training_batch_reader_roles ON public.learning_fold_assignment"
    )
    op.execute(f"""
        REVOKE SELECT ON public.learning_training_attempt FROM {READER};
        REVOKE SELECT (workspace_id, attempt_id, sequence, state, actor_principal)
            ON public.learning_attempt_event FROM {READER};
        REVOKE SELECT (workspace_id, normalized_dataset_id, manifest_hash, fold_plan_hash,
                       fold_id, training_samples, validation_samples, feature_count,
                       feature_schema_hash, profile_hash)
            ON public.learning_fold_resource_profile FROM {READER};
        REVOKE SELECT (workspace_id, manifest_hash, fold_plan_hash, fold_id, sample_id, role)
            ON public.learning_fold_assignment FROM {READER};
        REVOKE SELECT (workspace_id, manifest_hash, fold_plan_hash, sample_id, outcome_id,
                       outcome_hash, feature_snapshot_hash, feature_schema_hash, label_hash,
                       label_value, features, sample_hash)
            ON public.learning_normalized_sample FROM {READER};
        REVOKE SELECT (workspace_id, manifest_hash, split_policy_version, fold_plan_hash,
                       id, approval_hash)
            ON public.learning_normalized_dataset FROM {READER};
        REVOKE SELECT (workspace_id, manifest_hash, split_policy_version, fold_plan_hash,
                       approval_hash)
            ON public.learning_split_approval FROM {READER};
        REVOKE SELECT (workspace_id, manifest_hash, manifest)
            ON public.dataset_manifest FROM {READER};
    """)
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {READER}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {READER}")
    op.execute(f"DROP OWNED BY {READER}")
    op.execute(f"DROP ROLE {READER}")

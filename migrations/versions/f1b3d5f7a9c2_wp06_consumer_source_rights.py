"""WP06: recheck source TRAINING rights at evaluator/trainer access receipts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1b3d5f7a9c2"
down_revision: str | None = "f0b2d4e6a8c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RIGHTS_READER = "crypidian_learning_source_rights_reader"
EVALUATOR = "crypidian_learning_evaluator"
EVALUATION_READER = "crypidian_learning_evaluation_reader"
TRAINER = "crypidian_trainer_worker"
TRAINER_READER = "crypidian_learning_batch_reader"
ADMIN = "crypidian_admin_web"
APP = "crypidian_app"
TABLE = "learning_run_source_rights_receipt"
GATE = "public.wp06_assert_training_source_rights"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{RIGHTS_READER}') THEN
            CREATE ROLE {RIGHTS_READER} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSIF EXISTS (SELECT 1 FROM pg_catalog.pg_roles AS role WHERE role.rolname='{RIGHTS_READER}'
            AND (role.rolcanlogin OR role.rolsuper OR role.rolbypassrls OR role.rolinherit
                 OR role.rolcreatedb OR role.rolcreaterole OR role.rolreplication
                 OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                            WHERE membership.roleid=role.oid OR membership.member=role.oid))) THEN
            RAISE EXCEPTION 'WP06_SOURCE_RIGHTS_READER_ROLE_UNSAFE';
        END IF;
    END $$""")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RIGHTS_READER}")

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
        sa.Column("access_kind", sa.String(16), nullable=False),
        sa.Column("access_id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint(
            "access_kind IN ('evaluation', 'training')",
            name="learning_run_source_rights_access_kind_valid",
        ),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND rights_hash ~ '^[0-9a-f]{64}$'",
            name="learning_run_source_rights_hashes_valid",
        ),
        sa.CheckConstraint(
            "purpose = 'training' AND policy_sequence > 0",
            name="learning_run_source_rights_purpose_valid",
        ),
        sa.CheckConstraint(
            "length(sample_id) BETWEEN 1 AND 128 AND length(source_id) BETWEEN 1 AND 128 "
            "AND length(item_id) BETWEEN 1 AND 128 AND length(revision_id) BETWEEN 1 AND 128 "
            "AND length(policy_id) BETWEEN 1 AND 160",
            name="learning_run_source_rights_identifiers_valid",
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
            name="fk_learning_run_source_rights_dataset",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_run_source_rights_receipt")),
        sa.UniqueConstraint(
            "workspace_id",
            "access_kind",
            "access_id",
            "sample_id",
            "source_id",
            "item_id",
            "revision_id",
            "policy_id",
            name="uq_learning_run_source_rights_access_reference",
        ),
    )
    op.create_index(
        "ix_learning_run_source_rights_manifest",
        TABLE,
        ["workspace_id", "manifest_hash", "checked_at"],
    )
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"REVOKE ALL ON TABLE public.{TABLE} FROM PUBLIC, {APP}, {ADMIN}, {TRAINER}, "
        f"{EVALUATOR}, {RIGHTS_READER}"
    )
    op.execute(
        f"CREATE POLICY learning_run_source_rights_admin_read ON public.{TABLE} "
        f"FOR SELECT TO {ADMIN} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_run_source_rights_gate_scope ON public.{TABLE} "
        f"FOR ALL TO {RIGHTS_READER} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {ADMIN}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{TABLE} TO {RIGHTS_READER}")
    op.execute("""
        CREATE FUNCTION public.reject_learning_run_source_rights_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN RAISE EXCEPTION 'WP06_RUN_SOURCE_RIGHTS_IMMUTABLE'; END $$
    """)
    op.execute(
        f"CREATE TRIGGER {TABLE}_immutable BEFORE UPDATE OR DELETE ON public.{TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION public.reject_learning_run_source_rights_mutation()"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.reject_learning_run_source_rights_mutation() FROM PUBLIC"
    )

    _grant_gate_metadata_reads()
    _create_gate()
    _replace_evaluator_gateway()
    _replace_trainer_gateway()


def _grant_gate_metadata_reads() -> None:
    tables = (
        "source_policy_revision",
        "source_policy_observation",
        "source_item_revision",
        "source_item_observation",
        "source_item_takedown",
        "dataset_manifest",
        "dataset_manifest_eligibility_event",
        "outbox_event",
        "consumer_receipt",
        "learning_source_training_receipt",
    )
    for table in tables:
        if table == "outbox_event":
            using = f"{WORKSPACE} AND event_type = 'research.rights_changed.v1'"
        elif table == "consumer_receipt":
            using = f"{WORKSPACE} AND consumer_name = 'wp06.dataset-rights-eligibility.v1'"
        else:
            using = WORKSPACE
        op.execute(
            f"CREATE POLICY wp06_source_rights_gate_select ON public.{table} "
            f"FOR SELECT TO {RIGHTS_READER} USING ({using})"
        )

    op.execute(f"""
        GRANT SELECT (workspace_id, source_id, sequence, policy_id, state, allowed_purposes,
                      valid_until)
            ON public.source_policy_revision TO {RIGHTS_READER};
        GRANT SELECT (workspace_id, source_id, sequence, policy_id, known_at)
            ON public.source_policy_observation TO {RIGHTS_READER};
        GRANT SELECT (workspace_id, source_id, item_id, revision_id, sequence, available_at,
                      tombstoned, retention_expires_at)
            ON public.source_item_revision TO {RIGHTS_READER};
        GRANT SELECT (workspace_id, source_id, item_id, revision_id, sequence, known_at)
            ON public.source_item_observation TO {RIGHTS_READER};
        GRANT SELECT (workspace_id, source_id, item_id)
            ON public.source_item_takedown TO {RIGHTS_READER};
        GRANT SELECT (workspace_id, id, manifest_hash)
            ON public.dataset_manifest TO {RIGHTS_READER};
        GRANT SELECT (workspace_id, manifest_id, sample_id, source_id, item_id, revision_id,
                      policy_id, policy_sequence, purpose, rights_hash, reason_code)
            ON public.dataset_manifest_eligibility_event TO {RIGHTS_READER};
        GRANT SELECT (id, workspace_id, event_type, payload)
            ON public.outbox_event TO {RIGHTS_READER};
        GRANT SELECT (workspace_id, outbox_event_id, consumer_name)
            ON public.consumer_receipt TO {RIGHTS_READER};
        GRANT SELECT (workspace_id, normalized_dataset_id, manifest_hash, fold_plan_hash,
                      sample_id, source_id, item_id, revision_id, policy_id, policy_sequence,
                      purpose, rights_hash)
            ON public.learning_source_training_receipt TO {RIGHTS_READER};
    """)


def _create_gate() -> None:
    op.execute(f"""
        CREATE FUNCTION {GATE}(
            target_workspace uuid,
            target_manifest_hash text,
            target_manifest jsonb,
            target_normalized_dataset_id uuid,
            target_fold_plan_hash text,
            target_access_kind text,
            target_access_id uuid
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        <<gate>>
        DECLARE
            manifest_id uuid;
            checked_at timestamptz;
            sample_entry jsonb;
            reference_entry jsonb;
            source_key text;
            sample_id text;
            source_id text;
            item_id text;
            revision_id text;
            policy_id text;
            policy_sequence integer;
            purpose text;
            rights_hash text;
            expected_count integer := 0;
            stored_count integer := 0;
            current_policy_id text;
            current_policy_sequence integer;
            current_policy_state text;
            current_policy_purposes jsonb;
            current_policy_valid_until timestamptz;
            current_revision_id text;
            current_revision_sequence integer;
            current_available_at timestamptz;
            current_retention_expires_at timestamptz;
            current_tombstoned boolean;
        BEGIN
            IF (target_access_kind = 'evaluation' AND session_user <> '{EVALUATOR}')
               OR (target_access_kind = 'training' AND session_user <> '{TRAINER}')
               OR target_access_kind NOT IN ('evaluation', 'training') THEN
                RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_CALLER_DENIED';
            END IF;
            IF target_workspace IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_WORKSPACE_DENIED';
            END IF;
            IF target_normalized_dataset_id IS NULL OR target_access_id IS NULL
               OR target_manifest_hash !~ '^[0-9a-f]{{64}}$'
               OR target_fold_plan_hash !~ '^[0-9a-f]{{64}}$'
               OR target_manifest->>'workspace_id' IS DISTINCT FROM target_workspace::text
               OR pg_catalog.jsonb_typeof(target_manifest->'samples') IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_SCOPE_INVALID';
            END IF;
            SELECT record.id INTO manifest_id
              FROM public.dataset_manifest AS record
             WHERE record.workspace_id = target_workspace
               AND record.manifest_hash = target_manifest_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_MANIFEST_MISSING';
            END IF;

            FOR sample_entry IN
                SELECT sample.value
                  FROM pg_catalog.jsonb_array_elements(target_manifest->'samples') AS sample(value)
            LOOP
                sample_id := sample_entry->>'sample_id';
                IF sample_id IS NULL OR length(sample_id) NOT BETWEEN 1 AND 128 THEN
                    RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_LINEAGE_INVALID';
                END IF;
                IF COALESCE((sample_entry->>'source_dependent')::boolean, false) THEN
                    IF target_manifest->>'manifest_version' <> 'dataset-manifest-v3'
                       OR pg_catalog.jsonb_typeof(sample_entry->'source_references')
                           IS DISTINCT FROM 'array'
                       OR pg_catalog.jsonb_array_length(sample_entry->'source_references') = 0 THEN
                        RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_LINEAGE_INVALID';
                    END IF;
                    expected_count := expected_count +
                        pg_catalog.jsonb_array_length(sample_entry->'source_references');
                ELSIF target_manifest->>'manifest_version' = 'dataset-manifest-v3'
                      AND pg_catalog.jsonb_array_length(
                          COALESCE(sample_entry->'source_references', '[]'::jsonb)) > 0 THEN
                    RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_LINEAGE_INVALID';
                ELSIF target_manifest->>'manifest_version' <> 'dataset-manifest-v3'
                      AND (COALESCE((sample_entry->>'source_dependent')::boolean, false)
                           OR pg_catalog.jsonb_array_length(
                               COALESCE(sample_entry->'source_rights_hashes', '[]'::jsonb)) > 0
                           OR pg_catalog.jsonb_array_length(
                               COALESCE(sample_entry->'source_revision_refs', '[]'::jsonb)) > 0
                      ) THEN
                    RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_LINEAGE_INVALID';
                END IF;
            END LOOP;

            IF expected_count = 0 THEN
                RETURN;
            END IF;

            FOR source_key IN
                SELECT DISTINCT reference.value->>'source_id'
                  FROM pg_catalog.jsonb_array_elements(target_manifest->'samples') AS sample(value)
                  CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
                      COALESCE(sample.value->'source_references', '[]'::jsonb)
                  ) AS reference(value)
                 WHERE COALESCE((sample.value->>'source_dependent')::boolean, false)
                 ORDER BY 1
            LOOP
                IF source_key IS NULL OR source_key !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,127}}$' THEN
                    RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_LINEAGE_INVALID';
                END IF;
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || source_key, 0));
            END LOOP;

            checked_at := pg_catalog.clock_timestamp();
            SELECT count(*)::integer INTO stored_count
              FROM public.learning_source_training_receipt AS receipt
             WHERE receipt.workspace_id = target_workspace
               AND receipt.normalized_dataset_id = target_normalized_dataset_id
               AND receipt.manifest_hash = target_manifest_hash
               AND receipt.fold_plan_hash = target_fold_plan_hash;
            IF stored_count <> expected_count THEN
                RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_BUILDER_RECEIPT_MISMATCH';
            END IF;

            FOR sample_entry IN
                SELECT sample.value
                  FROM pg_catalog.jsonb_array_elements(target_manifest->'samples') AS sample(value)
                 WHERE COALESCE((sample.value->>'source_dependent')::boolean, false)
            LOOP
                sample_id := sample_entry->>'sample_id';
                FOR reference_entry IN
                    SELECT reference.value
                      FROM pg_catalog.jsonb_array_elements(
                          sample_entry->'source_references'
                      ) AS reference(value)
                LOOP
                    source_id := reference_entry->>'source_id';
                    item_id := reference_entry->>'item_id';
                    revision_id := reference_entry->>'revision_id';
                    policy_id := reference_entry->>'policy_id';
                    purpose := reference_entry->>'purpose';
                    rights_hash := reference_entry->>'rights_hash';
                    policy_sequence := (reference_entry->>'policy_sequence')::integer;
                    IF pg_catalog.jsonb_typeof(reference_entry) IS DISTINCT FROM 'object'
                       OR source_id !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,127}}$'
                       OR item_id !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,127}}$'
                       OR revision_id !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,127}}$'
                       OR policy_id IS NULL OR length(policy_id) NOT BETWEEN 1 AND 160
                       OR policy_sequence <= 0 OR purpose <> 'training'
                       OR rights_hash !~ '^[0-9a-f]{{64}}$' THEN
                        RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_LINEAGE_INVALID';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1
                          FROM public.learning_source_training_receipt AS receipt
                         WHERE receipt.workspace_id = target_workspace
                           AND receipt.normalized_dataset_id = target_normalized_dataset_id
                           AND receipt.manifest_hash = target_manifest_hash
                           AND receipt.fold_plan_hash = target_fold_plan_hash
                           AND receipt.sample_id = gate.sample_id
                           AND receipt.source_id = gate.source_id
                           AND receipt.item_id = gate.item_id
                           AND receipt.revision_id = gate.revision_id
                           AND receipt.policy_id = gate.policy_id
                           AND receipt.policy_sequence = gate.policy_sequence
                           AND receipt.purpose = gate.purpose
                           AND receipt.rights_hash = gate.rights_hash
                    ) THEN
                        RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_BUILDER_RECEIPT_MISMATCH';
                    END IF;
                    IF EXISTS (
                        SELECT 1 FROM public.outbox_event AS event
                         WHERE event.workspace_id = target_workspace
                           AND event.event_type = 'research.rights_changed.v1'
                           AND event.payload->>'source_id' = gate.source_id
                           AND (NOT (event.payload ? 'item_id')
                                OR event.payload->>'item_id' = gate.item_id)
                           AND NOT EXISTS (
                               SELECT 1 FROM public.consumer_receipt AS receipt
                                WHERE receipt.workspace_id = event.workspace_id
                                  AND receipt.outbox_event_id = event.id
                                  AND receipt.consumer_name =
                                      'wp06.dataset-rights-eligibility.v1'
                           )
                    ) OR EXISTS (
                        SELECT 1 FROM public.dataset_manifest_eligibility_event AS invalidation
                         WHERE invalidation.workspace_id = target_workspace
                           AND invalidation.manifest_id = gate.manifest_id
                           AND invalidation.sample_id = gate.sample_id
                           AND invalidation.source_id = gate.source_id
                           AND invalidation.item_id = gate.item_id
                           AND invalidation.revision_id = gate.revision_id
                           AND invalidation.policy_id = gate.policy_id
                           AND invalidation.policy_sequence = gate.policy_sequence
                           AND invalidation.purpose = gate.purpose
                           AND invalidation.rights_hash = gate.rights_hash
                    ) THEN
                        RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_INVALIDATED';
                    END IF;

                    IF EXISTS (
                        SELECT 1 FROM public.source_policy_revision AS policy
                         WHERE policy.workspace_id = target_workspace
                           AND policy.source_id = gate.source_id
                           AND NOT EXISTS (
                               SELECT 1 FROM public.source_policy_observation AS observation
                                WHERE observation.workspace_id = policy.workspace_id
                                  AND observation.source_id = policy.source_id
                                  AND observation.sequence = policy.sequence
                                  AND observation.policy_id = policy.policy_id
                           )
                    ) OR EXISTS (
                        SELECT 1 FROM public.source_item_revision AS revision
                         WHERE revision.workspace_id = target_workspace
                           AND revision.source_id = gate.source_id
                           AND revision.item_id = gate.item_id
                           AND NOT EXISTS (
                               SELECT 1 FROM public.source_item_observation AS observation
                                WHERE observation.workspace_id = revision.workspace_id
                                  AND observation.source_id = revision.source_id
                                  AND observation.item_id = revision.item_id
                                  AND observation.sequence = revision.sequence
                                  AND observation.revision_id = revision.revision_id
                           )
                    ) THEN
                        RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_OBSERVATION_MISSING';
                    END IF;

                    SELECT policy.policy_id, policy.sequence, policy.state,
                           policy.allowed_purposes, policy.valid_until
                      INTO current_policy_id, current_policy_sequence, current_policy_state,
                           current_policy_purposes, current_policy_valid_until
                      FROM public.source_policy_revision AS policy
                     WHERE policy.workspace_id = target_workspace
                       AND policy.source_id = gate.source_id
                     ORDER BY policy.sequence DESC LIMIT 1;
                    IF NOT FOUND OR current_policy_id IS DISTINCT FROM policy_id
                       OR current_policy_sequence IS DISTINCT FROM policy_sequence
                       OR current_policy_state IS DISTINCT FROM 'verified'
                       OR NOT (current_policy_purposes @> '["training"]'::jsonb)
                       OR (current_policy_valid_until IS NOT NULL
                           AND current_policy_valid_until <= checked_at)
                       OR NOT EXISTS (
                           SELECT 1 FROM public.source_policy_observation AS observation
                            WHERE observation.workspace_id = target_workspace
                              AND observation.source_id = gate.source_id
                              AND observation.sequence = gate.policy_sequence
                              AND observation.policy_id = gate.policy_id
                              AND observation.known_at < gate.checked_at
                       ) THEN
                        RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_POLICY_DENIED';
                    END IF;

                    SELECT revision.revision_id, revision.sequence, revision.available_at,
                           revision.tombstoned, revision.retention_expires_at
                      INTO current_revision_id, current_revision_sequence,
                           current_available_at, current_tombstoned,
                           current_retention_expires_at
                      FROM public.source_item_revision AS revision
                     WHERE revision.workspace_id = target_workspace
                       AND revision.source_id = gate.source_id
                       AND revision.item_id = gate.item_id
                     ORDER BY revision.sequence DESC LIMIT 1;
                    IF NOT FOUND OR current_revision_id IS DISTINCT FROM revision_id
                       OR current_available_at IS NULL OR current_available_at > checked_at
                       OR current_tombstoned
                       OR NOT EXISTS (
                           SELECT 1 FROM public.source_item_observation AS observation
                            WHERE observation.workspace_id = target_workspace
                              AND observation.source_id = gate.source_id
                              AND observation.item_id = gate.item_id
                              AND observation.sequence = current_revision_sequence
                              AND observation.revision_id = gate.revision_id
                              AND observation.known_at < gate.checked_at
                       )
                       OR (current_retention_expires_at IS NOT NULL
                           AND current_retention_expires_at <= checked_at)
                       OR EXISTS (
                           SELECT 1 FROM public.source_item_takedown AS takedown
                            WHERE takedown.workspace_id = target_workspace
                              AND takedown.source_id = gate.source_id
                              AND takedown.item_id = gate.item_id
                       ) THEN
                        RAISE EXCEPTION 'WP06_SOURCE_TRAINING_RIGHTS_ITEM_DENIED';
                    END IF;

                    INSERT INTO public.{TABLE}(
                        id, workspace_id, access_kind, access_id, normalized_dataset_id,
                        manifest_hash, fold_plan_hash, sample_id, source_id, item_id,
                        revision_id, policy_id, policy_sequence, purpose, rights_hash, checked_at
                    ) VALUES (
                        pg_catalog.gen_random_uuid(), target_workspace, target_access_kind,
                        target_access_id, target_normalized_dataset_id, target_manifest_hash,
                        target_fold_plan_hash, sample_id, source_id, item_id, revision_id,
                        policy_id, policy_sequence, purpose, rights_hash, checked_at
                    );
                END LOOP;
            END LOOP;
        END $$
    """)
    op.execute(
        f"ALTER FUNCTION {GATE}(uuid, text, jsonb, uuid, text, text, uuid) OWNER TO {RIGHTS_READER}"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {GATE}(uuid, text, jsonb, uuid, text, text, uuid) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {GATE}(uuid, text, jsonb, uuid, text, text, uuid) "
        f"TO {EVALUATION_READER}, {TRAINER_READER}"
    )


def _replace_evaluator_gateway() -> None:
    op.execute(f"""
        CREATE OR REPLACE FUNCTION public.wp06_open_learning_evaluation(
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
            stored_dataset_id uuid;
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
            SELECT record.manifest, record.canonical_manifest, record.split_policy_version
              INTO stored_manifest, stored_canonical, stored_policy
              FROM public.dataset_manifest AS record
             WHERE record.workspace_id = target_workspace
               AND record.manifest_hash = target_manifest_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_EVALUATION_MANIFEST_NOT_FOUND';
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
            SELECT dataset.id INTO stored_dataset_id
              FROM public.learning_normalized_dataset AS dataset
             WHERE dataset.workspace_id = target_workspace
               AND dataset.manifest_hash = target_manifest_hash
               AND dataset.split_policy_version = stored_policy
               AND dataset.fold_plan_hash = stored_fold_hash
               AND dataset.approval_hash = stored_approval_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_EVALUATION_NORMALIZED_DATASET_NOT_FOUND';
            END IF;
            PERFORM {GATE}(
                target_workspace, target_manifest_hash, stored_manifest, stored_dataset_id,
                stored_fold_hash, 'evaluation', target_evaluation_run_id
            );
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
        END $$
    """)


def _replace_trainer_gateway() -> None:
    op.execute(f"""
        CREATE OR REPLACE FUNCTION public.wp06_claim_learning_training_batch(
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
            IF session_user <> '{TRAINER}' THEN
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
             WHERE attempt.workspace_id = target_workspace AND attempt.id = target_attempt_id;
            IF NOT FOUND OR attempt_row.initial_state <> 'queued' THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_ATTEMPT_NOT_FOUND';
            END IF;
            SELECT event.state, event.sequence, event.actor_principal
              INTO latest_state, latest_sequence, latest_actor
              FROM public.learning_attempt_event AS event
             WHERE event.workspace_id = target_workspace AND event.attempt_id = target_attempt_id
             ORDER BY event.sequence DESC LIMIT 1;
            IF NOT FOUND OR latest_state <> 'running' OR latest_sequence <> 2
                OR latest_actor <> '{TRAINER}' THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_ATTEMPT_NOT_RUNNING';
            END IF;
            IF EXISTS (SELECT 1 FROM public.learning_training_access_receipt AS receipt
                WHERE receipt.workspace_id = target_workspace
                  AND receipt.attempt_id = target_attempt_id) THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_ALREADY_CONSUMED';
            END IF;
            SELECT manifest_record.manifest INTO stored_manifest
              FROM public.dataset_manifest AS manifest_record
             WHERE manifest_record.workspace_id = target_workspace
               AND manifest_record.manifest_hash = attempt_row.manifest_hash;
            IF NOT FOUND OR pg_catalog.jsonb_typeof(stored_manifest->'samples')
                    IS DISTINCT FROM 'array' THEN
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
            PERFORM {GATE}(
                target_workspace, attempt_row.manifest_hash, stored_manifest, stored_dataset_id,
                attempt_row.fold_plan_hash, 'training', target_attempt_id
            );
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
            INSERT INTO public.learning_training_access_receipt(
                id, workspace_id, attempt_id, normalized_dataset_id, manifest_hash,
                fold_plan_hash, fold_id, approval_hash, request_fingerprint, profile_hash,
                purpose, actor_principal, training_samples, validation_samples, sample_count
            ) VALUES (
                pg_catalog.gen_random_uuid(), target_workspace, target_attempt_id,
                stored_dataset_id, attempt_row.manifest_hash, attempt_row.fold_plan_hash,
                attempt_row.fold_id, attempt_row.approval_hash, attempt_row.content_hash,
                profile_hash, 'trainer-input', '{TRAINER}',
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


def downgrade() -> None:
    op.execute(f"LOCK TABLE public.{TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.{TABLE}) THEN
            RAISE EXCEPTION 'WP06_RUN_SOURCE_RIGHTS_DOWNGRADE_BLOCKED';
        END IF;
    END $$""")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_claim_learning_training_batch(uuid, uuid) "
        f"FROM {TRAINER}"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_open_learning_evaluation(uuid, text, uuid, text) "
        f"FROM {EVALUATOR}"
    )
    _restore_evaluator_gateway()
    _restore_trainer_gateway()
    op.execute(f"DROP FUNCTION {GATE}(uuid, text, jsonb, uuid, text, text, uuid)")
    op.execute(f"DROP POLICY learning_run_source_rights_gate_scope ON public.{TABLE}")
    op.execute(f"DROP POLICY learning_run_source_rights_admin_read ON public.{TABLE}")
    op.execute(f"REVOKE ALL ON TABLE public.{TABLE} FROM {RIGHTS_READER}, {ADMIN}")
    op.execute(f"DROP TRIGGER {TABLE}_immutable ON public.{TABLE}")
    op.execute("DROP FUNCTION public.reject_learning_run_source_rights_mutation()")
    op.drop_index("ix_learning_run_source_rights_manifest", table_name=TABLE)
    op.drop_table(TABLE)
    for table in (
        "learning_source_training_receipt",
        "consumer_receipt",
        "outbox_event",
        "dataset_manifest_eligibility_event",
        "dataset_manifest",
        "source_item_takedown",
        "source_item_observation",
        "source_item_revision",
        "source_policy_observation",
        "source_policy_revision",
    ):
        op.execute(f"DROP POLICY wp06_source_rights_gate_select ON public.{table}")
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM {RIGHTS_READER}")
    op.execute(f"DROP OWNED BY {RIGHTS_READER}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {RIGHTS_READER}")
    op.execute(f"DROP ROLE {RIGHTS_READER}")


def _restore_evaluator_gateway() -> None:
    """Khôi phục gateway evaluator fail-closed của migration trước."""
    op.execute(f"""
        CREATE OR REPLACE FUNCTION public.wp06_open_learning_evaluation(
            target_workspace uuid, target_manifest_hash text, target_evaluation_run_id uuid,
            target_request_fingerprint text)
        RETURNS TABLE(receipt_id uuid, manifest jsonb, canonical_manifest text,
                      split_policy_version text, fold_plan_hash text, approval_hash text)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            stored_manifest jsonb; stored_canonical text; stored_policy text;
            stored_fold_hash text; stored_approval_hash text; new_receipt_id uuid;
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
            IF NOT FOUND THEN RAISE EXCEPTION 'WP06_EVALUATION_MANIFEST_NOT_FOUND'; END IF;
            IF EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_array_elements(
                    COALESCE(stored_manifest->'samples', '[]'::jsonb)) AS sample(value)
                WHERE COALESCE((sample.value->>'source_dependent')::boolean, false)
                   OR pg_catalog.jsonb_array_length(
                       COALESCE(sample.value->'source_references', '[]'::jsonb)) > 0
            ) THEN RAISE EXCEPTION 'WP06_EVALUATION_SOURCE_RIGHTS_FAIL_CLOSED'; END IF;
            SELECT approval.fold_plan_hash, approval.approval_hash
              INTO stored_fold_hash, stored_approval_hash
              FROM public.learning_split_approval AS approval
             WHERE approval.workspace_id = target_workspace
               AND approval.manifest_hash = target_manifest_hash
               AND approval.split_policy_version = stored_policy;
            IF NOT FOUND THEN RAISE EXCEPTION 'WP06_EVALUATION_APPROVAL_NOT_FOUND'; END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.learning_normalized_dataset AS dataset
                 WHERE dataset.workspace_id = target_workspace
                   AND dataset.manifest_hash = target_manifest_hash
                   AND dataset.split_policy_version = stored_policy
                   AND dataset.fold_plan_hash = stored_fold_hash
                   AND dataset.approval_hash = stored_approval_hash
            ) THEN RAISE EXCEPTION 'WP06_EVALUATION_NORMALIZED_DATASET_NOT_FOUND'; END IF;
            INSERT INTO public.learning_evaluation_access_receipt(
                id, workspace_id, evaluation_run_id, manifest_hash, split_policy_version,
                fold_plan_hash, approval_hash, request_fingerprint, purpose, actor_principal)
            SELECT pg_catalog.gen_random_uuid(), target_workspace, target_evaluation_run_id,
                   target_manifest_hash, stored_policy, stored_fold_hash, stored_approval_hash,
                   target_request_fingerprint, 'diagnostic-evaluation', '{EVALUATOR}'
            RETURNING id INTO new_receipt_id;
            RETURN QUERY SELECT new_receipt_id, stored_manifest, stored_canonical, stored_policy,
                                stored_fold_hash, stored_approval_hash;
        END $$
    """)
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_open_learning_evaluation(uuid, text, uuid, text) "
        f"TO {EVALUATOR}"
    )


def _restore_trainer_gateway() -> None:
    """Khôi phục gateway trainer fail-closed của migration trước."""
    op.execute(f"""
        CREATE OR REPLACE FUNCTION public.wp06_claim_learning_training_batch(
            target_workspace uuid, target_attempt_id uuid
        ) RETURNS TABLE (
            attempt_id uuid, workspace_id uuid, normalized_dataset_id uuid,
            manifest_hash text, split_policy_version text, fold_plan_hash text,
            approval_hash text, fold_id text, seed integer, trainer_version text,
            dependency_fingerprint text, sample_id text, role text, outcome_id text,
            outcome_hash text, feature_snapshot_hash text, feature_schema_hash text,
            label_hash text, label_value integer, features jsonb, sample_hash text
        ) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            attempt_row public.learning_training_attempt%ROWTYPE;
            stored_manifest jsonb; latest_state text; latest_actor text; latest_sequence integer;
            approved_hash text; stored_dataset_id uuid; train_count integer;
            validation_count integer;
            invalid_count integer; profile_train_count integer; profile_validation_count integer;
            profile_feature_count integer; profile_schema_hash text; profile_hash text;
        BEGIN
            IF session_user <> '{TRAINER}' THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_CALLER_DENIED';
            END IF;
            IF target_workspace IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_WORKSPACE_DENIED';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(target_attempt_id::text, 0)
            );
            SELECT attempt.* INTO attempt_row FROM public.learning_training_attempt AS attempt
             WHERE attempt.workspace_id = target_workspace AND attempt.id = target_attempt_id;
            IF NOT FOUND OR attempt_row.initial_state <> 'queued' THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_ATTEMPT_NOT_FOUND';
            END IF;
            SELECT event.state, event.sequence, event.actor_principal
              INTO latest_state, latest_sequence, latest_actor
              FROM public.learning_attempt_event AS event
             WHERE event.workspace_id = target_workspace AND event.attempt_id = target_attempt_id
             ORDER BY event.sequence DESC LIMIT 1;
            IF NOT FOUND OR latest_state <> 'running' OR latest_sequence <> 2
                OR latest_actor <> '{TRAINER}' THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_ATTEMPT_NOT_RUNNING';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.learning_training_access_receipt AS receipt
                 WHERE receipt.workspace_id = target_workspace
                   AND receipt.attempt_id = target_attempt_id
            ) THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_ALREADY_CONSUMED';
            END IF;
            SELECT manifest_record.manifest INTO stored_manifest
              FROM public.dataset_manifest AS manifest_record
             WHERE manifest_record.workspace_id = target_workspace
               AND manifest_record.manifest_hash = attempt_row.manifest_hash;
            IF NOT FOUND
               OR pg_catalog.jsonb_typeof(stored_manifest->'samples') IS DISTINCT FROM 'array'
               OR EXISTS (
                    SELECT 1
                      FROM pg_catalog.jsonb_array_elements(
                          stored_manifest->'samples'
                      ) AS sample(value)
                    WHERE COALESCE((sample.value->>'source_dependent')::boolean, true)
                       OR pg_catalog.jsonb_array_length(
                           COALESCE(sample.value->'source_references', '[]'::jsonb)
                       ) <> 0
                       OR pg_catalog.jsonb_array_length(
                           COALESCE(sample.value->'source_rights_hashes', '[]'::jsonb)
                       ) <> 0
                       OR pg_catalog.jsonb_array_length(
                           COALESCE(sample.value->'source_revision_refs', '[]'::jsonb)
                       ) <> 0
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
            IF NOT FOUND
                OR profile_train_count
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
            INSERT INTO public.learning_training_access_receipt(
                id, workspace_id, attempt_id, normalized_dataset_id, manifest_hash,
                fold_plan_hash,
                fold_id, approval_hash, request_fingerprint, profile_hash, purpose, actor_principal,
                training_samples, validation_samples, sample_count)
            VALUES (pg_catalog.gen_random_uuid(), target_workspace, target_attempt_id,
                stored_dataset_id,
                attempt_row.manifest_hash, attempt_row.fold_plan_hash, attempt_row.fold_id,
                attempt_row.approval_hash, attempt_row.content_hash, profile_hash, 'trainer-input',
                '{TRAINER}', train_count, validation_count, train_count + validation_count);
            RETURN QUERY SELECT attempt_row.id, attempt_row.workspace_id,
                attempt_row.normalized_dataset_id,
                attempt_row.manifest_hash::text, attempt_row.split_policy_version::text,
                attempt_row.fold_plan_hash::text, attempt_row.approval_hash::text,
                attempt_row.fold_id::text, attempt_row.seed,
                attempt_row.trainer_version::text, attempt_row.dependency_fingerprint::text,
                sample.sample_id, assignment.role::text, sample.outcome_id::text,
                sample.outcome_hash::text,
                sample.feature_snapshot_hash::text, sample.feature_schema_hash::text,
                sample.label_hash::text,
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
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_claim_learning_training_batch(uuid, uuid) "
        f"TO {TRAINER}"
    )

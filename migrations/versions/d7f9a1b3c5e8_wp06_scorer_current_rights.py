"""WP06 C4: recheck scorer lineage and source rights without disclosing payload."""

from collections.abc import Sequence

from alembic import op

revision: str = "d7f9a1b3c5e8"
down_revision: str | None = "c5e7a9b1d3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
ADMIN = "crypidian_admin_web"
RUNTIME = "crypidian_trading_runtime"
MARKET_SIGNAL_WORKER = "crypidian_market_signal_worker"
EVALUATOR = "crypidian_learning_evaluator"
COMPARATOR = "crypidian_manifest_comparator"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    _create_role()
    _grant_metadata_reads()
    _create_manifest_source_ref_metadata()
    _create_policy_fingerprint()
    _create_scorer_source_rights_gateway()
    _create_deployment_rights_gateway()


def _create_role() -> None:
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{RIGHTS_ROLE}') THEN
            CREATE ROLE {RIGHTS_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSIF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
             WHERE role.rolname='{RIGHTS_ROLE}'
               AND (role.rolcanlogin OR role.rolsuper OR role.rolbypassrls OR role.rolinherit
                    OR role.rolcreatedb OR role.rolcreaterole OR role.rolreplication
                    OR EXISTS (
                        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                         WHERE membership.roleid=role.oid OR membership.member=role.oid))
        ) THEN
            RAISE EXCEPTION 'WP06_SCORER_RIGHTS_ROLE_UNSAFE';
        END IF;
    END $$""")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RIGHTS_ROLE}")


def _create_manifest_source_ref_metadata() -> None:
    """Chỉ chiếu metadata source ref canonical cho các kiểm tra nội bộ."""
    op.execute("""
        CREATE FUNCTION public.wp06_scorer_manifest_source_refs(
            target_workspace uuid, target_manifest_id uuid
        ) RETURNS TABLE(
            sample_id text,
            source_id text,
            item_id text,
            revision_id text,
            policy_id text,
            policy_sequence integer,
            purpose text,
            rights_hash text
        ) LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            canonical_manifest jsonb;
            canonical_payload text;
            canonical_hash text;
            manifest_version text;
        BEGIN
            IF target_workspace IS NULL OR target_manifest_id IS NULL
               OR target_workspace IS DISTINCT FROM
                   NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_WORKSPACE_DENIED';
            END IF;
            SELECT manifest.manifest, manifest.canonical_manifest, manifest.manifest_hash
              INTO canonical_manifest, canonical_payload, canonical_hash
              FROM public.dataset_manifest AS manifest
             WHERE manifest.workspace_id = target_workspace
               AND manifest.id = target_manifest_id;
            manifest_version := canonical_manifest->>'manifest_version';
            IF canonical_manifest IS NULL OR manifest_version NOT IN (
                    'dataset-manifest-v1','dataset-manifest-v2','dataset-manifest-v3')
               OR canonical_payload::jsonb IS DISTINCT FROM canonical_manifest
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    canonical_payload, 'UTF8')), 'hex') IS DISTINCT FROM canonical_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_MANIFEST_MISSING';
            END IF;
            IF manifest_version IN ('dataset-manifest-v1','dataset-manifest-v2') THEN
                IF EXISTS (
                    SELECT 1
                      FROM pg_catalog.jsonb_array_elements(
                               COALESCE(canonical_manifest->'samples', '[]'::jsonb)
                           ) AS sample(value)
                     WHERE (sample.value ? 'source_dependent'
                            AND pg_catalog.jsonb_typeof(sample.value->'source_dependent')
                                IS DISTINCT FROM 'boolean')
                        OR COALESCE(sample.value->>'source_dependent','false')
                            IS DISTINCT FROM 'false'
                        OR sample.value ? 'source_references'
                        OR (sample.value ? 'source_rights_hashes'
                            AND (pg_catalog.jsonb_typeof(
                                     sample.value->'source_rights_hashes')
                                    IS DISTINCT FROM 'array'
                                 OR pg_catalog.jsonb_array_length(
                                     sample.value->'source_rights_hashes') <> 0))
                        OR (sample.value ? 'source_revision_refs'
                            AND (pg_catalog.jsonb_typeof(
                                     sample.value->'source_revision_refs')
                                    IS DISTINCT FROM 'array'
                                 OR pg_catalog.jsonb_array_length(
                                     sample.value->'source_revision_refs') <> 0))
                ) THEN
                    RAISE EXCEPTION 'WP06_SCORER_RIGHTS_LEGACY_SOURCE_LINEAGE_DENIED';
                END IF;
                RETURN;
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM pg_catalog.jsonb_array_elements(
                           COALESCE(canonical_manifest->'samples', '[]'::jsonb)
                       ) AS sample(value)
                 WHERE pg_catalog.jsonb_typeof(sample.value->'source_dependent')
                            IS DISTINCT FROM 'boolean'
                    OR pg_catalog.jsonb_typeof(sample.value->'source_references')
                            IS DISTINCT FROM 'array'
                    OR (sample.value->>'source_dependent' = 'true'
                        AND pg_catalog.jsonb_array_length(
                            sample.value->'source_references') = 0)
                    OR (sample.value->>'source_dependent' = 'false'
                        AND pg_catalog.jsonb_array_length(
                            sample.value->'source_references') <> 0)
                    OR sample.value ? 'source_rights_hashes'
                    OR sample.value ? 'source_revision_refs'
            ) THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_TYPED_SOURCE_LINEAGE_INVALID';
            END IF;
            RETURN QUERY
            SELECT sample.value->>'sample_id',
                   source_ref.value->>'source_id',
                   source_ref.value->>'item_id',
                   source_ref.value->>'revision_id',
                   source_ref.value->>'policy_id',
                   (source_ref.value->>'policy_sequence')::integer,
                   source_ref.value->>'purpose',
                   source_ref.value->>'rights_hash'
              FROM pg_catalog.jsonb_array_elements(
                       COALESCE(canonical_manifest->'samples', '[]'::jsonb)
                   ) AS sample(value)
              CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
                       COALESCE(sample.value->'source_references', '[]'::jsonb)
                   ) AS source_ref(value)
             ORDER BY sample.value->>'sample_id' COLLATE "C",
                      source_ref.value->>'source_id' COLLATE "C",
                      source_ref.value->>'item_id' COLLATE "C",
                      source_ref.value->>'revision_id' COLLATE "C",
                      source_ref.value->>'policy_id' COLLATE "C";
        END $$
    """)
    op.execute(
        f"ALTER FUNCTION public.wp06_scorer_manifest_source_refs(uuid, uuid) OWNER TO {COMPARATOR}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_scorer_manifest_source_refs(uuid, uuid) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_scorer_manifest_source_refs(uuid, uuid) "
        f"TO {RIGHTS_ROLE}"
    )

    op.execute("""
        CREATE FUNCTION public.wp06_fence_manifest_source_ref_insert() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            source_key text;
        BEGIN
            FOR source_key IN
                SELECT DISTINCT reference.source_id
                  FROM public.wp06_scorer_manifest_source_refs(
                      NEW.workspace_id, NEW.manifest_id
                  ) AS reference
                 ORDER BY reference.source_id
            LOOP
                IF source_key IS NULL
                   OR source_key !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' THEN
                    RAISE EXCEPTION 'WP06_SCORER_RIGHTS_SOURCE_ID_INVALID';
                END IF;
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || NEW.workspace_id::text || ':' || source_key, 0));
            END LOOP;
            RETURN NEW;
        END $$
    """)
    op.execute(
        f"ALTER FUNCTION public.wp06_fence_manifest_source_ref_insert() OWNER TO {COMPARATOR}"
    )
    op.execute("REVOKE ALL ON FUNCTION public.wp06_fence_manifest_source_ref_insert() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER dataset_manifest_source_ref_scoring_fence "
        "BEFORE INSERT ON public.dataset_manifest_source_ref "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_fence_manifest_source_ref_insert()"
    )


def _grant_metadata_reads() -> None:
    tables = (
        "learning_training_attempt",
        "learning_training_artifact",
        "learning_scorer_artifact",
        "learning_normalized_dataset",
        "learning_fold_assignment",
        "learning_normalized_sample",
        "backtest_trade_outcome",
        "backtest_decision_opportunity",
        "backtest_run",
        "learning_scorer_test_opportunity_proof",
        "learning_scorer_test_access_receipt",
        "learning_scorer_test_payload_consumption",
        "learning_scorer_test_evaluation_report",
        "dataset_manifest",
        "dataset_manifest_source_ref",
        "learning_source_training_receipt",
        "learning_run_source_rights_receipt",
        "dataset_manifest_eligibility_event",
        "source_policy_revision",
        "source_policy_observation",
        "source_item_revision",
        "source_item_observation",
        "source_item_takedown",
        "outbox_event",
        "consumer_receipt",
        "backtest_dataset_replay_bundle",
        "backtest_dataset_manifest",
    )
    for table in tables:
        scope = WORKSPACE
        if table == "outbox_event":
            scope += " AND event_type = 'research.rights_changed.v1'"
        elif table == "consumer_receipt":
            scope += " AND consumer_name = 'wp06.dataset-rights-eligibility.v1'"
        elif table == "learning_run_source_rights_receipt":
            scope += " AND access_kind = 'evaluation'"
        op.execute(
            f"CREATE POLICY wp06_scorer_rights_reader_workspace "
            f"ON public.{table} FOR SELECT TO {RIGHTS_ROLE} USING ({scope})"
        )

    grants = {
        "learning_training_attempt": (
            "workspace_id,id,normalized_dataset_id,manifest_hash,fold_plan_hash,fold_id,"
            "approval_hash,split_policy_version"
        ),
        "learning_training_artifact": "workspace_id,id,attempt_id,artifact_hash",
        "learning_scorer_artifact": (
            "workspace_id,id,attempt_id,base_artifact_id,normalized_dataset_id,manifest_hash,"
            "fold_plan_hash,fold_id,approval_hash,base_artifact_hash,schema_version,state,"
            "calibration_policy_version,calibration_policy_hash,vector_schema_version,"
            "vector_schema_hash,training_membership_digest,validation_membership_digest,"
            "training_sample_count,validation_sample_count,dependency_fingerprint,artifact_hash,"
            "strategy_release_id,strategy_release_content_hash,"
            "canonical_artifact"
        ),
        "learning_normalized_dataset": (
            "workspace_id,id,manifest_hash,fold_plan_hash,approval_hash"
        ),
        "learning_fold_assignment": (
            "workspace_id,manifest_hash,fold_plan_hash,fold_id,sample_id,role"
        ),
        "learning_normalized_sample": (
            "workspace_id,manifest_hash,fold_plan_hash,sample_id,outcome_id,outcome_hash,"
            "feature_schema_hash,sample_hash"
        ),
        "backtest_trade_outcome": (
            "workspace_id,id,run_id,trial_record_id,manifest_hash,outcome_hash,"
            "entry_opportunity_event_id,entry_candidate_id,entry_vector_hash,"
            "entry_feature_schema_hash"
        ),
        "backtest_decision_opportunity": (
            "workspace_id,run_id,trial_record_id,manifest_hash,event_id,schema_version,input_state,"
            "strategy_state,strategy_release_id,strategy_content_hash,feature_schema_hash,"
            "vector_hash,candidate_id"
        ),
        "backtest_run": "workspace_id,id,manifest",
        "learning_scorer_test_opportunity_proof": (
            "workspace_id,id,evaluation_run_id,attempt_id,normalized_dataset_id,manifest_hash,"
            "fold_plan_hash,fold_id,approval_hash,bundle_hash,vector_schema_version,"
            "vector_schema_hash,test_membership_digest,source_scope_hash,expected_opportunity_count,"
            "candidate_count,no_candidate_count,input_excluded_count,source_gap_count,"
            "membership_mismatch_count,unlabeled_candidate_count,completeness_verified,proof_hash,"
            "canonical_proof"
        ),
        "learning_scorer_test_access_receipt": (
            "workspace_id,id,evaluation_run_id,attempt_id,normalized_dataset_id,proof_id,"
            "manifest_hash,fold_plan_hash,fold_id,approval_hash,bundle_hash,vector_schema_hash,"
            "test_membership_digest,source_scope_hash,proof_hash,scorer_artifact_hash,policy_hash,"
            "current_rights_receipt_hash,receipt_hash,purpose,actor_principal"
        ),
        "learning_scorer_test_payload_consumption": (
            "workspace_id,id,receipt_id,membership_digest,consumption_hash,"
            "current_rights_receipt_hash,request_fingerprint"
        ),
        "learning_scorer_test_evaluation_report": (
            "workspace_id,id,evaluation_run_id,access_receipt_id,payload_consumption_id,proof_id,"
            "report_hash,access_receipt_hash,current_rights_receipt_hash,payload_consumption_hash,"
            "proof_hash,scorer_artifact_hash,base_artifact_hash,policy_hash,policy_version,"
            "evaluation_status,candidate_completeness_verified,scored_candidate_coverage_verified,"
            "test_sample_count,opportunity_count,report,canonical_report"
        ),
        "dataset_manifest": "workspace_id,id,manifest_hash",
        "dataset_manifest_source_ref": (
            "workspace_id,manifest_id,sample_id,source_id,item_id,revision_id,policy_id,"
            "policy_sequence,purpose,rights_hash"
        ),
        "learning_source_training_receipt": (
            "workspace_id,normalized_dataset_id,manifest_hash,fold_plan_hash,sample_id,source_id,"
            "item_id,revision_id,policy_id,policy_sequence,purpose,rights_hash"
        ),
        "learning_run_source_rights_receipt": (
            "workspace_id,access_kind,access_id,normalized_dataset_id,manifest_hash,fold_plan_hash,"
            "sample_id,source_id,item_id,revision_id,policy_id,policy_sequence,purpose,rights_hash"
        ),
        "dataset_manifest_eligibility_event": (
            "workspace_id,manifest_id,sample_id,source_id,item_id,revision_id,policy_id,"
            "policy_sequence,purpose,rights_hash"
        ),
        "source_policy_revision": (
            "workspace_id,source_id,sequence,policy_id,state,allowed_purposes,evidence_ref,"
            "reviewed_at,valid_until,approved_by,publisher,endpoint_url,access_method,auth_scope,"
            "attribution,retention_ref,quota_window_seconds,quota_max_requests,quota_max_bytes,"
            "quota_max_cost,quota_cost_unit,created_at"
        ),
        "source_policy_observation": "workspace_id,source_id,sequence,policy_id,known_at",
        "source_item_revision": (
            "workspace_id,source_id,item_id,revision_id,sequence,available_at,tombstoned,"
            "retention_expires_at"
        ),
        "source_item_observation": ("workspace_id,source_id,item_id,revision_id,sequence,known_at"),
        "source_item_takedown": "workspace_id,source_id,item_id",
        "outbox_event": "id,workspace_id,event_type,payload",
        "consumer_receipt": "workspace_id,outbox_event_id,consumer_name",
        "backtest_dataset_replay_bundle": (
            "workspace_id,bundle_hash,schema_version,symbol,evaluation_start,range_end,"
            "rights_provider,rights_purpose,policy_fingerprint,bundle_json,canonical_bundle"
        ),
        "backtest_dataset_manifest": (
            "workspace_id,dataset_hash,status,symbol,interval,warmup_start,evaluation_start,"
            "range_end,prepared_at,policy_fingerprint,manifest_json,canonical_manifest"
        ),
    }
    for table, columns in grants.items():
        op.execute(f"GRANT SELECT ({columns}) ON public.{table} TO {RIGHTS_ROLE}")

    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_sha256_canonical_json(json) TO {RIGHTS_ROLE}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_canonical_utc_timestamp_json(timestamptz) "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_compact_canonical_json(json) TO {RIGHTS_ROLE}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_scorer_test_report_has_forbidden_payload(jsonb) "
        f"TO {RIGHTS_ROLE}"
    )


def _create_policy_fingerprint() -> None:
    op.execute("""
        CREATE FUNCTION public.wp06_scorer_decimal_python_text(value numeric)
        RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            rendered text;
            digits text;
            exponent integer;
            adjusted integer;
            decimal_places integer;
            point_at integer;
            sign_prefix text := '';
            mantissa text;
        BEGIN
            IF value = 0 THEN
                RETURN '0';
            END IF;
            IF value < 0 THEN
                sign_prefix := '-';
            END IF;
            rendered := pg_catalog.abs(value)::text;
            decimal_places := CASE WHEN pg_catalog.strpos(rendered, '.') = 0 THEN 0
                                   ELSE pg_catalog.length(pg_catalog.split_part(rendered, '.', 2))
                              END;
            digits := pg_catalog.replace(rendered, '.', '');
            digits := pg_catalog.regexp_replace(digits, '^0+', '');
            IF digits = '' THEN
                digits := '0';
            END IF;
            WHILE pg_catalog.length(digits) > 1 AND pg_catalog.right(digits, 1) = '0' LOOP
                digits := pg_catalog.left(digits, pg_catalog.length(digits) - 1);
                decimal_places := decimal_places - 1;
            END LOOP;
            exponent := -decimal_places;
            adjusted := exponent + pg_catalog.length(digits) - 1;
            IF exponent > 0 OR adjusted < -6 THEN
                mantissa := pg_catalog.left(digits, 1);
                IF pg_catalog.length(digits) > 1 THEN
                    mantissa := mantissa || '.' || pg_catalog.substr(digits, 2);
                END IF;
                RETURN sign_prefix || mantissa || 'E'
                    || CASE WHEN adjusted >= 0 THEN '+' ELSE '' END || adjusted::text;
            END IF;
            point_at := pg_catalog.length(digits) + exponent;
            IF point_at <= 0 THEN
                RETURN sign_prefix || '0.' || pg_catalog.repeat('0', -point_at) || digits;
            ELSIF point_at >= pg_catalog.length(digits) THEN
                RETURN sign_prefix || digits
                    || pg_catalog.repeat('0', point_at - pg_catalog.length(digits));
            END IF;
            RETURN sign_prefix || pg_catalog.left(digits, point_at) || '.'
                || pg_catalog.substr(digits, point_at + 1);
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.wp06_scorer_decimal_python_text(numeric) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_scorer_decimal_python_text(numeric) "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute("""
        CREATE FUNCTION public.wp06_scorer_source_policy_fingerprint(
            arg_source_id text,
            arg_sequence integer,
            arg_policy_id text,
            arg_state text,
            arg_allowed_purposes jsonb,
            arg_evidence_ref text,
            arg_reviewed_at timestamptz,
            arg_valid_until timestamptz,
            arg_approved_by text,
            arg_publisher text,
            arg_endpoint_url text,
            arg_access_method text,
            arg_auth_scope text,
            arg_attribution text,
            arg_retention_ref text,
            arg_quota_window_seconds bigint,
            arg_quota_max_requests bigint,
            arg_quota_max_bytes bigint,
            arg_quota_max_cost numeric,
            arg_quota_cost_unit text
        ) RETURNS text LANGUAGE sql IMMUTABLE
        SET search_path = pg_catalog, pg_temp AS $$
            SELECT public.wp06_sha256_canonical_json(pg_catalog.json_build_object(
                'access_method', arg_access_method,
                'allowed_purposes', (
                    SELECT COALESCE(pg_catalog.json_agg(purpose.value ORDER BY purpose.value),
                                    '[]'::json)
                      FROM pg_catalog.jsonb_array_elements_text(arg_allowed_purposes)
                           AS purpose(value)
                ),
                'approved_by', arg_approved_by,
                'attribution', arg_attribution,
                'auth_scope', arg_auth_scope,
                'budget', pg_catalog.json_build_object(
                    'cost_unit', arg_quota_cost_unit,
                    'max_bytes', arg_quota_max_bytes,
                    'max_cost', public.wp06_scorer_decimal_python_text(arg_quota_max_cost),
                    'max_requests', arg_quota_max_requests,
                    'window', pg_catalog.json_build_array(
                        pg_catalog.div(arg_quota_window_seconds, 86400),
                        pg_catalog.mod(arg_quota_window_seconds, 86400), 0
                    )
                ),
                'endpoint_url', arg_endpoint_url,
                'evidence_ref', arg_evidence_ref,
                'policy_id', arg_policy_id,
                'publisher', arg_publisher,
                'retention_ref', arg_retention_ref,
                'reviewed_at', public.wp06_canonical_utc_timestamp_json(arg_reviewed_at),
                'sequence', arg_sequence,
                'source_id', arg_source_id,
                'state', arg_state,
                'valid_until', public.wp06_canonical_utc_timestamp_json(arg_valid_until)
            ))
        $$
    """)
    signature = (
        "text,integer,text,text,jsonb,text,timestamptz,timestamptz,text,text,text,text,text,"
        "text,text,bigint,bigint,bigint,numeric,text"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_scorer_source_policy_fingerprint("
        f"{signature}) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_scorer_source_policy_fingerprint("
        f"{signature}) TO {RIGHTS_ROLE}"
    )


def _create_scorer_source_rights_gateway() -> None:
    op.execute("""
        CREATE FUNCTION public.wp06_recheck_scorer_current_rights(
            target_workspace uuid, target_scorer_artifact_id uuid
        ) RETURNS TABLE(
            scorer_artifact_id uuid,
            artifact_hash text,
            proof_hash text,
            bundle_hash text,
            source_scope_hash text,
            training_membership_digest text,
            validation_membership_digest text,
            binance_training_scope_digest text,
            source_rights_digest text,
            fingerprint text,
            checked_at timestamptz
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            artifact_row record;
            attempt_row record;
            dataset_row record;
            proof_row record;
            access_row record;
            policy_row record;
            item_ref record;
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
            source_key text;
            v_manifest_id uuid;
            rights_checked_at timestamptz;
            assignment_train_count integer;
            assignment_validation_count integer;
            lineage_train_count integer;
            lineage_validation_count integer;
            train_members json;
            validation_members json;
            training_digest text;
            validation_digest text;
            source_rights jsonb;
            source_count integer;
            receipt_count integer;
            manifest_source_count integer;
            evaluation_source_count integer;
            current_policy_fingerprint text;
            training_scope json;
            training_scope_hash text;
            c3_current_rights_hash text;
            bundle_row record;
            child_row record;
            source_rights_hash text;
            source_fingerprint text;
        BEGIN
            IF target_workspace IS NULL OR target_scorer_artifact_id IS NULL
               OR target_workspace IS DISTINCT FROM
                   NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_WORKSPACE_DENIED';
            END IF;
            IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_READ_COMMITTED_REQUIRED';
            END IF;

            SELECT artifact.id, artifact.attempt_id, artifact.base_artifact_id,
                   artifact.normalized_dataset_id, artifact.manifest_hash,
                   artifact.fold_plan_hash, artifact.fold_id, artifact.approval_hash,
                   artifact.base_artifact_hash, artifact.schema_version, artifact.state,
                   artifact.calibration_policy_hash, artifact.vector_schema_version,
                   artifact.vector_schema_hash, artifact.training_membership_digest,
                   artifact.validation_membership_digest, artifact.training_sample_count,
                   artifact.validation_sample_count, artifact.artifact_hash,
                   artifact.dependency_fingerprint, artifact.strategy_release_id,
                   artifact.strategy_release_content_hash
              INTO artifact_row
              FROM public.learning_scorer_artifact AS artifact
             WHERE artifact.workspace_id = target_workspace
               AND artifact.id = target_scorer_artifact_id;
            IF NOT FOUND OR artifact_row.schema_version
                    <> 'crypidian.learning.scorer-temperature.v2'
               OR artifact_row.state <> 'ready_for_evaluation'
               OR artifact_row.artifact_hash !~ '^[0-9a-f]{64}$'
               OR artifact_row.calibration_policy_hash !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_ARTIFACT_NOT_READY';
            END IF;

            SELECT attempt.workspace_id, attempt.id, attempt.normalized_dataset_id,
                   attempt.manifest_hash, attempt.fold_plan_hash, attempt.fold_id,
                   attempt.approval_hash, attempt.split_policy_version
              INTO attempt_row
              FROM public.learning_training_attempt AS attempt
             WHERE attempt.workspace_id = target_workspace
               AND attempt.id = artifact_row.attempt_id;
            IF NOT FOUND OR attempt_row.normalized_dataset_id
                    IS DISTINCT FROM artifact_row.normalized_dataset_id
               OR attempt_row.manifest_hash IS DISTINCT FROM artifact_row.manifest_hash
               OR attempt_row.fold_plan_hash IS DISTINCT FROM artifact_row.fold_plan_hash
               OR attempt_row.fold_id IS DISTINCT FROM artifact_row.fold_id
               OR attempt_row.approval_hash IS DISTINCT FROM artifact_row.approval_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_ATTEMPT_LINEAGE_MISMATCH';
            END IF;

            SELECT dataset.workspace_id, dataset.id, dataset.manifest_hash,
                   dataset.fold_plan_hash, dataset.approval_hash
              INTO dataset_row
              FROM public.learning_normalized_dataset AS dataset
             WHERE dataset.workspace_id = target_workspace
               AND dataset.id = artifact_row.normalized_dataset_id;
            IF NOT FOUND OR dataset_row.manifest_hash IS DISTINCT FROM artifact_row.manifest_hash
               OR dataset_row.fold_plan_hash IS DISTINCT FROM artifact_row.fold_plan_hash
               OR dataset_row.approval_hash IS DISTINCT FROM artifact_row.approval_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_DATASET_LINEAGE_MISMATCH';
            END IF;

            SELECT proof.id, proof.evaluation_run_id, proof.attempt_id,
                   proof.normalized_dataset_id, proof.manifest_hash, proof.fold_plan_hash,
                   proof.fold_id, proof.approval_hash, proof.bundle_hash,
                   proof.vector_schema_version, proof.vector_schema_hash,
                   proof.source_scope_hash, proof.proof_hash, proof.canonical_proof
              INTO proof_row
              FROM public.learning_scorer_test_opportunity_proof AS proof
             WHERE proof.workspace_id = target_workspace
               AND proof.attempt_id = artifact_row.attempt_id
               AND proof.fold_id = artifact_row.fold_id;
            IF NOT FOUND OR proof_row.normalized_dataset_id
                    IS DISTINCT FROM artifact_row.normalized_dataset_id
               OR proof_row.manifest_hash IS DISTINCT FROM artifact_row.manifest_hash
               OR proof_row.fold_plan_hash IS DISTINCT FROM artifact_row.fold_plan_hash
               OR proof_row.fold_id IS DISTINCT FROM artifact_row.fold_id
               OR proof_row.approval_hash IS DISTINCT FROM artifact_row.approval_hash
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    proof_row.canonical_proof, 'UTF8')), 'hex')
                    IS DISTINCT FROM proof_row.proof_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_PROOF_LINEAGE_MISMATCH';
            END IF;

            SELECT access.id, access.evaluation_run_id, access.attempt_id,
                   access.normalized_dataset_id, access.proof_id, access.manifest_hash,
                   access.fold_plan_hash, access.fold_id, access.approval_hash,
                   access.bundle_hash, access.source_scope_hash, access.proof_hash,
                   access.scorer_artifact_hash, access.policy_hash,
                   access.current_rights_receipt_hash, access.receipt_hash
              INTO access_row
              FROM public.learning_scorer_test_access_receipt AS access
             WHERE access.workspace_id = target_workspace
               AND access.evaluation_run_id = proof_row.evaluation_run_id
               AND access.attempt_id = artifact_row.attempt_id;
            -- policy_hash là ngưỡng đánh giá TEST C3; không đồng nhất với calibration policy.
            IF NOT FOUND OR access_row.id IS NULL
               OR access_row.proof_id IS DISTINCT FROM proof_row.id
               OR access_row.normalized_dataset_id IS DISTINCT FROM
                    artifact_row.normalized_dataset_id
               OR access_row.manifest_hash IS DISTINCT FROM artifact_row.manifest_hash
               OR access_row.fold_plan_hash IS DISTINCT FROM artifact_row.fold_plan_hash
               OR access_row.fold_id IS DISTINCT FROM artifact_row.fold_id
               OR access_row.approval_hash IS DISTINCT FROM artifact_row.approval_hash
               OR access_row.bundle_hash IS DISTINCT FROM proof_row.bundle_hash
               OR access_row.source_scope_hash IS DISTINCT FROM proof_row.source_scope_hash
               OR access_row.proof_hash IS DISTINCT FROM proof_row.proof_hash
               OR access_row.scorer_artifact_hash IS DISTINCT FROM artifact_row.artifact_hash
               OR access_row.policy_hash IS NULL
               OR access_row.policy_hash !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_TEST_RECEIPT_MISMATCH';
            END IF;

            SELECT manifest.id INTO v_manifest_id
              FROM public.dataset_manifest AS manifest
             WHERE manifest.workspace_id = target_workspace
               AND manifest.manifest_hash = artifact_row.manifest_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_MANIFEST_MISSING';
            END IF;

            SELECT count(*) FILTER (WHERE assignment.role = 'train')::integer,
                   count(*) FILTER (WHERE assignment.role = 'validation')::integer
              INTO assignment_train_count, assignment_validation_count
              FROM public.learning_fold_assignment AS assignment
             WHERE assignment.workspace_id = target_workspace
               AND assignment.manifest_hash = artifact_row.manifest_hash
               AND assignment.fold_plan_hash = artifact_row.fold_plan_hash
               AND assignment.fold_id = artifact_row.fold_id
               AND assignment.role IN ('train','validation');

            SELECT count(*) FILTER (WHERE assignment.role = 'train')::integer,
                   count(*) FILTER (WHERE assignment.role = 'validation')::integer,
                   COALESCE(pg_catalog.json_agg(pg_catalog.json_build_object(
                       'sample_hash', sample.sample_hash,
                       'sample_id', sample.sample_id,
                       'vector_hash', outcome.entry_vector_hash
                   ) ORDER BY sample.sample_id COLLATE "C")
                       FILTER (WHERE assignment.role = 'train'), '[]'::json),
                   COALESCE(pg_catalog.json_agg(pg_catalog.json_build_object(
                       'sample_hash', sample.sample_hash,
                       'sample_id', sample.sample_id,
                       'vector_hash', outcome.entry_vector_hash
                   ) ORDER BY sample.sample_id COLLATE "C")
                       FILTER (WHERE assignment.role = 'validation'), '[]'::json),
                   pg_catalog.bool_and(
                       opportunity.strategy_release_id = artifact_row.strategy_release_id
                       AND opportunity.strategy_content_hash =
                           artifact_row.strategy_release_content_hash
                       AND opportunity.feature_schema_hash = artifact_row.vector_schema_hash
                       AND opportunity.vector_hash IS NOT NULL
                   )
              INTO lineage_train_count, lineage_validation_count, train_members,
                   validation_members, current_tombstoned
              FROM public.learning_fold_assignment AS assignment
              JOIN public.learning_normalized_sample AS sample
                ON sample.workspace_id = assignment.workspace_id
               AND sample.manifest_hash = assignment.manifest_hash
               AND sample.fold_plan_hash = assignment.fold_plan_hash
               AND sample.sample_id = assignment.sample_id
              JOIN public.backtest_trade_outcome AS outcome
                ON outcome.workspace_id = sample.workspace_id
               AND outcome.id::text = sample.outcome_id
               AND outcome.outcome_hash = sample.outcome_hash
              JOIN public.backtest_decision_opportunity AS opportunity
                ON opportunity.workspace_id = outcome.workspace_id
               AND opportunity.trial_record_id = outcome.trial_record_id
               AND opportunity.run_id = outcome.run_id
               AND opportunity.manifest_hash = outcome.manifest_hash
               AND opportunity.event_id = outcome.entry_opportunity_event_id
              JOIN public.backtest_run AS run
                ON run.workspace_id = opportunity.workspace_id
               AND run.id = opportunity.run_id
             WHERE assignment.workspace_id = target_workspace
               AND assignment.manifest_hash = artifact_row.manifest_hash
               AND assignment.fold_plan_hash = artifact_row.fold_plan_hash
               AND assignment.fold_id = artifact_row.fold_id
               AND assignment.role IN ('train','validation')
               AND opportunity.schema_version = 'crypidian-replay-decision-opportunity-v3'
               AND opportunity.input_state = 'EVALUATED'
               AND opportunity.strategy_state = 'CANDIDATE'
               AND opportunity.candidate_id = outcome.entry_candidate_id
               AND opportunity.vector_hash = outcome.entry_vector_hash
               AND opportunity.feature_schema_hash = outcome.entry_feature_schema_hash
               AND opportunity.feature_schema_hash = sample.feature_schema_hash
               AND run.manifest->>'strategy_release_id' = opportunity.strategy_release_id
               AND run.manifest->>'strategy_content_hash' = opportunity.strategy_content_hash;
            IF assignment_train_count IS DISTINCT FROM artifact_row.training_sample_count
               OR assignment_validation_count
                    IS DISTINCT FROM artifact_row.validation_sample_count
               OR lineage_train_count IS DISTINCT FROM assignment_train_count
               OR lineage_validation_count IS DISTINCT FROM assignment_validation_count
               OR current_tombstoned IS DISTINCT FROM true THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_TRAINING_MEMBERSHIP_LINEAGE_INVALID';
            END IF;
            training_digest := public.wp06_sha256_canonical_json(train_members);
            validation_digest := public.wp06_sha256_canonical_json(validation_members);
            IF training_digest IS DISTINCT FROM artifact_row.training_membership_digest
               OR validation_digest IS DISTINCT FROM artifact_row.validation_membership_digest THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_TRAINING_MEMBERSHIP_MISMATCH';
            END IF;

            FOR source_key IN
                SELECT lock_source.source_id
                  FROM (
                      SELECT DISTINCT reference.source_id
                        FROM public.wp06_scorer_manifest_source_refs(
                            target_workspace, v_manifest_id
                        ) AS reference
                      UNION SELECT 'BINANCE_USDM_REST'::text AS source_id
                  ) AS lock_source
                 ORDER BY lock_source.source_id
            LOOP
                IF source_key IS NULL
                   OR source_key !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' THEN
                    RAISE EXCEPTION 'WP06_SCORER_RIGHTS_SOURCE_ID_INVALID';
                END IF;
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || source_key, 0));
            END LOOP;

            IF (SELECT count(*) FROM public.dataset_manifest_source_ref AS indexed_ref
                 WHERE indexed_ref.workspace_id = target_workspace
                   AND indexed_ref.manifest_id = v_manifest_id)
               IS DISTINCT FROM
               (SELECT count(*) FROM public.wp06_scorer_manifest_source_refs(
                    target_workspace, v_manifest_id))
               OR EXISTS (
                   SELECT 1
                     FROM public.wp06_scorer_manifest_source_refs(
                         target_workspace, v_manifest_id
                     ) AS canonical_ref
                    WHERE NOT EXISTS (
                        SELECT 1 FROM public.dataset_manifest_source_ref AS indexed_ref
                         WHERE indexed_ref.workspace_id = target_workspace
                           AND indexed_ref.manifest_id = v_manifest_id
                           AND indexed_ref.sample_id = canonical_ref.sample_id
                           AND indexed_ref.source_id = canonical_ref.source_id
                           AND indexed_ref.item_id = canonical_ref.item_id
                           AND indexed_ref.revision_id = canonical_ref.revision_id
                           AND indexed_ref.policy_id = canonical_ref.policy_id
                           AND indexed_ref.policy_sequence = canonical_ref.policy_sequence
                           AND indexed_ref.purpose = canonical_ref.purpose
                           AND indexed_ref.rights_hash = canonical_ref.rights_hash
                    )
               ) OR EXISTS (
                   SELECT 1 FROM public.dataset_manifest_source_ref AS indexed_ref
                    WHERE indexed_ref.workspace_id = target_workspace
                      AND indexed_ref.manifest_id = v_manifest_id
                      AND NOT EXISTS (
                          SELECT 1
                            FROM public.wp06_scorer_manifest_source_refs(
                                target_workspace, v_manifest_id
                            ) AS canonical_ref
                           WHERE canonical_ref.sample_id = indexed_ref.sample_id
                             AND canonical_ref.source_id = indexed_ref.source_id
                             AND canonical_ref.item_id = indexed_ref.item_id
                             AND canonical_ref.revision_id = indexed_ref.revision_id
                             AND canonical_ref.policy_id = indexed_ref.policy_id
                             AND canonical_ref.policy_sequence = indexed_ref.policy_sequence
                             AND canonical_ref.purpose = indexed_ref.purpose
                             AND canonical_ref.rights_hash = indexed_ref.rights_hash
                      )
               ) THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_SOURCE_REF_INDEX_MISMATCH';
            END IF;

            rights_checked_at := pg_catalog.clock_timestamp();
            SELECT count(*)::integer INTO source_count
              FROM public.dataset_manifest_source_ref AS reference
              JOIN public.learning_fold_assignment AS assignment
                ON assignment.workspace_id = target_workspace
               AND assignment.manifest_hash = artifact_row.manifest_hash
               AND assignment.fold_plan_hash = artifact_row.fold_plan_hash
               AND assignment.fold_id = artifact_row.fold_id
               AND assignment.sample_id = reference.sample_id
               AND assignment.role IN ('train','validation')
             WHERE reference.workspace_id = target_workspace
               AND reference.manifest_id = v_manifest_id;
            SELECT count(*)::integer INTO receipt_count
              FROM public.learning_source_training_receipt AS receipt
              JOIN public.learning_fold_assignment AS assignment
                ON assignment.workspace_id = target_workspace
               AND assignment.manifest_hash = artifact_row.manifest_hash
               AND assignment.fold_plan_hash = artifact_row.fold_plan_hash
               AND assignment.fold_id = artifact_row.fold_id
               AND assignment.sample_id = receipt.sample_id
               AND assignment.role IN ('train','validation')
             WHERE receipt.workspace_id = target_workspace
               AND receipt.normalized_dataset_id = artifact_row.normalized_dataset_id
               AND receipt.manifest_hash = artifact_row.manifest_hash
               AND receipt.fold_plan_hash = artifact_row.fold_plan_hash;
            IF receipt_count IS DISTINCT FROM source_count THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_BUILDER_RECEIPT_MISMATCH';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM public.dataset_manifest_source_ref AS reference
                  JOIN public.learning_fold_assignment AS assignment
                    ON assignment.workspace_id = target_workspace
                   AND assignment.manifest_hash = artifact_row.manifest_hash
                   AND assignment.fold_plan_hash = artifact_row.fold_plan_hash
                   AND assignment.fold_id = artifact_row.fold_id
                   AND assignment.sample_id = reference.sample_id
                   AND assignment.role IN ('train','validation')
                 WHERE reference.workspace_id = target_workspace
                   AND reference.manifest_id = v_manifest_id
                   AND NOT EXISTS (
                       SELECT 1 FROM public.learning_source_training_receipt AS receipt
                        WHERE receipt.workspace_id = target_workspace
                          AND receipt.normalized_dataset_id = artifact_row.normalized_dataset_id
                          AND receipt.manifest_hash = artifact_row.manifest_hash
                          AND receipt.fold_plan_hash = artifact_row.fold_plan_hash
                          AND receipt.sample_id = reference.sample_id
                          AND receipt.source_id = reference.source_id
                          AND receipt.item_id = reference.item_id
                          AND receipt.revision_id = reference.revision_id
                          AND receipt.policy_id = reference.policy_id
                          AND receipt.policy_sequence = reference.policy_sequence
                          AND receipt.purpose = reference.purpose
                          AND receipt.rights_hash = reference.rights_hash
                   )
            ) OR EXISTS (
                SELECT 1
                  FROM public.learning_source_training_receipt AS receipt
                  JOIN public.learning_fold_assignment AS assignment
                    ON assignment.workspace_id = target_workspace
                   AND assignment.manifest_hash = artifact_row.manifest_hash
                   AND assignment.fold_plan_hash = artifact_row.fold_plan_hash
                   AND assignment.fold_id = artifact_row.fold_id
                   AND assignment.sample_id = receipt.sample_id
                   AND assignment.role IN ('train','validation')
                 WHERE receipt.workspace_id = target_workspace
                   AND receipt.normalized_dataset_id = artifact_row.normalized_dataset_id
                   AND receipt.manifest_hash = artifact_row.manifest_hash
                   AND receipt.fold_plan_hash = artifact_row.fold_plan_hash
                   AND NOT EXISTS (
                       SELECT 1 FROM public.dataset_manifest_source_ref AS reference
                        WHERE reference.workspace_id = target_workspace
                          AND reference.manifest_id = v_manifest_id
                          AND reference.sample_id = receipt.sample_id
                          AND reference.source_id = receipt.source_id
                          AND reference.item_id = receipt.item_id
                          AND reference.revision_id = receipt.revision_id
                          AND reference.policy_id = receipt.policy_id
                          AND reference.policy_sequence = receipt.policy_sequence
                          AND reference.purpose = receipt.purpose
                          AND reference.rights_hash = receipt.rights_hash
                   )
            ) THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_BUILDER_RECEIPT_MISMATCH';
            END IF;

            SELECT count(*)::integer INTO manifest_source_count
              FROM public.dataset_manifest_source_ref AS reference
             WHERE reference.workspace_id = target_workspace
               AND reference.manifest_id = v_manifest_id;
            SELECT count(*)::integer INTO evaluation_source_count
              FROM public.learning_run_source_rights_receipt AS rights
             WHERE rights.workspace_id = target_workspace
               AND rights.access_kind = 'evaluation'
               AND rights.access_id = access_row.id
               AND rights.normalized_dataset_id = artifact_row.normalized_dataset_id
               AND rights.manifest_hash = artifact_row.manifest_hash
               AND rights.fold_plan_hash = artifact_row.fold_plan_hash;
            IF evaluation_source_count IS DISTINCT FROM manifest_source_count
               OR EXISTS (
                   SELECT 1
                     FROM public.dataset_manifest_source_ref AS reference
                    WHERE reference.workspace_id = target_workspace
                      AND reference.manifest_id = v_manifest_id
                      AND NOT EXISTS (
                          SELECT 1
                            FROM public.learning_run_source_rights_receipt AS rights
                           WHERE rights.workspace_id = target_workspace
                             AND rights.access_kind = 'evaluation'
                             AND rights.access_id = access_row.id
                             AND rights.normalized_dataset_id = artifact_row.normalized_dataset_id
                             AND rights.manifest_hash = artifact_row.manifest_hash
                             AND rights.fold_plan_hash = artifact_row.fold_plan_hash
                             AND rights.sample_id = reference.sample_id
                             AND rights.source_id = reference.source_id
                             AND rights.item_id = reference.item_id
                             AND rights.revision_id = reference.revision_id
                             AND rights.policy_id = reference.policy_id
                             AND rights.policy_sequence = reference.policy_sequence
                             AND rights.purpose = reference.purpose
                             AND rights.rights_hash = reference.rights_hash
                      )
               ) OR EXISTS (
                   SELECT 1
                     FROM public.learning_run_source_rights_receipt AS rights
                    WHERE rights.workspace_id = target_workspace
                      AND rights.access_kind = 'evaluation'
                      AND rights.access_id = access_row.id
                      AND rights.normalized_dataset_id = artifact_row.normalized_dataset_id
                      AND rights.manifest_hash = artifact_row.manifest_hash
                      AND rights.fold_plan_hash = artifact_row.fold_plan_hash
                      AND NOT EXISTS (
                          SELECT 1
                            FROM public.dataset_manifest_source_ref AS reference
                           WHERE reference.workspace_id = target_workspace
                             AND reference.manifest_id = v_manifest_id
                             AND reference.sample_id = rights.sample_id
                             AND reference.source_id = rights.source_id
                             AND reference.item_id = rights.item_id
                             AND reference.revision_id = rights.revision_id
                             AND reference.policy_id = rights.policy_id
                             AND reference.policy_sequence = rights.policy_sequence
                             AND reference.purpose = rights.purpose
                             AND reference.rights_hash = rights.rights_hash
                      )
               ) THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_EVALUATION_RECEIPT_MISMATCH';
            END IF;

            FOR item_ref IN
                SELECT rights.sample_id, rights.source_id, rights.item_id,
                       rights.revision_id, rights.policy_id, rights.policy_sequence,
                       rights.purpose, rights.rights_hash
                  FROM public.learning_run_source_rights_receipt AS rights
                 WHERE rights.workspace_id = target_workspace
                   AND rights.access_kind = 'evaluation'
                   AND rights.access_id = access_row.id
                   AND rights.normalized_dataset_id = artifact_row.normalized_dataset_id
                   AND rights.manifest_hash = artifact_row.manifest_hash
                   AND rights.fold_plan_hash = artifact_row.fold_plan_hash
                 ORDER BY rights.source_id COLLATE "C", rights.item_id COLLATE "C",
                          rights.sample_id COLLATE "C", rights.revision_id COLLATE "C",
                          rights.policy_id COLLATE "C"
            LOOP
                IF item_ref.purpose <> 'training'
                   OR item_ref.source_id !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
                   OR item_ref.item_id !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
                   OR item_ref.revision_id !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
                   OR item_ref.policy_id IS NULL
                   OR item_ref.policy_sequence <= 0
                   OR item_ref.rights_hash !~ '^[0-9a-f]{64}$' THEN
                    RAISE EXCEPTION 'WP06_SCORER_RIGHTS_SOURCE_LINEAGE_INVALID';
                END IF;

                IF EXISTS (
                    SELECT 1 FROM public.outbox_event AS event
                     WHERE event.workspace_id = target_workspace
                       AND event.event_type = 'research.rights_changed.v1'
                       AND event.payload->>'source_id' = item_ref.source_id
                       AND (NOT (event.payload ? 'item_id')
                            OR event.payload->>'item_id' = item_ref.item_id)
                       AND NOT EXISTS (
                           SELECT 1 FROM public.consumer_receipt AS consumer
                            WHERE consumer.workspace_id = event.workspace_id
                              AND consumer.outbox_event_id = event.id
                              AND consumer.consumer_name =
                                  'wp06.dataset-rights-eligibility.v1'
                       )
                ) OR EXISTS (
                    SELECT 1 FROM public.dataset_manifest_eligibility_event AS invalidation
                     WHERE invalidation.workspace_id = target_workspace
                       AND invalidation.manifest_id = v_manifest_id
                       AND invalidation.sample_id = item_ref.sample_id
                       AND invalidation.source_id = item_ref.source_id
                       AND invalidation.item_id = item_ref.item_id
                       AND invalidation.revision_id = item_ref.revision_id
                       AND invalidation.policy_id = item_ref.policy_id
                       AND invalidation.policy_sequence = item_ref.policy_sequence
                       AND invalidation.purpose = item_ref.purpose
                       AND invalidation.rights_hash = item_ref.rights_hash
                ) THEN
                    RAISE EXCEPTION 'WP06_SCORER_RIGHTS_INVALIDATED';
                END IF;

                IF EXISTS (
                    SELECT 1 FROM public.source_policy_revision AS policy
                     WHERE policy.workspace_id = target_workspace
                       AND policy.source_id = item_ref.source_id
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
                       AND revision.source_id = item_ref.source_id
                       AND revision.item_id = item_ref.item_id
                       AND NOT EXISTS (
                           SELECT 1 FROM public.source_item_observation AS observation
                            WHERE observation.workspace_id = revision.workspace_id
                              AND observation.source_id = revision.source_id
                              AND observation.item_id = revision.item_id
                              AND observation.sequence = revision.sequence
                              AND observation.revision_id = revision.revision_id
                       )
                ) THEN
                    RAISE EXCEPTION 'WP06_SCORER_RIGHTS_OBSERVATION_MISSING';
                END IF;

                SELECT policy.source_id, policy.sequence, policy.policy_id, policy.state,
                       policy.allowed_purposes, policy.evidence_ref, policy.reviewed_at,
                       policy.valid_until, policy.approved_by, policy.publisher,
                       policy.endpoint_url, policy.access_method, policy.auth_scope,
                       policy.attribution, policy.retention_ref, policy.quota_window_seconds,
                       policy.quota_max_requests, policy.quota_max_bytes, policy.quota_max_cost,
                       policy.quota_cost_unit, policy.created_at
                  INTO policy_row
                  FROM public.source_policy_revision AS policy
                 WHERE policy.workspace_id = target_workspace
                   AND policy.source_id = item_ref.source_id
                 ORDER BY policy.sequence DESC LIMIT 1;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'WP06_SCORER_RIGHTS_POLICY_DENIED';
                END IF;
                current_policy_id := policy_row.policy_id;
                current_policy_sequence := policy_row.sequence;
                current_policy_state := policy_row.state;
                current_policy_purposes := policy_row.allowed_purposes;
                current_policy_valid_until := policy_row.valid_until;
                source_rights_hash := public.wp06_scorer_source_policy_fingerprint(
                    policy_row.source_id, policy_row.sequence, policy_row.policy_id,
                    policy_row.state, policy_row.allowed_purposes, policy_row.evidence_ref,
                    policy_row.reviewed_at, policy_row.valid_until, policy_row.approved_by,
                    policy_row.publisher, policy_row.endpoint_url, policy_row.access_method,
                    policy_row.auth_scope, policy_row.attribution, policy_row.retention_ref,
                    policy_row.quota_window_seconds, policy_row.quota_max_requests,
                    policy_row.quota_max_bytes, policy_row.quota_max_cost,
                    policy_row.quota_cost_unit);
                IF current_policy_id IS DISTINCT FROM item_ref.policy_id
                   OR current_policy_sequence IS DISTINCT FROM item_ref.policy_sequence
                   OR current_policy_state IS DISTINCT FROM 'verified'
                   OR NOT (current_policy_purposes @> '["training"]'::jsonb)
                   OR policy_row.reviewed_at IS NULL
                   OR policy_row.reviewed_at > rights_checked_at
                   OR (current_policy_valid_until IS NOT NULL
                       AND current_policy_valid_until <= rights_checked_at)
                   OR source_rights_hash IS DISTINCT FROM item_ref.rights_hash
                   OR NOT EXISTS (
                       SELECT 1 FROM public.source_policy_observation AS observation
                        WHERE observation.workspace_id = target_workspace
                          AND observation.source_id = item_ref.source_id
                          AND observation.sequence = item_ref.policy_sequence
                          AND observation.policy_id = item_ref.policy_id
                          AND observation.known_at >= policy_row.created_at
                          AND observation.known_at >= policy_row.reviewed_at
                          AND observation.known_at < rights_checked_at
                   ) THEN
                    RAISE EXCEPTION 'WP06_SCORER_RIGHTS_POLICY_DENIED';
                END IF;

                SELECT revision.revision_id, revision.sequence, revision.available_at,
                       revision.tombstoned, revision.retention_expires_at
                  INTO current_revision_id, current_revision_sequence,
                       current_available_at, current_tombstoned,
                       current_retention_expires_at
                  FROM public.source_item_revision AS revision
                 WHERE revision.workspace_id = target_workspace
                   AND revision.source_id = item_ref.source_id
                   AND revision.item_id = item_ref.item_id
                 ORDER BY revision.sequence DESC LIMIT 1;
                IF NOT FOUND OR current_revision_id IS DISTINCT FROM item_ref.revision_id
                   OR current_available_at IS NULL OR current_available_at > rights_checked_at
                   OR current_tombstoned
                   OR (current_retention_expires_at IS NOT NULL
                       AND current_retention_expires_at <= rights_checked_at)
                   OR NOT EXISTS (
                       SELECT 1 FROM public.source_item_observation AS observation
                        WHERE observation.workspace_id = target_workspace
                          AND observation.source_id = item_ref.source_id
                          AND observation.item_id = item_ref.item_id
                          AND observation.sequence = current_revision_sequence
                          AND observation.revision_id = item_ref.revision_id
                          AND observation.known_at < rights_checked_at
                   )
                   OR EXISTS (
                       SELECT 1 FROM public.source_item_takedown AS takedown
                        WHERE takedown.workspace_id = target_workspace
                          AND takedown.source_id = item_ref.source_id
                          AND takedown.item_id = item_ref.item_id
                   ) THEN
                    RAISE EXCEPTION 'WP06_SCORER_RIGHTS_ITEM_DENIED';
                END IF;
            END LOOP;

            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                       'item_id', rights.item_id,
                       'policy_id', rights.policy_id,
                       'policy_sequence', rights.policy_sequence,
                       'purpose', rights.purpose,
                       'rights_hash', rights.rights_hash,
                       'revision_id', rights.revision_id,
                       'sample_id', rights.sample_id,
                       'source_id', rights.source_id
                   ) ORDER BY rights.sample_id COLLATE "C", rights.source_id COLLATE "C",
                              rights.item_id COLLATE "C", rights.revision_id COLLATE "C",
                              rights.policy_id COLLATE "C"), '[]'::jsonb)
              INTO source_rights
              FROM public.learning_run_source_rights_receipt AS rights
             WHERE rights.workspace_id = target_workspace
               AND rights.access_kind = 'evaluation'
               AND rights.access_id = access_row.id
               AND rights.normalized_dataset_id = artifact_row.normalized_dataset_id
               AND rights.manifest_hash = artifact_row.manifest_hash
               AND rights.fold_plan_hash = artifact_row.fold_plan_hash;
            source_fingerprint := public.wp06_sha256_canonical_json(source_rights::json);

            SELECT bundle.workspace_id, bundle.bundle_hash, bundle.schema_version, bundle.symbol,
                   bundle.evaluation_start, bundle.range_end, bundle.rights_provider,
                   bundle.rights_purpose, bundle.policy_fingerprint, bundle.bundle_json,
                   bundle.canonical_bundle INTO bundle_row
              FROM public.backtest_dataset_replay_bundle AS bundle
             WHERE bundle.workspace_id = target_workspace
               AND bundle.bundle_hash = proof_row.bundle_hash;
            IF NOT FOUND OR bundle_row.bundle_hash IS DISTINCT FROM proof_row.bundle_hash
               OR bundle_row.rights_provider <> 'BINANCE_USDM_REST'
               OR bundle_row.rights_purpose <> 'BACKTEST_RESEARCH'
               OR bundle_row.schema_version <> 1
               OR bundle_row.canonical_bundle::jsonb IS DISTINCT FROM bundle_row.bundle_json
               OR public.wp06_compact_canonical_json(bundle_row.canonical_bundle::json)
                    IS DISTINCT FROM bundle_row.canonical_bundle
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    bundle_row.canonical_bundle, 'UTF8')), 'hex')
                    IS DISTINCT FROM bundle_row.bundle_hash
               OR bundle_row.bundle_json->>'workspace_id' IS DISTINCT FROM target_workspace::text
               OR bundle_row.bundle_json->>'schema_version' IS DISTINCT FROM
                    bundle_row.schema_version::text
               OR bundle_row.bundle_json->>'symbol' IS DISTINCT FROM bundle_row.symbol
               OR bundle_row.bundle_json->>'evaluation_start' IS DISTINCT FROM
                    (public.wp06_canonical_utc_timestamp_json(bundle_row.evaluation_start)#>>'{}')
               OR bundle_row.bundle_json->>'range_end' IS DISTINCT FROM
                    (public.wp06_canonical_utc_timestamp_json(bundle_row.range_end)#>>'{}')
               OR bundle_row.bundle_json->>'rights_provider' IS DISTINCT FROM
                    bundle_row.rights_provider
               OR bundle_row.bundle_json->>'rights_purpose' IS DISTINCT FROM
                    bundle_row.rights_purpose
               OR bundle_row.bundle_json->>'policy_fingerprint' IS DISTINCT FROM
                    bundle_row.policy_fingerprint
               OR pg_catalog.jsonb_typeof(bundle_row.bundle_json->'components')
                    IS DISTINCT FROM 'array'
               OR pg_catalog.jsonb_array_length(bundle_row.bundle_json->'components') = 0 THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_BUNDLE_INVALID';
            END IF;

            SELECT policy.source_id, policy.sequence, policy.policy_id, policy.state,
                   policy.allowed_purposes, policy.evidence_ref, policy.reviewed_at,
                   policy.valid_until, policy.approved_by, policy.publisher,
                   policy.endpoint_url, policy.access_method, policy.auth_scope,
                   policy.attribution, policy.retention_ref, policy.quota_window_seconds,
                   policy.quota_max_requests, policy.quota_max_bytes, policy.quota_max_cost,
                   policy.quota_cost_unit, policy.created_at INTO policy_row
              FROM public.source_policy_revision AS policy
             WHERE policy.workspace_id = target_workspace
               AND policy.source_id = 'BINANCE_USDM_REST'
             ORDER BY policy.sequence DESC LIMIT 1;
            IF NOT FOUND OR policy_row.state <> 'verified'
               OR NOT (policy_row.allowed_purposes @> '["training"]'::jsonb)
               OR policy_row.reviewed_at IS NULL
               OR (policy_row.valid_until IS NOT NULL
                   AND policy_row.valid_until <= rights_checked_at)
               OR policy_row.evidence_ref IS NULL
               OR policy_row.evidence_ref !~ '^https://[^/?#]+([/?#].*)?$' THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_BINANCE_POLICY_DENIED';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.source_policy_revision AS policy
                 WHERE policy.workspace_id = target_workspace
                   AND policy.source_id = 'BINANCE_USDM_REST'
                   AND NOT EXISTS (
                       SELECT 1 FROM public.source_policy_observation AS observation
                        WHERE observation.workspace_id = policy.workspace_id
                          AND observation.source_id = policy.source_id
                          AND observation.sequence = policy.sequence
                          AND observation.policy_id = policy.policy_id
                          AND observation.known_at >= policy.created_at
                          AND (policy.reviewed_at IS NULL
                               OR observation.known_at >= policy.reviewed_at)
                   )
            ) OR NOT EXISTS (
                SELECT 1 FROM public.source_policy_observation AS observation
                 WHERE observation.workspace_id = target_workspace
                   AND observation.source_id = 'BINANCE_USDM_REST'
                   AND observation.sequence = policy_row.sequence
                   AND observation.policy_id = policy_row.policy_id
                   AND observation.known_at < rights_checked_at
            ) OR EXISTS (
                SELECT 1
                  FROM public.source_policy_observation AS current_observation
                  JOIN public.source_policy_observation AS previous_observation
                    ON previous_observation.workspace_id = current_observation.workspace_id
                   AND previous_observation.source_id = current_observation.source_id
                   AND previous_observation.sequence = current_observation.sequence - 1
                 WHERE current_observation.workspace_id = target_workspace
                   AND current_observation.source_id = 'BINANCE_USDM_REST'
                   AND current_observation.known_at < previous_observation.known_at
            ) THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_BINANCE_OBSERVATION_DENIED';
            END IF;
            IF (SELECT count(*) FROM public.source_policy_revision AS policy
                 WHERE policy.workspace_id = target_workspace
                   AND policy.source_id = 'BINANCE_USDM_REST')
               <> policy_row.sequence THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_BINANCE_POLICY_HISTORY_INVALID';
            END IF;

            current_policy_fingerprint := public.wp06_scorer_source_policy_fingerprint(
                policy_row.source_id, policy_row.sequence, policy_row.policy_id,
                policy_row.state, policy_row.allowed_purposes, policy_row.evidence_ref,
                policy_row.reviewed_at, policy_row.valid_until, policy_row.approved_by,
                policy_row.publisher, policy_row.endpoint_url, policy_row.access_method,
                policy_row.auth_scope, policy_row.attribution, policy_row.retention_ref,
                policy_row.quota_window_seconds, policy_row.quota_max_requests,
                policy_row.quota_max_bytes, policy_row.quota_max_cost,
                policy_row.quota_cost_unit);
            SELECT COALESCE(pg_catalog.json_agg(pg_catalog.json_build_object(
                       'bars_content_hash', child.manifest_json->>'bars_content_hash',
                       'bundle_hash', proof_row.bundle_hash,
                       'child_dataset_hash', child.dataset_hash,
                       'interval', child.interval,
                       'policy_fingerprint', current_policy_fingerprint,
                       'policy_id', policy_row.policy_id,
                       'provider', 'BINANCE_USDM_REST',
                       'purpose', 'training',
                       'symbol', bundle_row.symbol,
                       'terms_url', policy_row.evidence_ref,
                       'verified_at', public.wp06_canonical_utc_timestamp_json(
                           policy_row.reviewed_at),
                       'workspace_id', target_workspace::text
                   ) ORDER BY child.dataset_hash COLLATE "C"), '[]'::json)
              INTO training_scope
              FROM pg_catalog.jsonb_array_elements(bundle_row.bundle_json->'components')
                   AS component(value)
              JOIN public.backtest_dataset_manifest AS child
                ON child.workspace_id = target_workspace
               AND child.dataset_hash = component.value->>'dataset_hash'
             WHERE child.status = 'READY'
               AND child.symbol = bundle_row.symbol
               AND child.interval = component.value->>'interval'
               AND child.policy_fingerprint = bundle_row.policy_fingerprint
               AND child.evaluation_start = bundle_row.evaluation_start
               AND child.range_end = bundle_row.range_end
               AND child.manifest_json->>'dataset_hash' = child.dataset_hash
               AND child.manifest_json->>'status' = child.status
               AND child.manifest_json->>'symbol' = child.symbol
               AND child.manifest_json->>'interval' = child.interval
               AND child.manifest_json->>'warmup_start' =
                    (public.wp06_canonical_utc_timestamp_json(child.warmup_start)#>>'{}')
               AND child.manifest_json->>'prepared_at' =
                    (public.wp06_canonical_utc_timestamp_json(child.prepared_at)#>>'{}')
               AND child.manifest_json->>'bars_content_hash' =
                    component.value->>'bars_content_hash'
               AND child.manifest_json->>'rights_workspace_id' = target_workspace::text
               AND child.manifest_json->>'provider' = bundle_row.rights_provider
               AND child.manifest_json->>'purpose' = bundle_row.rights_purpose
               AND child.manifest_json->>'policy_fingerprint' = child.policy_fingerprint
               AND child.manifest_json->>'evaluation_start' =
                    (public.wp06_canonical_utc_timestamp_json(bundle_row.evaluation_start)#>>'{}')
               AND child.manifest_json->>'range_end' =
                    (public.wp06_canonical_utc_timestamp_json(bundle_row.range_end)#>>'{}')
               AND child.manifest_json = child.canonical_manifest::jsonb
               AND public.wp06_compact_canonical_json(child.canonical_manifest::json)
                    IS NOT DISTINCT FROM child.canonical_manifest
               AND pg_catalog.length(child.canonical_manifest) - pg_catalog.length(
                    pg_catalog.replace(child.canonical_manifest,
                        ',"dataset_hash":"' || child.dataset_hash || '"', ''))
                    = pg_catalog.length(',"dataset_hash":"' || child.dataset_hash || '"')
               AND pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    pg_catalog.replace(child.canonical_manifest,
                        ',"dataset_hash":"' || child.dataset_hash || '"', ''), 'UTF8')),
                    'hex') IS NOT DISTINCT FROM child.dataset_hash;
            IF training_scope IS NULL
               OR pg_catalog.json_array_length(training_scope)
                    <> pg_catalog.jsonb_array_length(bundle_row.bundle_json->'components') THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_BUNDLE_CHILD_INVALID';
            END IF;
            training_scope_hash := public.wp06_sha256_canonical_json(training_scope);
            c3_current_rights_hash := pg_catalog.encode(pg_catalog.sha256(
                pg_catalog.convert_to(pg_catalog.jsonb_build_object(
                    'training_scope_digest', training_scope_hash,
                    'manifest_hash', artifact_row.manifest_hash,
                    'fold_plan_hash', artifact_row.fold_plan_hash,
                    'normalized_dataset_id', artifact_row.normalized_dataset_id,
                    'source_rights', source_rights
                )::text, 'UTF8')), 'hex');
            IF c3_current_rights_hash IS DISTINCT FROM
                    access_row.current_rights_receipt_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_TEST_RECEIPT_STALE';
            END IF;

            fingerprint := public.wp06_sha256_canonical_json(pg_catalog.json_build_object(
                'artifact_hash', artifact_row.artifact_hash,
                'binance_training_scope_digest', training_scope_hash,
                'bundle_hash', proof_row.bundle_hash,
                'proof_hash', proof_row.proof_hash,
                'source_rights_digest', source_fingerprint,
                'source_scope_hash', proof_row.source_scope_hash,
                'test_current_rights_receipt_hash', access_row.current_rights_receipt_hash,
                'training_membership_digest', training_digest,
                'validation_membership_digest', validation_digest,
                'workspace_id', target_workspace::text
            ));

            scorer_artifact_id := artifact_row.id;
            artifact_hash := artifact_row.artifact_hash;
            proof_hash := proof_row.proof_hash;
            bundle_hash := proof_row.bundle_hash;
            source_scope_hash := proof_row.source_scope_hash;
            training_membership_digest := training_digest;
            validation_membership_digest := validation_digest;
            binance_training_scope_digest := training_scope_hash;
            source_rights_digest := source_fingerprint;
            checked_at := rights_checked_at;
            RETURN NEXT;
        END $$
    """)
    op.execute(
        f"ALTER FUNCTION public.wp06_recheck_scorer_current_rights(uuid, uuid) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_recheck_scorer_current_rights(uuid, uuid) "
        "FROM PUBLIC, " + ADMIN + ", " + RUNTIME + ", " + EVALUATOR
    )


def _create_deployment_rights_gateway() -> None:
    op.execute(f"""
        CREATE FUNCTION public.wp06_verify_scorer_deployment_rights(
            target_workspace uuid,
            target_scorer_artifact_id uuid,
            target_evaluation_report_id uuid
        ) RETURNS TABLE(
            workspace_id uuid,
            scorer_artifact_id uuid,
            evaluation_report_id uuid,
            shadow_eligible boolean,
            paper_gate_eligible boolean,
            evaluation_status text,
            candidate_completeness_verified boolean,
            scored_candidate_coverage_verified boolean,
            fingerprint text,
            checked_at timestamptz,
            artifact_hash text,
            report_hash text,
            proof_hash text,
            policy_hash text,
            bundle_hash text,
            source_scope_hash text,
            training_membership_digest text,
            validation_membership_digest text
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            artifact_row record;
            report_row record;
            receipt_row record;
            consumption_row record;
            proof_row record;
            base_row record;
            current_rights_row record;
            accepted_shadow boolean;
            accepted_paper boolean;
        BEGIN
            IF session_user NOT IN ('{ADMIN}','{RUNTIME}','{MARKET_SIGNAL_WORKER}') THEN
                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_RIGHTS_CALLER_DENIED';
            END IF;
            IF target_workspace IS NULL OR target_scorer_artifact_id IS NULL
               OR target_evaluation_report_id IS NULL
               OR target_workspace IS DISTINCT FROM
                   NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_RIGHTS_WORKSPACE_DENIED';
            END IF;
            IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_RIGHTS_READ_COMMITTED_REQUIRED';
            END IF;

            SELECT artifact.id, artifact.attempt_id, artifact.base_artifact_id,
                   artifact.base_artifact_hash, artifact.normalized_dataset_id,
                   artifact.manifest_hash, artifact.fold_plan_hash, artifact.fold_id,
                   artifact.approval_hash, artifact.schema_version, artifact.state,
                   artifact.training_membership_digest, artifact.validation_membership_digest,
                   artifact.artifact_hash
              INTO artifact_row
              FROM public.learning_scorer_artifact AS artifact
             WHERE artifact.workspace_id = target_workspace
               AND artifact.id = target_scorer_artifact_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_ARTIFACT_NOT_FOUND';
            END IF;

            SELECT report.id, report.evaluation_run_id, report.access_receipt_id,
                   report.payload_consumption_id, report.proof_id, report.report_hash,
                   report.access_receipt_hash, report.current_rights_receipt_hash,
                   report.payload_consumption_hash, report.proof_hash,
                   report.scorer_artifact_hash, report.base_artifact_hash, report.policy_hash,
                   report.policy_version, report.evaluation_status,
                   report.candidate_completeness_verified,
                   report.scored_candidate_coverage_verified, report.report, report.canonical_report
              INTO report_row
              FROM public.learning_scorer_test_evaluation_report AS report
             WHERE report.workspace_id = target_workspace
               AND report.id = target_evaluation_report_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_REPORT_NOT_FOUND';
            END IF;
            IF report_row.evaluation_status NOT IN (
                'ACCEPTED_SHADOW_ONLY','ACCEPTED_PAPER_GATE_ELIGIBLE') THEN
                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_REPORT_NOT_ACCEPTED';
            END IF;
            IF report_row.report_hash IS DISTINCT FROM
                    pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                        report_row.canonical_report, 'UTF8')), 'hex')
               OR report_row.report IS DISTINCT FROM report_row.canonical_report::jsonb
               OR report_row.report->>'evaluation_status' IS DISTINCT FROM
                    report_row.evaluation_status
               OR report_row.report->>'scorer_artifact_hash' IS DISTINCT FROM
                    artifact_row.artifact_hash
               OR report_row.scorer_artifact_hash IS DISTINCT FROM artifact_row.artifact_hash
               OR report_row.base_artifact_hash IS DISTINCT FROM artifact_row.base_artifact_hash
               OR public.wp06_scorer_test_report_has_forbidden_payload(report_row.report) THEN
                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_REPORT_INVALID';
            END IF;

            SELECT receipt.id, receipt.evaluation_run_id, receipt.attempt_id,
                   receipt.normalized_dataset_id, receipt.proof_id, receipt.manifest_hash,
                   receipt.fold_plan_hash, receipt.fold_id, receipt.approval_hash,
                   receipt.bundle_hash, receipt.source_scope_hash, receipt.proof_hash,
                   receipt.scorer_artifact_hash, receipt.policy_hash,
                   receipt.current_rights_receipt_hash, receipt.receipt_hash
              INTO receipt_row
              FROM public.learning_scorer_test_access_receipt AS receipt
             WHERE receipt.workspace_id = target_workspace
               AND receipt.id = report_row.access_receipt_id;
            IF NOT FOUND OR receipt_row.evaluation_run_id IS DISTINCT FROM
                    report_row.evaluation_run_id
               OR receipt_row.attempt_id IS DISTINCT FROM artifact_row.attempt_id
               OR receipt_row.normalized_dataset_id IS DISTINCT FROM
                    artifact_row.normalized_dataset_id
               OR receipt_row.scorer_artifact_hash IS DISTINCT FROM artifact_row.artifact_hash
               OR receipt_row.policy_hash IS DISTINCT FROM report_row.policy_hash
               OR receipt_row.receipt_hash IS DISTINCT FROM report_row.access_receipt_hash
               OR receipt_row.current_rights_receipt_hash IS DISTINCT FROM
                    report_row.current_rights_receipt_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_RECEIPT_MISMATCH';
            END IF;

            SELECT consumption.id, consumption.receipt_id, consumption.membership_digest,
                   consumption.consumption_hash, consumption.current_rights_receipt_hash
              INTO consumption_row
              FROM public.learning_scorer_test_payload_consumption AS consumption
             WHERE consumption.workspace_id = target_workspace
               AND consumption.id = report_row.payload_consumption_id;
            IF NOT FOUND OR consumption_row.receipt_id IS DISTINCT FROM receipt_row.id
               OR consumption_row.consumption_hash IS DISTINCT FROM
                    report_row.payload_consumption_hash
               OR consumption_row.current_rights_receipt_hash IS DISTINCT FROM
                    report_row.current_rights_receipt_hash
               OR consumption_row.membership_digest IS DISTINCT FROM
                    receipt_row.test_membership_digest THEN
                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_CONSUMPTION_MISMATCH';
            END IF;

            SELECT proof.id, proof.attempt_id, proof.evaluation_run_id,
                   proof.normalized_dataset_id, proof.manifest_hash, proof.fold_plan_hash,
                   proof.fold_id, proof.approval_hash, proof.bundle_hash,
                   proof.source_scope_hash, proof.proof_hash, proof.canonical_proof,
                   proof.completeness_verified
              INTO proof_row
              FROM public.learning_scorer_test_opportunity_proof AS proof
             WHERE proof.workspace_id = target_workspace
               AND proof.id = report_row.proof_id;
            IF NOT FOUND OR proof_row.id IS DISTINCT FROM receipt_row.proof_id
               OR proof_row.attempt_id IS DISTINCT FROM artifact_row.attempt_id
               OR proof_row.evaluation_run_id IS DISTINCT FROM report_row.evaluation_run_id
               OR proof_row.normalized_dataset_id IS DISTINCT FROM
                    artifact_row.normalized_dataset_id
               OR proof_row.manifest_hash IS DISTINCT FROM artifact_row.manifest_hash
               OR proof_row.fold_plan_hash IS DISTINCT FROM artifact_row.fold_plan_hash
               OR proof_row.fold_id IS DISTINCT FROM artifact_row.fold_id
               OR proof_row.approval_hash IS DISTINCT FROM artifact_row.approval_hash
               OR proof_row.bundle_hash IS DISTINCT FROM receipt_row.bundle_hash
               OR proof_row.source_scope_hash IS DISTINCT FROM receipt_row.source_scope_hash
               OR proof_row.proof_hash IS DISTINCT FROM report_row.proof_hash
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    proof_row.canonical_proof, 'UTF8')), 'hex')
                    IS DISTINCT FROM proof_row.proof_hash
               OR report_row.candidate_completeness_verified
                    IS DISTINCT FROM proof_row.completeness_verified THEN
                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_PROOF_MISMATCH';
            END IF;

            SELECT base.id, base.attempt_id, base.artifact_hash
              INTO base_row
              FROM public.learning_training_artifact AS base
             WHERE base.workspace_id = target_workspace
               AND base.id = artifact_row.base_artifact_id;
            IF NOT FOUND OR base_row.attempt_id IS DISTINCT FROM artifact_row.attempt_id
               OR base_row.artifact_hash IS DISTINCT FROM artifact_row.base_artifact_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_BASE_ARTIFACT_MISMATCH';
            END IF;

            SELECT rights_result.* INTO current_rights_row
              FROM public.wp06_recheck_scorer_current_rights(
                    target_workspace, target_scorer_artifact_id) AS rights_result;
            IF NOT FOUND OR current_rights_row.artifact_hash IS DISTINCT FROM
                    artifact_row.artifact_hash
               OR current_rights_row.proof_hash IS DISTINCT FROM proof_row.proof_hash
               OR current_rights_row.bundle_hash IS DISTINCT FROM proof_row.bundle_hash
               OR current_rights_row.source_scope_hash IS DISTINCT FROM proof_row.source_scope_hash
               OR current_rights_row.training_membership_digest IS DISTINCT FROM
                    artifact_row.training_membership_digest
               OR current_rights_row.validation_membership_digest IS DISTINCT FROM
                    artifact_row.validation_membership_digest THEN
                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_CURRENT_RIGHTS_MISMATCH';
            END IF;

            accepted_shadow := report_row.evaluation_status IN (
                'ACCEPTED_SHADOW_ONLY','ACCEPTED_PAPER_GATE_ELIGIBLE');
            accepted_paper := report_row.evaluation_status = 'ACCEPTED_PAPER_GATE_ELIGIBLE'
                AND report_row.candidate_completeness_verified IS TRUE
                AND report_row.scored_candidate_coverage_verified IS TRUE;
            IF report_row.evaluation_status = 'ACCEPTED_PAPER_GATE_ELIGIBLE'
               AND NOT accepted_paper THEN
                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_PAPER_GATE_INCOMPLETE';
            END IF;

            workspace_id := target_workspace;
            scorer_artifact_id := target_scorer_artifact_id;
            evaluation_report_id := target_evaluation_report_id;
            shadow_eligible := accepted_shadow;
            paper_gate_eligible := accepted_paper;
            evaluation_status := report_row.evaluation_status;
            candidate_completeness_verified := report_row.candidate_completeness_verified;
            scored_candidate_coverage_verified := report_row.scored_candidate_coverage_verified;
            fingerprint := public.wp06_sha256_canonical_json(pg_catalog.json_build_object(
                'artifact_hash', artifact_row.artifact_hash,
                'binance_training_scope_digest', current_rights_row.binance_training_scope_digest,
                'bundle_hash', proof_row.bundle_hash,
                'current_rights_receipt_hash', report_row.current_rights_receipt_hash,
                'evaluation_report_hash', report_row.report_hash,
                'policy_hash', report_row.policy_hash,
                'proof_hash', proof_row.proof_hash,
                'scorer_current_rights_fingerprint', current_rights_row.fingerprint,
                'source_scope_hash', proof_row.source_scope_hash,
                'training_membership_digest', current_rights_row.training_membership_digest,
                'validation_membership_digest', current_rights_row.validation_membership_digest
            ));
            checked_at := current_rights_row.checked_at;
            artifact_hash := artifact_row.artifact_hash;
            report_hash := report_row.report_hash;
            proof_hash := proof_row.proof_hash;
            policy_hash := report_row.policy_hash;
            bundle_hash := proof_row.bundle_hash;
            source_scope_hash := proof_row.source_scope_hash;
            training_membership_digest := current_rights_row.training_membership_digest;
            validation_membership_digest := current_rights_row.validation_membership_digest;
            RETURN NEXT;
        END $$
    """)
    op.execute(
        f"ALTER FUNCTION public.wp06_verify_scorer_deployment_rights(uuid, uuid, uuid) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_verify_scorer_deployment_rights(uuid, uuid, uuid) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_verify_scorer_deployment_rights(uuid, uuid, uuid) "
        f"TO {ADMIN}, {RUNTIME}, {MARKET_SIGNAL_WORKER}"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE public.learning_scoring_deployment_event IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.learning_scoring_deployment_event
                    WHERE mode IN ('SHADOW','PAPER_GATE')) THEN
            RAISE EXCEPTION 'WP06_SCORER_RIGHTS_DOWNGRADE_ACTIVE_DEPLOYMENT_DENIED';
        END IF;
    END $$""")
    op.execute(
        "DROP TRIGGER dataset_manifest_source_ref_scoring_fence "
        "ON public.dataset_manifest_source_ref"
    )
    op.execute("DROP FUNCTION public.wp06_fence_manifest_source_ref_insert()")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_scorer_manifest_source_refs(uuid, uuid) "
        f"FROM {RIGHTS_ROLE}"
    )
    op.execute("DROP FUNCTION public.wp06_scorer_manifest_source_refs(uuid, uuid)")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_verify_scorer_deployment_rights(uuid, uuid, uuid) "
        f"FROM {ADMIN}, {RUNTIME}, {MARKET_SIGNAL_WORKER}"
    )
    op.execute("DROP FUNCTION public.wp06_verify_scorer_deployment_rights(uuid, uuid, uuid)")
    op.execute("DROP FUNCTION public.wp06_recheck_scorer_current_rights(uuid, uuid)")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_scorer_source_policy_fingerprint("
        "text,integer,text,text,jsonb,text,timestamptz,timestamptz,text,text,text,text,text,"
        "text,text,bigint,bigint,bigint,numeric,text) FROM " + RIGHTS_ROLE
    )
    op.execute(
        "DROP FUNCTION public.wp06_scorer_source_policy_fingerprint("
        "text,integer,text,text,jsonb,text,timestamptz,timestamptz,text,text,text,text,text,"
        "text,text,bigint,bigint,bigint,numeric,text)"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_scorer_decimal_python_text(numeric) "
        f"FROM {RIGHTS_ROLE}"
    )
    op.execute("DROP FUNCTION public.wp06_scorer_decimal_python_text(numeric)")
    for table in reversed(
        (
            "learning_training_attempt",
            "learning_training_artifact",
            "learning_scorer_artifact",
            "learning_normalized_dataset",
            "learning_fold_assignment",
            "learning_normalized_sample",
            "backtest_trade_outcome",
            "backtest_decision_opportunity",
            "backtest_run",
            "learning_scorer_test_opportunity_proof",
            "learning_scorer_test_access_receipt",
            "learning_scorer_test_payload_consumption",
            "learning_scorer_test_evaluation_report",
            "dataset_manifest",
            "dataset_manifest_source_ref",
            "learning_source_training_receipt",
            "learning_run_source_rights_receipt",
            "dataset_manifest_eligibility_event",
            "source_policy_revision",
            "source_policy_observation",
            "source_item_revision",
            "source_item_observation",
            "source_item_takedown",
            "outbox_event",
            "consumer_receipt",
            "backtest_dataset_replay_bundle",
            "backtest_dataset_manifest",
        )
    ):
        op.execute(f"DROP POLICY wp06_scorer_rights_reader_workspace ON public.{table}")
        op.execute(f"REVOKE SELECT ON public.{table} FROM {RIGHTS_ROLE}")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION public.wp06_sha256_canonical_json(json) FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_canonical_utc_timestamp_json(timestamptz) "
        f"FROM {RIGHTS_ROLE}"
    )
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION public.wp06_compact_canonical_json(json) FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_scorer_test_report_has_forbidden_payload(jsonb) "
        f"FROM {RIGHTS_ROLE}"
    )
    op.execute(f"DROP OWNED BY {RIGHTS_ROLE}")
    op.execute(f"DROP ROLE {RIGHTS_ROLE}")

"""WP06: kiểm quyền nguồn chính xác cho Shadow/Drift."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "f8e0a2c4d6bb"
down_revision: str | None = "f8e0a2c4d6ba"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
OBSERVER = "crypidian_learning_shadow_observer"
RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
SHADOW_GUARD = "crypidian_learning_shadow_integrity_guard"
RESTORE_SCHEMA = "wp06_shadow_drift_source_scope"
RESTORE_TABLE = f"{RESTORE_SCHEMA}.function_restore"


def _backup_legacy_functions() -> None:
    """Lưu DDL các hàm cũ để khôi phục chính xác khi hạ migration rỗng."""
    op.execute(f"CREATE SCHEMA {RESTORE_SCHEMA}")
    op.execute(
        f"REVOKE ALL ON SCHEMA {RESTORE_SCHEMA} FROM PUBLIC, crypidian_app, "
        "crypidian_trading_runtime, crypidian_admin_web, "
        "crypidian_scorer_deployment_rights_reader"
    )
    op.execute(f"""
        CREATE TABLE {RESTORE_TABLE} (
            signature text PRIMARY KEY,
            function_definition text NOT NULL
        )
    """)
    op.execute(
        f"REVOKE ALL ON TABLE {RESTORE_TABLE} FROM PUBLIC, crypidian_app, "
        "crypidian_trading_runtime, crypidian_admin_web, "
        "crypidian_scorer_deployment_rights_reader"
    )
    op.execute(f"""
        INSERT INTO {RESTORE_TABLE} (signature, function_definition)
        SELECT legacy.signature::text, pg_catalog.pg_get_functiondef(legacy.signature)
          FROM (VALUES
              (pg_catalog.to_regprocedure(
                    'public.wp06_shadow_observation_idempotency_key'
                    '(public.learning_shadow_observation)')),
              (pg_catalog.to_regprocedure(
                    'public.wp06_shadow_observation_canonical_document'
                    '(public.learning_shadow_observation)')),
              (pg_catalog.to_regprocedure(
                    'public.wp06_lock_learning_shadow_cohort(uuid,uuid,integer)')),
              (pg_catalog.to_regprocedure(
                    'public.wp06_drift_assessment_idempotency_key'
                    '(public.learning_drift_assessment)')),
              (pg_catalog.to_regprocedure(
                    'public.wp06_drift_assessment_content_hash'
                    '(public.learning_drift_assessment,json)')),
              (pg_catalog.to_regprocedure(
                    'public.wp06_lock_learning_drift_assessment_cohort()')),
              (pg_catalog.to_regprocedure(
                    'public.wp06_require_learning_drift_member_lineage()')),
              (pg_catalog.to_regprocedure(
                    'public.wp06_validate_learning_drift_assessment_membership()')),
              (pg_catalog.to_regprocedure(
                    'public.wp06_require_learning_shadow_lineage()')),
              (pg_catalog.to_regprocedure(
                    'public.wp06_learning_incident_eligible_target(uuid,uuid)')),
              (pg_catalog.to_regprocedure(
                    'public.wp06_assert_complete_bot_decision_source_bar_set()'))
          ) AS legacy(signature)
    """)


def _add_columns() -> None:
    op.add_column(
        "learning_shadow_observation",
        sa.Column("source_scope_fingerprint", sa.String(64), nullable=True),
    )
    op.add_column(
        "learning_drift_assessment",
        sa.Column("source_rights_digest", sa.String(64), nullable=True),
    )
    op.add_column(
        "learning_drift_assessment_member",
        sa.Column("current_rights_eligible", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "learning_drift_assessment_member",
        sa.Column("current_rights_fingerprint", sa.String(64), nullable=True),
    )
    op.add_column(
        "learning_drift_assessment_member",
        sa.Column("current_source_ids", JSONB(), nullable=True),
    )
    op.add_column(
        "learning_drift_assessment_member",
        sa.Column("current_eligible", sa.Boolean(), nullable=True),
    )
    op.execute("""
        ALTER TABLE public.learning_shadow_observation
        ADD CONSTRAINT learning_shadow_observation_source_scope_hash_valid
        CHECK (source_scope_fingerprint IS NULL OR source_scope_fingerprint ~ '^[0-9a-f]{64}$')
    """)
    op.execute("""
        ALTER TABLE public.learning_shadow_observation
        ADD CONSTRAINT learning_shadow_observation_exact_rights_valid
        CHECK ((rights_eligible AND source_scope_fingerprint IS NOT NULL)
            OR (NOT rights_eligible AND source_scope_fingerprint IS NULL))
        NOT VALID
    """)
    op.execute("""
        ALTER TABLE public.learning_drift_assessment
        ADD CONSTRAINT learning_drift_assessment_source_rights_digest_valid
        CHECK (source_rights_digest IS NULL OR source_rights_digest ~ '^[0-9a-f]{64}$')
    """)
    op.execute("""
        ALTER TABLE public.learning_drift_assessment_member
        DROP CONSTRAINT ck_learning_drift_assessment_member_learning_drift_asse_5fd5
    """)
    op.execute("""
        ALTER TABLE public.learning_drift_assessment_member
        ADD CONSTRAINT learning_drift_assessment_member_current_scope_valid
        CHECK (
            (current_rights_eligible IS NULL AND current_rights_fingerprint IS NULL
                AND current_source_ids IS NULL AND current_eligible IS NULL)
            OR
        (current_rights_eligible IS NOT NULL
            AND current_source_ids IS NOT NULL
            AND pg_catalog.jsonb_typeof(current_source_ids) = 'array'
            AND current_eligible IS NOT NULL
            AND ((current_rights_eligible AND current_rights_fingerprint IS NOT NULL
                    AND current_rights_fingerprint ~ '^[0-9a-f]{64}$')
                 OR (NOT current_rights_eligible AND current_rights_fingerprint IS NULL)))
        ) NOT VALID
    """)


def _grant_shadow_drift_rights_metadata() -> None:
    """Cấp cho rights reader đúng metadata cần để recheck quyền và membership."""
    for table in (
        "learning_shadow_observation",
        "learning_drift_assessment",
        "learning_drift_assessment_member",
    ):
        op.execute(f"""
            CREATE POLICY wp06_shadow_drift_rights_{table}_select
            ON public.{table} FOR SELECT TO {RIGHTS_ROLE}
            USING (workspace_id = NULLIF(
                pg_catalog.current_setting('app.workspace_id', true), '')::uuid)
        """)
    op.execute(
        "GRANT SELECT (workspace_id,id,incumbent_decision_id,rights_eligible,"
        "source_scope_fingerprint,challenger_registry_id,challenger_revision,observed_at) "
        "ON public.learning_shadow_observation "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute(
        "GRANT SELECT (workspace_id,id,source_rights_digest,cohort_registry_id,"
        "cohort_revision,window_start,window_end,member_count,membership_digest) "
        "ON public.learning_drift_assessment "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute(
        "GRANT SELECT (workspace_id,assessment_id,observation_id,current_rights_eligible,"
        "current_rights_fingerprint,current_source_ids,current_eligible,baseline_available,"
        "schema_compatible) ON public.learning_drift_assessment_member "
        f"TO {RIGHTS_ROLE}"
    )


def _revoke_shadow_drift_rights_metadata() -> None:
    """Gỡ RLS và column grant do migration này thêm trước khi bỏ các cột."""
    for table in (
        "learning_shadow_observation",
        "learning_drift_assessment",
        "learning_drift_assessment_member",
    ):
        op.execute(f"DROP POLICY wp06_shadow_drift_rights_{table}_select ON public.{table}")
    op.execute(
        "REVOKE SELECT (workspace_id,id,incumbent_decision_id,rights_eligible,"
        "source_scope_fingerprint,challenger_registry_id,challenger_revision,observed_at) "
        "ON TABLE public.learning_shadow_observation "
        f"FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,id,source_rights_digest,cohort_registry_id,"
        "cohort_revision,window_start,window_end,member_count,membership_digest) "
        "ON TABLE public.learning_drift_assessment "
        f"FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,assessment_id,observation_id,current_rights_eligible,"
        "current_rights_fingerprint,current_source_ids,current_eligible,baseline_available,"
        "schema_compatible) ON TABLE public.learning_drift_assessment_member "
        f"FROM {RIGHTS_ROLE}"
    )


def _create_exact_scope_gates() -> None:
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_paper_performance_decision_source_ids(uuid,uuid) "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_authorize_paper_performance_decision_source_scope"
        f"(uuid,uuid,timestamptz) TO {RIGHTS_ROLE}"
    )
    op.execute(f"""
        CREATE FUNCTION public.wp06_shadow_drift_decision_source_ids(
            target_workspace uuid, target_decision uuid
        ) RETURNS text[] LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE source_scope_row record; source_ids text[]; source_count integer;
        BEGIN
            IF session_user NOT IN ('{ADMIN}', '{OBSERVER}')
               OR target_workspace IS NULL OR target_decision IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_SOURCE_SCOPE_DENIED';
            END IF;
            BEGIN
                SELECT scope.decision_snapshot_id, scope.decision_lineage_fingerprint
                  INTO source_scope_row
                  FROM public.wp06_verify_bot_decision_source_scope(
                      target_workspace, target_decision
                  ) AS scope;
                IF NOT FOUND
                   OR source_scope_row.decision_snapshot_id !~ '^[0-9a-f]{{64}}$'
                   OR source_scope_row.decision_lineage_fingerprint !~ '^[0-9a-f]{{64}}$' THEN
                    RAISE EXCEPTION 'WP06_SHADOW_DRIFT_SOURCE_LINEAGE_MISSING';
                END IF;
                SELECT pg_catalog.count(*)::integer,
                       pg_catalog.array_agg(DISTINCT provider.source_id
                                            ORDER BY provider.source_id)
                  INTO source_count, source_ids
                  FROM public.bot_decision_source_bar AS source
                  JOIN public.market_bar AS bar
                    ON bar.workspace_id = source.workspace_id
                   AND bar.id = source.market_bar_id
                  CROSS JOIN LATERAL (SELECT CASE bar.source
                    WHEN 'BINANCE_USDM' THEN 'BINANCE_USDM_REST'
                    WHEN 'BINANCE_USDM_REST_BACKFILL' THEN 'BINANCE_USDM_REST'
                    WHEN 'CRYPIDIAN_FIRST_PARTY_QA_FIXTURE'
                        THEN 'CRYPIDIAN_FIRST_PARTY_QA'
                    ELSE NULL END AS source_id) AS provider
                 WHERE source.workspace_id = target_workspace
                   AND source.bot_decision_id = target_decision
                   AND source.decision_snapshot_id = source_scope_row.decision_snapshot_id
                   AND source.source_event_id ~ '^market-bar:[0-9a-f]{{64}}$';
                IF source_count = 0 OR source_ids IS NULL
                   OR pg_catalog.array_position(source_ids, NULL) IS NOT NULL THEN
                    RAISE EXCEPTION 'WP06_SHADOW_DRIFT_SOURCE_PROVIDER_UNKNOWN';
                END IF;
                RETURN source_ids;
            EXCEPTION WHEN raise_exception THEN
                RETURN ARRAY[]::text[];
            END;
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_shadow_drift_decision_source_ids(uuid,uuid) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_shadow_drift_decision_source_ids(uuid,uuid) "
        "FROM PUBLIC, crypidian_app, crypidian_trading_runtime"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_shadow_drift_decision_source_ids(uuid,uuid) "
        f"TO {ADMIN}, {OBSERVER}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_shadow_drift_decision_source_ids(uuid,uuid) "
        f"TO {SHADOW_GUARD}"
    )

    op.execute(f"""
        CREATE FUNCTION public.wp06_authorize_shadow_inference_source_scope(
            target_workspace uuid, target_decision uuid, checked_at timestamptz
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            source_scope_row record;
            source_ids text[];
            source_id text;
            group_row record;
            policy_row record;
            observation_row record;
            evidence_row record;
            current_scope jsonb;
            current_scope_hash text;
            fingerprint_document jsonb := '[]'::jsonb;
            lineage_document jsonb;
        BEGIN
            IF session_user NOT IN ('{ADMIN}', '{OBSERVER}')
               OR target_workspace IS NULL OR target_decision IS NULL OR checked_at IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SHADOW_INFERENCE_SOURCE_SCOPE_DENIED';
            END IF;
            BEGIN
                SELECT scope.decision_snapshot_id, scope.timeframe_snapshot_ids,
                       scope.source_event_digests, scope.decision_scope_fingerprint,
                       scope.decision_lineage_fingerprint
                  INTO source_scope_row
                  FROM public.wp06_verify_bot_decision_source_scope(
                        target_workspace, target_decision
                  ) AS scope;
                IF NOT FOUND
                   OR source_scope_row.decision_scope_fingerprint !~ '^[0-9a-f]{64}$'
                   OR source_scope_row.decision_lineage_fingerprint !~ '^[0-9a-f]{64}$'
                   OR source_scope_row.timeframe_snapshot_ids IS NULL
                   OR source_scope_row.source_event_digests IS NULL THEN
                    RAISE EXCEPTION 'WP06_SHADOW_INFERENCE_LINEAGE_MISSING';
                END IF;
                source_ids := public.wp06_shadow_drift_decision_source_ids(
                    target_workspace, target_decision);
                IF source_ids IS NULL OR pg_catalog.cardinality(source_ids) = 0 THEN
                    RAISE EXCEPTION 'WP06_SHADOW_INFERENCE_LINEAGE_MISSING';
                END IF;
                FOREACH source_id IN ARRAY source_ids LOOP
                    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                        'research-source:' || target_workspace::text || ':' || source_id, 0));
                END LOOP;
                IF 'CRYPIDIAN_FIRST_PARTY_QA' = ANY(source_ids) THEN
                    RAISE EXCEPTION 'WP06_SHADOW_INFERENCE_QA_DENIED';
                END IF;
                lineage_document := pg_catalog.jsonb_build_object(
                    'decision_scope_fingerprint', source_scope_row.decision_scope_fingerprint,
                    'decision_lineage_fingerprint', source_scope_row.decision_lineage_fingerprint,
                    'source_event_digests', source_scope_row.source_event_digests,
                    'timeframe_snapshot_ids', source_scope_row.timeframe_snapshot_ids
                );
                FOR group_row IN
                    SELECT provider.source_id, bar.symbol, bar.timeframe,
                           pg_catalog.min(bar.open_time) AS data_start,
                           pg_catalog.max(bar.open_time) + interval '1 microsecond' AS data_end,
                           pg_catalog.array_agg(source.market_bar_id::text
                               ORDER BY source.timeframe, source.ordinal) AS bar_ids,
                           pg_catalog.array_agg(source.source_event_id
                               ORDER BY source.timeframe, source.ordinal) AS source_event_ids
                      FROM public.bot_decision_source_bar AS source
                      JOIN public.market_bar AS bar
                        ON bar.workspace_id = source.workspace_id
                       AND bar.id = source.market_bar_id
                      CROSS JOIN LATERAL (SELECT CASE bar.source
                        WHEN 'BINANCE_USDM' THEN 'BINANCE_USDM_REST'
                        WHEN 'BINANCE_USDM_REST_BACKFILL' THEN 'BINANCE_USDM_REST'
                        WHEN 'CRYPIDIAN_FIRST_PARTY_QA_FIXTURE' THEN 'CRYPIDIAN_FIRST_PARTY_QA'
                        ELSE NULL END AS source_id) AS provider
                     WHERE source.workspace_id = target_workspace
                       AND source.bot_decision_id = target_decision
                     GROUP BY provider.source_id, bar.symbol, bar.timeframe
                     ORDER BY provider.source_id, bar.symbol, bar.timeframe
                LOOP
                    IF group_row.source_id IS NULL OR group_row.symbol IS NULL
                       OR group_row.timeframe IS NULL
                       OR group_row.data_end <= group_row.data_start
                       OR group_row.bar_ids IS NULL OR group_row.source_event_ids IS NULL THEN
                        RAISE EXCEPTION 'WP06_SHADOW_INFERENCE_LINEAGE_MISSING';
                    END IF;
                    SELECT policy.workspace_id, policy.source_id, policy.sequence,
                           policy.policy_id, policy.state, policy.allowed_purposes,
                           policy.provider, policy.product, policy.venue, policy.contract,
                           policy.reviewed_at, policy.valid_until, policy.evidence_ref,
                           policy.created_at, policy.license_scope, policy.license_evidence_id,
                           policy.license_digest, policy.license_scope_hash,
                           public.wp06_license_source_policy_fingerprint(
                               pg_catalog.jsonb_populate_record(
                                   NULL::public.source_policy_revision,
                                   pg_catalog.jsonb_build_object(
                                       'source_id', policy.source_id,
                                       'sequence', policy.sequence,
                                       'policy_id', policy.policy_id,
                                       'state', policy.state,
                                       'allowed_purposes', policy.allowed_purposes,
                                       'evidence_ref', policy.evidence_ref,
                                       'reviewed_at', policy.reviewed_at,
                                       'valid_until', policy.valid_until,
                                       'approved_by', policy.approved_by,
                                       'publisher', policy.publisher,
                                       'endpoint_url', policy.endpoint_url,
                                       'access_method', policy.access_method,
                                       'auth_scope', policy.auth_scope,
                                       'attribution', policy.attribution,
                                       'retention_ref', policy.retention_ref,
                                       'quota_window_seconds', policy.quota_window_seconds,
                                       'quota_max_requests', policy.quota_max_requests,
                                       'quota_max_bytes', policy.quota_max_bytes,
                                       'quota_max_cost', policy.quota_max_cost,
                                       'quota_cost_unit', policy.quota_cost_unit,
                                       'created_at', policy.created_at,
                                       'license_evidence_id', policy.license_evidence_id,
                                       'license_digest', policy.license_digest,
                                       'license_scope_hash', policy.license_scope_hash
                                   )
                               )
                           ) AS policy_fingerprint
                      INTO policy_row
                      FROM public.source_policy_revision AS policy
                     WHERE policy.workspace_id = target_workspace
                       AND policy.source_id = group_row.source_id
                     ORDER BY policy.sequence DESC LIMIT 1;
                    IF NOT FOUND OR policy_row.state IS DISTINCT FROM 'verified'
                       OR policy_row.provider IS DISTINCT FROM group_row.source_id
                       OR policy_row.product IS NULL OR policy_row.venue IS NULL
                       OR policy_row.contract IS NULL
                       OR NOT (policy_row.allowed_purposes @> '["paper_demo"]'::jsonb)
                       OR policy_row.reviewed_at IS NULL OR policy_row.reviewed_at > checked_at
                       OR (policy_row.valid_until IS NOT NULL
                           AND policy_row.valid_until <= checked_at)
                       OR policy_row.evidence_ref IS NULL
                       OR policy_row.evidence_ref !~ '^https://[^/?#]+([/?#].*)?$'
                       OR policy_row.license_scope IS NULL
                       OR policy_row.license_evidence_id IS NULL
                       OR policy_row.license_digest IS NULL
                       OR policy_row.license_digest !~ '^[0-9a-f]{64}$'
                       OR policy_row.license_scope_hash IS NULL
                       OR policy_row.license_scope_hash !~ '^[0-9a-f]{64}$'
                       OR policy_row.policy_fingerprint IS NULL
                       OR policy_row.policy_fingerprint !~ '^[0-9a-f]{64}$'
                       OR NOT public.wp06_license_scope_valid(policy_row.license_scope)
                       OR policy_row.license_scope_hash IS DISTINCT FROM pg_catalog.encode(
                            pg_catalog.sha256(pg_catalog.convert_to(
                                public.wp06_license_canonical_json(policy_row.license_scope),
                                'UTF8')), 'hex')
                       OR EXISTS (
                            SELECT 1 FROM public.outbox_event AS event
                             WHERE event.workspace_id = target_workspace
                               AND event.event_type = 'research.rights_changed.v1'
                               AND event.payload->>'source_id' = group_row.source_id
                               AND NOT EXISTS (
                                    SELECT 1 FROM public.consumer_receipt AS receipt
                                     WHERE receipt.workspace_id = event.workspace_id
                                       AND receipt.outbox_event_id = event.id
                                       AND receipt.consumer_name =
                                            'wp06.dataset-rights-eligibility.v1'
                               )
                       ) THEN
                        RAISE EXCEPTION 'WP06_SHADOW_INFERENCE_CURRENT_RIGHTS_DENIED';
                    END IF;
                    IF policy_row.license_scope->>'source_id' IS DISTINCT FROM group_row.source_id
                       OR policy_row.license_scope->>'provider' IS DISTINCT FROM policy_row.provider
                       OR policy_row.license_scope->>'product' IS DISTINCT FROM policy_row.product
                       OR policy_row.license_scope->>'venue' IS DISTINCT FROM policy_row.venue
                       OR policy_row.license_scope->>'contract' IS DISTINCT FROM policy_row.contract
                       OR NOT (policy_row.license_scope->'symbols'
                                    @> pg_catalog.jsonb_build_array(group_row.symbol))
                       OR NOT (policy_row.license_scope->'intervals'
                                    @> pg_catalog.jsonb_build_array(group_row.timeframe))
                       OR (policy_row.license_scope->>'data_start')::timestamptz
                            > group_row.data_start
                       OR (policy_row.license_scope->>'data_end')::timestamptz
                            < group_row.data_end
                       OR NOT (policy_row.license_scope->'allowed_purposes'
                                    @> '["paper_demo"]'::jsonb)
                       OR NOT (policy_row.license_scope->'allowed_outputs'
                                    @> '["paper_inference"]'::jsonb)
                       OR NOT (policy_row.license_scope->'derived_use'
                                    @> '["paper_inference"]'::jsonb)
                       OR policy_row.license_scope->'max_retention_days'
                            IS DISTINCT FROM 'null'::jsonb THEN
                        RAISE EXCEPTION 'WP06_SHADOW_INFERENCE_CURRENT_RIGHTS_DENIED';
                    END IF;
                    SELECT observation.policy_id, observation.known_at
                      INTO observation_row
                      FROM public.source_policy_observation AS observation
                     WHERE observation.workspace_id = target_workspace
                       AND observation.source_id = group_row.source_id
                       AND observation.sequence = policy_row.sequence
                     LIMIT 1;
                    IF NOT FOUND OR observation_row.policy_id IS DISTINCT FROM policy_row.policy_id
                       OR observation_row.known_at < policy_row.created_at
                       OR observation_row.known_at < policy_row.reviewed_at
                       OR observation_row.known_at >= checked_at THEN
                        RAISE EXCEPTION 'WP06_SHADOW_INFERENCE_CURRENT_RIGHTS_DENIED';
                    END IF;
                    SELECT evidence.workspace_id, evidence.source_id, evidence.id,
                           evidence.policy_id, evidence.evidence_ref, evidence.digest,
                           evidence.license_scope, evidence.canonical_scope, evidence.scope_hash
                      INTO evidence_row
                      FROM public.source_license_evidence AS evidence
                     WHERE evidence.workspace_id = target_workspace
                       AND evidence.source_id = group_row.source_id
                       AND evidence.id = policy_row.license_evidence_id
                       AND EXISTS (
                            SELECT 1 FROM public.source_policy_revision AS pending
                             WHERE pending.workspace_id = target_workspace
                               AND pending.source_id = group_row.source_id
                               AND pending.policy_id = evidence.policy_id
                               AND pending.state = 'rights_pending'
                               AND pending.evidence_ref = evidence.evidence_ref
                               AND pending.license_scope = evidence.license_scope
                       );
                    IF NOT FOUND
                       OR evidence_row.evidence_ref IS DISTINCT FROM policy_row.evidence_ref
                       OR evidence_row.digest IS DISTINCT FROM policy_row.license_digest
                       OR evidence_row.license_scope IS DISTINCT FROM policy_row.license_scope
                       OR evidence_row.scope_hash IS DISTINCT FROM policy_row.license_scope_hash
                       OR evidence_row.canonical_scope IS DISTINCT FROM
                            public.wp06_license_canonical_json(evidence_row.license_scope)
                       OR evidence_row.scope_hash IS DISTINCT FROM pg_catalog.encode(
                            pg_catalog.sha256(pg_catalog.convert_to(
                                evidence_row.canonical_scope, 'UTF8')), 'hex')
                       OR public.wp06_license_canonical_json(evidence_row.license_scope)
                            IS DISTINCT FROM
                                public.wp06_license_canonical_json(policy_row.license_scope)
                       OR public.wp06_license_scope_valid(evidence_row.license_scope)
                            IS DISTINCT FROM true THEN
                        RAISE EXCEPTION 'WP06_SHADOW_INFERENCE_CURRENT_RIGHTS_DENIED';
                    END IF;
                    current_scope := pg_catalog.jsonb_build_object(
                        'source_id', group_row.source_id,
                        'provider', policy_row.provider,
                        'product', policy_row.product,
                        'venue', policy_row.venue,
                        'contract', policy_row.contract,
                        'symbols', pg_catalog.jsonb_build_array(group_row.symbol),
                        'intervals', pg_catalog.jsonb_build_array(group_row.timeframe),
                        'data_start', pg_catalog.to_char(
                            group_row.data_start AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                        'data_end', pg_catalog.to_char(
                            group_row.data_end AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                        'purpose', 'paper_demo',
                        'outputs', pg_catalog.jsonb_build_array('paper_inference'),
                        'derived_use', pg_catalog.jsonb_build_array('paper_inference'),
                        'retention_days', NULL
                    );
                    current_scope_hash := pg_catalog.encode(pg_catalog.sha256(
                        pg_catalog.convert_to(
                            public.wp06_license_canonical_json(current_scope), 'UTF8')), 'hex');
                    fingerprint_document := fingerprint_document ||
                        pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
                            'source_id', group_row.source_id,
                            'policy_id', policy_row.policy_id,
                            'sequence', policy_row.sequence,
                            'policy_fingerprint', policy_row.policy_fingerprint,
                            'license_evidence_id', policy_row.license_evidence_id::text,
                            'license_digest', policy_row.license_digest,
                            'license_scope_hash', policy_row.license_scope_hash,
                            'use_scope_hash', current_scope_hash,
                            'symbol', group_row.symbol,
                            'interval', group_row.timeframe,
                            'data_start', pg_catalog.to_char(
                                group_row.data_start AT TIME ZONE 'UTC',
                                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                            'data_end', pg_catalog.to_char(
                                group_row.data_end AT TIME ZONE 'UTC',
                                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                            'retention_days', NULL,
                            'bar_ids', group_row.bar_ids,
                            'source_event_ids', group_row.source_event_ids,
                            'lineage', lineage_document
                        ));
                END LOOP;
                IF fingerprint_document = '[]'::jsonb THEN
                    RAISE EXCEPTION 'WP06_SHADOW_INFERENCE_LINEAGE_MISSING';
                END IF;
                RETURN pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    public.wp06_license_canonical_json(fingerprint_document), 'UTF8')), 'hex');
            EXCEPTION WHEN raise_exception THEN
                RETURN NULL;
            END;
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_authorize_shadow_inference_source_scope(uuid,uuid,"
        f"timestamptz) OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_authorize_shadow_inference_source_scope(uuid,uuid,"
        "timestamptz) FROM PUBLIC, crypidian_app, crypidian_trading_runtime"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_authorize_shadow_inference_source_scope(uuid,uuid,"
        f"timestamptz) TO {ADMIN}, {OBSERVER}"
    )

    op.execute(f"""
        CREATE FUNCTION public.wp06_authorize_learning_drift_source_scope(
            target_workspace uuid, target_decision uuid, checked_at timestamptz
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF session_user <> '{ADMIN}'
               OR target_workspace IS NULL OR target_decision IS NULL OR checked_at IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_SOURCE_SCOPE_DENIED';
            END IF;
            BEGIN
                RETURN public.wp06_authorize_paper_performance_decision_source_scope(
                    target_workspace, target_decision, checked_at);
            EXCEPTION WHEN raise_exception THEN
                RETURN NULL;
            END;
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_authorize_learning_drift_source_scope(uuid,uuid,"
        f"timestamptz) OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_authorize_learning_drift_source_scope(uuid,uuid,"
        "timestamptz) FROM PUBLIC, crypidian_app, crypidian_trading_runtime"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_authorize_learning_drift_source_scope(uuid,uuid,"
        f"timestamptz) TO {ADMIN}"
    )


def _create_hash_and_shadow_writer_gates() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION public.wp06_shadow_observation_idempotency_key(
            row_value public.learning_shadow_observation
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE document jsonb;
        BEGIN
            IF row_value.source_scope_fingerprint IS NULL THEN
                RETURN public.wp06_sha256_canonical_json(pg_catalog.json_build_object(
                    'active_artifact_hash', row_value.active_artifact_hash,
                    'active_pointer_version', row_value.active_pointer_version,
                    'active_registry_id', row_value.active_registry_id::text,
                    'active_registry_revision', row_value.active_registry_revision,
                    'challenger_reason', row_value.challenger_reason,
                    'challenger_registry_id', row_value.challenger_registry_id::text,
                    'challenger_revision', row_value.challenger_revision,
                    'incumbent_decision_hash', row_value.decision_hash,
                    'incumbent_decision_id', row_value.incumbent_decision_id::text,
                    'incumbent_strategy_release_hash', row_value.strategy_release_hash,
                    'registry_approval_event_hash', row_value.registry_approval_event_hash,
                    'registry_approval_event_id', row_value.registry_approval_event_id::text,
                    'registry_approval_event_sequence', row_value.registry_approval_event_sequence,
                    'rights_eligible', row_value.rights_eligible,
                    'rights_fingerprint', row_value.rights_fingerprint,
                    'rights_known_at', public.wp06_canonical_utc_timestamp_json(
                        row_value.rights_known_at),
                    'rights_policy_id', row_value.rights_policy_id,
                    'rights_policy_sequence', row_value.rights_policy_sequence,
                    'rights_source_id', row_value.rights_source_id,
                    'schema_compatible', row_value.schema_compatible,
                    'snapshot_coverage', public.wp06_canonical_float_json(
                        row_value.snapshot_coverage),
                    'snapshot_hash', row_value.snapshot_hash,
                    'snapshot_quality_ok', row_value.snapshot_quality_ok,
                    'workspace_id', row_value.workspace_id::text
                ));
            END IF;
            document := pg_catalog.jsonb_build_object(
                'active_artifact_hash', row_value.active_artifact_hash,
                'active_pointer_version', row_value.active_pointer_version,
                'active_registry_id', row_value.active_registry_id::text,
                'active_registry_revision', row_value.active_registry_revision,
                'challenger_reason', row_value.challenger_reason,
                'challenger_registry_id', row_value.challenger_registry_id::text,
                'challenger_revision', row_value.challenger_revision,
                'incumbent_decision_hash', row_value.decision_hash,
                'incumbent_decision_id', row_value.incumbent_decision_id::text,
                'incumbent_strategy_release_hash', row_value.strategy_release_hash,
                'registry_approval_event_hash', row_value.registry_approval_event_hash,
                'registry_approval_event_id', row_value.registry_approval_event_id::text,
                'registry_approval_event_sequence', row_value.registry_approval_event_sequence,
                'rights_eligible', row_value.rights_eligible,
                'rights_fingerprint', row_value.rights_fingerprint,
                'rights_known_at', public.wp06_canonical_utc_timestamp_json(
                    row_value.rights_known_at),
                'rights_policy_id', row_value.rights_policy_id,
                'rights_policy_sequence', row_value.rights_policy_sequence,
                'rights_source_id', row_value.rights_source_id,
                'schema_compatible', row_value.schema_compatible,
                'snapshot_coverage', public.wp06_canonical_float_json(
                    row_value.snapshot_coverage),
                'snapshot_hash', row_value.snapshot_hash,
                'snapshot_quality_ok', row_value.snapshot_quality_ok,
                'workspace_id', row_value.workspace_id::text
            );
            IF row_value.source_scope_fingerprint IS NOT NULL THEN
                document := document || pg_catalog.jsonb_build_object(
                    'source_scope_fingerprint', row_value.source_scope_fingerprint);
            END IF;
            RETURN public.wp06_sha256_canonical_json(document::json);
        END $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.wp06_shadow_observation_canonical_document(
            row_value public.learning_shadow_observation
        ) RETURNS json LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE document jsonb;
        BEGIN
            IF row_value.source_scope_fingerprint IS NULL THEN
                RETURN pg_catalog.json_build_object(
                    'active_artifact_hash', row_value.active_artifact_hash,
                    'active_pointer_version', row_value.active_pointer_version,
                    'active_registry_id', row_value.active_registry_id::text,
                    'active_registry_revision', row_value.active_registry_revision,
                    'challenger_model', pg_catalog.json_build_object(
                        'artifact_hash', row_value.challenger_artifact_hash,
                        'artifact_id', row_value.challenger_artifact_id::text,
                        'fold_plan_hash', row_value.challenger_fold_plan_hash,
                        'manifest_hash', row_value.challenger_manifest_hash,
                        'registry_approval_event_hash', row_value.registry_approval_event_hash,
                        'registry_approval_event_id', row_value.registry_approval_event_id::text,
                        'registry_approval_event_sequence',
                            row_value.registry_approval_event_sequence,
                        'registry_id', row_value.challenger_registry_id::text,
                        'revision', row_value.challenger_revision,
                        'state', row_value.challenger_state,
                        'training_approval_hash', row_value.training_approval_hash,
                        'workspace_id', row_value.workspace_id::text
                    ),
                    'challenger_reason', row_value.challenger_reason,
                    'challenger_snapshot_state', pg_catalog.json_build_object(
                        'coverage', public.wp06_canonical_float_json(row_value.snapshot_coverage),
                        'quality_ok', row_value.snapshot_quality_ok,
                        'schema_compatible', row_value.schema_compatible
                    ),
                    'incumbent_decision', pg_catalog.json_build_object(
                        'decision_hash', row_value.decision_hash,
                        'decision_id', row_value.incumbent_decision_id::text,
                        'evaluated_at', public.wp06_canonical_utc_timestamp_json(
                            row_value.decision_evaluated_at),
                        'status', row_value.incumbent_status,
                        'strategy_release_hash', row_value.strategy_release_hash,
                        'strategy_release_id', row_value.strategy_release_id
                    ),
                    'observation_id', row_value.id::text,
                    'observed_at', public.wp06_canonical_utc_timestamp_json(row_value.observed_at),
                    'snapshot', pg_catalog.json_build_object(
                        'as_of', public.wp06_canonical_utc_timestamp_json(row_value.snapshot_as_of),
                        'schema_hash', row_value.snapshot_schema_hash,
                        'snapshot_hash', row_value.snapshot_hash,
                        'snapshot_id', row_value.snapshot_id
                    ),
                    'source_rights', pg_catalog.json_build_object(
                        'eligible', row_value.rights_eligible,
                        'fingerprint', row_value.rights_fingerprint,
                        'known_at', public.wp06_canonical_utc_timestamp_json(
                            row_value.rights_known_at),
                        'policy_id', row_value.rights_policy_id,
                        'policy_sequence', row_value.rights_policy_sequence,
                        'source_id', row_value.rights_source_id
                    ),
                    'workspace_id', row_value.workspace_id::text
                );
            END IF;
            document := pg_catalog.jsonb_build_object(
                'active_artifact_hash', row_value.active_artifact_hash,
                'active_pointer_version', row_value.active_pointer_version,
                'active_registry_id', row_value.active_registry_id::text,
                'active_registry_revision', row_value.active_registry_revision,
                'challenger_model', pg_catalog.jsonb_build_object(
                    'artifact_hash', row_value.challenger_artifact_hash,
                    'artifact_id', row_value.challenger_artifact_id::text,
                    'fold_plan_hash', row_value.challenger_fold_plan_hash,
                    'manifest_hash', row_value.challenger_manifest_hash,
                    'registry_approval_event_hash', row_value.registry_approval_event_hash,
                    'registry_approval_event_id', row_value.registry_approval_event_id::text,
                    'registry_approval_event_sequence', row_value.registry_approval_event_sequence,
                    'registry_id', row_value.challenger_registry_id::text,
                    'revision', row_value.challenger_revision,
                    'state', row_value.challenger_state,
                    'training_approval_hash', row_value.training_approval_hash,
                    'workspace_id', row_value.workspace_id::text
                ),
                'challenger_reason', row_value.challenger_reason,
                'challenger_snapshot_state', pg_catalog.jsonb_build_object(
                    'coverage', public.wp06_canonical_float_json(row_value.snapshot_coverage),
                    'quality_ok', row_value.snapshot_quality_ok,
                    'schema_compatible', row_value.schema_compatible
                ),
                'incumbent_decision', pg_catalog.jsonb_build_object(
                    'decision_hash', row_value.decision_hash,
                    'decision_id', row_value.incumbent_decision_id::text,
                    'evaluated_at', public.wp06_canonical_utc_timestamp_json(
                        row_value.decision_evaluated_at),
                    'status', row_value.incumbent_status,
                    'strategy_release_hash', row_value.strategy_release_hash,
                    'strategy_release_id', row_value.strategy_release_id
                ),
                'observation_id', row_value.id::text,
                'observed_at', public.wp06_canonical_utc_timestamp_json(row_value.observed_at),
                'snapshot', pg_catalog.jsonb_build_object(
                    'as_of', public.wp06_canonical_utc_timestamp_json(row_value.snapshot_as_of),
                    'schema_hash', row_value.snapshot_schema_hash,
                    'snapshot_hash', row_value.snapshot_hash,
                    'snapshot_id', row_value.snapshot_id
                ),
                'source_rights', pg_catalog.jsonb_build_object(
                    'eligible', row_value.rights_eligible,
                    'fingerprint', row_value.rights_fingerprint,
                    'known_at', public.wp06_canonical_utc_timestamp_json(row_value.rights_known_at),
                    'policy_id', row_value.rights_policy_id,
                    'policy_sequence', row_value.rights_policy_sequence,
                    'source_id', row_value.rights_source_id
                ),
                'workspace_id', row_value.workspace_id::text
            );
            IF row_value.source_scope_fingerprint IS NOT NULL THEN
                document := document || pg_catalog.jsonb_build_object(
                    'source_scope_fingerprint', row_value.source_scope_fingerprint);
            END IF;
            RETURN document::json;
        END $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.wp06_lock_learning_shadow_cohort(
            target_workspace_id uuid, target_registry_id uuid, target_revision integer
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF target_workspace_id IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR target_registry_id IS NULL OR target_revision IS NULL OR target_revision < 1 THEN
                RAISE EXCEPTION 'WP06_SHADOW_COHORT_SCOPE_INVALID';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'wp06-learning-observation-member-set:' || target_workspace_id::text, 0));
        END $$
    """)
    op.execute("""
        DO $$
        DECLARE function_definition text; call_start integer; call_end integer;
                call_marker constant text :=
                    'PERFORM public.wp06_lock_learning_shadow_cohort(';
        BEGIN
            SELECT pg_catalog.pg_get_functiondef(
                'public.wp06_require_learning_shadow_lineage()'::regprocedure)
              INTO function_definition;
            call_start := pg_catalog.strpos(function_definition, call_marker);
            IF call_start = 0 THEN
                RAISE EXCEPTION 'WP06_SHADOW_LINEAGE_FENCE_REWRITE_FAILED';
            END IF;
            call_end := call_start + pg_catalog.strpos(
                pg_catalog.substr(function_definition, call_start), ';') - 1;
            function_definition := pg_catalog.substr(function_definition, 1, call_start - 1)
                || pg_catalog.substr(function_definition, call_end + 1);
            EXECUTE function_definition;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_require_shadow_exact_source_scope()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE source_id text; source_ids text[]; authorized_fingerprint text;
        BEGIN
            IF NEW.workspace_id IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SHADOW_INFERENCE_SOURCE_SCOPE_DENIED';
            END IF;
            source_ids := public.wp06_shadow_drift_decision_source_ids(
                NEW.workspace_id, NEW.incumbent_decision_id);
            FOR source_id IN SELECT item FROM pg_catalog.unnest(source_ids) AS item
                             ORDER BY item LOOP
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || NEW.workspace_id::text || ':' || source_id, 0));
            END LOOP;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'wp06-learning-observation-member-set:' || NEW.workspace_id::text, 0));
            authorized_fingerprint := public.wp06_authorize_shadow_inference_source_scope(
                NEW.workspace_id, NEW.incumbent_decision_id, pg_catalog.clock_timestamp());
            IF NEW.rights_eligible THEN
                IF authorized_fingerprint IS NULL
                   OR NEW.source_scope_fingerprint IS DISTINCT FROM authorized_fingerprint THEN
                    RAISE EXCEPTION 'WP06_SHADOW_INFERENCE_SOURCE_SCOPE_STALE';
                END IF;
            ELSIF NEW.source_scope_fingerprint IS NOT NULL THEN
                RAISE EXCEPTION 'WP06_SHADOW_INFERENCE_SOURCE_SCOPE_INVALID';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION public.wp06_require_shadow_exact_source_scope() FROM PUBLIC
    """)
    op.execute("""
        CREATE TRIGGER zz_wp06_shadow_observation_exact_source_scope
        BEFORE INSERT ON public.learning_shadow_observation FOR EACH ROW
        EXECUTE FUNCTION public.wp06_require_shadow_exact_source_scope()
    """)


def _create_consumer_guards() -> None:
    _create_hash_and_shadow_writer_gates()
    _create_drift_writer_gates()
    _create_admin_rights_read_gates()


def _create_drift_writer_gates() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION public.wp06_drift_assessment_idempotency_key(
            row_value public.learning_drift_assessment
        ) RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE document jsonb;
        BEGIN
            IF row_value.source_rights_digest IS NULL THEN
                RETURN public.wp06_sha256_canonical_json(pg_catalog.json_build_object(
                    'membership_digest', row_value.membership_digest,
                    'policy_version', row_value.policy_version,
                    'registry_id', row_value.cohort_registry_id::text,
                    'revision', row_value.cohort_revision,
                    'workspace_id', row_value.workspace_id::text
                ));
            END IF;
            document := pg_catalog.jsonb_build_object(
                'membership_digest', row_value.membership_digest,
                'policy_version', row_value.policy_version,
                'registry_id', row_value.cohort_registry_id::text,
                'revision', row_value.cohort_revision,
                'workspace_id', row_value.workspace_id::text
            );
            IF row_value.source_rights_digest IS NOT NULL THEN
                document := document || pg_catalog.jsonb_build_object(
                    'source_rights_digest', row_value.source_rights_digest);
            END IF;
            RETURN public.wp06_sha256_canonical_json(document::json);
        END $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.wp06_drift_assessment_content_hash(
            row_value public.learning_drift_assessment, member_ids json
        ) RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE document jsonb;
        BEGIN
            IF row_value.source_rights_digest IS NULL THEN
                RETURN public.wp06_sha256_canonical_json(pg_catalog.json_build_object(
                    'abstention_rate', public.wp06_canonical_float_json(row_value.abstention_rate),
                    'assessment_id', row_value.id::text,
                    'cohort_registry_id', row_value.cohort_registry_id::text,
                    'cohort_revision', row_value.cohort_revision,
                    'created_at', public.wp06_canonical_utc_timestamp_json(row_value.created_at),
                    'data_quality_status', row_value.data_quality_status,
                    'eligible_count', row_value.eligible_count,
                    'member_count', row_value.member_count,
                    'member_ids', member_ids,
                    'membership_digest', row_value.membership_digest,
                    'performance_status', row_value.performance_status,
                    'policy_version', row_value.policy_version,
                    'quality_rate', public.wp06_canonical_float_json(row_value.quality_rate),
                    'reason_codes', row_value.reason_codes::json,
                    'snapshot_coverage_rate', public.wp06_canonical_float_json(
                        row_value.snapshot_coverage_rate),
                    'window_end', public.wp06_canonical_utc_timestamp_json(row_value.window_end),
                    'window_start', public.wp06_canonical_utc_timestamp_json(
                        row_value.window_start),
                    'workspace_id', row_value.workspace_id::text
                ));
            END IF;
            document := pg_catalog.jsonb_build_object(
                'abstention_rate', public.wp06_canonical_float_json(row_value.abstention_rate),
                'assessment_id', row_value.id::text,
                'cohort_registry_id', row_value.cohort_registry_id::text,
                'cohort_revision', row_value.cohort_revision,
                'created_at', public.wp06_canonical_utc_timestamp_json(row_value.created_at),
                'data_quality_status', row_value.data_quality_status,
                'eligible_count', row_value.eligible_count,
                'member_count', row_value.member_count,
                'member_ids', member_ids,
                'membership_digest', row_value.membership_digest,
                'performance_status', row_value.performance_status,
                'policy_version', row_value.policy_version,
                'quality_rate', public.wp06_canonical_float_json(row_value.quality_rate),
                'reason_codes', row_value.reason_codes::json,
                'snapshot_coverage_rate', public.wp06_canonical_float_json(
                    row_value.snapshot_coverage_rate),
                'window_end', public.wp06_canonical_utc_timestamp_json(row_value.window_end),
                'window_start', public.wp06_canonical_utc_timestamp_json(row_value.window_start),
                'workspace_id', row_value.workspace_id::text
            );
            IF row_value.source_rights_digest IS NOT NULL THEN
                document := document || pg_catalog.jsonb_build_object(
                    'source_rights_digest', row_value.source_rights_digest);
            END IF;
            RETURN public.wp06_sha256_canonical_json(document::json);
        END $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.wp06_lock_learning_drift_assessment_cohort()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE source_id text; actual_count integer; actual_digest text;
        BEGIN
            IF NEW.created_at IS DISTINCT FROM transaction_timestamp()
               OR NEW.policy_version IS DISTINCT FROM 'coverage-v2-30d'
               OR NEW.observation_window_days IS DISTINCT FROM 30
               OR NEW.minimum_observations <> 3
               OR NEW.minimum_snapshot_coverage <> 0.8
               OR NEW.minimum_quality_rate <> 0.9 OR NEW.member_count = 0
               OR NEW.cohort_registry_id IS NULL OR NEW.cohort_revision IS NULL
               OR NEW.window_start IS NULL OR NEW.window_end IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_POLICY_OR_TIME_INVALID';
            END IF;
            FOR source_id IN
                SELECT DISTINCT providers.source_id
                  FROM public.learning_shadow_observation AS observation
                  CROSS JOIN LATERAL pg_catalog.unnest(
                      public.wp06_shadow_drift_decision_source_ids(
                          NEW.workspace_id, observation.incumbent_decision_id))
                      AS providers(source_id)
                 WHERE observation.workspace_id = NEW.workspace_id
                   AND observation.challenger_registry_id = NEW.cohort_registry_id
                   AND observation.challenger_revision = NEW.cohort_revision
                   AND observation.observed_at >= NEW.window_start
                   AND observation.observed_at <= NEW.window_end
                 ORDER BY providers.source_id
            LOOP
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || NEW.workspace_id::text || ':' || source_id, 0));
            END LOOP;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'wp06-learning-observation-member-set:' || NEW.workspace_id::text, 0));
            SELECT pg_catalog.count(*)::integer,
                   public.wp06_sha256_canonical_json(COALESCE(
                       pg_catalog.json_agg(observation.id::text
                           ORDER BY observation.id::text), '[]'::json))
              INTO actual_count, actual_digest
              FROM public.learning_shadow_observation AS observation
             WHERE observation.workspace_id = NEW.workspace_id
               AND observation.challenger_registry_id = NEW.cohort_registry_id
               AND observation.challenger_revision = NEW.cohort_revision
               AND observation.observed_at >= NEW.window_start
               AND observation.observed_at <= NEW.window_end;
            IF actual_count <> NEW.member_count
               OR actual_digest IS DISTINCT FROM NEW.membership_digest THEN
                RAISE EXCEPTION 'WP06 CURRENT MEMBER SET CHANGED';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.wp06_require_learning_drift_member_lineage()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE observation_row record; snapshot_row record; assessment_row record;
                baseline_found boolean; expected_source_ids jsonb;
                expected_fingerprint text; expected_current_rights boolean;
        BEGIN
            SELECT observation.id, observation.workspace_id, observation.incumbent_decision_id,
                   observation.challenger_registry_id, observation.challenger_revision,
                   observation.decision_hash, observation.snapshot_id, observation.snapshot_hash,
                   observation.observed_at, observation.snapshot_coverage,
                   observation.snapshot_quality_ok, observation.schema_compatible,
                   observation.rights_eligible, observation.rights_fingerprint,
                   observation.challenger_decision
              INTO observation_row
              FROM public.learning_shadow_observation AS observation
             WHERE observation.workspace_id = NEW.workspace_id
               AND observation.id = NEW.observation_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_MEMBER_LINEAGE_INVALID';
            END IF;
            SELECT snapshot.workspace_id, snapshot.snapshot_id, snapshot.data_coverage,
                   snapshot.data_freshness, snapshot.quality_flags, snapshot.is_usable
              INTO snapshot_row
              FROM public.feature_snapshot AS snapshot
             WHERE snapshot.workspace_id = NEW.workspace_id
               AND snapshot.snapshot_id = observation_row.snapshot_id;
            SELECT assessment.cohort_registry_id, assessment.cohort_revision
              INTO assessment_row
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = NEW.workspace_id
               AND assessment.id = NEW.assessment_id;
            IF NOT FOUND OR snapshot_row.snapshot_id IS NULL THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_MEMBER_LINEAGE_INVALID';
            END IF;
            SELECT EXISTS (
                SELECT 1 FROM public.bot_decision AS decision
                 WHERE decision.workspace_id = observation_row.workspace_id
                   AND decision.id = observation_row.incumbent_decision_id
                   AND decision.decision_hash = observation_row.decision_hash
            ) INTO baseline_found;
            SELECT COALESCE(pg_catalog.jsonb_agg(item ORDER BY item), '[]'::jsonb)
              INTO expected_source_ids
              FROM pg_catalog.unnest(public.wp06_shadow_drift_decision_source_ids(
                    NEW.workspace_id, observation_row.incumbent_decision_id)) AS item;
            expected_fingerprint := CASE WHEN pg_catalog.jsonb_array_length(expected_source_ids) > 0
                THEN public.wp06_authorize_learning_drift_source_scope(
                    NEW.workspace_id, observation_row.incumbent_decision_id,
                    pg_catalog.clock_timestamp())
                ELSE NULL END;
            expected_current_rights := observation_row.rights_eligible
                AND expected_fingerprint IS NOT NULL;
            IF observation_row.observed_at <> NEW.observed_at
                OR observation_row.challenger_registry_id
                    IS DISTINCT FROM assessment_row.cohort_registry_id
                OR observation_row.challenger_revision
                    IS DISTINCT FROM assessment_row.cohort_revision
                OR observation_row.snapshot_hash <> NEW.snapshot_hash
                OR snapshot_row.data_coverage <> NEW.snapshot_coverage
                OR observation_row.snapshot_coverage <> NEW.snapshot_coverage
                OR observation_row.snapshot_quality_ok <> (
                    snapshot_row.is_usable AND snapshot_row.data_freshness = 'FRESH'
                    AND snapshot_row.quality_flags = '[]'::jsonb)
                OR observation_row.snapshot_quality_ok <> NEW.snapshot_quality_ok
                OR observation_row.schema_compatible <> NEW.schema_compatible
                OR observation_row.rights_eligible <> NEW.rights_eligible
                OR observation_row.rights_fingerprint IS DISTINCT FROM NEW.rights_fingerprint
                OR baseline_found <> NEW.baseline_available
                OR observation_row.challenger_decision <> 'abstain'
                OR NEW.challenger_decision <> 'abstain'
                OR NEW.current_source_ids IS DISTINCT FROM expected_source_ids
                OR NEW.current_rights_eligible IS DISTINCT FROM expected_current_rights
                OR NEW.current_rights_fingerprint IS DISTINCT FROM
                    (CASE WHEN expected_current_rights THEN expected_fingerprint ELSE NULL END)
                OR NEW.current_eligible IS DISTINCT FROM (
                    NEW.baseline_available AND NEW.schema_compatible AND expected_current_rights)
                OR NEW.eligible IS DISTINCT FROM NEW.current_eligible THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_MEMBER_LINEAGE_INVALID';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        ALTER TABLE public.learning_drift_assessment_member
        ADD CONSTRAINT learning_drift_assessment_member_current_eligible_valid
        CHECK (current_eligible IS NULL OR
            (current_eligible = (baseline_available AND schema_compatible
                AND current_rights_eligible) AND eligible = current_eligible)) NOT VALID
    """)
    _create_drift_validation_gateway()


def _create_drift_validation_gateway() -> None:
    op.execute("""
        CREATE FUNCTION public.wp06_learning_drift_current_rights_digest(
            target_workspace uuid, target_assessment uuid, checked_at timestamptz
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE member_row record; source_ids text[]; source_ids_json jsonb;
                fingerprint text; expected_rights boolean; document jsonb := '[]'::jsonb;
        BEGIN
            IF target_workspace IS NULL OR target_assessment IS NULL OR checked_at IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RETURN NULL;
            END IF;
            FOR member_row IN
                SELECT member.workspace_id, member.assessment_id, member.observation_id,
                       member.current_rights_eligible, member.current_rights_fingerprint,
                       member.current_source_ids, member.current_eligible,
                       member.baseline_available, member.schema_compatible,
                       observation.incumbent_decision_id, observation.rights_eligible
                  FROM public.learning_drift_assessment_member AS member
                  JOIN public.learning_shadow_observation AS observation
                    ON observation.workspace_id = member.workspace_id
                   AND observation.id = member.observation_id
                 WHERE member.workspace_id = target_workspace
                   AND member.assessment_id = target_assessment
                 ORDER BY member.observation_id::text
            LOOP
                source_ids := public.wp06_shadow_drift_decision_source_ids(
                    target_workspace, member_row.incumbent_decision_id);
                source_ids_json := pg_catalog.to_jsonb(COALESCE(source_ids, ARRAY[]::text[]));
                fingerprint := CASE WHEN pg_catalog.cardinality(source_ids) > 0
                    THEN public.wp06_authorize_learning_drift_source_scope(
                        target_workspace, member_row.incumbent_decision_id, checked_at)
                    ELSE NULL END;
                expected_rights := member_row.rights_eligible AND fingerprint IS NOT NULL;
                IF member_row.current_source_ids IS DISTINCT FROM source_ids_json
                   OR member_row.current_rights_eligible IS DISTINCT FROM expected_rights
                   OR member_row.current_rights_fingerprint IS DISTINCT FROM
                        (CASE WHEN expected_rights THEN fingerprint ELSE NULL END)
                   OR member_row.current_eligible IS DISTINCT FROM (
                        member_row.baseline_available AND member_row.schema_compatible
                        AND expected_rights) THEN
                    RETURN NULL;
                END IF;
                document := document || pg_catalog.jsonb_build_array(
                    pg_catalog.jsonb_build_object(
                        'decision_id', member_row.incumbent_decision_id::text,
                        'observation_id', member_row.observation_id::text,
                        'source_ids', source_ids_json,
                        'source_scope_fingerprint', CASE
                            WHEN expected_rights THEN fingerprint ELSE NULL END
                    ));
            END LOOP;
            IF document = '[]'::jsonb THEN
                RETURN NULL;
            END IF;
            RETURN pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                public.wp06_license_canonical_json(document), 'UTF8')), 'hex');
        END $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_learning_drift_current_rights_digest(uuid,uuid,timestamptz) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute("""
        REVOKE ALL ON FUNCTION public.wp06_learning_drift_current_rights_digest(
            uuid,uuid,timestamptz) FROM PUBLIC
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.wp06_validate_learning_drift_assessment_membership()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE target_workspace uuid; target_assessment uuid; assessment_row record;
                actual_member_ids json; actual_digest text; actual_count integer;
                actual_eligible_count integer; actual_window_start timestamptz;
                actual_window_end timestamptz; actual_coverage float8; actual_quality float8;
                actual_abstention float8; current_digest text; actual_cohort_count integer;
                actual_cohort_digest text; invalid_ordinal boolean; required_complete boolean;
                has_low_coverage boolean := false; has_low_quality boolean := false;
                reason_list text[] := ARRAY['performance_not_evaluable']::text[];
                expected_status text; expected_reasons jsonb; expected_idempotency_key text;
                expected_content_hash text;
        BEGIN
            IF TG_TABLE_NAME = 'learning_drift_assessment' THEN
                target_workspace := NEW.workspace_id; target_assessment := NEW.id;
            ELSE
                target_workspace := NEW.workspace_id; target_assessment := NEW.assessment_id;
            END IF;
            SELECT assessment.* INTO assessment_row
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = target_workspace
               AND assessment.id = target_assessment;
            IF NOT FOUND THEN RETURN NULL; END IF;
            SELECT pg_catalog.count(*)::integer,
                   pg_catalog.count(*) FILTER (WHERE member.current_eligible)::integer,
                   pg_catalog.min(member.observed_at), pg_catalog.max(member.observed_at),
                   pg_catalog.round(pg_catalog.sum(member.snapshot_coverage::text::numeric)
                       FILTER (WHERE member.current_eligible)
                       / NULLIF(pg_catalog.count(*) FILTER (WHERE member.current_eligible), 0),
                       12)::float8,
                   pg_catalog.round((pg_catalog.count(*) FILTER (
                       WHERE member.current_eligible AND member.snapshot_quality_ok))::numeric
                       / NULLIF(pg_catalog.count(*) FILTER (WHERE member.current_eligible), 0),
                       12)::float8,
                   CASE WHEN pg_catalog.bool_or(
                       member.current_rights_eligible IS DISTINCT FROM true)
                       THEN NULL
                       ELSE pg_catalog.round((pg_catalog.count(*) FILTER (
                           WHERE member.challenger_decision = 'abstain'))::numeric
                           / NULLIF(pg_catalog.count(*), 0), 12)::float8 END,
                   COALESCE(pg_catalog.json_agg(member.observation_id::text
                       ORDER BY member.observation_id::text), '[]'::json),
                   public.wp06_sha256_canonical_json(COALESCE(pg_catalog.json_agg(
                       member.observation_id::text ORDER BY member.observation_id::text),
                       '[]'::json))
              INTO actual_count, actual_eligible_count, actual_window_start, actual_window_end,
                   actual_coverage, actual_quality, actual_abstention, actual_member_ids,
                   actual_digest
              FROM public.learning_drift_assessment_member AS member
             WHERE member.workspace_id = target_workspace
               AND member.assessment_id = target_assessment;
            SELECT EXISTS (
                SELECT 1 FROM (
                    SELECT member.ordinal,
                           pg_catalog.row_number() OVER (ORDER BY member.observation_id::text) - 1
                               AS expected_ordinal
                      FROM public.learning_drift_assessment_member AS member
                     WHERE member.workspace_id = target_workspace
                       AND member.assessment_id = target_assessment
                ) AS ordered WHERE ordered.ordinal <> ordered.expected_ordinal
            ) INTO invalid_ordinal;
            SELECT pg_catalog.count(*)::integer,
                   public.wp06_sha256_canonical_json(COALESCE(pg_catalog.json_agg(
                       observation.id::text ORDER BY observation.id::text), '[]'::json))
              INTO actual_cohort_count, actual_cohort_digest
              FROM public.learning_shadow_observation AS observation
             WHERE observation.workspace_id = target_workspace
               AND observation.challenger_registry_id = assessment_row.cohort_registry_id
               AND observation.challenger_revision = assessment_row.cohort_revision
               AND observation.observed_at >= assessment_row.window_start
               AND observation.observed_at <= assessment_row.window_end;
            current_digest := public.wp06_learning_drift_current_rights_digest(
                target_workspace, target_assessment, pg_catalog.clock_timestamp());
            SELECT public.wp06_drift_assessment_idempotency_key(assessment)
              INTO expected_idempotency_key
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = target_workspace
               AND assessment.id = target_assessment;
            SELECT public.wp06_drift_assessment_content_hash(assessment, actual_member_ids)
              INTO expected_content_hash
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = target_workspace
               AND assessment.id = target_assessment;
            required_complete := actual_count >= assessment_row.minimum_observations
                AND actual_count = actual_eligible_count;
            IF actual_count < assessment_row.minimum_observations THEN
                reason_list := pg_catalog.array_append(reason_list, 'low_sample');
            END IF;
            IF EXISTS (SELECT 1 FROM public.learning_drift_assessment_member AS member
                WHERE member.workspace_id = target_workspace
                  AND member.assessment_id = target_assessment
                  AND NOT member.baseline_available) THEN
                reason_list := pg_catalog.array_append(reason_list, 'baseline_missing');
            END IF;
            IF EXISTS (SELECT 1 FROM public.learning_drift_assessment_member AS member
                WHERE member.workspace_id = target_workspace
                  AND member.assessment_id = target_assessment
                  AND NOT member.schema_compatible) THEN
                reason_list := pg_catalog.array_append(reason_list, 'schema_incompatible');
            END IF;
            IF EXISTS (SELECT 1 FROM public.learning_drift_assessment_member AS member
                WHERE member.workspace_id = target_workspace
                  AND member.assessment_id = target_assessment
                  AND member.current_rights_eligible IS DISTINCT FROM true) THEN
                reason_list := pg_catalog.array_append(reason_list, 'rights_unverified');
            END IF;
            IF required_complete THEN
                has_low_coverage := actual_coverage < assessment_row.minimum_snapshot_coverage;
                has_low_quality := actual_quality < assessment_row.minimum_quality_rate;
                IF has_low_coverage THEN
                    reason_list := pg_catalog.array_append(reason_list, 'coverage_below_threshold');
                END IF;
                IF has_low_quality THEN
                    reason_list := pg_catalog.array_append(reason_list, 'quality_below_threshold');
                END IF;
                expected_status := CASE WHEN has_low_coverage OR has_low_quality
                    THEN 'detected' ELSE 'no_drift' END;
            ELSE
                actual_coverage := NULL; actual_quality := NULL;
                expected_status := 'inconclusive';
            END IF;
            SELECT pg_catalog.jsonb_agg(reason ORDER BY reason) INTO expected_reasons
              FROM pg_catalog.unnest(reason_list) AS reason;
            IF assessment_row.source_rights_digest IS NULL
               OR assessment_row.source_rights_digest IS DISTINCT FROM current_digest
               OR assessment_row.idempotency_key IS DISTINCT FROM expected_idempotency_key
               OR assessment_row.content_hash IS DISTINCT FROM expected_content_hash
               OR actual_count <> assessment_row.member_count
               OR actual_count <> actual_cohort_count
               OR actual_eligible_count <> assessment_row.eligible_count
               OR assessment_row.window_start IS DISTINCT FROM actual_window_start
               OR assessment_row.window_end IS DISTINCT FROM assessment_row.created_at
               OR assessment_row.created_at < actual_window_end
               OR assessment_row.membership_digest IS DISTINCT FROM actual_digest
               OR actual_digest IS DISTINCT FROM actual_cohort_digest
               OR invalid_ordinal
               OR assessment_row.performance_status <> 'inconclusive'
               OR assessment_row.data_quality_status <> expected_status
               OR assessment_row.reason_codes <> expected_reasons
               OR assessment_row.snapshot_coverage_rate IS DISTINCT FROM actual_coverage
               OR assessment_row.quality_rate IS DISTINCT FROM actual_quality
               OR assessment_row.abstention_rate IS DISTINCT FROM actual_abstention THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_MEMBERSHIP_OR_SOURCE_RIGHTS_INVALID';
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.wp06_recheck_learning_drift_assessment_source_rights(
            target_workspace uuid, target_assessment uuid
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE assessment_row record; member_row record; source_id text; source_ids text[];
                current_count integer; current_membership_digest text; current_rights_digest text;
        BEGIN
            IF session_user <> '{ADMIN}' OR target_workspace IS NULL OR target_assessment IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RETURN false;
            END IF;
            SELECT assessment.workspace_id, assessment.id,
                   assessment.source_rights_digest, assessment.cohort_registry_id,
                   assessment.cohort_revision, assessment.window_start,
                   assessment.window_end, assessment.member_count,
                   assessment.membership_digest
              INTO assessment_row
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = target_workspace
               AND assessment.id = target_assessment;
            IF NOT FOUND OR assessment_row.source_rights_digest IS NULL THEN RETURN false; END IF;
            FOR source_id IN
                SELECT DISTINCT providers.source_id
                  FROM public.learning_drift_assessment_member AS member
                  JOIN public.learning_shadow_observation AS observation
                    ON observation.workspace_id = member.workspace_id
                   AND observation.id = member.observation_id
                  CROSS JOIN LATERAL pg_catalog.unnest(
                      public.wp06_shadow_drift_decision_source_ids(
                          target_workspace, observation.incumbent_decision_id))
                      AS providers(source_id)
                 WHERE member.workspace_id = target_workspace
                   AND member.assessment_id = target_assessment
                 ORDER BY providers.source_id
            LOOP
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || source_id, 0));
            END LOOP;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'wp06-learning-observation-member-set:' || target_workspace::text, 0));
            SELECT pg_catalog.count(*)::integer,
                   public.wp06_sha256_canonical_json(COALESCE(pg_catalog.json_agg(
                       observation.id::text ORDER BY observation.id::text), '[]'::json))
              INTO current_count, current_membership_digest
              FROM public.learning_shadow_observation AS observation
             WHERE observation.workspace_id = target_workspace
               AND observation.challenger_registry_id = assessment_row.cohort_registry_id
               AND observation.challenger_revision = assessment_row.cohort_revision
               AND observation.observed_at >= assessment_row.window_start
               AND observation.observed_at <= assessment_row.window_end;
            IF current_count <> assessment_row.member_count
               OR current_membership_digest IS DISTINCT FROM assessment_row.membership_digest THEN
                RETURN false;
            END IF;
            current_rights_digest := public.wp06_learning_drift_current_rights_digest(
                target_workspace, target_assessment, pg_catalog.clock_timestamp());
            RETURN current_rights_digest IS NOT NULL
                AND current_rights_digest = assessment_row.source_rights_digest;
        EXCEPTION WHEN raise_exception THEN
            RETURN false;
        END $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_recheck_learning_drift_assessment_source_rights(uuid,uuid) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute("""
        REVOKE ALL ON FUNCTION public.wp06_recheck_learning_drift_assessment_source_rights(
            uuid,uuid) FROM PUBLIC, crypidian_app, crypidian_trading_runtime
    """)
    op.execute(f"""
        GRANT EXECUTE ON FUNCTION public.wp06_recheck_learning_drift_assessment_source_rights(
            uuid,uuid) TO {ADMIN}
    """)


def _create_admin_rights_read_gates() -> None:
    op.execute(f"""
        CREATE FUNCTION public.wp06_recheck_learning_shadow_observation_source_rights(
            target_workspace uuid, target_observation uuid
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE observation_row record; source_ids text[]; source_id text; fingerprint text;
        BEGIN
            IF session_user <> '{ADMIN}' OR target_workspace IS NULL OR target_observation IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RETURN false;
            END IF;
            SELECT observation.workspace_id, observation.incumbent_decision_id,
                   observation.rights_eligible, observation.source_scope_fingerprint
              INTO observation_row
              FROM public.learning_shadow_observation AS observation
             WHERE observation.workspace_id = target_workspace
               AND observation.id = target_observation;
            IF NOT FOUND OR observation_row.rights_eligible IS DISTINCT FROM true
               OR observation_row.source_scope_fingerprint IS NULL THEN
                RETURN false;
            END IF;
            source_ids := public.wp06_shadow_drift_decision_source_ids(
                target_workspace, observation_row.incumbent_decision_id);
            IF source_ids IS NULL OR pg_catalog.cardinality(source_ids) = 0 THEN
                RETURN false;
            END IF;
            FOR source_id IN SELECT item FROM pg_catalog.unnest(source_ids) AS item
                             ORDER BY item LOOP
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || source_id, 0));
            END LOOP;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'wp06-learning-observation-member-set:' || target_workspace::text, 0));
            fingerprint := public.wp06_authorize_shadow_inference_source_scope(
                target_workspace, observation_row.incumbent_decision_id,
                pg_catalog.clock_timestamp());
            RETURN fingerprint IS NOT NULL
                AND fingerprint = observation_row.source_scope_fingerprint;
        EXCEPTION WHEN raise_exception THEN
            RETURN false;
        END $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_recheck_learning_shadow_observation_source_rights(uuid,uuid) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_recheck_learning_shadow_observation_source_rights("
        "uuid,uuid) FROM PUBLIC, crypidian_app, crypidian_trading_runtime"
    )
    op.execute(f"""
        GRANT EXECUTE ON FUNCTION public.wp06_recheck_learning_shadow_observation_source_rights(
            uuid,uuid) TO {ADMIN}
    """)


def _create_incident_target_source_scope() -> None:
    """Lấy scope provider của target active để khóa chung trước khi HOLD recheck."""
    op.execute(f"""
        CREATE FUNCTION public.wp06_learning_incident_target_source_ids(
            target_workspace uuid
        ) RETURNS text[] LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE deployment_row record; target_document jsonb; artifact_row record;
                proof_row record; source_ids text[];
        BEGIN
            IF session_user <> '{ADMIN}' OR target_workspace IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_INCIDENT_SOURCE_SCOPE_DENIED';
            END IF;
            SELECT pointer.mode, pointer.event_id, event.event_payload->'target' AS target
              INTO deployment_row
              FROM public.learning_scoring_deployment_pointer AS pointer
              LEFT JOIN public.learning_scoring_deployment_event AS event
                ON event.workspace_id = pointer.workspace_id
               AND event.id = pointer.event_id
             WHERE pointer.workspace_id = target_workspace;
            IF NOT FOUND OR deployment_row.mode = 'DISABLED' THEN
                RETURN ARRAY[]::text[];
            END IF;
            IF deployment_row.mode NOT IN ('SHADOW','PAPER_GATE')
               OR deployment_row.event_id IS NULL
               OR pg_catalog.jsonb_typeof(deployment_row.target) IS DISTINCT FROM 'object'
               OR deployment_row.target->>'scorer_artifact_id' !~
                    '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
               OR deployment_row.target->>'evaluation_report_id' !~
                    '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$' THEN
                RAISE EXCEPTION 'WP06_INCIDENT_SOURCE_SCOPE_TARGET_INVALID';
            END IF;
            SELECT artifact.attempt_id, artifact.manifest_hash, artifact.fold_id
              INTO artifact_row
              FROM public.learning_scorer_artifact AS artifact
             WHERE artifact.workspace_id = target_workspace
               AND artifact.id = (deployment_row.target->>'scorer_artifact_id')::uuid;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_INCIDENT_SOURCE_SCOPE_ARTIFACT_MISSING';
            END IF;
            SELECT proof.bundle_hash, bundle.rights_provider
              INTO proof_row
              FROM public.learning_scorer_test_evaluation_report AS report
              JOIN public.learning_scorer_test_opportunity_proof AS proof
                ON proof.workspace_id = report.workspace_id AND proof.id = report.proof_id
              JOIN public.backtest_dataset_replay_bundle AS bundle
                ON bundle.workspace_id = proof.workspace_id
               AND bundle.bundle_hash = proof.bundle_hash
             WHERE report.workspace_id = target_workspace
               AND report.id = (deployment_row.target->>'evaluation_report_id')::uuid
               AND proof.attempt_id = artifact_row.attempt_id
               AND proof.fold_id = artifact_row.fold_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_INCIDENT_SOURCE_SCOPE_PROOF_MISSING';
            END IF;
            SELECT pg_catalog.array_agg(DISTINCT source.source_id ORDER BY source.source_id)
              INTO source_ids
              FROM (
                    SELECT reference.source_id
                      FROM public.dataset_manifest AS manifest
                      CROSS JOIN LATERAL public.wp06_scorer_manifest_source_refs(
                          target_workspace, manifest.id) AS reference
                     WHERE manifest.workspace_id = target_workspace
                       AND manifest.manifest_hash = artifact_row.manifest_hash
                    UNION
                    SELECT bundle.rights_provider
                      FROM public.learning_scorer_test_opportunity_proof AS proof
                      JOIN public.backtest_dataset_replay_bundle AS bundle
                        ON bundle.workspace_id = proof.workspace_id
                       AND bundle.bundle_hash = proof.bundle_hash
                     WHERE proof.workspace_id = target_workspace
                       AND proof.attempt_id = artifact_row.attempt_id
                       AND proof.fold_id = artifact_row.fold_id
                    UNION
                    SELECT proof_row.rights_provider
                    UNION SELECT 'BINANCE_USDM_REST'::text
                    UNION SELECT 'CRYPIDIAN_FIRST_PARTY_QA'::text
              ) AS source;
            IF source_ids IS NULL OR pg_catalog.cardinality(source_ids) = 0
               OR pg_catalog.array_position(source_ids, NULL) IS NOT NULL THEN
                RAISE EXCEPTION 'WP06_INCIDENT_SOURCE_SCOPE_INVALID';
            END IF;
            RETURN source_ids;
        END $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_learning_incident_target_source_ids(uuid) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_learning_incident_target_source_ids(uuid) "
        "FROM PUBLIC, crypidian_app, crypidian_trading_runtime, "
        "crypidian_learning_shadow_observer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_learning_incident_target_source_ids(uuid) "
        f"TO {ADMIN}"
    )


def _fence_bot_decision_source_bar_append() -> None:
    """Khóa toàn bộ source của lineage deferred trước khi commit bộ bar mới."""
    op.execute("""
        CREATE OR REPLACE FUNCTION public.wp06_assert_complete_bot_decision_source_bar_set()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE group_row record; expected_digest text; expected_timeframes integer;
                actual_timeframes integer; actual_snapshot_timeframes integer;
                source_id text;
        BEGIN
            IF NEW.ordinal <> 0 AND EXISTS (
                SELECT 1 FROM public.bot_decision_source_bar AS first_row
                 WHERE first_row.workspace_id = NEW.workspace_id
                   AND first_row.bot_decision_id = NEW.bot_decision_id
                   AND first_row.timeframe = NEW.timeframe
                   AND first_row.ordinal = 0
            ) THEN
                RETURN NULL;
            END IF;
            FOR source_id IN
                SELECT DISTINCT provider.source_id
                  FROM public.bot_decision_source_bar AS source
                  JOIN public.market_bar AS bar
                    ON bar.workspace_id = source.workspace_id
                   AND bar.id = source.market_bar_id
                  CROSS JOIN LATERAL (SELECT CASE bar.source
                    WHEN 'BINANCE_USDM' THEN 'BINANCE_USDM_REST'
                    WHEN 'BINANCE_USDM_REST_BACKFILL' THEN 'BINANCE_USDM_REST'
                    WHEN 'CRYPIDIAN_FIRST_PARTY_QA_FIXTURE'
                        THEN 'CRYPIDIAN_FIRST_PARTY_QA'
                    ELSE NULL END AS source_id) AS provider
                 WHERE source.workspace_id = NEW.workspace_id
                   AND source.bot_decision_id = NEW.bot_decision_id
                 ORDER BY provider.source_id
            LOOP
                IF source_id IS NULL
                   OR source_id !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' THEN
                    RAISE EXCEPTION 'WP06_SOURCE_BAR_PROVIDER_UNKNOWN';
                END IF;
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || NEW.workspace_id::text || ':' || source_id, 0));
            END LOOP;
            IF source_id IS NULL THEN
                RAISE EXCEPTION 'WP06_SOURCE_BAR_LINEAGE_INCOMPLETE';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'wp06-learning-observation-member-set:' || NEW.workspace_id::text, 0));
            FOR group_row IN
                SELECT timeframe, count(*)::integer AS actual_count,
                       min(ordinal) AS min_ordinal, max(ordinal) AS max_ordinal,
                       min(timeframe_bar_count) AS minimum_expected_count,
                       max(timeframe_bar_count) AS maximum_expected_count,
                       min(snapshot_timeframe_count) AS minimum_expected_timeframes,
                       max(snapshot_timeframe_count) AS maximum_expected_timeframes,
                       count(DISTINCT source_event_digest)::integer AS digest_count,
                       min(source_event_digest) AS source_event_digest
                  FROM public.bot_decision_source_bar
                 WHERE workspace_id = NEW.workspace_id
                   AND bot_decision_id = NEW.bot_decision_id
                 GROUP BY timeframe
            LOOP
                IF group_row.actual_count <> group_row.minimum_expected_count
                   OR group_row.minimum_expected_count <> group_row.maximum_expected_count
                   OR group_row.min_ordinal <> 0
                   OR group_row.max_ordinal <> group_row.actual_count - 1
                   OR group_row.minimum_expected_timeframes <> group_row.maximum_expected_timeframes
                   OR group_row.digest_count <> 1 THEN
                    RAISE EXCEPTION 'WP06_SOURCE_BAR_LINEAGE_INCOMPLETE';
                END IF;
                SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    public.wp06_license_canonical_json(pg_catalog.jsonb_build_object(
                        'timeframe', group_row.timeframe,
                        'source_event_ids', (
                            SELECT pg_catalog.jsonb_agg(item.source_event_id ORDER BY item.ordinal)
                              FROM public.bot_decision_source_bar AS item
                             WHERE item.workspace_id = NEW.workspace_id
                               AND item.bot_decision_id = NEW.bot_decision_id
                               AND item.timeframe = group_row.timeframe
                        ),
                        'source_bar_ids', (
                            SELECT pg_catalog.jsonb_agg(
                                'market-bar-record:' || item.market_bar_id::text
                                ORDER BY item.ordinal
                            )
                              FROM public.bot_decision_source_bar AS item
                             WHERE item.workspace_id = NEW.workspace_id
                               AND item.bot_decision_id = NEW.bot_decision_id
                               AND item.timeframe = group_row.timeframe
                        )
                    )), 'UTF8')), 'hex')
                  INTO expected_digest;
                IF expected_digest IS DISTINCT FROM group_row.source_event_digest THEN
                    RAISE EXCEPTION 'WP06_SOURCE_BAR_LINEAGE_DIGEST_MISMATCH';
                END IF;
            END LOOP;
            SELECT count(DISTINCT timeframe)::integer,
                   min(snapshot_timeframe_count), max(snapshot_timeframe_count)
              INTO actual_timeframes, expected_timeframes, actual_snapshot_timeframes
              FROM public.bot_decision_source_bar
             WHERE workspace_id = NEW.workspace_id
               AND bot_decision_id = NEW.bot_decision_id;
            IF actual_timeframes < 1
               OR expected_timeframes <> actual_snapshot_timeframes
               OR actual_timeframes <> expected_timeframes THEN
                RAISE EXCEPTION 'WP06_SOURCE_BAR_TIMEFRAME_SET_INCOMPLETE';
            END IF;
            RETURN NULL;
        END; $$
    """)


def _harden_incident_eligibility() -> None:
    """Revalidate HOLD rights after locking the complete active/member source union."""
    op.execute("""
        CREATE OR REPLACE FUNCTION public.wp06_learning_incident_eligible_target(
            target_workspace uuid, target_proposal uuid
        ) RETURNS TABLE (
            incumbent_registry_id uuid, incumbent_registry_revision integer,
            incumbent_artifact_hash text, active_pointer_version integer,
            deployment_revision integer
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE proposal public.learning_retrain_proposal%ROWTYPE;
                assessment public.learning_drift_assessment%ROWTYPE;
                registry public.learning_model_registry%ROWTYPE;
                incumbent record; resolved record; source_id text;
                target_source_ids text[]; target_source_ids_after text[];
                assessment_source_ids text[]; locked_source_ids text[];
                current_source_ids text[]; membership_count integer; tuple_count integer;
                current_member_count integer; current_member_digest text;
                current_cohort_count integer; current_cohort_digest text;
                current_rights_digest text;
        BEGIN
            PERFORM public.wp06_lock_learning_incident_scope(target_workspace, NULL);
            SELECT * INTO proposal FROM public.learning_retrain_proposal AS p
             WHERE p.workspace_id = target_workspace AND p.id = target_proposal;
            IF NOT FOUND THEN RAISE EXCEPTION 'WP06_INCIDENT_PROPOSAL_MISSING'; END IF;
            SELECT * INTO assessment FROM public.learning_drift_assessment AS a
             WHERE a.workspace_id = target_workspace AND a.id = proposal.assessment_id;
            IF NOT FOUND OR proposal.assessment_hash IS DISTINCT FROM assessment.content_hash
               OR proposal.membership_digest IS DISTINCT FROM assessment.membership_digest
               OR proposal.policy_version IS DISTINCT FROM assessment.policy_version
               OR assessment.data_quality_status <> 'detected'
               OR NOT (assessment.reason_codes ?| ARRAY[
                    'quality_below_threshold','coverage_below_threshold']) THEN
                RAISE EXCEPTION 'WP06_INCIDENT_ASSESSMENT_INELIGIBLE';
            END IF;
            SELECT count(*)::integer,
                   count(DISTINCT (observation.active_registry_id,
                       observation.active_registry_revision, observation.active_artifact_hash,
                       observation.active_pointer_version))::integer
              INTO membership_count, tuple_count
              FROM public.learning_drift_assessment_member AS member
              JOIN public.learning_shadow_observation AS observation
                ON observation.workspace_id = member.workspace_id
               AND observation.id = member.observation_id
             WHERE member.workspace_id = target_workspace
               AND member.assessment_id = assessment.id;
            IF membership_count <> assessment.member_count OR membership_count = 0
               OR tuple_count <> 1 THEN
                RAISE EXCEPTION 'WP06_INCIDENT_MIXED_INCUMBENT';
            END IF;
            SELECT observation.active_registry_id AS registry_id,
                   observation.active_registry_revision AS registry_revision,
                   observation.active_artifact_hash AS artifact_hash,
                   observation.active_pointer_version AS pointer_version
              INTO incumbent
              FROM public.learning_drift_assessment_member AS member
              JOIN public.learning_shadow_observation AS observation
                ON observation.workspace_id = member.workspace_id
               AND observation.id = member.observation_id
             WHERE member.workspace_id = target_workspace
               AND member.assessment_id = assessment.id
             LIMIT 1;

            -- Từ chối target đã cũ trước khi yêu cầu lineage cho một target không còn active.
            SELECT gateway.registry_id, gateway.registry_revision,
                   gateway.active_pointer_version, gateway.deployment_revision
              INTO resolved
              FROM public.wp06_resolve_scorer_runtime_target(
                  target_workspace, NULL::text, NULL::jsonb, NULL::uuid,
                  NULL::integer, false) AS gateway;
            IF NOT FOUND OR resolved.registry_id IS DISTINCT FROM incumbent.registry_id
               OR resolved.registry_revision IS DISTINCT FROM incumbent.registry_revision
               OR resolved.active_pointer_version IS DISTINCT FROM incumbent.pointer_version THEN
                RAISE EXCEPTION 'WP06_INCIDENT_TARGET_STALE';
            END IF;

            -- Deployment, active pointer và registry đã được khóa trước khi khóa nguồn.
            target_source_ids := public.wp06_learning_incident_target_source_ids(
                target_workspace);
            SELECT pg_catalog.array_agg(DISTINCT providers.source_id
                                        ORDER BY providers.source_id)
              INTO assessment_source_ids
              FROM public.learning_drift_assessment_member AS member
              JOIN public.learning_shadow_observation AS observation
                ON observation.workspace_id = member.workspace_id
               AND observation.id = member.observation_id
              CROSS JOIN LATERAL pg_catalog.unnest(
                  public.wp06_shadow_drift_decision_source_ids(
                      target_workspace, observation.incumbent_decision_id
                  )) AS providers(source_id)
             WHERE member.workspace_id = target_workspace
               AND member.assessment_id = assessment.id;
            IF target_source_ids IS NULL OR assessment_source_ids IS NULL
               OR pg_catalog.cardinality(assessment_source_ids) = 0 THEN
                RAISE EXCEPTION 'WP06_INCIDENT_SOURCE_LINEAGE_MISSING';
            END IF;
            SELECT pg_catalog.array_agg(DISTINCT source_id ORDER BY source_id)
              INTO locked_source_ids
              FROM pg_catalog.unnest(target_source_ids || assessment_source_ids)
                   AS source_items(source_id);
            FOR source_id IN
                SELECT item.source_id
                  FROM pg_catalog.unnest(locked_source_ids) AS item(source_id)
                 ORDER BY item.source_id
            LOOP
                IF source_id IS NULL
                   OR pg_catalog.char_length(source_id) > 128
                   OR source_id !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]*$' THEN
                    RAISE EXCEPTION 'WP06_INCIDENT_SOURCE_SCOPE_INVALID';
                END IF;
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || source_id, 0));
            END LOOP;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'wp06-learning-observation-member-set:' || target_workspace::text, 0));

            SELECT pg_catalog.count(*)::integer,
                   public.wp06_sha256_canonical_json(COALESCE(pg_catalog.json_agg(
                       member.observation_id::text ORDER BY member.observation_id::text),
                       '[]'::json))
              INTO current_member_count, current_member_digest
              FROM public.learning_drift_assessment_member AS member
             WHERE member.workspace_id = target_workspace
               AND member.assessment_id = assessment.id;
            SELECT pg_catalog.count(*)::integer,
                   public.wp06_sha256_canonical_json(COALESCE(pg_catalog.json_agg(
                       observation.id::text ORDER BY observation.id::text), '[]'::json))
              INTO current_cohort_count, current_cohort_digest
              FROM public.learning_shadow_observation AS observation
             WHERE observation.workspace_id = target_workspace
               AND observation.challenger_registry_id = assessment.cohort_registry_id
               AND observation.challenger_revision = assessment.cohort_revision
               AND observation.observed_at >= assessment.window_start
               AND observation.observed_at <= assessment.window_end;
            IF current_member_count <> assessment.member_count
               OR current_member_digest IS DISTINCT FROM assessment.membership_digest
               OR current_cohort_count <> assessment.member_count
               OR current_cohort_digest IS DISTINCT FROM assessment.membership_digest THEN
                RAISE EXCEPTION 'WP06_INCIDENT_MEMBER_SET_STALE';
            END IF;
            SELECT pg_catalog.array_agg(DISTINCT providers.source_id
                                        ORDER BY providers.source_id)
              INTO current_source_ids
              FROM public.learning_drift_assessment_member AS member
              JOIN public.learning_shadow_observation AS observation
                ON observation.workspace_id = member.workspace_id
               AND observation.id = member.observation_id
              CROSS JOIN LATERAL pg_catalog.unnest(
                  public.wp06_shadow_drift_decision_source_ids(
                      target_workspace, observation.incumbent_decision_id
                  )) AS providers(source_id)
             WHERE member.workspace_id = target_workspace
               AND member.assessment_id = assessment.id;
            IF current_source_ids IS DISTINCT FROM assessment_source_ids THEN
                RAISE EXCEPTION 'WP06_INCIDENT_SOURCE_LINEAGE_STALE';
            END IF;
            IF assessment.source_rights_digest IS NULL THEN
                RAISE EXCEPTION 'WP06_INCIDENT_SOURCE_RIGHTS_STALE';
            END IF;
            current_rights_digest := public.wp06_learning_drift_current_rights_digest(
                target_workspace, assessment.id, pg_catalog.clock_timestamp());
            IF current_rights_digest IS NULL
               OR current_rights_digest IS DISTINCT FROM assessment.source_rights_digest THEN
                RAISE EXCEPTION 'WP06_INCIDENT_SOURCE_RIGHTS_STALE';
            END IF;
            target_source_ids_after := public.wp06_learning_incident_target_source_ids(
                target_workspace);
            IF target_source_ids_after IS DISTINCT FROM target_source_ids THEN
                RAISE EXCEPTION 'WP06_INCIDENT_TARGET_SOURCE_SCOPE_STALE';
            END IF;

            -- Đọc lại target sau khi đã khóa toàn bộ nguồn của target và assessment.
            SELECT gateway.registry_id, gateway.registry_revision,
                   gateway.active_pointer_version, gateway.deployment_revision
              INTO resolved
              FROM public.wp06_resolve_scorer_runtime_target(
                  target_workspace, NULL::text, NULL::jsonb, NULL::uuid,
                  NULL::integer, false) AS gateway;
            IF NOT FOUND OR resolved.registry_id IS DISTINCT FROM incumbent.registry_id
               OR resolved.registry_revision IS DISTINCT FROM incumbent.registry_revision
               OR resolved.active_pointer_version IS DISTINCT FROM incumbent.pointer_version THEN
                RAISE EXCEPTION 'WP06_INCIDENT_TARGET_STALE';
            END IF;
            SELECT * INTO registry FROM public.learning_model_registry AS r
             WHERE r.workspace_id = target_workspace AND r.id = incumbent.registry_id;
            IF NOT FOUND OR registry.artifact_hash IS DISTINCT FROM incumbent.artifact_hash THEN
                RAISE EXCEPTION 'WP06_INCIDENT_TARGET_STALE';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.learning_drift_assessment AS newer
                 WHERE newer.workspace_id = target_workspace
                   AND newer.cohort_registry_id = assessment.cohort_registry_id
                   AND newer.cohort_revision = assessment.cohort_revision
                   AND newer.policy_version = assessment.policy_version
                   AND (newer.created_at, newer.id) > (assessment.created_at, assessment.id)
            ) THEN
                RAISE EXCEPTION 'WP06_INCIDENT_LATEST_ASSESSMENT_REQUIRED';
            END IF;
            RETURN QUERY SELECT incumbent.registry_id, incumbent.registry_revision,
                incumbent.artifact_hash::text, incumbent.pointer_version,
                resolved.deployment_revision;
        END $$
    """)


def upgrade() -> None:
    _backup_legacy_functions()
    _add_columns()
    _grant_shadow_drift_rights_metadata()
    _create_exact_scope_gates()
    _create_consumer_guards()
    _create_incident_target_source_scope()
    _fence_bot_decision_source_bar_append()
    _harden_incident_eligibility()


def downgrade() -> None:
    op.execute("LOCK TABLE public.learning_shadow_observation IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE public.learning_drift_assessment IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE public.learning_drift_assessment_member IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.learning_shadow_observation
                        WHERE source_scope_fingerprint IS NOT NULL)
               OR EXISTS (SELECT 1 FROM public.learning_drift_assessment
                           WHERE source_rights_digest IS NOT NULL)
               OR EXISTS (SELECT 1 FROM public.learning_drift_assessment_member
                           WHERE current_rights_eligible IS NOT NULL) THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_SOURCE_SCOPE_DOWNGRADE_POPULATED_DENIED';
            END IF;
        END $$
    """)
    op.execute(f"""
        DO $$
        DECLARE backup_row record; backup_count integer;
        BEGIN
            SELECT pg_catalog.count(*)::integer INTO backup_count
              FROM {RESTORE_TABLE};
            IF backup_count <> 11 THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_SOURCE_SCOPE_FUNCTION_BACKUP_INVALID';
            END IF;
            FOR backup_row IN
                SELECT function_definition FROM {RESTORE_TABLE} ORDER BY signature
            LOOP
                EXECUTE backup_row.function_definition;
            END LOOP;
        END $$
    """)
    op.execute(
        "DROP TRIGGER zz_wp06_shadow_observation_exact_source_scope "
        "ON public.learning_shadow_observation"
    )
    op.execute("DROP FUNCTION public.wp06_require_shadow_exact_source_scope()")
    op.execute("DROP FUNCTION public.wp06_learning_incident_target_source_ids(uuid)")
    op.execute(
        "DROP FUNCTION public.wp06_recheck_learning_shadow_observation_source_rights(uuid,uuid)"
    )
    op.execute(
        "DROP FUNCTION public.wp06_recheck_learning_drift_assessment_source_rights(uuid,uuid)"
    )
    op.execute(
        "DROP FUNCTION public.wp06_learning_drift_current_rights_digest(uuid,uuid,timestamptz)"
    )
    op.execute(
        "DROP FUNCTION public.wp06_authorize_learning_drift_source_scope(uuid,uuid,timestamptz)"
    )
    op.execute(
        "DROP FUNCTION public.wp06_authorize_shadow_inference_source_scope(uuid,uuid,timestamptz)"
    )
    op.execute("DROP FUNCTION public.wp06_shadow_drift_decision_source_ids(uuid,uuid)")
    _revoke_shadow_drift_rights_metadata()
    op.execute("""
        ALTER TABLE public.learning_drift_assessment_member
        ADD CONSTRAINT ck_learning_drift_assessment_member_learning_drift_asse_5fd5
        CHECK (eligible = (baseline_available AND schema_compatible AND rights_eligible))
    """)
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_authorize_paper_performance_decision_source_scope"
        f"(uuid,uuid,timestamptz) FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_paper_performance_decision_source_ids(uuid,uuid) "
        f"FROM {RIGHTS_ROLE}"
    )
    op.drop_column("learning_drift_assessment_member", "current_eligible")
    op.drop_column("learning_drift_assessment_member", "current_source_ids")
    op.drop_column("learning_drift_assessment_member", "current_rights_fingerprint")
    op.drop_column("learning_drift_assessment_member", "current_rights_eligible")
    op.drop_column("learning_drift_assessment", "source_rights_digest")
    op.drop_column("learning_shadow_observation", "source_scope_fingerprint")
    op.execute(f"DROP TABLE {RESTORE_TABLE}")
    op.execute(f"DROP SCHEMA {RESTORE_SCHEMA}")

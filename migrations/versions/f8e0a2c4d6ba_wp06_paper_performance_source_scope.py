"""WP06: kiểm quyền nguồn PAPER chính xác trước aggregate và disclosure."""

from collections.abc import Sequence

from alembic import op

revision: str = "f8e0a2c4d6ba"
down_revision: str | None = "f8e0a2c4d6b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
EVALUATOR = "crypidian_learning_paper_performance_evaluator"
RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
FUNCTION = "public.wp06_aggregate_paper_performance(uuid,uuid,text,uuid,text,timestamptz)"
UNSCOPED_FUNCTION = (
    "public.wp06_aggregate_paper_performance_unscoped(uuid,uuid,text,uuid,text,timestamptz)"
)
SNAPSHOT_FUNCTION = (
    "public.wp06_compute_paper_performance_rights_snapshot(uuid,uuid,text,uuid,text,timestamptz)"
)
REPORT_RIGHTS_FUNCTION = "public.wp06_recheck_paper_performance_report_rights(uuid,uuid)"


def _create_decision_source_ids() -> None:
    """Trả provider ID và xác nhận structural lineage trong role đọc rights hẹp."""
    op.execute(f"""
        CREATE FUNCTION public.wp06_paper_performance_decision_source_ids(
            target_workspace uuid, target_decision uuid
        ) RETURNS text[] LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            source_scope_row record;
            source_ids text[];
            source_count integer;
        BEGIN
            IF session_user <> '{ADMIN}'
               OR target_workspace IS NULL OR target_decision IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_SOURCE_SCOPE_DENIED';
            END IF;
            SELECT scope.decision_snapshot_id, scope.decision_lineage_fingerprint
              INTO source_scope_row
              FROM public.wp06_verify_bot_decision_source_scope(
                    target_workspace, target_decision
              ) AS scope;
            IF NOT FOUND
               OR source_scope_row.decision_snapshot_id !~ '^[0-9a-f]{{64}}$'
               OR source_scope_row.decision_lineage_fingerprint !~ '^[0-9a-f]{{64}}$' THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_SOURCE_LINEAGE_MISSING';
            END IF;
            SELECT count(*)::integer,
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
                WHEN 'CRYPIDIAN_FIRST_PARTY_QA_FIXTURE' THEN 'CRYPIDIAN_FIRST_PARTY_QA'
                ELSE NULL END AS source_id) AS provider
             WHERE source.workspace_id = target_workspace
               AND source.bot_decision_id = target_decision
               AND source.decision_snapshot_id = source_scope_row.decision_snapshot_id
               AND source.source_event_id ~ '^market-bar:[0-9a-f]{{64}}$';
            IF source_count = 0 OR source_ids IS NULL
               OR pg_catalog.array_position(source_ids, NULL) IS NOT NULL THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_SOURCE_PROVIDER_UNKNOWN';
            END IF;
            RETURN source_ids;
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_paper_performance_decision_source_ids(uuid,uuid) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_paper_performance_decision_source_ids(uuid,uuid) "
        f"FROM PUBLIC, {ADMIN}, crypidian_app, crypidian_trading_runtime"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_paper_performance_decision_source_ids(uuid,uuid) "
        f"TO {EVALUATOR}"
    )


def _create_decision_source_authorizer() -> None:
    """Xác minh quyền aggregate trên từng scope source/bar của một decision."""
    op.execute(f"""
        CREATE FUNCTION public.wp06_authorize_paper_performance_decision_source_scope(
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
            IF session_user <> '{ADMIN}'
               OR target_workspace IS NULL OR target_decision IS NULL OR checked_at IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_SOURCE_SCOPE_DENIED';
            END IF;
            SELECT scope.decision_snapshot_id, scope.timeframe_snapshot_ids,
                   scope.source_event_digests, scope.decision_scope_fingerprint,
                   scope.decision_lineage_fingerprint
              INTO source_scope_row
              FROM public.wp06_verify_bot_decision_source_scope(
                    target_workspace, target_decision
              ) AS scope;
            IF NOT FOUND
               OR source_scope_row.decision_scope_fingerprint !~ '^[0-9a-f]{{64}}$'
               OR source_scope_row.decision_lineage_fingerprint !~ '^[0-9a-f]{{64}}$'
               OR source_scope_row.timeframe_snapshot_ids IS NULL
               OR source_scope_row.source_event_digests IS NULL THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_SOURCE_LINEAGE_MISSING';
            END IF;
            source_ids := public.wp06_paper_performance_decision_source_ids(
                target_workspace, target_decision);
            FOREACH source_id IN ARRAY source_ids LOOP
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || source_id, 0));
            END LOOP;
            IF 'CRYPIDIAN_FIRST_PARTY_QA' = ANY(source_ids) THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_SOURCE_QA_DENIED';
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
                    RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_SOURCE_LINEAGE_MISSING';
                END IF;
                SELECT policy.workspace_id, policy.source_id, policy.sequence, policy.policy_id,
                       policy.state, policy.allowed_purposes, policy.provider, policy.product,
                       policy.venue, policy.contract, policy.reviewed_at, policy.valid_until,
                       policy.evidence_ref, policy.created_at, policy.license_scope,
                       policy.license_evidence_id, policy.license_digest,
                       policy.license_scope_hash,
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
                   OR NOT (policy_row.allowed_purposes @> '[\"paper_demo\"]'::jsonb)
                   OR policy_row.reviewed_at IS NULL OR policy_row.reviewed_at > checked_at
                   OR (policy_row.valid_until IS NOT NULL
                       AND policy_row.valid_until <= checked_at)
                   OR policy_row.evidence_ref IS NULL
                   OR policy_row.evidence_ref !~ '^https://[^/?#]+([/?#].*)?$'
                   OR policy_row.license_scope IS NULL
                   OR policy_row.license_evidence_id IS NULL
                   OR policy_row.license_digest IS NULL
                   OR policy_row.license_digest !~ '^[0-9a-f]{{64}}$'
                   OR policy_row.license_scope_hash IS NULL
                   OR policy_row.license_scope_hash !~ '^[0-9a-f]{{64}}$'
                   OR policy_row.policy_fingerprint IS NULL
                   OR policy_row.policy_fingerprint !~ '^[0-9a-f]{{64}}$'
                   OR NOT public.wp06_license_scope_valid(policy_row.license_scope)
                   OR policy_row.license_scope_hash IS DISTINCT FROM pg_catalog.encode(
                        pg_catalog.sha256(pg_catalog.convert_to(
                            public.wp06_license_canonical_json(policy_row.license_scope), 'UTF8')),
                        'hex')
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
                    RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
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
                   OR (policy_row.license_scope->>'data_start')::timestamptz > group_row.data_start
                   OR (policy_row.license_scope->>'data_end')::timestamptz < group_row.data_end
                   OR NOT (policy_row.license_scope->'allowed_purposes'
                                @> '[\"paper_demo\"]'::jsonb)
                   OR NOT (policy_row.license_scope->'allowed_outputs'
                                @> '[\"aggregate_report\"]'::jsonb)
                   OR NOT (policy_row.license_scope->'derived_use'
                                @> '[\"aggregate_reporting\"]'::jsonb)
                   OR policy_row.license_scope->'max_retention_days' <> 'null'::jsonb THEN
                    RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
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
                    RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
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
                    RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
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
                        'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),
                    'data_end', pg_catalog.to_char(
                        group_row.data_end AT TIME ZONE 'UTC',
                        'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),
                    'purpose', 'paper_demo',
                    'outputs', pg_catalog.jsonb_build_array('aggregate_report'),
                    'derived_use', pg_catalog.jsonb_build_array('aggregate_reporting'),
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
                            'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),
                        'data_end', pg_catalog.to_char(
                            group_row.data_end AT TIME ZONE 'UTC',
                            'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),
                        'bar_ids', group_row.bar_ids,
                        'source_event_ids', group_row.source_event_ids,
                        'lineage', lineage_document
                    ));
            END LOOP;
            IF fingerprint_document = '[]'::jsonb THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_SOURCE_LINEAGE_MISSING';
            END IF;
            RETURN pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                public.wp06_license_canonical_json(fingerprint_document), 'UTF8')), 'hex');
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_authorize_paper_performance_decision_source_scope(uuid,uuid,"
        "timestamptz) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_authorize_paper_performance_decision_source_scope("
        "uuid,uuid,timestamptz) FROM PUBLIC, "
        f"{ADMIN}, crypidian_app, crypidian_trading_runtime"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_authorize_paper_performance_decision_source_scope("
        f"uuid,uuid,timestamptz) TO {EVALUATOR}"
    )


def _create_fixed_scope_decision_gate() -> None:
    """Chỉ trả boolean cho phép dùng nguồn của một decision vào báo cáo PAPER."""
    op.execute(f"""
        CREATE FUNCTION public.wp06_can_aggregate_paper_performance_decision_source(
            target_workspace uuid, target_decision uuid
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF session_user <> '{ADMIN}'
               OR target_workspace IS NULL OR target_decision IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RETURN false;
            END IF;
            PERFORM public.wp06_authorize_paper_performance_decision_source_scope(
                target_workspace, target_decision, pg_catalog.clock_timestamp());
            RETURN true;
        EXCEPTION WHEN OTHERS THEN
            RETURN false;
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_can_aggregate_paper_performance_decision_source(uuid,uuid) "
        f"OWNER TO {EVALUATOR}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.wp06_can_aggregate_paper_performance_decision_source(uuid,uuid) "
        f"FROM PUBLIC, crypidian_app, crypidian_trading_runtime, {EVALUATOR}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.wp06_can_aggregate_paper_performance_decision_source(uuid,uuid) "
        f"TO {ADMIN}"
    )


def _create_eligible_decision_selector() -> None:
    """Chọn đúng ID decision có intent PAPER; chưa đọc probability hay trade."""
    op.execute(f"""
        CREATE FUNCTION public.wp06_list_paper_performance_decisions(
            target_workspace uuid, target_bot uuid, expected_target_fingerprint text,
            expected_baseline_report_id uuid, expected_baseline_report_hash text,
            requested_as_of timestamptz
        ) RETURNS TABLE(cohort_name text, bot_decision_id uuid, decision_at timestamptz)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            active_row record;
        BEGIN
            IF session_user <> '{ADMIN}'
               OR target_workspace IS NULL OR target_bot IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR expected_target_fingerprint !~ '^[0-9a-f]{{64}}$'
               OR expected_baseline_report_id IS NULL
               OR expected_baseline_report_hash !~ '^[0-9a-f]{{64}}$'
               OR requested_as_of IS NULL
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_SOURCE_SCOPE_DENIED';
            END IF;
            SELECT active.* INTO active_row
              FROM public.wp06_resolve_scorer_runtime_target(
                    target_workspace, NULL::text, NULL::jsonb, NULL::uuid, NULL::integer, false
              ) AS active;
            IF NOT FOUND OR active_row.mode NOT IN ('SHADOW','PAPER_GATE') THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_ACTIVE_TARGET_REQUIRED';
            END IF;
            RETURN QUERY
            WITH event_targets AS (
                SELECT event.workspace_id, event.bot_id, event.bot_decision_id,
                       event.bot_decision_hash, event.candidate_id,
                       event.evaluation_report_id, event.evaluation_report_hash,
                       decision.created_at AS decision_at,
                       pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                           public.wp06_license_canonical_json(pg_catalog.jsonb_build_object(
                               'workspace_id', event.workspace_id::text,
                               'bot_id', event.bot_id::text,
                               'registry_id', event.registry_id::text,
                               'registry_revision', event.registry_revision,
                               'scorer_artifact_id', event.scorer_artifact_id::text,
                               'scorer_artifact_hash', event.scorer_artifact_hash,
                               'base_artifact_hash', event.base_artifact_hash,
                               'policy_id', event.policy_id,
                               'policy_hash', event.policy_hash,
                               'deployment_event_id', event.deployment_event_id::text,
                               'deployment_revision', event.deployment_revision,
                               'evaluation_report_id', event.evaluation_report_id::text,
                               'evaluation_report_hash', event.evaluation_report_hash,
                               'proof_hash', event.proof_hash,
                               'report_scope_hash', event.report_scope_hash,
                               'feature_schema_hash', event.feature_schema_hash,
                               'source_rights_fingerprint', event.source_rights_fingerprint
                           )), 'UTF8')), 'hex') AS target_fingerprint
                  FROM public.learning_score_event AS event
                  JOIN public.bot_decision AS decision
                    ON decision.workspace_id = event.workspace_id
                   AND decision.bot_id = event.bot_id
                   AND decision.id = event.bot_decision_id
                   AND decision.decision_hash = event.bot_decision_hash
                   AND decision.candidate_id = event.candidate_id
                 WHERE event.workspace_id = target_workspace
                   AND event.bot_id = target_bot
                   AND event.decision = 'APPROVE'
                   AND event.final_status = 'CANDIDATE'
                   AND event.mode = active_row.mode
                   AND event.evaluation_report_id = expected_baseline_report_id
                   AND event.evaluation_report_hash = expected_baseline_report_hash
                   AND decision.status = 'CANDIDATE'
                   AND decision.created_at >= requested_as_of - interval '90 days'
                   AND decision.created_at < requested_as_of - interval '30 days'
            ), eligible AS (
                SELECT CASE WHEN event.decision_at < requested_as_of - interval '60 days'
                            THEN 'older' ELSE 'newer' END AS cohort_name,
                       event.bot_decision_id, event.decision_at
                  FROM event_targets AS event
                  JOIN public.order_intent AS intent
                    ON intent.workspace_id = event.workspace_id
                   AND intent.bot_id = event.bot_id
                   AND intent.bot_decision_id = event.bot_decision_id
                   AND intent.candidate_id = event.candidate_id
                   AND intent.mode = 'PAPER'
                 WHERE event.target_fingerprint = expected_target_fingerprint
                   AND intent.created_at <= requested_as_of - interval '30 days'
            )
            SELECT eligible.cohort_name, eligible.bot_decision_id, eligible.decision_at
              FROM eligible
             ORDER BY eligible.cohort_name, eligible.decision_at, eligible.bot_decision_id;
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_list_paper_performance_decisions(uuid,uuid,text,uuid,text,"
        "timestamptz) "
        f"OWNER TO {EVALUATOR}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_list_paper_performance_decisions(uuid,uuid,text,uuid,"
        f"text,timestamptz) FROM PUBLIC, {ADMIN}, crypidian_app, crypidian_trading_runtime"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_list_paper_performance_decisions(uuid,uuid,text,"
        f"uuid,text,timestamptz) TO {EVALUATOR}"
    )


def _create_rights_snapshot() -> None:
    """Khóa source, xác minh mọi member rồi mới dựng membership digest metadata-only."""
    op.execute(f"""
        CREATE FUNCTION public.wp06_compute_paper_performance_rights_snapshot(
            target_workspace uuid, target_bot uuid, expected_target_fingerprint text,
            expected_baseline_report_id uuid, expected_baseline_report_hash text,
            requested_as_of timestamptz
        ) RETURNS TABLE(
            rights_digest text,
            older_membership_digest text,
            newer_membership_digest text
        ) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            member_row record;
            source_ids text[];
            all_source_ids text[] := ARRAY[]::text[];
            source_id text;
            checked_at timestamptz;
            decision_scope_digest text;
            frozen_member_set jsonb := '[]'::jsonb;
            member_set_after_fence jsonb;
            rights_document jsonb := '[]'::jsonb;
        BEGIN
            IF session_user <> '{ADMIN}'
               OR target_workspace IS NULL OR target_bot IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR expected_target_fingerprint !~ '^[0-9a-f]{{64}}$'
               OR expected_baseline_report_id IS NULL
               OR expected_baseline_report_hash !~ '^[0-9a-f]{{64}}$'
               OR requested_as_of IS NULL
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_SOURCE_SCOPE_DENIED';
            END IF;

            FOR member_row IN
                SELECT member.cohort_name, member.bot_decision_id, member.decision_at
                  FROM public.wp06_list_paper_performance_decisions(
                    target_workspace, target_bot, expected_target_fingerprint,
                    expected_baseline_report_id, expected_baseline_report_hash, requested_as_of
                  ) AS member
                 ORDER BY member.cohort_name, member.decision_at, member.bot_decision_id
            LOOP
                frozen_member_set := frozen_member_set || pg_catalog.jsonb_build_array(
                    pg_catalog.jsonb_build_object(
                        'cohort_name', member_row.cohort_name,
                        'bot_decision_id', member_row.bot_decision_id::text,
                        'decision_at', pg_catalog.to_char(
                            member_row.decision_at AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                    ));
                source_ids := public.wp06_paper_performance_decision_source_ids(
                    target_workspace, member_row.bot_decision_id);
                IF source_ids IS NULL OR pg_catalog.cardinality(source_ids) = 0 THEN
                    RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_SOURCE_LINEAGE_MISSING';
                END IF;
                all_source_ids := all_source_ids || source_ids;
            END LOOP;
            FOR source_id IN
                SELECT DISTINCT item.source_id
                  FROM pg_catalog.unnest(all_source_ids) AS item(source_id)
                 ORDER BY item.source_id
            LOOP
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || source_id, 0));
            END LOOP;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'wp06-learning-observation-member-set:' || target_workspace::text, 0));
            SELECT COALESCE(pg_catalog.jsonb_agg(
                       pg_catalog.jsonb_build_object(
                           'cohort_name', member.cohort_name,
                           'bot_decision_id', member.bot_decision_id::text,
                           'decision_at', pg_catalog.to_char(
                               member.decision_at AT TIME ZONE 'UTC',
                               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                       ) ORDER BY member.cohort_name, member.decision_at,
                                  member.bot_decision_id), '[]'::jsonb)
              INTO member_set_after_fence
              FROM public.wp06_list_paper_performance_decisions(
                    target_workspace, target_bot, expected_target_fingerprint,
                    expected_baseline_report_id, expected_baseline_report_hash, requested_as_of
              ) AS member;
            IF member_set_after_fence IS DISTINCT FROM frozen_member_set THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_MEMBER_SET_CHANGED';
            END IF;
            frozen_member_set := member_set_after_fence;
            checked_at := pg_catalog.clock_timestamp();

            FOR member_row IN
                SELECT member.cohort_name, member.bot_decision_id, member.decision_at
                  FROM pg_catalog.jsonb_to_recordset(frozen_member_set) AS member(
                        cohort_name text, bot_decision_id uuid, decision_at timestamptz)
                 ORDER BY member.cohort_name, member.decision_at, member.bot_decision_id
            LOOP
                decision_scope_digest :=
                    public.wp06_authorize_paper_performance_decision_source_scope(
                        target_workspace, member_row.bot_decision_id, checked_at);
                IF decision_scope_digest IS NULL
                   OR decision_scope_digest !~ '^[0-9a-f]{{64}}$' THEN
                    RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
                END IF;
                rights_document := rights_document || pg_catalog.jsonb_build_array(
                    pg_catalog.jsonb_build_object(
                        'cohort_name', member_row.cohort_name,
                        'decision_id', member_row.bot_decision_id::text,
                        'decision_at', pg_catalog.to_char(
                            member_row.decision_at AT TIME ZONE 'UTC',
                            'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),
                        'decision_source_rights_fingerprint', decision_scope_digest
                    ));
            END LOOP;
            rights_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                public.wp06_license_canonical_json(rights_document), 'UTF8')), 'hex');

            WITH eligible AS (
                SELECT member.cohort_name, member.decision_at,
                       decision.id AS decision_id, event.event_hash,
                       intent.id AS intent_id, intent.account_id,
                       intent.created_at AS intent_at
                  FROM pg_catalog.jsonb_to_recordset(frozen_member_set) AS member(
                        cohort_name text, bot_decision_id uuid, decision_at timestamptz)
                  JOIN public.learning_score_event AS event
                    ON event.workspace_id = target_workspace
                   AND event.bot_id = target_bot
                   AND event.bot_decision_id = member.bot_decision_id
                  JOIN public.bot_decision AS decision
                    ON decision.workspace_id = event.workspace_id
                   AND decision.bot_id = event.bot_id
                   AND decision.id = event.bot_decision_id
                   AND decision.decision_hash = event.bot_decision_hash
                   AND decision.candidate_id = event.candidate_id
                  JOIN public.order_intent AS intent
                    ON intent.workspace_id = event.workspace_id
                   AND intent.bot_id = event.bot_id
                   AND intent.bot_decision_id = event.bot_decision_id
                   AND intent.candidate_id = event.candidate_id
                   AND intent.mode = 'PAPER'
            ), closed AS (
                SELECT eligible.*,
                       pg_catalog.count(trade.id)::bigint AS closed_trade_count,
                       pg_catalog.array_agg(trade.id::text ORDER BY trade.id::text)
                           FILTER (WHERE trade.id IS NOT NULL) AS closed_trade_ids,
                       pg_catalog.min(trade.closed_at) AS closed_at,
                       pg_catalog.min(trade.net_pnl) AS net_pnl
                  FROM eligible
                  LEFT JOIN public.trade AS trade
                    ON trade.workspace_id = target_workspace
                   AND trade.bot_id = target_bot
                   AND trade.account_id = eligible.account_id
                   AND trade.intent_id = eligible.intent_id
                   AND trade.mode = 'PAPER' AND trade.status = 'CLOSED'
                 GROUP BY eligible.cohort_name, eligible.decision_at, eligible.decision_id,
                          eligible.event_hash, eligible.intent_id, eligible.account_id,
                          eligible.intent_at
            ), outcomes AS (
                SELECT closed.*,
                       CASE WHEN closed_trade_count = 1
                              AND closed_at >= intent_at
                              AND closed_at <= intent_at + interval '30 days'
                              AND closed_at <= requested_as_of
                            THEN CASE WHEN net_pnl > 0 THEN 1
                                      WHEN net_pnl < 0 THEN -1 ELSE 0 END
                            ELSE NULL END AS outcome_label
                  FROM closed
            ), membership_rows AS (
                SELECT outcomes.*,
                       decision_id::text || ':' || event_hash || ':' || intent_id::text || ':'
                       || COALESCE(pg_catalog.array_to_string(closed_trade_ids, ','), 'none')
                       || ':' || COALESCE(outcome_label::text, 'uncovered') AS member_digest
                  FROM outcomes
            )
            SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                       COALESCE(pg_catalog.string_agg(member_digest, E'\\n'
                           ORDER BY decision_at, decision_id)
                           FILTER (WHERE cohort_name = 'older'), ''), 'UTF8')), 'hex'),
                   pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                       COALESCE(pg_catalog.string_agg(member_digest, E'\\n'
                           ORDER BY decision_at, decision_id)
                           FILTER (WHERE cohort_name = 'newer'), ''), 'UTF8')), 'hex')
              INTO older_membership_digest, newer_membership_digest
              FROM membership_rows;
            IF older_membership_digest IS NULL OR newer_membership_digest IS NULL THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_MEMBERSHIP_MISSING';
            END IF;
            RETURN NEXT;
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_compute_paper_performance_rights_snapshot(uuid,uuid,text,uuid,"
        "text,timestamptz) "
        f"OWNER TO {EVALUATOR}"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SNAPSHOT_FUNCTION} FROM PUBLIC, {ADMIN}, crypidian_app, "
        "crypidian_trading_runtime"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {SNAPSHOT_FUNCTION} TO {EVALUATOR}")


def _grant_report_metadata_read() -> None:
    """Tách ADMIN metadata khỏi report payload; chỉ evaluator no-login đọc row trong gateway."""
    op.execute(f"""
        CREATE POLICY wp06_paper_performance_evaluator_report_metadata
        ON public.learning_paper_performance_report FOR SELECT
        TO {EVALUATOR}
        USING (workspace_id = NULLIF(
            pg_catalog.current_setting('app.workspace_id', true), '')::uuid)
    """)
    op.execute(f"REVOKE SELECT ON TABLE public.learning_paper_performance_report FROM {ADMIN}")
    op.execute(
        "GRANT SELECT (workspace_id,id,bot_id,idempotency_key,target_fingerprint,policy_hash,"
        "as_of,membership_digest,created_at) ON public.learning_paper_performance_report "
        f"TO {ADMIN}"
    )
    op.execute(f"GRANT SELECT ON TABLE public.learning_paper_performance_report TO {EVALUATOR}")


def _grant_canonical_json_execute() -> None:
    """Cho evaluator băm metadata đã dựng; không cấp quyền đọc evidence bytes."""
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_license_canonical_json(jsonb) TO {EVALUATOR}"
    )


def _create_member_set_writer_fence() -> None:
    """Khóa tập thành viên trước khi ghi thay đổi có thể lọt vào cohort."""
    op.execute("""
        CREATE FUNCTION public.wp06_lock_learning_observation_member_set()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            row_document jsonb;
            old_workspace uuid;
            new_workspace uuid;
            workspace_key text;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                row_document := pg_catalog.to_jsonb(OLD);
                IF TG_TABLE_NAME = 'trade'
                   AND row_document->>'mode' = 'PAPER'
                   AND row_document->>'workspace_id' IS NOT NULL THEN
                    old_workspace := (row_document->>'workspace_id')::uuid;
                ELSIF TG_TABLE_NAME = 'order_intent'
                   AND row_document->>'mode' = 'PAPER'
                   AND row_document->>'bot_decision_id' IS NOT NULL THEN
                    old_workspace := (row_document->>'workspace_id')::uuid;
                ELSIF TG_TABLE_NAME = 'learning_score_event'
                   AND row_document->>'bot_decision_id' IS NOT NULL THEN
                    old_workspace := (row_document->>'workspace_id')::uuid;
                END IF;
            END IF;
            IF TG_OP <> 'DELETE' THEN
                row_document := pg_catalog.to_jsonb(NEW);
                IF TG_TABLE_NAME = 'trade'
                   AND row_document->>'mode' = 'PAPER'
                   AND row_document->>'workspace_id' IS NOT NULL THEN
                    new_workspace := (row_document->>'workspace_id')::uuid;
                ELSIF TG_TABLE_NAME = 'order_intent'
                   AND row_document->>'mode' = 'PAPER'
                   AND row_document->>'bot_decision_id' IS NOT NULL THEN
                    new_workspace := (row_document->>'workspace_id')::uuid;
                ELSIF TG_TABLE_NAME = 'learning_score_event'
                   AND row_document->>'bot_decision_id' IS NOT NULL THEN
                    new_workspace := (row_document->>'workspace_id')::uuid;
                END IF;
            END IF;
            FOR workspace_key IN
                SELECT DISTINCT item.workspace_id::text
                  FROM (VALUES (old_workspace), (new_workspace)) AS item(workspace_id)
                 WHERE item.workspace_id IS NOT NULL
                 ORDER BY item.workspace_id::text
            LOOP
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'wp06-learning-observation-member-set:' || workspace_key, 0));
            END LOOP;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END; $$
    """)
    op.execute(
        f"ALTER FUNCTION public.wp06_lock_learning_observation_member_set() OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_lock_learning_observation_member_set() "
        f"FROM PUBLIC, {ADMIN}, crypidian_app, crypidian_trading_runtime"
    )
    for table in ("learning_score_event", "order_intent", "trade"):
        op.execute(f"""
            CREATE TRIGGER zz_wp06_learning_observation_member_set_fence
            BEFORE INSERT OR UPDATE OR DELETE ON public.{table}
            FOR EACH ROW EXECUTE FUNCTION public.wp06_lock_learning_observation_member_set()
        """)


def _replace_aggregate_gateway() -> None:
    """Đổi gateway thành source-rights gate trước metrics, giữ contract target cũ."""
    op.execute(f"ALTER FUNCTION {FUNCTION} RENAME TO wp06_aggregate_paper_performance_unscoped")
    op.execute(
        f"REVOKE ALL ON FUNCTION {UNSCOPED_FUNCTION} FROM PUBLIC, {ADMIN}, crypidian_app, "
        "crypidian_trading_runtime"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {UNSCOPED_FUNCTION} TO {EVALUATOR}")
    op.execute(f"""
        CREATE FUNCTION public.wp06_aggregate_paper_performance(
            target_workspace uuid,
            target_bot uuid,
            expected_target_fingerprint text,
            expected_baseline_report_id uuid,
            expected_baseline_report_hash text,
            requested_as_of timestamptz
        ) RETURNS TABLE(
            target_fingerprint text,
            baseline_report_id uuid,
            baseline_report_hash text,
            baseline_brier numeric,
            baseline_log_loss numeric,
            cohort_name text,
            window_start timestamptz,
            window_end timestamptz,
            eligible_decision_count bigint,
            covered_outcome_count bigint,
            class_minus_one_count bigint,
            class_zero_count bigint,
            class_plus_one_count bigint,
            observed_brier numeric,
            observed_log_loss numeric,
            membership_digest text,
            rights_digest text
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            rights_row record;
            final_rights_row record;
            aggregate_row record;
            row_count integer := 0;
        BEGIN
            IF session_user <> '{ADMIN}'
               OR target_workspace IS NULL OR target_bot IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR requested_as_of IS DISTINCT FROM pg_catalog.transaction_timestamp()
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
            END IF;
            SELECT snapshot.rights_digest, snapshot.older_membership_digest,
                   snapshot.newer_membership_digest
              INTO rights_row
              FROM public.wp06_compute_paper_performance_rights_snapshot(
                    target_workspace, target_bot, expected_target_fingerprint,
                    expected_baseline_report_id, expected_baseline_report_hash, requested_as_of
              ) AS snapshot;
            IF NOT FOUND OR rights_row.rights_digest !~ '^[0-9a-f]{{64}}$' THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
            END IF;
            FOR aggregate_row IN
                SELECT * FROM public.wp06_aggregate_paper_performance_unscoped(
                    target_workspace, target_bot, expected_target_fingerprint,
                    expected_baseline_report_id, expected_baseline_report_hash, requested_as_of
                ) AS aggregate
            LOOP
                IF (aggregate_row.cohort_name = 'older'
                    AND aggregate_row.membership_digest
                        IS DISTINCT FROM rights_row.older_membership_digest)
                   OR (aggregate_row.cohort_name = 'newer'
                    AND aggregate_row.membership_digest
                        IS DISTINCT FROM rights_row.newer_membership_digest)
                   OR aggregate_row.cohort_name NOT IN ('older','newer') THEN
                    RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
                END IF;
                target_fingerprint := aggregate_row.target_fingerprint;
                baseline_report_id := aggregate_row.baseline_report_id;
                baseline_report_hash := aggregate_row.baseline_report_hash;
                baseline_brier := aggregate_row.baseline_brier;
                baseline_log_loss := aggregate_row.baseline_log_loss;
                cohort_name := aggregate_row.cohort_name;
                window_start := aggregate_row.window_start;
                window_end := aggregate_row.window_end;
                eligible_decision_count := aggregate_row.eligible_decision_count;
                covered_outcome_count := aggregate_row.covered_outcome_count;
                class_minus_one_count := aggregate_row.class_minus_one_count;
                class_zero_count := aggregate_row.class_zero_count;
                class_plus_one_count := aggregate_row.class_plus_one_count;
                observed_brier := aggregate_row.observed_brier;
                observed_log_loss := aggregate_row.observed_log_loss;
                membership_digest := aggregate_row.membership_digest;
                rights_digest := rights_row.rights_digest;
                row_count := row_count + 1;
                RETURN NEXT;
            END LOOP;
            IF row_count <> 2 THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
            END IF;
            SELECT snapshot.rights_digest, snapshot.older_membership_digest,
                   snapshot.newer_membership_digest
              INTO final_rights_row
              FROM public.wp06_compute_paper_performance_rights_snapshot(
                    target_workspace, target_bot, expected_target_fingerprint,
                    expected_baseline_report_id, expected_baseline_report_hash, requested_as_of
              ) AS snapshot;
            IF NOT FOUND
               OR final_rights_row.rights_digest IS DISTINCT FROM rights_row.rights_digest
               OR final_rights_row.older_membership_digest
                    IS DISTINCT FROM rights_row.older_membership_digest
               OR final_rights_row.newer_membership_digest
                    IS DISTINCT FROM rights_row.newer_membership_digest THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
            END IF;
        END; $$
    """)
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {EVALUATOR}")
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC, crypidian_app")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {ADMIN}")


def _create_report_insert_guard() -> None:
    """Tái kiểm digest trong cùng transaction trước khi report được append."""
    op.execute(f"""
        CREATE FUNCTION public.wp06_validate_paper_performance_source_rights()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            snapshot record;
            older_digest text;
            newer_digest text;
            combined_document jsonb;
            combined_digest text;
        BEGIN
            IF session_user <> '{ADMIN}'
               OR NEW.workspace_id IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR NEW.as_of IS DISTINCT FROM pg_catalog.transaction_timestamp()
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed'
               OR pg_catalog.jsonb_typeof(NEW.report) IS DISTINCT FROM 'object'
               OR NEW.report->>'schema_version' IS DISTINCT FROM
                    'crypidian.learning.paper-performance.v1'
               OR NEW.report->>'scope' IS DISTINCT FROM 'PAPER_ONLY_DIAGNOSTIC'
               OR NEW.report->>'target_fingerprint' IS DISTINCT FROM NEW.target_fingerprint
               OR NEW.report->'baseline'->>'report_hash' IS DISTINCT FROM
                    NEW.baseline_report_hash
               OR (NEW.report->>'as_of')::timestamptz IS DISTINCT FROM NEW.as_of
               OR pg_catalog.jsonb_typeof(NEW.report->'cohorts') IS DISTINCT FROM 'array'
               OR pg_catalog.jsonb_array_length(NEW.report->'cohorts') <> 2 THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
            END IF;
            IF (NEW.report->'cohorts'->0->>'window_start')::timestamptz
                    IS DISTINCT FROM NEW.as_of - interval '90 days'
               OR (NEW.report->'cohorts'->0->>'window_end')::timestamptz
                    IS DISTINCT FROM NEW.as_of - interval '60 days'
               OR (NEW.report->'cohorts'->1->>'window_start')::timestamptz
                    IS DISTINCT FROM NEW.as_of - interval '60 days'
               OR (NEW.report->'cohorts'->1->>'window_end')::timestamptz
                    IS DISTINCT FROM NEW.as_of - interval '30 days' THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
            END IF;
            older_digest := NEW.report->'cohorts'->0->>'membership_digest';
            newer_digest := NEW.report->'cohorts'->1->>'membership_digest';
            IF older_digest IS NULL OR newer_digest IS NULL
               OR older_digest !~ '^[0-9a-f]{{64}}$'
               OR newer_digest !~ '^[0-9a-f]{{64}}$' THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
            END IF;
            SELECT current_snapshot.rights_digest,
                   current_snapshot.older_membership_digest,
                   current_snapshot.newer_membership_digest
              INTO snapshot
              FROM public.wp06_compute_paper_performance_rights_snapshot(
                    NEW.workspace_id, NEW.bot_id, NEW.target_fingerprint,
                    NEW.baseline_report_id, NEW.baseline_report_hash, NEW.as_of
              ) AS current_snapshot;
            IF NOT FOUND
               OR snapshot.rights_digest IS NULL
               OR snapshot.rights_digest !~ '^[0-9a-f]{{64}}$'
               OR snapshot.older_membership_digest IS DISTINCT FROM older_digest
               OR snapshot.newer_membership_digest IS DISTINCT FROM newer_digest THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
            END IF;
            combined_document := pg_catalog.jsonb_build_object(
                'cohort_membership_digests', pg_catalog.jsonb_build_array(
                    older_digest, newer_digest),
                'rights_digest', snapshot.rights_digest,
                'schema_version', 'crypidian.wp06.paper-performance-membership.v2'
            );
            combined_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                public.wp06_license_canonical_json(combined_document), 'UTF8')), 'hex');
            IF combined_digest IS DISTINCT FROM NEW.membership_digest THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
            END IF;
            RETURN NEW;
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_validate_paper_performance_source_rights() "
        f"OWNER TO {EVALUATOR}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_validate_paper_performance_source_rights() "
        "FROM PUBLIC, crypidian_app, crypidian_trading_runtime"
    )
    op.execute("""
        CREATE TRIGGER learning_paper_performance_source_rights_validate
        BEFORE INSERT ON public.learning_paper_performance_report
        FOR EACH ROW EXECUTE FUNCTION public.wp06_validate_paper_performance_source_rights()
    """)


def _create_report_rights_recheck() -> None:
    """Trả bool metadata-only; không đọc report JSON/metric trước khi tái kiểm quyền."""
    op.execute(f"""
        CREATE FUNCTION public.wp06_recheck_paper_performance_report_rights(
            target_workspace uuid, target_report uuid
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            report_row record;
            current_snapshot record;
            combined_document jsonb;
            combined_digest text;
        BEGIN
            IF session_user <> '{ADMIN}'
               OR target_workspace IS NULL OR target_report IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RETURN false;
            END IF;
            SELECT report.workspace_id, report.id, report.bot_id,
                   report.baseline_report_id, report.baseline_report_hash,
                   report.target_fingerprint, report.as_of, report.membership_digest
              INTO report_row
              FROM public.learning_paper_performance_report AS report
             WHERE report.workspace_id = target_workspace AND report.id = target_report;
            IF NOT FOUND OR report_row.membership_digest IS NULL
               OR report_row.membership_digest !~ '^[0-9a-f]{{64}}$' THEN
                RETURN false;
            END IF;
            BEGIN
                SELECT snapshot.rights_digest, snapshot.older_membership_digest,
                       snapshot.newer_membership_digest
                  INTO current_snapshot
                  FROM public.wp06_compute_paper_performance_rights_snapshot(
                        report_row.workspace_id, report_row.bot_id,
                        report_row.target_fingerprint, report_row.baseline_report_id,
                        report_row.baseline_report_hash, report_row.as_of
                  ) AS snapshot;
            EXCEPTION WHEN OTHERS THEN
                RETURN false;
            END;
            IF NOT FOUND
               OR current_snapshot.rights_digest IS NULL
               OR current_snapshot.older_membership_digest IS NULL
               OR current_snapshot.newer_membership_digest IS NULL
               OR current_snapshot.rights_digest !~ '^[0-9a-f]{{64}}$'
               OR current_snapshot.older_membership_digest !~ '^[0-9a-f]{{64}}$'
               OR current_snapshot.newer_membership_digest !~ '^[0-9a-f]{{64}}$' THEN
                RETURN false;
            END IF;
            combined_document := pg_catalog.jsonb_build_object(
                'cohort_membership_digests', pg_catalog.jsonb_build_array(
                    current_snapshot.older_membership_digest,
                    current_snapshot.newer_membership_digest),
                'rights_digest', current_snapshot.rights_digest,
                'schema_version', 'crypidian.wp06.paper-performance-membership.v2'
            );
            combined_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                public.wp06_license_canonical_json(combined_document), 'UTF8')), 'hex');
            IF combined_digest IS DISTINCT FROM report_row.membership_digest THEN
                RETURN false;
            END IF;
            RETURN true;
        EXCEPTION WHEN OTHERS THEN
            RETURN false;
        END; $$
    """)
    op.execute(f"ALTER FUNCTION {REPORT_RIGHTS_FUNCTION} OWNER TO {EVALUATOR}")
    op.execute(
        f"REVOKE ALL ON FUNCTION {REPORT_RIGHTS_FUNCTION} FROM PUBLIC, crypidian_app, "
        "crypidian_trading_runtime"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {REPORT_RIGHTS_FUNCTION} TO {ADMIN}")


def _create_report_read_gateway() -> None:
    """Chỉ chiếu payload sau khi current-rights check giữ source/cohort locks."""
    op.execute(f"""
        CREATE FUNCTION public.wp06_read_paper_performance_report(
            target_workspace uuid, target_report uuid
        ) RETURNS SETOF public.learning_paper_performance_report
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF session_user <> '{ADMIN}'
               OR target_workspace IS NULL OR target_report IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed'
               OR public.wp06_recheck_paper_performance_report_rights(
                    target_workspace, target_report
               ) IS DISTINCT FROM true THEN
                RETURN;
            END IF;
            RETURN QUERY
            SELECT stored.*
              FROM public.learning_paper_performance_report AS stored
             WHERE stored.workspace_id = target_workspace
               AND stored.id = target_report;
        END; $$
    """)
    op.execute(
        f"ALTER FUNCTION public.wp06_read_paper_performance_report(uuid,uuid) OWNER TO {EVALUATOR}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_read_paper_performance_report(uuid,uuid) "
        f"FROM PUBLIC, crypidian_app, crypidian_trading_runtime, {EVALUATOR}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_read_paper_performance_report(uuid,uuid) TO {ADMIN}"
    )


def upgrade() -> None:
    """Thay quyền source-wide bằng recheck chính xác theo lineage decision/bar."""
    _create_decision_source_ids()
    _create_decision_source_authorizer()
    _create_fixed_scope_decision_gate()
    _create_eligible_decision_selector()
    _create_rights_snapshot()
    _create_member_set_writer_fence()
    _grant_report_metadata_read()
    _grant_canonical_json_execute()
    _replace_aggregate_gateway()
    _create_report_insert_guard()
    _create_report_rights_recheck()
    _create_report_read_gateway()


def downgrade() -> None:
    """Chặn downgrade khi report tồn tại vì recheck rights không còn khả dụng."""
    op.execute("LOCK TABLE public.learning_paper_performance_report IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.learning_paper_performance_report) THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_RIGHTS_DOWNGRADE_POPULATED_DENIED';
            END IF;
        END $$
    """)
    op.execute(
        "REVOKE SELECT (workspace_id,id,bot_id,baseline_report_id,baseline_report_hash,"
        "target_fingerprint,as_of,membership_digest) "
        "ON public.learning_paper_performance_report FROM " + EVALUATOR
    )
    op.execute("REVOKE SELECT ON TABLE public.learning_paper_performance_report FROM " + EVALUATOR)
    op.execute(
        "REVOKE SELECT (workspace_id,id,bot_id,idempotency_key,target_fingerprint,policy_hash,"
        "as_of,membership_digest,created_at) ON public.learning_paper_performance_report "
        f"FROM {ADMIN}"
    )
    op.execute(f"GRANT SELECT ON TABLE public.learning_paper_performance_report TO {ADMIN}")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION public.wp06_license_canonical_json(jsonb) FROM {EVALUATOR}"
    )
    op.execute(
        "DROP POLICY wp06_paper_performance_evaluator_report_metadata "
        "ON public.learning_paper_performance_report"
    )
    op.execute("DROP TRIGGER zz_wp06_learning_observation_member_set_fence ON public.trade")
    op.execute("DROP TRIGGER zz_wp06_learning_observation_member_set_fence ON public.order_intent")
    op.execute(
        "DROP TRIGGER zz_wp06_learning_observation_member_set_fence ON public.learning_score_event"
    )
    op.execute("DROP FUNCTION public.wp06_lock_learning_observation_member_set()")
    op.execute(
        "DROP TRIGGER learning_paper_performance_source_rights_validate "
        "ON public.learning_paper_performance_report"
    )
    op.execute("DROP FUNCTION public.wp06_validate_paper_performance_source_rights()")
    op.execute("DROP FUNCTION public.wp06_read_paper_performance_report(uuid,uuid)")
    op.execute(f"DROP FUNCTION {REPORT_RIGHTS_FUNCTION}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {UNSCOPED_FUNCTION} FROM {EVALUATOR}")
    op.execute(f"DROP FUNCTION {FUNCTION}")
    op.execute(f"ALTER FUNCTION {UNSCOPED_FUNCTION} RENAME TO wp06_aggregate_paper_performance")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {ADMIN}")
    op.execute(f"DROP FUNCTION {SNAPSHOT_FUNCTION}")
    op.execute(
        "DROP FUNCTION public.wp06_list_paper_performance_decisions(uuid,uuid,text,uuid,text,"
        "timestamptz)"
    )
    op.execute(
        "DROP FUNCTION public.wp06_can_aggregate_paper_performance_decision_source(uuid,uuid)"
    )
    op.execute(
        "DROP FUNCTION public.wp06_authorize_paper_performance_decision_source_scope("
        "uuid,uuid,timestamptz)"
    )
    op.execute("DROP FUNCTION public.wp06_paper_performance_decision_source_ids(uuid,uuid)")

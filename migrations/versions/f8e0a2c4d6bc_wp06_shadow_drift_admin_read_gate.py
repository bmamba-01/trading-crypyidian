"""WP06: cấp feed Shadow/Drift và context HOLD qua gateway quyền hiện hành."""

from collections.abc import Sequence

from alembic import op

revision: str = "f8e0a2c4d6bc"
down_revision: str | None = "f8e0a2c4d6bb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
TABLES = (
    "learning_shadow_observation",
    "learning_drift_assessment",
    "learning_drift_assessment_member",
    "learning_retrain_proposal",
)
PROJECTION_COLUMNS = {
    "learning_shadow_observation": (
        "workspace_id,id,incumbent_decision_id,challenger_registry_id,"
        "challenger_revision,observed_at,challenger_reason,incumbent_status,"
        "snapshot_coverage,snapshot_quality_ok,schema_compatible,rights_eligible,"
        "active_pointer_version,source_scope_fingerprint"
    ),
    "learning_drift_assessment": (
        "workspace_id,id,created_at,cohort_registry_id,cohort_revision,member_count,"
        "policy_version,observation_window_days,window_start,window_end,minimum_observations,"
        "eligible_count,snapshot_coverage_rate,quality_rate,abstention_rate,"
        "data_quality_status,performance_status,reason_codes,source_rights_digest,content_hash"
    ),
    "learning_drift_assessment_member": (
        "workspace_id,assessment_id,observation_id,ordinal,current_rights_eligible,"
        "current_rights_fingerprint,current_source_ids,current_eligible,"
        "baseline_available,schema_compatible"
    ),
    "learning_retrain_proposal": (
        "workspace_id,id,assessment_id,created_at,policy_version,reason_codes,"
        "membership_digest,assessment_hash,idempotency_key,content_hash"
    ),
}


def _grant_projection_access() -> None:
    for table, selected_columns in PROJECTION_COLUMNS.items():
        op.execute(f"""
            CREATE POLICY wp06_shadow_drift_admin_gateway_read
            ON public.{table} FOR SELECT TO {RIGHTS_ROLE}
            USING (workspace_id = NULLIF(
                pg_catalog.current_setting('app.workspace_id', true), '')::uuid)
        """)
        op.execute(f"GRANT SELECT ({selected_columns}) ON public.{table} TO {RIGHTS_ROLE}")
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {ADMIN}")

    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.wp06_recheck_learning_shadow_observation_source_rights(uuid,uuid) "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.wp06_recheck_learning_drift_assessment_source_rights(uuid,uuid) "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_shadow_drift_decision_source_ids(uuid,uuid) "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_learning_incident_eligible_target(uuid,uuid) "
        f"TO {RIGHTS_ROLE}"
    )


def _create_admin_feed_gateway() -> None:
    op.execute(f"""
        CREATE FUNCTION public.wp06_learning_observation_admin_feed_source_ids(
            target_workspace uuid, page_identity jsonb
        ) RETURNS text[] LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE decision_row record; source_ids text[] := ARRAY[]::text[];
                ordered_source_ids text[];
        BEGIN
            IF session_user <> '{ADMIN}' OR target_workspace IS NULL OR page_identity IS NULL
               OR target_workspace IS DISTINCT FROM NULLIF(
                    pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_ADMIN_FEED_SCOPE_DENIED';
            END IF;
            FOR decision_row IN
                SELECT DISTINCT scope.incumbent_decision_id
                  FROM (
                    SELECT observation.incumbent_decision_id
                      FROM pg_catalog.jsonb_to_recordset(page_identity) AS page(
                            kind text, record_id uuid, assessment_id uuid)
                      JOIN public.learning_shadow_observation AS observation
                        ON page.kind = 'observation'
                       AND observation.workspace_id = target_workspace
                       AND observation.id = page.record_id
                    UNION
                    SELECT observation.incumbent_decision_id
                      FROM pg_catalog.jsonb_to_recordset(page_identity) AS page(
                            kind text, record_id uuid, assessment_id uuid)
                      JOIN public.learning_drift_assessment_member AS member
                        ON page.kind IN ('assessment','proposal')
                       AND member.workspace_id = target_workspace
                       AND member.assessment_id = page.assessment_id
                      JOIN public.learning_shadow_observation AS observation
                        ON observation.workspace_id = member.workspace_id
                       AND observation.id = member.observation_id
                  ) AS scope
                 WHERE scope.incumbent_decision_id IS NOT NULL
                 ORDER BY scope.incumbent_decision_id
            LOOP
                source_ids := source_ids || COALESCE(
                    public.wp06_shadow_drift_decision_source_ids(
                        target_workspace, decision_row.incumbent_decision_id),
                    ARRAY[]::text[]);
            END LOOP;
            SELECT COALESCE(pg_catalog.array_agg(DISTINCT item.source_id
                       ORDER BY item.source_id), ARRAY[]::text[])
              INTO ordered_source_ids
              FROM pg_catalog.unnest(source_ids) AS item(source_id);
            RETURN ordered_source_ids;
        END $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_learning_observation_admin_feed_source_ids(uuid,jsonb) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_learning_observation_admin_feed_source_ids"
        "(uuid,jsonb) FROM PUBLIC, crypidian_app, crypidian_trading_runtime, "
        f"{ADMIN}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_learning_observation_admin_feed_source_ids"
        f"(uuid,jsonb) TO {RIGHTS_ROLE}"
    )

    op.execute(f"""
        CREATE FUNCTION public.wp06_read_learning_observations_admin_feed(
            target_workspace uuid, target_limit integer
        ) RETURNS TABLE(kind text, record_id uuid, occurred_at timestamptz, metadata jsonb)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE identity_before jsonb; identity_after jsonb;
                source_ids_before text[]; source_ids_after text[]; source_id text;
        BEGIN
            IF session_user <> '{ADMIN}' OR target_workspace IS NULL
               OR target_workspace IS DISTINCT FROM NULLIF(
                    pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed'
               OR target_limit IS NULL OR target_limit < 1 OR target_limit > 50 THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_ADMIN_FEED_SCOPE_DENIED';
            END IF;

            SELECT COALESCE(pg_catalog.jsonb_agg(
                       pg_catalog.jsonb_build_object(
                           'kind', page.kind,
                           'record_id', page.record_id,
                           'assessment_id', page.assessment_id,
                           'occurred_at', pg_catalog.to_char(
                               page.occurred_at AT TIME ZONE 'UTC',
                               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                       ) ORDER BY page.occurred_at DESC, page.record_id DESC, page.kind ASC),
                       '[]'::jsonb)
              INTO identity_before
              FROM (
                SELECT feed.kind, feed.record_id, feed.assessment_id, feed.occurred_at
                  FROM (
                    SELECT 'observation'::text AS kind, observation.id AS record_id,
                           NULL::uuid AS assessment_id, observation.observed_at AS occurred_at
                      FROM public.learning_shadow_observation AS observation
                     WHERE observation.workspace_id = target_workspace
                    UNION ALL
                    SELECT 'assessment'::text, assessment.id, assessment.id,
                           assessment.created_at
                      FROM public.learning_drift_assessment AS assessment
                     WHERE assessment.workspace_id = target_workspace
                    UNION ALL
                    SELECT 'proposal'::text, proposal.id, proposal.assessment_id,
                           proposal.created_at
                      FROM public.learning_retrain_proposal AS proposal
                     WHERE proposal.workspace_id = target_workspace
                  ) AS feed
                 ORDER BY feed.occurred_at DESC, feed.record_id DESC, feed.kind ASC
                 LIMIT target_limit
              ) AS page;

            source_ids_before := public.wp06_learning_observation_admin_feed_source_ids(
                target_workspace, identity_before);
            FOR source_id IN
                SELECT item.source_id FROM pg_catalog.unnest(source_ids_before) AS item(source_id)
                 ORDER BY item.source_id
            LOOP
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || source_id, 0));
            END LOOP;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'wp06-learning-observation-member-set:' || target_workspace::text, 0));

            SELECT COALESCE(pg_catalog.jsonb_agg(
                       pg_catalog.jsonb_build_object(
                           'kind', page.kind,
                           'record_id', page.record_id,
                           'assessment_id', page.assessment_id,
                           'occurred_at', pg_catalog.to_char(
                               page.occurred_at AT TIME ZONE 'UTC',
                               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                       ) ORDER BY page.occurred_at DESC, page.record_id DESC, page.kind ASC),
                       '[]'::jsonb)
              INTO identity_after
              FROM (
                SELECT feed.kind, feed.record_id, feed.assessment_id, feed.occurred_at
                  FROM (
                    SELECT 'observation'::text AS kind, observation.id AS record_id,
                           NULL::uuid AS assessment_id, observation.observed_at AS occurred_at
                      FROM public.learning_shadow_observation AS observation
                     WHERE observation.workspace_id = target_workspace
                    UNION ALL
                    SELECT 'assessment'::text, assessment.id, assessment.id,
                           assessment.created_at
                      FROM public.learning_drift_assessment AS assessment
                     WHERE assessment.workspace_id = target_workspace
                    UNION ALL
                    SELECT 'proposal'::text, proposal.id, proposal.assessment_id,
                           proposal.created_at
                      FROM public.learning_retrain_proposal AS proposal
                     WHERE proposal.workspace_id = target_workspace
                  ) AS feed
                 ORDER BY feed.occurred_at DESC, feed.record_id DESC, feed.kind ASC
                 LIMIT target_limit
              ) AS page;
            IF identity_after IS DISTINCT FROM identity_before THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_ADMIN_FEED_MEMBER_SET_CHANGED';
            END IF;
            source_ids_after := public.wp06_learning_observation_admin_feed_source_ids(
                target_workspace, identity_after);
            IF source_ids_after IS DISTINCT FROM source_ids_before THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_ADMIN_FEED_SOURCE_SET_CHANGED';
            END IF;

            RETURN QUERY
            WITH feed AS MATERIALIZED (
                SELECT 'observation'::text AS kind, observation.id AS record_id,
                       NULL::uuid AS assessment_id, observation.observed_at AS occurred_at,
                       pg_catalog.jsonb_build_object(
                           'challenger_registry_id', observation.challenger_registry_id,
                           'challenger_revision', observation.challenger_revision,
                           'challenger_reason', observation.challenger_reason,
                           'incumbent_status', observation.incumbent_status,
                           'snapshot_coverage', observation.snapshot_coverage,
                           'snapshot_quality_ok', observation.snapshot_quality_ok,
                           'schema_compatible', observation.schema_compatible,
                           'rights_eligible', observation.rights_eligible,
                           'active_pointer_version', observation.active_pointer_version
                       ) AS metadata
                  FROM public.learning_shadow_observation AS observation
                 WHERE observation.workspace_id = target_workspace
                UNION ALL
                SELECT 'assessment'::text, assessment.id, assessment.id,
                       assessment.created_at,
                       pg_catalog.jsonb_build_object(
                           'cohort_registry_id', assessment.cohort_registry_id,
                           'cohort_revision', assessment.cohort_revision,
                           'member_count', assessment.member_count,
                           'policy_version', assessment.policy_version,
                           'observation_window_days', assessment.observation_window_days,
                           'window_start', assessment.window_start,
                           'window_end', assessment.window_end,
                           'minimum_observations', assessment.minimum_observations,
                           'eligible_count', assessment.eligible_count,
                           'snapshot_coverage_rate', assessment.snapshot_coverage_rate,
                           'quality_rate', assessment.quality_rate,
                           'abstention_rate', assessment.abstention_rate,
                           'data_quality_status', assessment.data_quality_status,
                           'performance_status', assessment.performance_status,
                           'reason_codes', assessment.reason_codes
                       )
                  FROM public.learning_drift_assessment AS assessment
                 WHERE assessment.workspace_id = target_workspace
                UNION ALL
                SELECT 'proposal'::text, proposal.id, proposal.assessment_id,
                       proposal.created_at,
                       pg_catalog.jsonb_build_object(
                           'assessment_id', proposal.assessment_id,
                           'policy_version', proposal.policy_version,
                           'reason_codes', proposal.reason_codes,
                           'membership_digest', proposal.membership_digest
                       )
                  FROM public.learning_retrain_proposal AS proposal
                  JOIN public.learning_drift_assessment AS assessment
                    ON assessment.workspace_id = proposal.workspace_id
                   AND assessment.id = proposal.assessment_id
                 WHERE proposal.workspace_id = target_workspace
            ), page AS MATERIALIZED (
                SELECT feed.* FROM feed
                 ORDER BY feed.occurred_at DESC, feed.record_id DESC, feed.kind ASC
                 LIMIT target_limit
            )
            SELECT page.kind, page.record_id, page.occurred_at, page.metadata
              FROM page
             WHERE (page.kind = 'observation'
                    AND public.wp06_recheck_learning_shadow_observation_source_rights(
                        target_workspace, page.record_id))
                OR (page.kind IN ('assessment','proposal')
                    AND public.wp06_recheck_learning_drift_assessment_source_rights(
                        target_workspace, page.assessment_id))
             ORDER BY page.occurred_at DESC, page.record_id DESC, page.kind ASC;
        END $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_read_learning_observations_admin_feed(uuid,integer) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_read_learning_observations_admin_feed(uuid,integer) "
        "FROM PUBLIC, crypidian_app, crypidian_trading_runtime, crypidian_learning_shadow_observer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_read_learning_observations_admin_feed(uuid,integer) "
        f"TO {ADMIN}"
    )


def _create_incident_context_gateway() -> None:
    op.execute(f"""
        CREATE FUNCTION public.wp06_read_learning_incident_context(
            target_workspace uuid, target_proposal uuid
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE eligible_row record; context_row jsonb;
        BEGIN
            IF session_user <> '{ADMIN}' OR target_workspace IS NULL OR target_proposal IS NULL
               OR target_workspace IS DISTINCT FROM NULLIF(
                    pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_INCIDENT_CONTEXT_SCOPE_DENIED';
            END IF;
            SELECT gateway.incumbent_registry_id, gateway.incumbent_registry_revision,
                   gateway.incumbent_artifact_hash, gateway.active_pointer_version,
                   gateway.deployment_revision
              INTO eligible_row
              FROM public.wp06_learning_incident_eligible_target(
                    target_workspace, target_proposal) AS gateway;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_INCIDENT_CONTEXT_NOT_ELIGIBLE';
            END IF;
            SELECT pg_catalog.jsonb_build_object(
                'eligible', pg_catalog.jsonb_build_object(
                    'incumbent_registry_id', eligible_row.incumbent_registry_id,
                    'incumbent_registry_revision', eligible_row.incumbent_registry_revision,
                    'incumbent_artifact_hash', eligible_row.incumbent_artifact_hash,
                    'active_pointer_version', eligible_row.active_pointer_version,
                    'deployment_revision', eligible_row.deployment_revision
                ),
                'proposal', pg_catalog.jsonb_build_object(
                    'id', proposal.id,
                    'workspace_id', proposal.workspace_id,
                    'assessment_id', proposal.assessment_id,
                    'assessment_hash', proposal.assessment_hash,
                    'policy_version', proposal.policy_version,
                    'membership_digest', proposal.membership_digest,
                    'idempotency_key', proposal.idempotency_key,
                    'reason_codes', proposal.reason_codes,
                    'content_hash', proposal.content_hash,
                    'created_at', proposal.created_at
                ),
                'assessment', pg_catalog.jsonb_build_object(
                    'id', assessment.id,
                    'workspace_id', assessment.workspace_id,
                    'policy_version', assessment.policy_version,
                    'cohort_registry_id', assessment.cohort_registry_id,
                    'cohort_revision', assessment.cohort_revision,
                    'window_start', assessment.window_start,
                    'window_end', assessment.window_end,
                    'membership_digest', assessment.membership_digest,
                    'member_count', assessment.member_count,
                    'eligible_count', assessment.eligible_count,
                    'snapshot_coverage_rate', assessment.snapshot_coverage_rate,
                    'quality_rate', assessment.quality_rate,
                    'abstention_rate', assessment.abstention_rate,
                    'data_quality_status', assessment.data_quality_status,
                    'performance_status', assessment.performance_status,
                    'reason_codes', assessment.reason_codes,
                    'created_at', assessment.created_at,
                    'source_rights_digest', assessment.source_rights_digest,
                    'content_hash', assessment.content_hash
                ),
                'member_ids', COALESCE((
                    SELECT pg_catalog.jsonb_agg(member.observation_id::text
                                               ORDER BY member.ordinal)
                      FROM public.learning_drift_assessment_member AS member
                     WHERE member.workspace_id = target_workspace
                       AND member.assessment_id = assessment.id
                ), '[]'::jsonb)
            ) INTO context_row
              FROM public.learning_retrain_proposal AS proposal
              JOIN public.learning_drift_assessment AS assessment
                ON assessment.workspace_id = proposal.workspace_id
               AND assessment.id = proposal.assessment_id
             WHERE proposal.workspace_id = target_workspace
               AND proposal.id = target_proposal;
            IF context_row IS NULL THEN
                RAISE EXCEPTION 'WP06_INCIDENT_CONTEXT_LINEAGE_MISSING';
            END IF;
            RETURN context_row;
        END $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_read_learning_incident_context(uuid,uuid) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_read_learning_incident_context(uuid,uuid) "
        "FROM PUBLIC, crypidian_app, crypidian_trading_runtime, crypidian_learning_shadow_observer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_read_learning_incident_context(uuid,uuid) "
        f"TO {ADMIN}"
    )


def upgrade() -> None:
    _grant_projection_access()
    _create_admin_feed_gateway()
    _create_incident_context_gateway()


def downgrade() -> None:
    op.execute(
        "LOCK TABLE public.learning_shadow_observation, public.learning_drift_assessment, "
        "public.learning_drift_assessment_member, public.learning_retrain_proposal "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("DROP FUNCTION public.wp06_read_learning_incident_context(uuid,uuid)")
    op.execute("DROP FUNCTION public.wp06_read_learning_observations_admin_feed(uuid,integer)")
    op.execute("DROP FUNCTION public.wp06_learning_observation_admin_feed_source_ids(uuid,jsonb)")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION "
        "public.wp06_recheck_learning_shadow_observation_source_rights(uuid,uuid) "
        f"FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION "
        "public.wp06_recheck_learning_drift_assessment_source_rights(uuid,uuid) "
        f"FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_shadow_drift_decision_source_ids(uuid,uuid) "
        f"FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_learning_incident_eligible_target(uuid,uuid) "
        f"FROM {RIGHTS_ROLE}"
    )
    for table in reversed(TABLES):
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {ADMIN}")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {ADMIN}")
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {RIGHTS_ROLE}")
        op.execute(f"DROP POLICY wp06_shadow_drift_admin_gateway_read ON public.{table}")

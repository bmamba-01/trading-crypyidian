"""WP06F: aggregate-only action latency scorer từ ledger PAPER hiện có."""

from collections.abc import Sequence

from alembic import op

revision: str = "f0a2c4e6b8d2"
down_revision: str | None = "f0a2c4e6b8d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
EVALUATOR = "crypidian_learning_paper_performance_evaluator"
HISTORY = "crypidian_scorer_history"
FUNCTION = (
    "public.wp06_aggregate_scorer_paper_action_latency(uuid,uuid,uuid,text,text,text,text,text)"
)


def _grant_evidence_metadata() -> None:
    """Cấp timestamp evidence cho evaluator, không cấp canonical payload."""
    op.execute(f"""
        CREATE POLICY wp06_scorer_action_latency_evaluator_read
        ON public.shared_signal_evidence FOR SELECT TO {EVALUATOR}
        USING (workspace_id = NULLIF(
            pg_catalog.current_setting('app.workspace_id', true), '')::uuid)
    """)
    op.execute(f"REVOKE ALL ON TABLE public.shared_signal_evidence FROM {EVALUATOR}")
    op.execute(
        "GRANT SELECT (workspace_id,id,created_at,available_at) "
        f"ON TABLE public.shared_signal_evidence TO {EVALUATOR}"
    )


def _create_aggregate_gateway() -> None:
    """Tái kiểm quyền toàn cohort trước khi trả ba dòng aggregate metadata."""
    op.execute(f"""
        CREATE FUNCTION public.wp06_aggregate_scorer_paper_action_latency(
            target_workspace uuid,
            target_bot uuid,
            target_report_id uuid,
            target_artifact_hash text,
            target_report_hash text,
            target_base_artifact_hash text,
            target_policy_hash text,
            window_code text
        ) RETURNS TABLE(
            stage_name text,
            window_start timestamptz,
            as_of timestamptz,
            candidate_decision_count bigint,
            candidate_no_intent_count bigint,
            future_intent_count bigint,
            valid_sample_count bigint,
            invalid_timestamp_count bigint,
            p50_ms bigint,
            p95_ms bigint
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            as_of_at timestamptz := pg_catalog.transaction_timestamp();
            window_start_at timestamptz;
            member_ids uuid[] := ARRAY[]::uuid[];
            member_ids_after_fence uuid[] := ARRAY[]::uuid[];
            source_ids text[];
            all_source_ids text[] := ARRAY[]::text[];
            source_id text;
            member_id uuid;
            checked_at timestamptz;
            decision_rights_digest text;
            candidate_count bigint;
            no_intent_count bigint;
            future_count bigint;
        BEGIN
            IF session_user <> '{ADMIN}' THEN
                RAISE EXCEPTION 'WP06_SCORER_ACTION_LATENCY_CALLER_DENIED';
            END IF;
            IF target_workspace IS NULL OR target_bot IS NULL OR target_report_id IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SCORER_ACTION_LATENCY_WORKSPACE_DENIED';
            END IF;
            IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SCORER_ACTION_LATENCY_ISOLATION_REQUIRED';
            END IF;
            IF target_artifact_hash !~ '^[0-9a-f]{{64}}$'
               OR target_report_hash !~ '^[0-9a-f]{{64}}$'
               OR target_base_artifact_hash !~ '^[0-9a-f]{{64}}$'
               OR target_policy_hash !~ '^[0-9a-f]{{64}}$' THEN
                RAISE EXCEPTION 'WP06_SCORER_ACTION_LATENCY_HASH_INVALID';
            END IF;
            CASE window_code
                WHEN '24h' THEN window_start_at := as_of_at - interval '24 hours';
                WHEN '7d' THEN window_start_at := as_of_at - interval '7 days';
                WHEN '30d' THEN window_start_at := as_of_at - interval '30 days';
                ELSE RAISE EXCEPTION 'WP06_SCORER_ACTION_LATENCY_WINDOW_INVALID';
            END CASE;
            IF NOT EXISTS (
                SELECT 1 FROM public.bot_instance AS bot
                 WHERE bot.workspace_id = target_workspace AND bot.id = target_bot
            ) THEN
                RAISE EXCEPTION 'WP06_SCORER_ACTION_LATENCY_BOT_NOT_FOUND';
            END IF;

            SELECT COALESCE(pg_catalog.array_agg(decision.id ORDER BY decision.id),
                            ARRAY[]::uuid[])
              INTO member_ids
              FROM public.learning_score_event AS event
              JOIN public.bot_decision AS decision
                ON decision.workspace_id = event.workspace_id
               AND decision.bot_id = event.bot_id
               AND decision.id = event.bot_decision_id
               AND decision.decision_hash = event.bot_decision_hash
               AND decision.candidate_id = event.candidate_id
              JOIN public.shared_signal_evidence AS evidence
                ON evidence.workspace_id = decision.workspace_id
               AND evidence.id = decision.evidence_id
             WHERE event.workspace_id = target_workspace
               AND event.bot_id = target_bot
               AND event.mode = 'PAPER_GATE'
               AND event.decision = 'APPROVE'
               AND event.final_status = 'CANDIDATE'
               AND event.evaluation_report_id = target_report_id
               AND event.evaluation_report_hash = target_report_hash
               AND event.scorer_artifact_hash = target_artifact_hash
               AND event.base_artifact_hash = target_base_artifact_hash
               AND event.policy_hash = target_policy_hash
               AND decision.status = 'CANDIDATE'
               AND evidence.available_at >= window_start_at
               AND evidence.available_at < as_of_at
               AND event.created_at < as_of_at
               AND decision.created_at < as_of_at;

            FOREACH member_id IN ARRAY member_ids LOOP
                source_ids := public.wp06_paper_performance_decision_source_ids(
                    target_workspace, member_id);
                IF source_ids IS NULL OR pg_catalog.cardinality(source_ids) = 0 THEN
                    RAISE EXCEPTION 'WP06_SCORER_ACTION_LATENCY_SOURCE_LINEAGE_MISSING';
                END IF;
                all_source_ids := all_source_ids || source_ids;
            END LOOP;
            FOR source_id IN
                SELECT DISTINCT source_item.source_id
                  FROM pg_catalog.unnest(all_source_ids) AS source_item(source_id)
                 ORDER BY source_item.source_id
            LOOP
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || source_id, 0));
            END LOOP;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'wp06-learning-observation-member-set:' || target_workspace::text, 0));

            SELECT COALESCE(pg_catalog.array_agg(decision.id ORDER BY decision.id),
                            ARRAY[]::uuid[])
              INTO member_ids_after_fence
              FROM public.learning_score_event AS event
              JOIN public.bot_decision AS decision
                ON decision.workspace_id = event.workspace_id
               AND decision.bot_id = event.bot_id
               AND decision.id = event.bot_decision_id
               AND decision.decision_hash = event.bot_decision_hash
               AND decision.candidate_id = event.candidate_id
              JOIN public.shared_signal_evidence AS evidence
                ON evidence.workspace_id = decision.workspace_id
               AND evidence.id = decision.evidence_id
             WHERE event.workspace_id = target_workspace
               AND event.bot_id = target_bot
               AND event.mode = 'PAPER_GATE'
               AND event.decision = 'APPROVE'
               AND event.final_status = 'CANDIDATE'
               AND event.evaluation_report_id = target_report_id
               AND event.evaluation_report_hash = target_report_hash
               AND event.scorer_artifact_hash = target_artifact_hash
               AND event.base_artifact_hash = target_base_artifact_hash
               AND event.policy_hash = target_policy_hash
               AND decision.status = 'CANDIDATE'
               AND evidence.available_at >= window_start_at
               AND evidence.available_at < as_of_at
               AND event.created_at < as_of_at
               AND decision.created_at < as_of_at;
            IF member_ids_after_fence IS DISTINCT FROM member_ids THEN
                RAISE EXCEPTION 'WP06_SCORER_ACTION_LATENCY_MEMBER_SET_CHANGED';
            END IF;

            checked_at := pg_catalog.clock_timestamp();
            FOREACH member_id IN ARRAY member_ids LOOP
                decision_rights_digest :=
                    public.wp06_authorize_paper_performance_decision_source_scope(
                        target_workspace, member_id, checked_at);
                IF decision_rights_digest IS NULL
                   OR decision_rights_digest !~ '^[0-9a-f]{{64}}$' THEN
                    RAISE EXCEPTION 'WP06_SCORER_ACTION_LATENCY_CURRENT_RIGHTS_DENIED';
                END IF;
            END LOOP;

            candidate_count := pg_catalog.cardinality(member_ids)::bigint;
            WITH member_set AS (
                SELECT member.bot_decision_id
                  FROM pg_catalog.unnest(member_ids) AS member(bot_decision_id)
            ), cohort AS (
                SELECT decision.id AS bot_decision_id,
                       evidence.available_at,
                       evidence.created_at AS evidence_created_at,
                       decision.created_at AS decision_created_at,
                       intent.id AS intent_id,
                       intent.created_at AS intent_created_at
                  FROM member_set AS member
                  JOIN public.bot_decision AS decision
                    ON decision.workspace_id = target_workspace
                   AND decision.bot_id = target_bot
                   AND decision.id = member.bot_decision_id
                  JOIN public.learning_score_event AS event
                    ON event.workspace_id = decision.workspace_id
                   AND event.bot_id = decision.bot_id
                   AND event.bot_decision_id = decision.id
                   AND event.bot_decision_hash = decision.decision_hash
                   AND event.candidate_id = decision.candidate_id
                   AND event.mode = 'PAPER_GATE'
                   AND event.decision = 'APPROVE'
                   AND event.final_status = 'CANDIDATE'
                   AND event.evaluation_report_id = target_report_id
                   AND event.evaluation_report_hash = target_report_hash
                   AND event.scorer_artifact_hash = target_artifact_hash
                   AND event.base_artifact_hash = target_base_artifact_hash
                   AND event.policy_hash = target_policy_hash
                  JOIN public.shared_signal_evidence AS evidence
                    ON evidence.workspace_id = decision.workspace_id
                   AND evidence.id = decision.evidence_id
                  LEFT JOIN public.order_intent AS intent
                    ON intent.workspace_id = decision.workspace_id
                   AND intent.bot_id = decision.bot_id
                   AND intent.bot_decision_id = decision.id
                   AND intent.candidate_id = decision.candidate_id
                   AND intent.mode = 'PAPER'
            )
            SELECT pg_catalog.count(*) FILTER (WHERE intent_id IS NULL)::bigint,
                   pg_catalog.count(*) FILTER (
                       WHERE intent_id IS NOT NULL AND intent_created_at >= as_of_at)::bigint
              INTO no_intent_count, future_count
              FROM cohort;

            RETURN QUERY
            WITH member_set AS (
                SELECT member.bot_decision_id
                  FROM pg_catalog.unnest(member_ids) AS member(bot_decision_id)
            ), cohort AS (
                SELECT decision.id AS bot_decision_id,
                       evidence.available_at,
                       evidence.created_at AS evidence_created_at,
                       decision.created_at AS decision_created_at,
                       intent.id AS intent_id,
                       intent.created_at AS intent_created_at
                  FROM member_set AS member
                  JOIN public.bot_decision AS decision
                    ON decision.workspace_id = target_workspace
                   AND decision.bot_id = target_bot
                   AND decision.id = member.bot_decision_id
                  JOIN public.learning_score_event AS event
                    ON event.workspace_id = decision.workspace_id
                   AND event.bot_id = decision.bot_id
                   AND event.bot_decision_id = decision.id
                   AND event.bot_decision_hash = decision.decision_hash
                   AND event.candidate_id = decision.candidate_id
                   AND event.mode = 'PAPER_GATE'
                   AND event.decision = 'APPROVE'
                   AND event.final_status = 'CANDIDATE'
                   AND event.evaluation_report_id = target_report_id
                   AND event.evaluation_report_hash = target_report_hash
                   AND event.scorer_artifact_hash = target_artifact_hash
                   AND event.base_artifact_hash = target_base_artifact_hash
                   AND event.policy_hash = target_policy_hash
                  JOIN public.shared_signal_evidence AS evidence
                    ON evidence.workspace_id = decision.workspace_id
                   AND evidence.id = decision.evidence_id
                  LEFT JOIN public.order_intent AS intent
                    ON intent.workspace_id = decision.workspace_id
                   AND intent.bot_id = decision.bot_id
                   AND intent.bot_decision_id = decision.id
                   AND intent.candidate_id = decision.candidate_id
                   AND intent.mode = 'PAPER'
            ), stage_samples AS (
                SELECT 'signal_available_to_intent'::text AS stage_name,
                       CASE WHEN intent_id IS NOT NULL AND intent_created_at < as_of_at
                                  AND available_at IS NOT NULL AND intent_created_at >= available_at
                            THEN pg_catalog.date_part('epoch',
                                 intent_created_at - available_at) * 1000.0
                       END::double precision AS duration_ms,
                       intent_id IS NOT NULL AND intent_created_at < as_of_at
                           AND (available_at IS NULL OR intent_created_at < available_at)
                           AS invalid_timestamp
                  FROM cohort
                UNION ALL
                SELECT 'evidence_persisted_to_intent'::text,
                       CASE WHEN intent_id IS NOT NULL AND intent_created_at < as_of_at
                                  AND evidence_created_at IS NOT NULL
                                  AND intent_created_at >= evidence_created_at
                            THEN pg_catalog.date_part('epoch',
                                 intent_created_at - evidence_created_at) * 1000.0
                       END::double precision,
                       intent_id IS NOT NULL AND intent_created_at < as_of_at
                           AND (evidence_created_at IS NULL
                                OR intent_created_at < evidence_created_at)
                  FROM cohort
                UNION ALL
                SELECT 'decision_persisted_to_intent'::text,
                       CASE WHEN intent_id IS NOT NULL AND intent_created_at < as_of_at
                                  AND decision_created_at IS NOT NULL
                                  AND intent_created_at >= decision_created_at
                            THEN pg_catalog.date_part('epoch',
                                 intent_created_at - decision_created_at) * 1000.0
                       END::double precision,
                       intent_id IS NOT NULL AND intent_created_at < as_of_at
                           AND (decision_created_at IS NULL
                                OR intent_created_at < decision_created_at)
                  FROM cohort
            ), stage_aggregates AS (
                SELECT sample.stage_name,
                       pg_catalog.count(*) FILTER (WHERE sample.duration_ms IS NOT NULL)::bigint
                           AS valid_sample_count,
                       pg_catalog.count(*) FILTER (WHERE sample.invalid_timestamp)::bigint
                           AS invalid_timestamp_count,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY sample.duration_ms)
                           FILTER (WHERE sample.duration_ms IS NOT NULL) AS p50_value,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY sample.duration_ms)
                           FILTER (WHERE sample.duration_ms IS NOT NULL) AS p95_value
                  FROM stage_samples AS sample
                 GROUP BY sample.stage_name
            ), stage_names(stage_order, stage_name) AS (
                VALUES
                    (1, 'signal_available_to_intent'::text),
                    (2, 'evidence_persisted_to_intent'::text),
                    (3, 'decision_persisted_to_intent'::text)
            )
            SELECT stage_names.stage_name,
                   window_start_at,
                   as_of_at,
                   candidate_count,
                   no_intent_count,
                   future_count,
                   COALESCE(stage_aggregates.valid_sample_count, 0),
                   COALESCE(stage_aggregates.invalid_timestamp_count, 0),
                   CASE WHEN COALESCE(stage_aggregates.valid_sample_count, 0) >= 100
                        THEN pg_catalog.round(stage_aggregates.p50_value::numeric)::bigint
                        ELSE NULL END,
                   CASE WHEN COALESCE(stage_aggregates.valid_sample_count, 0) >= 100
                        THEN pg_catalog.round(stage_aggregates.p95_value::numeric)::bigint
                        ELSE NULL END
              FROM stage_names
              LEFT JOIN stage_aggregates USING (stage_name)
             ORDER BY stage_names.stage_order;
        END; $$
    """)
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {EVALUATOR}")
    op.execute(
        f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC, {ADMIN}, {HISTORY}, "
        "crypidian_app, crypidian_trading_runtime, "
        "crypidian_learning_paper_performance_evaluator, crypidian_trainer_worker"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {ADMIN}")


def upgrade() -> None:
    _grant_evidence_metadata()
    _create_aggregate_gateway()


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {FUNCTION}")
    op.execute(
        "DROP POLICY IF EXISTS wp06_scorer_action_latency_evaluator_read "
        "ON public.shared_signal_evidence"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,id,created_at,available_at) "
        f"ON TABLE public.shared_signal_evidence FROM {EVALUATOR}"
    )

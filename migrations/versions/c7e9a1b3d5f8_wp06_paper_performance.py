"""WP06: aggregate PAPER scoring diagnostic and immutable report ledger."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "c7e9a1b3d5f8"
down_revision: str | None = "b5d7f9a1c3e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
EVALUATOR = "crypidian_learning_paper_performance_evaluator"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
FUNCTION = "public.wp06_aggregate_paper_performance(uuid,uuid,text,uuid,text,timestamptz)"


def _create_evaluator_role() -> None:
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{EVALUATOR}') THEN
            CREATE ROLE {EVALUATOR} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSIF EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE rolname='{EVALUATOR}' AND
            (rolcanlogin OR rolsuper OR rolbypassrls OR rolinherit OR rolcreatedb OR
             rolcreaterole OR rolreplication OR EXISTS (SELECT 1 FROM
                 pg_catalog.pg_auth_members m WHERE m.roleid=r.oid OR m.member=r.oid))) THEN
            RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_EVALUATOR_ROLE_UNSAFE';
        END IF;
    END $$""")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {EVALUATOR}")


def _create_report_table() -> None:
    op.create_table(
        "learning_paper_performance_report",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("baseline_report_id", sa.Uuid(), nullable=False),
        sa.Column("baseline_report_hash", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("target_fingerprint", sa.String(64), nullable=False),
        sa.Column("membership_digest", sa.String(64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("report_hash", sa.String(64), nullable=False),
        sa.Column("report", JSONB(), nullable=False),
        sa.Column("canonical_report", sa.Text(), nullable=False),
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('healthy','degraded','inconclusive')",
            name="learning_paper_performance_status_valid",
        ),
        sa.CheckConstraint(
            "policy_hash ~ '^[0-9a-f]{64}$' "
            "AND target_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND baseline_report_hash ~ '^[0-9a-f]{64}$' "
            "AND membership_digest ~ '^[0-9a-f]{64}$' "
            "AND report_hash ~ '^[0-9a-f]{64}$' AND idempotency_key IS NOT NULL",
            name="learning_paper_performance_hashes_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(report) = 'object' AND length(canonical_report) > 0 "
            "AND NOT (report ?| ARRAY['candidate_id','decision_id','decision_payload',"
            "'feature_payload','features','labels','probabilities','trade_id','trade_payload'])",
            name="learning_paper_performance_report_aggregate_only",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id"],
            ["bot_instance.workspace_id", "bot_instance.id"],
            ondelete="RESTRICT",
            name="fk_learning_paper_performance_bot",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "baseline_report_id"],
            [
                "learning_scorer_test_evaluation_report.workspace_id",
                "learning_scorer_test_evaluation_report.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_paper_performance_baseline_report",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_paper_performance_report")),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_learning_paper_performance_workspace_id"
        ),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_paper_performance_idempotency"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "target_fingerprint",
            "policy_hash",
            "as_of",
            "membership_digest",
            name="uq_learning_paper_performance_snapshot",
        ),
    )
    op.create_index(
        "ix_learning_paper_performance_workspace_created",
        "learning_paper_performance_report",
        ["workspace_id", "created_at"],
    )
    table = "public.learning_paper_performance_report"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY learning_paper_performance_workspace_select ON {table} "
        f"FOR SELECT TO {ADMIN} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_paper_performance_workspace_insert ON {table} "
        f"FOR INSERT TO {ADMIN} WITH CHECK ({WORKSPACE})"
    )
    op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC, {ADMIN}, {EVALUATOR}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE {table} TO {ADMIN}")
    op.execute("""CREATE FUNCTION public.reject_learning_paper_performance_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$ BEGIN
        RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_REPORT_IMMUTABLE'; END $$""")
    op.execute(
        "REVOKE ALL ON FUNCTION public.reject_learning_paper_performance_mutation() FROM PUBLIC"
    )
    op.execute(
        "CREATE TRIGGER learning_paper_performance_report_immutable "
        "BEFORE UPDATE OR DELETE ON public.learning_paper_performance_report "
        "FOR EACH ROW EXECUTE FUNCTION public.reject_learning_paper_performance_mutation()"
    )


def _grant_evaluator_reads() -> None:
    table_columns = {
        "learning_score_event": (
            "workspace_id,bot_id,bot_decision_id,bot_decision_hash,final_status,candidate_id,"
            "mode,deployment_event_id,deployment_revision,registry_id,registry_revision,"
            "active_pointer_version,scorer_artifact_id,scorer_artifact_hash,base_artifact_hash,"
            "evaluation_report_id,evaluation_report_hash,report_scope_hash,proof_hash,policy_id,"
            "policy_hash,feature_schema_hash,source_rights_fingerprint,decision,"
            "class_probabilities,event_hash"
        ),
        "bot_decision": "workspace_id,bot_id,id,status,candidate_id,decision_hash,created_at",
        "order_intent": (
            "workspace_id,bot_id,account_id,id,bot_decision_id,candidate_id,mode,created_at"
        ),
        "trade": "workspace_id,bot_id,account_id,id,intent_id,mode,status,closed_at,net_pnl",
        "learning_scorer_test_evaluation_report": (
            "workspace_id,id,report_hash,report,canonical_report,evaluation_status"
        ),
        "bot_instance": "workspace_id,id",
    }
    for table, columns in table_columns.items():
        op.execute(
            f"CREATE POLICY wp06_paper_performance_evaluator_read ON public.{table} "
            f"FOR SELECT TO {EVALUATOR} USING ({WORKSPACE})"
        )
        op.execute(f"GRANT SELECT ({columns}) ON TABLE public.{table} TO {EVALUATOR}")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_resolve_scorer_runtime_target("
        "uuid,text,jsonb,uuid,integer,boolean) TO " + EVALUATOR
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_verify_scorer_deployment_rights(uuid,uuid,uuid) TO "
        + EVALUATOR
    )


def _create_aggregate_gateway() -> None:
    op.execute("""
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
            membership_digest text
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            target_row record;
            baseline_row record;
            rights_row record;
            fingerprint_payload jsonb;
            actual_fingerprint text;
            vector_schema_hash text;
            cohort_index integer;
            aggregate_row record;
        BEGIN
            IF session_user <> 'crypidian_admin_web' THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CALLER_DENIED';
            END IF;
            IF target_workspace IS NULL OR target_bot IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_WORKSPACE_DENIED';
            END IF;
            IF requested_as_of IS DISTINCT FROM transaction_timestamp()
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_DATABASE_TIME_REQUIRED';
            END IF;
            IF expected_target_fingerprint !~ '^[0-9a-f]{64}$'
               OR expected_baseline_report_hash !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_HASH_INVALID';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.bot_instance AS bot
                WHERE bot.workspace_id=target_workspace AND bot.id=target_bot) THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_BOT_NOT_FOUND';
            END IF;

            SELECT active.* INTO target_row
              FROM public.wp06_resolve_scorer_runtime_target(
                target_workspace, NULL::text, NULL::jsonb, NULL::uuid, NULL::integer, false
              ) AS active;
            IF NOT FOUND OR target_row.mode NOT IN ('SHADOW','PAPER_GATE') THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_ACTIVE_TARGET_REQUIRED';
            END IF;
            vector_schema_hash :=
                target_row.scorer_artifact_canonical::jsonb->>'vector_schema_hash';
            IF vector_schema_hash !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_FEATURE_SCHEMA_INVALID';
            END IF;
            fingerprint_payload := jsonb_build_object(
                'workspace_id', target_workspace::text,
                'bot_id', target_bot::text,
                'registry_id', target_row.registry_id::text,
                'registry_revision', target_row.registry_revision,
                'scorer_artifact_id', target_row.scorer_artifact_id::text,
                'scorer_artifact_hash', target_row.scorer_artifact_hash,
                'base_artifact_hash', target_row.base_artifact_hash,
                'policy_id', target_row.policy_id,
                'policy_hash', target_row.policy_hash,
                'deployment_event_id', target_row.deployment_event_id::text,
                'deployment_revision', target_row.deployment_revision,
                'evaluation_report_id', target_row.evaluation_report_id::text,
                'evaluation_report_hash', target_row.evaluation_report_hash,
                'proof_hash', target_row.proof_hash,
                'report_scope_hash', target_row.report_scope_hash,
                'feature_schema_hash', vector_schema_hash,
                'source_rights_fingerprint', target_row.source_rights_fingerprint
            );
            actual_fingerprint := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                public.wp06_license_canonical_json(fingerprint_payload), 'UTF8')), 'hex');
            IF actual_fingerprint IS DISTINCT FROM expected_target_fingerprint
               OR target_row.evaluation_report_id IS DISTINCT FROM expected_baseline_report_id
               OR target_row.evaluation_report_hash
                    IS DISTINCT FROM expected_baseline_report_hash THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_TARGET_STALE';
            END IF;

            SELECT report.* INTO baseline_row
              FROM public.learning_scorer_test_evaluation_report AS report
             WHERE report.workspace_id=target_workspace
               AND report.id=expected_baseline_report_id;
            IF NOT FOUND OR baseline_row.report_hash IS DISTINCT FROM expected_baseline_report_hash
               OR baseline_row.evaluation_status NOT IN (
                    'ACCEPTED_SHADOW_ONLY','ACCEPTED_PAPER_GATE_ELIGIBLE')
               OR baseline_row.canonical_report::jsonb IS DISTINCT FROM baseline_row.report
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    baseline_row.canonical_report,'UTF8')), 'hex')
                    IS DISTINCT FROM baseline_row.report_hash
               OR baseline_row.scorer_artifact_hash IS DISTINCT FROM target_row.scorer_artifact_hash
               OR baseline_row.base_artifact_hash IS DISTINCT FROM target_row.base_artifact_hash
               OR baseline_row.policy_hash IS DISTINCT FROM target_row.policy_hash
               OR (baseline_row.report->>'brier_score') IS NULL
               OR (baseline_row.report->>'log_loss') IS NULL THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_BASELINE_INVALID';
            END IF;
            IF (baseline_row.report->>'brier_score')::numeric < 0
               OR (baseline_row.report->>'log_loss')::numeric < 0 THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_BASELINE_INVALID';
            END IF;
            SELECT rights.* INTO rights_row
              FROM public.wp06_verify_scorer_deployment_rights(
                target_workspace, target_row.scorer_artifact_id, target_row.evaluation_report_id
              ) AS rights;
            IF NOT FOUND OR rights_row.fingerprint IS NULL
               OR rights_row.fingerprint IS DISTINCT FROM target_row.source_rights_fingerprint THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED';
            END IF;

            FOR cohort_index IN 0..1 LOOP
                cohort_name := CASE WHEN cohort_index=0 THEN 'older' ELSE 'newer' END;
                window_start := CASE WHEN cohort_index=0
                    THEN requested_as_of - interval '90 days'
                    ELSE requested_as_of - interval '60 days' END;
                window_end := CASE WHEN cohort_index=0
                    THEN requested_as_of - interval '60 days'
                    ELSE requested_as_of - interval '30 days' END;
                WITH eligible AS (
                    SELECT decision.id AS decision_id, decision.created_at AS decision_at,
                           event.event_hash, event.class_probabilities,
                           intent.id AS intent_id, intent.account_id, intent.created_at AS intent_at
                      FROM public.learning_score_event AS event
                      JOIN public.bot_decision AS decision
                        ON decision.workspace_id=event.workspace_id
                       AND decision.bot_id=event.bot_id
                       AND decision.id=event.bot_decision_id
                       AND decision.decision_hash=event.bot_decision_hash
                       AND decision.candidate_id=event.candidate_id
                      JOIN public.order_intent AS intent
                        ON intent.workspace_id=event.workspace_id
                       AND intent.bot_id=event.bot_id
                       AND intent.bot_decision_id=event.bot_decision_id
                       AND intent.candidate_id=event.candidate_id
                       AND intent.mode='PAPER'
                     WHERE event.workspace_id=target_workspace
                       AND event.bot_id=target_bot
                       AND decision.status='CANDIDATE'
                       AND event.decision='APPROVE' AND event.final_status='CANDIDATE'
                       AND event.mode=target_row.mode
                       AND event.deployment_event_id=target_row.deployment_event_id
                       AND event.deployment_revision=target_row.deployment_revision
                       AND event.registry_id=target_row.registry_id
                       AND event.registry_revision=target_row.registry_revision
                       AND event.active_pointer_version=target_row.active_pointer_version
                       AND event.scorer_artifact_id=target_row.scorer_artifact_id
                       AND event.scorer_artifact_hash=target_row.scorer_artifact_hash
                       AND event.base_artifact_hash=target_row.base_artifact_hash
                       AND event.evaluation_report_id=target_row.evaluation_report_id
                       AND event.evaluation_report_hash=target_row.evaluation_report_hash
                       AND event.evaluation_status=target_row.evaluation_status
                       AND event.proof_hash=target_row.proof_hash
                       AND event.report_scope_hash=target_row.report_scope_hash
                       AND event.policy_id=target_row.policy_id
                       AND event.policy_hash=target_row.policy_hash
                       AND event.feature_schema_hash=vector_schema_hash
                       AND event.source_rights_fingerprint=target_row.source_rights_fingerprint
                       AND decision.created_at >= window_start
                       AND decision.created_at < window_end
                       AND intent.created_at <= requested_as_of - interval '30 days'
                ), closed AS (
                    SELECT eligible.*,
                           pg_catalog.count(trade.id)::bigint AS closed_trade_count,
                           pg_catalog.array_agg(trade.id::text ORDER BY trade.id::text)
                               FILTER (WHERE trade.id IS NOT NULL) AS closed_trade_ids,
                           pg_catalog.min(trade.closed_at) AS closed_at,
                           pg_catalog.min(trade.net_pnl) AS net_pnl
                      FROM eligible
                      LEFT JOIN public.trade AS trade
                        ON trade.workspace_id=target_workspace
                       AND trade.bot_id=target_bot
                       AND trade.account_id=eligible.account_id
                       AND trade.intent_id=eligible.intent_id
                       AND trade.mode='PAPER' AND trade.status='CLOSED'
                     GROUP BY eligible.decision_id, eligible.decision_at, eligible.event_hash,
                              eligible.class_probabilities, eligible.intent_id,
                              eligible.account_id, eligible.intent_at
                ), outcomes AS (
                    SELECT closed.*,
                           CASE WHEN closed_trade_count=1
                                  AND closed_at >= intent_at
                                  AND closed_at <= intent_at + interval '30 days'
                                  AND closed_at <= requested_as_of
                                THEN CASE WHEN net_pnl > 0 THEN 1
                                          WHEN net_pnl < 0 THEN -1 ELSE 0 END
                                ELSE NULL END AS outcome_label
                      FROM closed
                ), scored AS (
                    SELECT outcomes.*,
                           (class_probabilities->0->>1)::numeric AS p_minus,
                           (class_probabilities->1->>1)::numeric AS p_zero,
                           (class_probabilities->2->>1)::numeric AS p_plus
                      FROM outcomes
                ), metric_rows AS (
                    SELECT scored.*,
                           ((p_minus - CASE WHEN outcome_label=-1 THEN 1 ELSE 0 END)^2
                            +(p_zero - CASE WHEN outcome_label=0 THEN 1 ELSE 0 END)^2
                            +(p_plus - CASE WHEN outcome_label=1 THEN 1 ELSE 0 END)^2)
                            AS brier_value,
                           -pg_catalog.ln(pg_catalog.greatest(
                                CASE WHEN outcome_label=-1 THEN p_minus
                                     WHEN outcome_label=0 THEN p_zero ELSE p_plus END,
                                0.000000000001::numeric)) AS log_loss_value
                      FROM scored WHERE outcome_label IS NOT NULL
                )
                SELECT (SELECT pg_catalog.count(*) FROM outcomes)::bigint AS eligible_count,
                       pg_catalog.count(*)::bigint AS covered_count,
                       pg_catalog.count(*) FILTER (WHERE outcome_label=-1)::bigint AS minus_count,
                       pg_catalog.count(*) FILTER (WHERE outcome_label=0)::bigint AS zero_count,
                       pg_catalog.count(*) FILTER (WHERE outcome_label=1)::bigint AS plus_count,
                       pg_catalog.avg(brier_value) AS brier,
                       pg_catalog.avg(log_loss_value) AS log_loss,
                       pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                           COALESCE((SELECT pg_catalog.string_agg(
                               decision_id::text || ':' || event_hash || ':' || intent_id::text
                               || ':' || COALESCE(
                                   pg_catalog.array_to_string(closed_trade_ids, ','), 'none')
                               || ':' || COALESCE(outcome_label::text, 'uncovered'), E'\n'
                               ORDER BY decision_at, decision_id) FROM outcomes), ''), 'UTF8')),
                           'hex')
                           AS membership
                  INTO aggregate_row FROM metric_rows;

                target_fingerprint := actual_fingerprint;
                baseline_report_id := baseline_row.id;
                baseline_report_hash := baseline_row.report_hash;
                baseline_brier := (baseline_row.report->>'brier_score')::numeric;
                baseline_log_loss := (baseline_row.report->>'log_loss')::numeric;
                eligible_decision_count := COALESCE(aggregate_row.eligible_count,0);
                covered_outcome_count := COALESCE(aggregate_row.covered_count,0);
                class_minus_one_count := COALESCE(aggregate_row.minus_count,0);
                class_zero_count := COALESCE(aggregate_row.zero_count,0);
                class_plus_one_count := COALESCE(aggregate_row.plus_count,0);
                observed_brier := aggregate_row.brier;
                observed_log_loss := aggregate_row.log_loss;
                membership_digest := aggregate_row.membership;
                RETURN NEXT;
            END LOOP;
        END $$
    """)
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {EVALUATOR}")
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {ADMIN}")


def upgrade() -> None:
    _create_evaluator_role()
    _create_report_table()
    _grant_evaluator_reads()
    _create_aggregate_gateway()


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.learning_paper_performance_report) THEN
            RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_DOWNGRADE_POPULATED_DENIED';
        END IF;
    END $$""")
    op.execute(f"DROP FUNCTION {FUNCTION}")
    op.execute(
        "DROP TRIGGER learning_paper_performance_report_immutable "
        "ON public.learning_paper_performance_report"
    )
    op.drop_index(
        "ix_learning_paper_performance_workspace_created",
        table_name="learning_paper_performance_report",
    )
    op.drop_table("learning_paper_performance_report")
    op.execute("DROP FUNCTION public.reject_learning_paper_performance_mutation()")
    op.execute("DROP POLICY wp06_paper_performance_evaluator_read ON public.learning_score_event")
    op.execute("DROP POLICY wp06_paper_performance_evaluator_read ON public.bot_decision")
    op.execute("DROP POLICY wp06_paper_performance_evaluator_read ON public.order_intent")
    op.execute("DROP POLICY wp06_paper_performance_evaluator_read ON public.trade")
    op.execute(
        "DROP POLICY wp06_paper_performance_evaluator_read "
        "ON public.learning_scorer_test_evaluation_report"
    )
    op.execute("DROP POLICY wp06_paper_performance_evaluator_read ON public.bot_instance")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION public.wp06_resolve_scorer_runtime_target("
        f"uuid,text,jsonb,uuid,integer,boolean) FROM {EVALUATOR}"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_verify_scorer_deployment_rights(uuid,uuid,uuid) "
        f"FROM {EVALUATOR}"
    )
    for table in (
        "learning_score_event",
        "bot_decision",
        "order_intent",
        "trade",
        "learning_scorer_test_evaluation_report",
        "bot_instance",
    ):
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {EVALUATOR}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {EVALUATOR}")
    op.execute(f"DROP ROLE {EVALUATOR}")

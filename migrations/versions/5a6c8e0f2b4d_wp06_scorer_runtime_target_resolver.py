# ruff: noqa: E501
"""WP06 C4: phân giải đúng target scorer runtime qua gateway có quyền tối thiểu."""

from collections.abc import Sequence

from alembic import op

revision: str = "5a6c8e0f2b4d"
down_revision: str | None = "55c504fe5b86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
GUARD_ROLE = "crypidian_scorer_deployment_guard"
RUNTIME = "crypidian_trading_runtime"
ADMIN = "crypidian_admin_web"
MARKET_SIGNAL_WORKER = "crypidian_market_signal_worker"


def upgrade() -> None:
    _grant_exact_runtime_metadata()
    _revoke_runtime_deployment_reads()
    _create_target_resolver()
    _replace_active_deployment_fail_closed_marker()


def _grant_exact_runtime_metadata() -> None:
    op.execute(
        "CREATE POLICY wp06_scorer_runtime_deployment_pointer_read "
        "ON public.learning_scoring_deployment_pointer FOR SELECT "
        f"TO {RIGHTS_ROLE} USING (workspace_id = "
        "NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid)"
    )
    op.execute(
        f"GRANT SELECT (workspace_id, event_id, revision, active_pointer_version, mode) "
        f"ON public.learning_scoring_deployment_pointer TO {RIGHTS_ROLE}"
    )
    op.execute(
        f"GRANT SELECT (event_payload) ON public.learning_scoring_deployment_event TO {RIGHTS_ROLE}"
    )
    op.execute(
        "CREATE POLICY wp06_scorer_runtime_registry_pointer_read "
        "ON public.learning_model_active_pointer FOR SELECT "
        f"TO {RIGHTS_ROLE} USING (workspace_id = "
        "NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid)"
    )
    op.execute(
        "CREATE POLICY wp06_scorer_runtime_registry_read "
        "ON public.learning_model_registry FOR SELECT "
        f"TO {RIGHTS_ROLE} USING (workspace_id = "
        "NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid)"
    )
    op.execute(
        f"GRANT SELECT (workspace_id, registry_id, version) "
        f"ON public.learning_model_active_pointer TO {RIGHTS_ROLE}"
    )
    op.execute(
        f"GRANT SELECT (workspace_id, id, artifact_id, artifact_hash, approval_hash, state, revision) "
        f"ON public.learning_model_registry TO {RIGHTS_ROLE}"
    )
    op.execute(
        f"GRANT SELECT (workspace_id, id, attempt_id, artifact_hash, canonical_artifact, artifact) "
        f"ON public.learning_training_artifact TO {RIGHTS_ROLE}"
    )


def _revoke_runtime_deployment_reads() -> None:
    op.execute(
        f"REVOKE SELECT ON public.learning_scoring_deployment_event, "
        f"public.learning_scoring_deployment_pointer FROM {RUNTIME}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,id,mode,revision,scorer_artifact_id,evaluation_report_id) "
        f"ON public.learning_scoring_deployment_event FROM {RUNTIME}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,event_id,revision,active_pointer_version,mode) "
        f"ON public.learning_scoring_deployment_pointer FROM {RUNTIME}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,event_id,revision,active_pointer_version,mode) "
        f"ON public.learning_scoring_deployment_pointer FROM {MARKET_SIGNAL_WORKER}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,id,mode,revision,scorer_artifact_id,evaluation_report_id) "
        f"ON public.learning_scoring_deployment_event FROM {MARKET_SIGNAL_WORKER}"
    )
    op.execute(
        f"REVOKE SELECT (workspace_id,version) "
        f"ON public.learning_model_active_pointer FROM {MARKET_SIGNAL_WORKER}"
    )


def _restore_runtime_deployment_reads() -> None:
    op.execute(
        f"GRANT SELECT ON public.learning_scoring_deployment_event, "
        f"public.learning_scoring_deployment_pointer TO {RUNTIME}"
    )
    op.execute(
        "GRANT SELECT (workspace_id,event_id,revision,active_pointer_version,mode) "
        f"ON public.learning_scoring_deployment_pointer TO {MARKET_SIGNAL_WORKER}"
    )
    op.execute(
        "GRANT SELECT (workspace_id,id,mode,revision,scorer_artifact_id,evaluation_report_id) "
        f"ON public.learning_scoring_deployment_event TO {MARKET_SIGNAL_WORKER}"
    )
    op.execute(
        f"GRANT SELECT (workspace_id,version) "
        f"ON public.learning_model_active_pointer TO {MARKET_SIGNAL_WORKER}"
    )


def _create_target_resolver() -> None:
    op.execute("""
        CREATE FUNCTION public.wp06_resolve_scorer_runtime_target(
            target_workspace uuid,
            target_mode text,
            target jsonb,
            target_deployment_event_id uuid,
            target_deployment_revision integer,
            target_is_pending boolean
        ) RETURNS TABLE(
            workspace_id uuid,
            mode text,
            deployment_event_id uuid,
            deployment_revision integer,
            registry_id uuid,
            registry_revision integer,
            active_pointer_version integer,
            scorer_artifact_id uuid,
            scorer_artifact_hash text,
            base_artifact_id uuid,
            base_artifact_hash text,
            base_artifact_canonical text,
            scorer_artifact_canonical text,
            evaluation_report_id uuid,
            evaluation_report_hash text,
            evaluation_status text,
            candidate_completeness_verified boolean,
            scored_candidate_coverage_verified boolean,
            report_scope_hash text,
            proof_id uuid,
            proof_hash text,
            proof_scope_hash text,
            policy_id text,
            policy_hash text,
            current_rights_receipt_hash text,
            source_rights_fingerprint text
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            pointer_row record;
            registry_row record;
            scorer_row record;
            base_row record;
            report_row record;
            proof_row record;
            rights_row record;
            target_registry uuid;
            target_registry_revision integer;
            target_pointer_version integer;
            target_scorer uuid;
            target_scorer_hash text;
            target_report uuid;
            target_report_hash text;
            target_status text;
            target_proof uuid;
            target_proof_hash text;
            target_completeness boolean;
            target_report_scope text;
            target_proof_scope text;
            target_policy_id text;
            target_policy_hash text;
            target_receipt_hash text;
            deployment_pointer record;
            deployment_event record;
        BEGIN
            IF session_user NOT IN (
                'crypidian_trading_runtime', 'crypidian_admin_web',
                'crypidian_market_signal_worker'
            ) THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_CALLER_DENIED';
            END IF;
            IF target_is_pending IS TRUE AND session_user <> 'crypidian_admin_web' THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_PENDING_CALLER_DENIED';
            END IF;
            IF target_workspace IS NULL OR target_is_pending IS NULL
               OR (target_is_pending AND (
                    target_mode NOT IN ('SHADOW','PAPER_GATE') OR target IS NULL
                    OR target_deployment_revision IS NULL OR target_deployment_revision < 0
                    OR
                    (target_deployment_revision = 0 AND target_deployment_event_id IS NOT NULL)
                    OR (target_deployment_revision > 0 AND target_deployment_event_id IS NULL)
               ))
               OR (NOT target_is_pending AND NOT (
                    (target_mode IS NULL AND target IS NULL
                     AND target_deployment_event_id IS NULL
                     AND target_deployment_revision IS NULL)
                    OR (target_mode IN ('SHADOW','PAPER_GATE') AND target IS NOT NULL
                        AND target_deployment_event_id IS NOT NULL
                        AND target_deployment_revision >= 1)
               ))
               OR target_workspace IS DISTINCT FROM
                  NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_WORKSPACE_DENIED';
            END IF;
            IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_READ_COMMITTED_REQUIRED';
            END IF;
            IF NOT target_is_pending AND target_mode IS NULL THEN
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'learning-scoring-deployment' || chr(58) || target_workspace::text
                    || chr(58) || 'current-pointer', 0
                ));
                SELECT pointer.workspace_id, pointer.event_id, pointer.revision,
                       pointer.active_pointer_version, pointer.mode
                  INTO deployment_pointer
                  FROM public.learning_scoring_deployment_pointer AS pointer
                 WHERE pointer.workspace_id = target_workspace;
                IF NOT FOUND THEN
                    RETURN;
                END IF;
                IF deployment_pointer.mode = 'DISABLED' THEN
                    RETURN;
                END IF;
                IF deployment_pointer.mode NOT IN ('SHADOW','PAPER_GATE')
                   OR deployment_pointer.event_id IS NULL OR deployment_pointer.revision < 1 THEN
                    RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_DEPLOYMENT_STALE';
                END IF;
                target_mode := deployment_pointer.mode;
                target_deployment_event_id := deployment_pointer.event_id;
                target_deployment_revision := deployment_pointer.revision;
                SELECT event.workspace_id, event.id, event.mode, event.revision, event.event_payload
                  INTO deployment_event
                  FROM public.learning_scoring_deployment_event AS event
                 WHERE event.workspace_id = target_workspace
                   AND event.id = target_deployment_event_id;
                IF NOT FOUND OR deployment_event.mode IS DISTINCT FROM target_mode
                   OR deployment_event.revision IS DISTINCT FROM target_deployment_revision
                   OR deployment_event.event_payload->>'schema_version'
                        IS DISTINCT FROM 'crypidian.learning.scorer-deployment.v1'
                   OR deployment_event.event_payload->>'event_id'
                        IS DISTINCT FROM target_deployment_event_id::text
                   OR deployment_event.event_payload->>'workspace_id'
                        IS DISTINCT FROM target_workspace::text
                   OR deployment_event.event_payload->>'mode' IS DISTINCT FROM target_mode
                   OR (deployment_event.event_payload->>'revision')::integer
                        IS DISTINCT FROM target_deployment_revision THEN
                    RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_EVENT_STALE';
                END IF;
                target := deployment_event.event_payload->'target';
            ELSIF NOT target_is_pending THEN
                SELECT event.workspace_id, event.id, event.mode, event.revision, event.event_payload
                  INTO deployment_event
                  FROM public.learning_scoring_deployment_event AS event
                 WHERE event.workspace_id = target_workspace
                   AND event.id = target_deployment_event_id;
                IF NOT FOUND OR deployment_event.mode IS DISTINCT FROM target_mode
                   OR deployment_event.revision IS DISTINCT FROM target_deployment_revision
                   OR deployment_event.event_payload->>'schema_version'
                        IS DISTINCT FROM 'crypidian.learning.scorer-deployment.v1'
                   OR deployment_event.event_payload->>'event_id'
                        IS DISTINCT FROM target_deployment_event_id::text
                   OR deployment_event.event_payload->>'workspace_id'
                        IS DISTINCT FROM target_workspace::text
                   OR deployment_event.event_payload->>'mode' IS DISTINCT FROM target_mode
                   OR (deployment_event.event_payload->>'revision')::integer
                        IS DISTINCT FROM target_deployment_revision
                   OR deployment_event.event_payload->'target' IS DISTINCT FROM target THEN
                    RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_EVENT_STALE';
                END IF;
            END IF;

            IF pg_catalog.jsonb_typeof(target) IS DISTINCT FROM 'object'
               OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(target)) <> 16
               OR NOT target ?& ARRAY[
                    'registry_id','registry_revision','active_pointer_version',
                    'scorer_artifact_id','scorer_artifact_hash','evaluation_report_id',
                    'evaluation_report_hash','evaluation_status','proof_id','proof_hash',
                    'candidate_completeness_verified','report_scope_hash','proof_scope_hash',
                    'policy_id','policy_hash','current_rights_receipt_hash'
               ] THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_SCHEMA_INVALID';
            END IF;

            target_registry := (target->>'registry_id')::uuid;
            target_registry_revision := (target->>'registry_revision')::integer;
            target_pointer_version := (target->>'active_pointer_version')::integer;
            target_scorer := (target->>'scorer_artifact_id')::uuid;
            target_scorer_hash := target->>'scorer_artifact_hash';
            target_report := (target->>'evaluation_report_id')::uuid;
            target_report_hash := target->>'evaluation_report_hash';
            target_status := target->>'evaluation_status';
            target_proof := (target->>'proof_id')::uuid;
            target_proof_hash := target->>'proof_hash';
            target_completeness := (target->>'candidate_completeness_verified')::boolean;
            target_report_scope := target->>'report_scope_hash';
            target_proof_scope := target->>'proof_scope_hash';
            target_policy_id := target->>'policy_id';
            target_policy_hash := target->>'policy_hash';
            target_receipt_hash := target->>'current_rights_receipt_hash';

            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'learning-scoring-deployment' || chr(58) || target_workspace::text
                || chr(58) || 'current-pointer', 0
            ));
            SELECT pointer.workspace_id, pointer.event_id, pointer.revision,
                   pointer.active_pointer_version, pointer.mode
              INTO deployment_pointer
              FROM public.learning_scoring_deployment_pointer AS pointer
             WHERE pointer.workspace_id = target_workspace;
            IF NOT FOUND OR (
                target_is_pending AND (
                    deployment_pointer.event_id IS DISTINCT FROM target_deployment_event_id
                    OR deployment_pointer.revision IS DISTINCT FROM target_deployment_revision
                )
            ) OR (
                NOT target_is_pending AND (
                    deployment_pointer.event_id IS DISTINCT FROM target_deployment_event_id
                    OR deployment_pointer.revision IS DISTINCT FROM target_deployment_revision
                    OR deployment_pointer.mode IS DISTINCT FROM target_mode
                )
            ) THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_DEPLOYMENT_STALE';
            END IF;

            IF target_registry_revision < 1 OR target_pointer_version < 0
               OR target_scorer_hash !~ '^[0-9a-f]{64}$'
               OR target_report_hash !~ '^[0-9a-f]{64}$'
               OR target_proof_hash !~ '^[0-9a-f]{64}$'
               OR target_report_scope !~ '^[0-9a-f]{64}$'
               OR target_proof_scope !~ '^[0-9a-f]{64}$'
               OR target_policy_hash !~ '^[0-9a-f]{64}$'
               OR target_receipt_hash !~ '^[0-9a-f]{64}$'
               OR target_policy_id IS NULL OR pg_catalog.btrim(target_policy_id) = '' THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_HASH_INVALID';
            END IF;

            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'learning-model-registry' || chr(58) || target_workspace::text
                || chr(58) || 'active-pointer', 0
            ));
            SELECT pointer.workspace_id, pointer.registry_id, pointer.version
              INTO pointer_row
              FROM public.learning_model_active_pointer AS pointer
             WHERE pointer.workspace_id = target_workspace;
            IF NOT FOUND OR pointer_row.registry_id IS DISTINCT FROM target_registry
               OR pointer_row.version IS DISTINCT FROM target_pointer_version THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_POINTER_STALE';
            END IF;

            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'learning-model-registry' || chr(58) || target_workspace::text
                || chr(58) || 'registry-entry' || chr(58) || target_registry::text, 0
            ));
            SELECT registry.workspace_id, registry.id, registry.artifact_id,
                   registry.artifact_hash, registry.approval_hash, registry.state,
                   registry.revision
              INTO registry_row
              FROM public.learning_model_registry AS registry
             WHERE registry.workspace_id = target_workspace
               AND registry.id = target_registry;
            IF NOT FOUND OR registry_row.state IS DISTINCT FROM 'paper_active'
               OR registry_row.revision IS DISTINCT FROM target_registry_revision THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_REGISTRY_STALE';
            END IF;

            SELECT scorer.workspace_id, scorer.id, scorer.attempt_id, scorer.base_artifact_id,
                   scorer.approval_hash,
                   scorer.base_artifact_hash, scorer.artifact_hash, scorer.state,
                   scorer.canonical_artifact
              INTO scorer_row
              FROM public.learning_scorer_artifact AS scorer
             WHERE scorer.workspace_id = target_workspace AND scorer.id = target_scorer;
            SELECT base.workspace_id, base.id, base.attempt_id, base.artifact_hash,
                   base.canonical_artifact, base.artifact
              INTO base_row
              FROM public.learning_training_artifact AS base
             WHERE base.workspace_id = target_workspace
               AND base.id = registry_row.artifact_id;
            IF NOT FOUND OR scorer_row.workspace_id IS DISTINCT FROM target_workspace
               OR scorer_row.base_artifact_id IS DISTINCT FROM registry_row.artifact_id
               OR scorer_row.base_artifact_hash IS DISTINCT FROM registry_row.artifact_hash
               OR scorer_row.artifact_hash IS DISTINCT FROM target_scorer_hash
               OR scorer_row.state IS DISTINCT FROM 'ready_for_evaluation'
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    scorer_row.canonical_artifact, 'UTF8')), 'hex')
                    IS DISTINCT FROM scorer_row.artifact_hash
               OR base_row.id IS DISTINCT FROM registry_row.artifact_id
               OR base_row.artifact_hash IS DISTINCT FROM registry_row.artifact_hash
               OR base_row.attempt_id IS DISTINCT FROM scorer_row.attempt_id
               OR registry_row.approval_hash IS DISTINCT FROM scorer_row.approval_hash
               OR base_row.canonical_artifact::jsonb IS DISTINCT FROM base_row.artifact
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    base_row.canonical_artifact, 'UTF8')), 'hex')
                    IS DISTINCT FROM base_row.artifact_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_ARTIFACT_MISMATCH';
            END IF;

            SELECT report.workspace_id, report.id, report.report_hash,
                   report.evaluation_status, report.candidate_completeness_verified,
                   report.scored_candidate_coverage_verified, report.proof_id,
                   report.proof_hash, report.report->>'source_scope_hash' AS source_scope_hash,
                   report.scorer_artifact_hash, report.base_artifact_hash,
                   report.policy_version, report.policy_hash,
                   report.current_rights_receipt_hash, report.canonical_report, report.report
              INTO report_row
              FROM public.learning_scorer_test_evaluation_report AS report
             WHERE report.workspace_id = target_workspace AND report.id = target_report;
            SELECT proof.workspace_id, proof.id, proof.proof_hash,
                   proof.source_scope_hash, proof.completeness_verified, proof.canonical_proof
              INTO proof_row
              FROM public.learning_scorer_test_opportunity_proof AS proof
             WHERE proof.workspace_id = target_workspace AND proof.id = target_proof;
            IF NOT FOUND OR report_row.id IS DISTINCT FROM target_report
               OR report_row.report_hash IS DISTINCT FROM target_report_hash
               OR report_row.evaluation_status IS DISTINCT FROM target_status
               OR report_row.candidate_completeness_verified IS DISTINCT FROM target_completeness
               OR report_row.proof_id IS DISTINCT FROM target_proof
               OR report_row.proof_hash IS DISTINCT FROM target_proof_hash
               OR report_row.source_scope_hash IS DISTINCT FROM target_report_scope
               OR report_row.source_scope_hash IS DISTINCT FROM target_proof_scope
               OR report_row.scorer_artifact_hash IS DISTINCT FROM scorer_row.artifact_hash
               OR report_row.base_artifact_hash IS DISTINCT FROM base_row.artifact_hash
               OR report_row.policy_version IS DISTINCT FROM target_policy_id
               OR report_row.policy_hash IS DISTINCT FROM target_policy_hash
               OR report_row.current_rights_receipt_hash IS DISTINCT FROM target_receipt_hash
               OR report_row.canonical_report::jsonb IS DISTINCT FROM report_row.report
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    report_row.canonical_report, 'UTF8')), 'hex')
                    IS DISTINCT FROM report_row.report_hash
               OR proof_row.id IS DISTINCT FROM target_proof
               OR proof_row.proof_hash IS DISTINCT FROM target_proof_hash
               OR proof_row.source_scope_hash IS DISTINCT FROM target_proof_scope
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    proof_row.canonical_proof, 'UTF8')), 'hex')
                    IS DISTINCT FROM proof_row.proof_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_REPORT_PROOF_MISMATCH';
            END IF;
            IF target_status NOT IN ('ACCEPTED_SHADOW_ONLY','ACCEPTED_PAPER_GATE_ELIGIBLE')
               OR (target_mode = 'PAPER_GATE' AND (
                    target_status <> 'ACCEPTED_PAPER_GATE_ELIGIBLE'
                    OR target_completeness IS NOT TRUE
                    OR report_row.scored_candidate_coverage_verified IS NOT TRUE
               )) THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_NOT_ELIGIBLE';
            END IF;

            SELECT rights.* INTO rights_row
              FROM public.wp06_verify_scorer_deployment_rights(
                    target_workspace, target_scorer, target_report
              ) AS rights;
            IF NOT FOUND OR rights_row.artifact_hash IS DISTINCT FROM scorer_row.artifact_hash
               OR rights_row.report_hash IS DISTINCT FROM report_row.report_hash
               OR rights_row.proof_hash IS DISTINCT FROM proof_row.proof_hash
               OR rights_row.policy_hash IS DISTINCT FROM target_policy_hash
               OR rights_row.evaluation_status IS DISTINCT FROM target_status
               OR rights_row.candidate_completeness_verified IS DISTINCT FROM target_completeness
               OR rights_row.source_scope_hash IS DISTINCT FROM target_report_scope
               OR (target_mode = 'SHADOW' AND rights_row.shadow_eligible IS NOT TRUE)
               OR (target_mode = 'PAPER_GATE' AND rights_row.paper_gate_eligible IS NOT TRUE) THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_TARGET_CURRENT_RIGHTS_DENIED';
            END IF;

            workspace_id := target_workspace;
            mode := target_mode;
            deployment_event_id := target_deployment_event_id;
            deployment_revision := target_deployment_revision;
            registry_id := target_registry;
            registry_revision := target_registry_revision;
            active_pointer_version := target_pointer_version;
            scorer_artifact_id := target_scorer;
            scorer_artifact_hash := scorer_row.artifact_hash;
            base_artifact_id := base_row.id;
            base_artifact_hash := base_row.artifact_hash;
            base_artifact_canonical := base_row.canonical_artifact;
            scorer_artifact_canonical := scorer_row.canonical_artifact;
            evaluation_report_id := target_report;
            evaluation_report_hash := report_row.report_hash;
            evaluation_status := report_row.evaluation_status;
            candidate_completeness_verified := report_row.candidate_completeness_verified;
            scored_candidate_coverage_verified := report_row.scored_candidate_coverage_verified;
            report_scope_hash := report_row.source_scope_hash;
            proof_id := proof_row.id;
            proof_hash := proof_row.proof_hash;
            proof_scope_hash := proof_row.source_scope_hash;
            policy_id := report_row.policy_version;
            policy_hash := report_row.policy_hash;
            current_rights_receipt_hash := report_row.current_rights_receipt_hash;
            source_rights_fingerprint := rights_row.fingerprint;
            RETURN NEXT;
        END $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_resolve_scorer_runtime_target(uuid, text, jsonb, uuid, integer, boolean) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_resolve_scorer_runtime_target(uuid, text, jsonb, uuid, integer, boolean) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_resolve_scorer_runtime_target(uuid, text, jsonb, uuid, integer, boolean) "
        f"TO {GUARD_ROLE}, {ADMIN}, {RUNTIME}, {MARKET_SIGNAL_WORKER}"
    )


def _replace_active_deployment_fail_closed_marker() -> None:
    op.execute("""
        DO $$
        DECLARE
            definition text;
            old_marker text := 'RAISE EXCEPTION ''WP06_SCORING_DEPLOYMENT_RIGHTS_GATE_UNAVAILABLE'';';
            new_marker text := 'PERFORM * FROM public.wp06_resolve_scorer_runtime_target('
                || 'NEW.workspace_id, NEW.mode, NEW.event_payload->''target'', '
                || 'NEW.previous_event_id, NEW.expected_deployment_revision, true);';
        BEGIN
            SELECT pg_catalog.pg_get_functiondef(
                'public.wp06_scoring_deployment_guard()'::pg_catalog.regprocedure
            ) INTO definition;
            IF pg_catalog.strpos(definition, old_marker) = 0
               OR pg_catalog.length(definition) - pg_catalog.length(
                    pg_catalog.replace(definition, old_marker, '')
                  ) <> pg_catalog.length(old_marker) THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_GUARD_MARKER_MISMATCH';
            END IF;
            definition := pg_catalog.replace(definition, old_marker, new_marker);
            EXECUTE definition;
        END $$
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE public.learning_scoring_deployment_event IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM public.learning_scoring_deployment_event
             WHERE mode IN ('SHADOW','PAPER_GATE')
        ) THEN
            RAISE EXCEPTION 'WP06_SCORER_RUNTIME_RESOLVER_DOWNGRADE_ACTIVE_DEPLOYMENT_DENIED';
        END IF;
    END $$""")
    op.execute("""
        DO $$
        DECLARE
            definition text;
            old_marker text := 'PERFORM * FROM public.wp06_resolve_scorer_runtime_target('
                || 'NEW.workspace_id, NEW.mode, NEW.event_payload->''target'', '
                || 'NEW.previous_event_id, NEW.expected_deployment_revision, true);';
            new_marker text := 'RAISE EXCEPTION ''WP06_SCORING_DEPLOYMENT_RIGHTS_GATE_UNAVAILABLE'';';
        BEGIN
            SELECT pg_catalog.pg_get_functiondef(
                'public.wp06_scoring_deployment_guard()'::pg_catalog.regprocedure
            ) INTO definition;
            IF pg_catalog.strpos(definition, old_marker) = 0 THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_GUARD_DOWNGRADE_MARKER_MISMATCH';
            END IF;
            EXECUTE pg_catalog.replace(definition, old_marker, new_marker);
        END $$
    """)
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_resolve_scorer_runtime_target(uuid, text, jsonb, uuid, integer, boolean) "
        f"FROM {GUARD_ROLE}, {ADMIN}, {RUNTIME}, {MARKET_SIGNAL_WORKER}"
    )
    op.execute(
        "DROP FUNCTION public.wp06_resolve_scorer_runtime_target(uuid, text, jsonb, uuid, integer, boolean)"
    )
    op.execute(
        f"REVOKE SELECT (workspace_id, id, attempt_id, artifact_hash, canonical_artifact, artifact) "
        f"ON public.learning_training_artifact FROM {RIGHTS_ROLE}"
    )
    op.execute(
        f"REVOKE SELECT (workspace_id, event_id, revision, active_pointer_version, mode) "
        f"ON public.learning_scoring_deployment_pointer FROM {RIGHTS_ROLE}"
    )
    op.execute(
        f"REVOKE SELECT (event_payload) "
        f"ON public.learning_scoring_deployment_event FROM {RIGHTS_ROLE}"
    )
    op.execute(
        f"REVOKE SELECT (workspace_id, id, artifact_id, artifact_hash, approval_hash, state, revision) "
        f"ON public.learning_model_registry FROM {RIGHTS_ROLE}"
    )
    op.execute(
        f"REVOKE SELECT (workspace_id, registry_id, version) "
        f"ON public.learning_model_active_pointer FROM {RIGHTS_ROLE}"
    )
    _restore_runtime_deployment_reads()
    op.execute("DROP POLICY wp06_scorer_runtime_registry_read ON public.learning_model_registry")
    op.execute(
        "DROP POLICY wp06_scorer_runtime_registry_pointer_read "
        "ON public.learning_model_active_pointer"
    )
    op.execute(
        "DROP POLICY wp06_scorer_runtime_deployment_pointer_read "
        "ON public.learning_scoring_deployment_pointer"
    )

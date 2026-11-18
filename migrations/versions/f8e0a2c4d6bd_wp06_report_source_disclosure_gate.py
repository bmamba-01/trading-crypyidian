"""WP06: kiểm lại quyền nguồn trước khi đọc report TRAINING hoặc scorer TEST."""

from collections.abc import Sequence

from alembic import op

revision: str = "f8e0a2c4d6bd"
down_revision: str | None = "f8e0a2c4d6bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RIGHTS_READER = "crypidian_learning_source_rights_reader"
EVALUATOR = "crypidian_learning_evaluator"
ADMIN = "crypidian_admin_web"
SCOPE_CHECK = "public.wp06_check_replay_training_scope"
SCOPE_CHECK_REGPROCEDURE = (
    "public.wp06_check_replay_training_scope(uuid,text,jsonb,uuid,text,text,uuid,boolean)"
)
_EVALUATION_GUARD = (
    "$old_guard$OR target_access_kind NOT IN ('normalization', 'evaluation', 'training')$old_guard$"
)
_REPORT_READ_GUARD = (
    "$new_guard$OR (target_access_kind = 'report_read' AND "
    "session_user <> 'crypidian_admin_web')\n"
    "               OR target_access_kind NOT IN ('normalization', "
    "'evaluation', 'training', 'report_read')$new_guard$"
)
_REPORT_READ_GUARD_TO_REVERT = (
    "$old_guard$OR (target_access_kind = 'report_read' AND "
    "session_user <> 'crypidian_admin_web')\n"
    "               OR target_access_kind NOT IN ('normalization', "
    "'evaluation', 'training', 'report_read')$old_guard$"
)
_EVALUATION_GUARD_TO_RESTORE = (
    "$new_guard$OR target_access_kind NOT IN ('normalization', 'evaluation', 'training')$new_guard$"
)
_EVALUATION_CASE = "$old_case$CASE WHEN target_access_kind = 'evaluation'$old_case$"
_EVALUATION_REPORT_CASE = (
    "$new_case$CASE WHEN target_access_kind IN ('evaluation', 'report_read')$new_case$"
)
_EVALUATION_REPORT_CASE_TO_REVERT = (
    "$old_case$CASE WHEN target_access_kind IN ('evaluation', 'report_read')$old_case$"
)
_EVALUATION_CASE_TO_RESTORE = "$new_case$CASE WHEN target_access_kind = 'evaluation'$new_case$"
RIGHTS_READER_COLUMNS = {
    "learning_evaluation_report": (
        "workspace_id,id,idempotency_key,report_hash,manifest_hash,fold_plan_hash,"
        "split_policy_version,evaluation_mode,candidate_completeness_verified"
    ),
    "learning_evaluation_access_receipt": (
        "workspace_id,id,evaluation_run_id,manifest_hash,split_policy_version,"
        "fold_plan_hash,approval_hash,request_fingerprint,purpose,actor_principal"
    ),
    "learning_scorer_test_evaluation_report": (
        "workspace_id,id,evaluation_run_id,access_receipt_id,request_fingerprint,"
        "current_rights_receipt_hash,proof_id"
    ),
    "learning_scorer_test_access_receipt": (
        "workspace_id,id,evaluation_run_id,proof_id,normalized_dataset_id,manifest_hash,"
        "fold_plan_hash,request_fingerprint,current_rights_receipt_hash,purpose,actor_principal"
    ),
}
WORKSPACE_POLICY = (
    "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
)


def upgrade() -> None:
    _extend_scope_check_for_report_read()
    _grant_rights_reader_report_metadata()
    _create_evaluation_report_rights_recheck()
    _create_scorer_report_rights_recheck()


def _extend_scope_check_for_report_read() -> None:
    """Cho phép admin dùng cùng scope checker cho operation aggregate report."""
    op.execute(f"""
        DO $$
        DECLARE
            function_definition text;
            old_guard text := {_EVALUATION_GUARD};
            new_guard text := {_REPORT_READ_GUARD};
            old_case text := {_EVALUATION_CASE};
            new_case text := {_EVALUATION_REPORT_CASE};
            replaced_guard boolean;
            case_count integer;
        BEGIN
            SELECT pg_catalog.pg_get_functiondef(
                '{SCOPE_CHECK_REGPROCEDURE}'::regprocedure
            ) INTO function_definition;
            IF function_definition IS NULL
               OR pg_catalog.strpos(function_definition, 'report_read') > 0
               OR pg_catalog.strpos(function_definition, old_guard) = 0 THEN
                RAISE EXCEPTION 'WP06_REPORT_SCOPE_CHECK_SOURCE_MISMATCH';
            END IF;
            function_definition := pg_catalog.replace(function_definition, old_guard, new_guard);
            replaced_guard := pg_catalog.strpos(function_definition, new_guard) > 0;
            case_count := (pg_catalog.length(function_definition)
                - pg_catalog.length(pg_catalog.replace(function_definition, old_case, '')))
                / pg_catalog.length(old_case);
            IF NOT replaced_guard OR case_count <> 2 THEN
                RAISE EXCEPTION 'WP06_REPORT_SCOPE_CHECK_SOURCE_MISMATCH';
            END IF;
            function_definition := pg_catalog.replace(function_definition, old_case, new_case);
            EXECUTE function_definition;
        END $$
    """)


def _grant_rights_reader_report_metadata() -> None:
    """Cho phép rights reader kiểm metadata theo workspace, không đọc payload report."""
    for table, columns in RIGHTS_READER_COLUMNS.items():
        op.execute(f"""
            CREATE POLICY wp06_report_scope_rights_reader_select
            ON public.{table} FOR SELECT TO {RIGHTS_READER}
            USING ({WORKSPACE_POLICY})
        """)
        op.execute(f"GRANT SELECT ({columns}) ON public.{table} TO {RIGHTS_READER}")


def _create_evaluation_report_rights_recheck() -> None:
    op.execute(f"""
        CREATE FUNCTION public.wp06_recheck_learning_evaluation_report_rights(
            target_workspace uuid,
            target_evaluation_run_id uuid,
            target_request_fingerprint text
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        #variable_conflict use_variable
        DECLARE
            access_row record;
            report_row record;
            stored_manifest jsonb;
            normalized_dataset_id uuid;
        BEGIN
            IF session_user <> '{EVALUATOR}' THEN
                RAISE EXCEPTION 'WP06_EVALUATION_REPORT_CALLER_DENIED';
            END IF;
            IF target_workspace IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_EVALUATION_REPORT_WORKSPACE_DENIED';
            END IF;
            IF target_request_fingerprint !~ '^[0-9a-f]{{64}}$' THEN
                RAISE EXCEPTION 'WP06_EVALUATION_REPORT_FINGERPRINT_INVALID';
            END IF;

            SELECT access.workspace_id, access.id, access.evaluation_run_id,
                   access.manifest_hash, access.split_policy_version,
                   access.fold_plan_hash, access.approval_hash,
                   access.request_fingerprint, access.purpose, access.actor_principal
              INTO access_row
              FROM public.learning_evaluation_access_receipt AS access
             WHERE access.workspace_id = target_workspace
               AND access.evaluation_run_id = target_evaluation_run_id
               AND access.request_fingerprint = target_request_fingerprint
               AND access.purpose = 'diagnostic-evaluation'
               AND access.actor_principal = '{EVALUATOR}';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_EVALUATION_REPORT_RECEIPT_NOT_FOUND';
            END IF;

            SELECT report.workspace_id, report.idempotency_key,
                   report.report_hash, report.manifest_hash,
                   report.fold_plan_hash, report.split_policy_version,
                   report.evaluation_mode, report.candidate_completeness_verified
              INTO report_row
              FROM public.learning_evaluation_report AS report
             WHERE report.workspace_id = target_workspace
               AND report.idempotency_key = target_evaluation_run_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_EVALUATION_REPORT_RECEIPT_MISMATCH';
            END IF;
            IF report_row.manifest_hash IS DISTINCT FROM access_row.manifest_hash
               OR report_row.fold_plan_hash IS DISTINCT FROM access_row.fold_plan_hash
               OR report_row.split_policy_version IS DISTINCT FROM access_row.split_policy_version
               OR report_row.evaluation_mode <> 'diagnostic'
               OR report_row.candidate_completeness_verified IS DISTINCT FROM false THEN
                RAISE EXCEPTION 'WP06_EVALUATION_REPORT_RECEIPT_MISMATCH';
            END IF;

            SELECT manifest.manifest INTO stored_manifest
              FROM public.dataset_manifest AS manifest
             WHERE manifest.workspace_id = target_workspace
               AND manifest.manifest_hash = access_row.manifest_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_EVALUATION_REPORT_MANIFEST_MISSING';
            END IF;
            SELECT dataset.id INTO normalized_dataset_id
              FROM public.learning_normalized_dataset AS dataset
             WHERE dataset.workspace_id = target_workspace
               AND dataset.manifest_hash = access_row.manifest_hash
               AND dataset.split_policy_version = access_row.split_policy_version
               AND dataset.fold_plan_hash = access_row.fold_plan_hash
               AND dataset.approval_hash = access_row.approval_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_EVALUATION_REPORT_DATASET_MISSING';
            END IF;

            PERFORM {SCOPE_CHECK}(
                target_workspace, access_row.manifest_hash, stored_manifest,
                normalized_dataset_id, access_row.fold_plan_hash,
                'evaluation', access_row.evaluation_run_id, false
            );
        END $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_recheck_learning_evaluation_report_rights(uuid,uuid,text) "
        f"OWNER TO {RIGHTS_READER}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.wp06_recheck_learning_evaluation_report_rights(uuid,uuid,text) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.wp06_recheck_learning_evaluation_report_rights(uuid,uuid,text) "
        f"TO {EVALUATOR}"
    )


def _create_scorer_report_rights_recheck() -> None:
    op.execute(f"""
        CREATE FUNCTION public.wp06_recheck_scorer_test_report_source_rights(
            target_workspace uuid,
            target_report_id uuid
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        #variable_conflict use_variable
        DECLARE
            report_row record;
            stored_manifest jsonb;
            target_access_kind text;
            target_access_id uuid;
        BEGIN
            IF session_user <> '{ADMIN}' AND session_user <> '{EVALUATOR}' THEN
                RAISE EXCEPTION 'WP06_SCORER_REPORT_READ_CALLER_DENIED';
            END IF;
            IF target_workspace IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SCORER_REPORT_READ_WORKSPACE_DENIED';
            END IF;
            IF target_report_id IS NULL THEN
                RAISE EXCEPTION 'WP06_SCORER_REPORT_READ_ID_INVALID';
            END IF;

            SELECT report.workspace_id, report.id, report.evaluation_run_id,
                   report.access_receipt_id, report.request_fingerprint,
                   report.current_rights_receipt_hash, report.proof_id,
                   access.normalized_dataset_id, access.manifest_hash,
                   access.fold_plan_hash, access.proof_id AS receipt_proof_id,
                   access.request_fingerprint AS receipt_request_fingerprint,
                   access.current_rights_receipt_hash AS receipt_rights_hash,
                   access.purpose, access.actor_principal
              INTO report_row
              FROM public.learning_scorer_test_evaluation_report AS report
              JOIN public.learning_scorer_test_access_receipt AS access
                ON access.workspace_id = report.workspace_id
               AND access.id = report.access_receipt_id
               AND access.evaluation_run_id = report.evaluation_run_id
               AND access.proof_id = report.proof_id
             WHERE report.workspace_id = target_workspace
               AND report.id = target_report_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_REPORT_READ_RECEIPT_MISMATCH';
            END IF;
            IF report_row.request_fingerprint IS DISTINCT FROM
                    report_row.receipt_request_fingerprint
               OR report_row.current_rights_receipt_hash IS DISTINCT FROM
                    report_row.receipt_rights_hash
               OR report_row.proof_id IS DISTINCT FROM report_row.receipt_proof_id
               OR report_row.purpose <> 'scorer-test'
               OR report_row.actor_principal <> '{EVALUATOR}' THEN
                RAISE EXCEPTION 'WP06_SCORER_REPORT_READ_RECEIPT_MISMATCH';
            END IF;

            IF session_user = '{EVALUATOR}' THEN
                target_access_kind := 'evaluation';
                target_access_id := report_row.evaluation_run_id;
            ELSE
                target_access_kind := 'report_read';
                target_access_id := target_report_id;
            END IF;

            SELECT manifest.manifest INTO stored_manifest
              FROM public.dataset_manifest AS manifest
             WHERE manifest.workspace_id = target_workspace
               AND manifest.manifest_hash = report_row.manifest_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_REPORT_READ_MANIFEST_MISSING';
            END IF;

            PERFORM {SCOPE_CHECK}(
                target_workspace, report_row.manifest_hash, stored_manifest,
                report_row.normalized_dataset_id, report_row.fold_plan_hash,
                target_access_kind, target_access_id, false
            );
        END $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_recheck_scorer_test_report_source_rights(uuid,uuid) "
        f"OWNER TO {RIGHTS_READER}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_recheck_scorer_test_report_source_rights(uuid,uuid) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_recheck_scorer_test_report_source_rights(uuid,uuid) "
        f"TO {ADMIN}, {EVALUATOR}"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_recheck_scorer_test_report_source_rights(uuid,uuid) "
        f"FROM PUBLIC, {ADMIN}"
    )
    op.execute("DROP FUNCTION public.wp06_recheck_scorer_test_report_source_rights(uuid,uuid)")
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.wp06_recheck_learning_evaluation_report_rights(uuid,uuid,text) "
        f"FROM PUBLIC, {EVALUATOR}"
    )
    op.execute(
        "DROP FUNCTION public.wp06_recheck_learning_evaluation_report_rights(uuid,uuid,text)"
    )
    for table, columns in RIGHTS_READER_COLUMNS.items():
        op.execute(f"DROP POLICY wp06_report_scope_rights_reader_select ON public.{table}")
        op.execute(f"REVOKE SELECT ({columns}) ON public.{table} FROM {RIGHTS_READER}")
    _restore_scope_check_before_report_read()


def _restore_scope_check_before_report_read() -> None:
    op.execute(f"""
        DO $$
        DECLARE
            function_definition text;
            old_guard text := {_REPORT_READ_GUARD_TO_REVERT};
            new_guard text := {_EVALUATION_GUARD_TO_RESTORE};
            old_case text := {_EVALUATION_REPORT_CASE_TO_REVERT};
            new_case text := {_EVALUATION_CASE_TO_RESTORE};
            case_count integer;
        BEGIN
            SELECT pg_catalog.pg_get_functiondef(
                '{SCOPE_CHECK_REGPROCEDURE}'::regprocedure
            ) INTO function_definition;
            IF function_definition IS NULL
               OR pg_catalog.strpos(function_definition, old_guard) = 0 THEN
                RAISE EXCEPTION 'WP06_REPORT_SCOPE_CHECK_DOWNGRADE_SOURCE_MISMATCH';
            END IF;
            case_count := (pg_catalog.length(function_definition)
                - pg_catalog.length(pg_catalog.replace(function_definition, old_case, '')))
                / pg_catalog.length(old_case);
            IF case_count <> 2 THEN
                RAISE EXCEPTION 'WP06_REPORT_SCOPE_CHECK_DOWNGRADE_SOURCE_MISMATCH';
            END IF;
            function_definition := pg_catalog.replace(function_definition, old_guard, new_guard);
            function_definition := pg_catalog.replace(function_definition, old_case, new_case);
            EXECUTE function_definition;
        END $$
    """)

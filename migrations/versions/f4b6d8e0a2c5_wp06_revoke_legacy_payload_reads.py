"""WP06: thu hồi SELECT legacy có feature/label khỏi admin, app và trainer."""

from collections.abc import Sequence

from alembic import op

revision: str = "f4b6d8e0a2c5"
down_revision: str | None = "f2a4c6e8b0d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
APP = "crypidian_app"
TRAINER = "crypidian_trainer_worker"
BUILDER = "crypidian_candidate_builder"
EVALUATOR = "crypidian_learning_evaluator"
COMPARATOR = "crypidian_manifest_comparator"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
OUTCOME_LINEAGE = (
    "backtest_run",
    "backtest_trial",
    "backtest_report",
    "backtest_scenario",
    "backtest_trade_outcome",
)
MANIFEST_METADATA_COLUMNS = (
    "workspace_id",
    "id",
    "created_at",
    "manifest_hash",
    "query_hash",
    "manifest_version",
    "data_start",
    "data_end",
    "as_of",
    "feature_schema_version",
    "feature_version",
    "label_version",
    "sample_schema_version",
    "split_policy_version",
    "sample_count",
    "exclusion_count",
    "holdout_sealed_at",
    "retention_policy_ref",
)
COMPARE_FUNCTION_NAME = "public.wp06_manifest_payload_matches"
COMPARE_FUNCTION = f"{COMPARE_FUNCTION_NAME}(uuid, text, text)"


def _create_select_policy(table: str, policy: str) -> None:
    op.execute(
        f"CREATE POLICY {policy} ON public.{table} FOR SELECT TO {EVALUATOR} USING ({WORKSPACE})"
    )


def upgrade() -> None:
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE rolname='{EVALUATOR}'
            AND rolcanlogin AND NOT (rolsuper OR rolbypassrls OR rolinherit OR rolcreatedb
            OR rolcreaterole OR rolreplication)
            AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
                WHERE m.roleid=r.oid OR m.member=r.oid)) THEN
            RAISE EXCEPTION 'WP06_EVALUATOR_ROLE_UNSAFE';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{COMPARATOR}') THEN
            CREATE ROLE {COMPARATOR} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSIF EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE rolname='{COMPARATOR}' AND
            (rolcanlogin OR rolsuper OR rolbypassrls OR rolinherit OR rolcreatedb OR
            rolcreaterole OR rolreplication OR EXISTS (SELECT 1 FROM
                pg_catalog.pg_auth_members m WHERE m.roleid=r.oid OR m.member=r.oid))) THEN
            RAISE EXCEPTION 'WP06_MANIFEST_COMPARATOR_ROLE_UNSAFE';
        END IF;
    END $$""")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {EVALUATOR}, {COMPARATOR}")

    # App/admin chỉ cần tra metadata; payload có label chỉ dành builder/evaluator.
    op.execute('DROP POLICY workspace_isolation ON public."dataset_manifest"')
    op.execute(
        f'CREATE POLICY dataset_manifest_metadata_read ON public."dataset_manifest" '
        f"FOR SELECT TO {APP}, {ADMIN} USING ({WORKSPACE})"
    )
    op.execute(
        f'CREATE POLICY dataset_manifest_builder_select ON public."dataset_manifest" '
        f"FOR SELECT TO {BUILDER} USING ({WORKSPACE})"
    )
    op.execute(
        f'CREATE POLICY dataset_manifest_evaluator_select ON public."dataset_manifest" '
        f"FOR SELECT TO {EVALUATOR} USING ({WORKSPACE})"
    )
    op.execute(
        f'CREATE POLICY dataset_manifest_admin_insert ON public."dataset_manifest" '
        f"FOR INSERT TO {ADMIN} WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f'CREATE POLICY dataset_manifest_comparator_select ON public."dataset_manifest" '
        f"FOR SELECT TO {COMPARATOR} USING ({WORKSPACE})"
    )
    op.execute(
        f'REVOKE SELECT ON TABLE public."dataset_manifest" FROM PUBLIC, {ADMIN}, {APP}, '
        f"{TRAINER}, {BUILDER}, {EVALUATOR}"
    )
    op.execute(
        f'REVOKE SELECT (manifest, canonical_manifest) ON TABLE public."dataset_manifest" '
        f"FROM PUBLIC, {ADMIN}, {APP}, {TRAINER}"
    )
    op.execute(
        f"GRANT SELECT ({', '.join(MANIFEST_METADATA_COLUMNS)}) "
        f'ON TABLE public."dataset_manifest" TO {ADMIN}, {APP}'
    )
    op.execute(f'GRANT INSERT ON TABLE public."dataset_manifest" TO {ADMIN}')
    op.execute(f'GRANT SELECT ON TABLE public."dataset_manifest" TO {BUILDER}, {EVALUATOR}')
    op.execute(
        f"GRANT SELECT (workspace_id, manifest_hash, canonical_manifest) "
        f'ON TABLE public."dataset_manifest" TO {COMPARATOR}'
    )
    op.execute(f'GRANT SELECT (id, manifest) ON TABLE public."dataset_manifest" TO {COMPARATOR}')

    # Retry của admin chỉ nhận một bit so bytes canonical; không trả payload cũ.
    op.execute(f"""CREATE FUNCTION {COMPARE_FUNCTION_NAME}
        (target_workspace uuid, target_hash text, candidate_payload text)
        RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE stored_payload text;
        BEGIN
            IF session_user NOT IN ('{ADMIN}', '{BUILDER}')
                AND current_setting('role', true) NOT IN ('{ADMIN}', '{BUILDER}') THEN
                RAISE EXCEPTION 'WP06_MANIFEST_COMPARE_CALLER_DENIED';
            END IF;
            IF target_workspace IS DISTINCT FROM
                NULLIF(current_setting('app.workspace_id', true), '')::uuid THEN
                RETURN false;
            END IF;
            SELECT canonical_manifest INTO stored_payload
              FROM public.dataset_manifest
             WHERE workspace_id = target_workspace AND manifest_hash = target_hash;
            RETURN FOUND AND stored_payload = candidate_payload;
        END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION {COMPARE_FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT CREATE ON SCHEMA public TO {COMPARATOR}")
    op.execute(f"ALTER FUNCTION {COMPARE_FUNCTION} OWNER TO {COMPARATOR}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {COMPARATOR}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {COMPARE_FUNCTION} TO {ADMIN}, {BUILDER}")

    # Trigger lineage vẫn xác minh payload nhưng chỉ hàm no-login đọc được manifest.
    op.execute(f"GRANT CREATE ON SCHEMA public TO {COMPARATOR}")
    op.execute("ALTER FUNCTION public.enforce_dataset_manifest_source_ref() SECURITY DEFINER")
    op.execute(f"ALTER FUNCTION public.enforce_dataset_manifest_source_ref() OWNER TO {COMPARATOR}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {COMPARATOR}")

    # Raw outcome giữ quyền persist tách riêng; app và trainer không đọc được feature/label.
    op.execute(
        f"REVOKE SELECT ON TABLE public.backtest_trade_outcome FROM PUBLIC, "
        f"{APP}, {ADMIN}, {TRAINER}"
    )
    op.execute(
        "REVOKE SELECT (outcome) ON TABLE public.backtest_trade_outcome "
        f"FROM PUBLIC, {APP}, {ADMIN}, {TRAINER}"
    )
    for table in OUTCOME_LINEAGE:
        _create_select_policy(table, f"learning_evaluator_{table}_select")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {EVALUATOR}")

    # Verifier/report chẩn đoán legacy chỉ chạy dưới evaluator process cô lập.
    op.execute(
        "DROP POLICY learning_evaluation_report_trainer_select "
        'ON public."learning_evaluation_report"'
    )
    op.execute(
        "DROP POLICY learning_evaluation_report_trainer_insert "
        'ON public."learning_evaluation_report"'
    )
    op.execute(
        f"CREATE POLICY learning_evaluation_report_evaluator_select "
        f'ON public."learning_evaluation_report" FOR SELECT TO {EVALUATOR} USING ({WORKSPACE})'
    )
    op.execute(
        f"CREATE POLICY learning_evaluation_report_evaluator_insert "
        f'ON public."learning_evaluation_report" FOR INSERT TO {EVALUATOR} '
        f"WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        'REVOKE SELECT, INSERT ON TABLE public."learning_evaluation_report" '
        f"FROM {TRAINER}, {EVALUATOR}"
    )
    op.execute(f'GRANT SELECT, INSERT ON TABLE public."learning_evaluation_report" TO {EVALUATOR}')


def downgrade() -> None:
    op.execute(
        "DROP POLICY learning_evaluation_report_evaluator_insert "
        'ON public."learning_evaluation_report"'
    )
    op.execute(
        "DROP POLICY learning_evaluation_report_evaluator_select "
        'ON public."learning_evaluation_report"'
    )
    op.execute(
        "CREATE POLICY learning_evaluation_report_trainer_select "
        'ON public."learning_evaluation_report" FOR SELECT TO crypidian_trainer_worker '
        f"USING ({WORKSPACE})"
    )
    op.execute(
        "CREATE POLICY learning_evaluation_report_trainer_insert "
        'ON public."learning_evaluation_report" FOR INSERT TO crypidian_trainer_worker '
        f"WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f'REVOKE ALL PRIVILEGES ON TABLE public."learning_evaluation_report" FROM {EVALUATOR}'
    )
    op.execute(
        'GRANT SELECT, INSERT ON TABLE public."learning_evaluation_report" '
        "TO crypidian_trainer_worker"
    )

    op.execute(f"GRANT CREATE ON SCHEMA public TO {COMPARATOR}")
    op.execute(
        "ALTER FUNCTION public.enforce_dataset_manifest_source_ref() OWNER TO crypidian_admin"
    )
    op.execute("ALTER FUNCTION public.enforce_dataset_manifest_source_ref() SECURITY INVOKER")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {COMPARATOR}")

    for table in reversed(OUTCOME_LINEAGE):
        op.execute(f"DROP POLICY learning_evaluator_{table}_select ON public.{table}")
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {EVALUATOR}")
    op.execute(f"GRANT SELECT ON TABLE public.backtest_trade_outcome TO {ADMIN}, {TRAINER}")
    op.execute(
        'REVOKE SELECT (manifest, canonical_manifest) ON TABLE public."dataset_manifest" '
        f"FROM {ADMIN}, {APP}"
    )

    op.execute(f"REVOKE EXECUTE ON FUNCTION {COMPARE_FUNCTION} FROM {ADMIN}, {BUILDER}")
    op.execute(f"DROP FUNCTION {COMPARE_FUNCTION}")
    op.execute(
        "REVOKE SELECT (workspace_id, id, manifest, manifest_hash, canonical_manifest) "
        f'ON TABLE public."dataset_manifest" FROM {COMPARATOR}'
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM " + EVALUATOR + ", " + COMPARATOR)
    op.execute('DROP POLICY dataset_manifest_comparator_select ON public."dataset_manifest"')
    op.execute('DROP POLICY dataset_manifest_admin_insert ON public."dataset_manifest"')
    op.execute('DROP POLICY dataset_manifest_evaluator_select ON public."dataset_manifest"')
    op.execute('DROP POLICY dataset_manifest_builder_select ON public."dataset_manifest"')
    op.execute('DROP POLICY dataset_manifest_metadata_read ON public."dataset_manifest"')
    op.execute(
        'CREATE POLICY workspace_isolation ON public."dataset_manifest" '
        f"TO {APP}, {ADMIN}, {TRAINER} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        'REVOKE ALL PRIVILEGES ON TABLE public."dataset_manifest" '
        f"FROM {ADMIN}, {APP}, {TRAINER}, {BUILDER}, {EVALUATOR}"
    )
    op.execute('GRANT SELECT, INSERT ON TABLE public."dataset_manifest" TO ' + ADMIN)
    op.execute('GRANT SELECT ON TABLE public."dataset_manifest" TO ' + TRAINER)
    op.execute('GRANT SELECT ON TABLE public."dataset_manifest" TO ' + APP)
    op.execute('GRANT SELECT ON TABLE public."dataset_manifest" TO ' + BUILDER)
    op.execute(f"DROP ROLE {COMPARATOR}")

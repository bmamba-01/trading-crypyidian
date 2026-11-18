"""Gộp revision WP06 với migration sự kiện thị trường live."""

from collections.abc import Sequence

from alembic import op

revision: str = "a9c7e5f3d1b2"
down_revision: tuple[str, str] = ("e2f4a6c8b0d7", "b4d6f8a0c2e5")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
INCIDENT_GUARD_ROLE = "crypidian_learning_incident_guard"
INCIDENT_ELIGIBILITY_FUNCTION = "public.wp06_learning_incident_eligible_target(uuid,uuid)"
LOCK_FUNCTION = "public.wp06_lock_learning_incident_scope(uuid,uuid)"
COHORT_LOCK_FUNCTION = "public.wp06_lock_learning_shadow_cohort(uuid,uuid,integer)"
SHADOW_SCOPE_GUARD_FUNCTION = "public.wp06_require_shadow_exact_source_scope()"
INCIDENT_GUARD_FUNCTION = "public.wp06_learning_incident_guard()"


def upgrade() -> None:
    """Gộp schema và chuyển các trigger gateway khỏi owner superuser."""
    op.execute(f"""DO $$ DECLARE role_state record; BEGIN
        SELECT candidate.rolcanlogin, candidate.rolsuper, candidate.rolbypassrls,
               candidate.rolinherit, candidate.rolcreatedb, candidate.rolcreaterole,
               candidate.rolreplication,
               EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members membership
                        WHERE membership.roleid = candidate.oid
                           OR membership.member = candidate.oid) AS has_membership
          INTO role_state
          FROM pg_catalog.pg_roles candidate
         WHERE candidate.rolname = '{RIGHTS_ROLE}';
        IF NOT FOUND OR role_state.rolcanlogin OR role_state.rolsuper
           OR role_state.rolbypassrls OR role_state.rolinherit OR role_state.rolcreatedb
           OR role_state.rolcreaterole OR role_state.rolreplication OR role_state.has_membership
        THEN RAISE EXCEPTION 'WP06_INCIDENT_RIGHTS_ROLE_UNSAFE'; END IF;
    END $$""")
    op.execute(
        "GRANT SELECT (active_registry_id,active_registry_revision,active_artifact_hash) "
        f"ON public.learning_shadow_observation TO {RIGHTS_ROLE}"
    )
    op.execute(
        f"GRANT SELECT (membership_digest) ON public.learning_drift_assessment TO {RIGHTS_ROLE}"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {LOCK_FUNCTION} TO {RIGHTS_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {COHORT_LOCK_FUNCTION} TO {RIGHTS_ROLE}")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_shadow_drift_decision_source_ids(uuid,uuid) "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_authorize_shadow_inference_source_scope"
        f"(uuid,uuid,timestamptz) TO {RIGHTS_ROLE}"
    )
    _restrict_incident_eligibility_projection()

    _create_incident_guard_role()
    _grant_incident_guard_access()

    op.execute(f"GRANT CREATE ON SCHEMA public TO {RIGHTS_ROLE}")
    op.execute(f"GRANT CREATE ON SCHEMA public TO {INCIDENT_GUARD_ROLE}")
    op.execute(f"ALTER FUNCTION {INCIDENT_ELIGIBILITY_FUNCTION} OWNER TO {RIGHTS_ROLE}")
    op.execute(f"ALTER FUNCTION {LOCK_FUNCTION} OWNER TO {RIGHTS_ROLE}")
    op.execute(f"ALTER FUNCTION {SHADOW_SCOPE_GUARD_FUNCTION} OWNER TO {RIGHTS_ROLE}")
    op.execute(f"ALTER FUNCTION {INCIDENT_GUARD_FUNCTION} OWNER TO {INCIDENT_GUARD_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {RIGHTS_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {INCIDENT_GUARD_ROLE}")


def downgrade() -> None:
    """Khôi phục owner/grants trước revision gộp khi thực hiện downgrade."""
    op.execute(f"GRANT CREATE ON SCHEMA public TO {INCIDENT_GUARD_ROLE}")
    op.execute(f"GRANT CREATE ON SCHEMA public TO {RIGHTS_ROLE}")
    op.execute(f"ALTER FUNCTION {INCIDENT_GUARD_FUNCTION} OWNER TO crypidian_admin")
    op.execute(f"ALTER FUNCTION {SHADOW_SCOPE_GUARD_FUNCTION} OWNER TO crypidian_admin")
    op.execute(f"ALTER FUNCTION {LOCK_FUNCTION} OWNER TO crypidian_admin")
    op.execute(f"ALTER FUNCTION {INCIDENT_ELIGIBILITY_FUNCTION} OWNER TO crypidian_admin")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {INCIDENT_GUARD_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {RIGHTS_ROLE}")
    _revoke_incident_guard_access()
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_authorize_shadow_inference_source_scope"
        f"(uuid,uuid,timestamptz) FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_shadow_drift_decision_source_ids(uuid,uuid) "
        f"FROM {RIGHTS_ROLE}"
    )
    op.execute(f"REVOKE EXECUTE ON FUNCTION {LOCK_FUNCTION} FROM {RIGHTS_ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {COHORT_LOCK_FUNCTION} FROM {RIGHTS_ROLE}")
    op.execute(
        "REVOKE SELECT (active_registry_id,active_registry_revision,active_artifact_hash) "
        f"ON public.learning_shadow_observation FROM {RIGHTS_ROLE}"
    )
    op.execute(
        f"REVOKE SELECT (membership_digest) ON public.learning_drift_assessment FROM {RIGHTS_ROLE}"
    )


def _create_incident_guard_role() -> None:
    """Tạo role nội bộ không đăng nhập cho trigger kiểm tra ledger MFA."""
    op.execute(f"""DO $$ DECLARE role_state record; BEGIN
        SELECT candidate.rolcanlogin, candidate.rolsuper, candidate.rolbypassrls,
               candidate.rolinherit, candidate.rolcreatedb, candidate.rolcreaterole,
               candidate.rolreplication,
               EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members membership
                        WHERE membership.roleid = candidate.oid
                           OR membership.member = candidate.oid) AS has_membership
          INTO role_state
          FROM pg_catalog.pg_roles candidate
         WHERE candidate.rolname = '{INCIDENT_GUARD_ROLE}';
        IF NOT FOUND THEN
            CREATE ROLE {INCIDENT_GUARD_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSIF role_state.rolcanlogin OR role_state.rolsuper OR role_state.rolbypassrls
           OR role_state.rolinherit OR role_state.rolcreatedb OR role_state.rolcreaterole
           OR role_state.rolreplication OR role_state.has_membership THEN
            RAISE EXCEPTION 'WP06_INCIDENT_GUARD_ROLE_UNSAFE';
        END IF;
    END $$""")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {INCIDENT_GUARD_ROLE}")


def _restrict_incident_eligibility_projection() -> None:
    """Giữ truy vấn SECURITY DEFINER trong các cột đã cấp cho role quyền đọc."""
    op.execute("""
        DO $$ DECLARE
            function_ddl text;
            function_ddl_normalized text;
            already_projected boolean;
            proposal_projection text :=
                'SELECT p.workspace_id, p.id, p.assessment_id, p.assessment_hash, '
                'p.membership_digest, p.policy_version INTO proposal '
                'FROM public.learning_retrain_proposal';
            assessment_projection text :=
                'SELECT a.workspace_id, a.id, a.content_hash, a.membership_digest, '
                'a.policy_version, a.data_quality_status, a.reason_codes, a.member_count, '
                'a.cohort_registry_id, a.cohort_revision, a.created_at INTO assessment '
                'FROM public.learning_drift_assessment';
            registry_projection text :=
                'SELECT r.workspace_id, r.id, r.artifact_hash INTO registry '
                'FROM public.learning_model_registry';
            proposal_projection_found boolean;
            assessment_projection_found boolean;
            registry_projection_found boolean;
        BEGIN
            SELECT pg_catalog.pg_get_functiondef(
                'public.wp06_learning_incident_eligible_target(uuid,uuid)'::regprocedure)
              INTO function_ddl;
            IF function_ddl IS NULL THEN
                RAISE EXCEPTION 'WP06_INCIDENT_ELIGIBILITY_PROJECTION_NOT_FOUND';
            END IF;

            function_ddl_normalized := pg_catalog.regexp_replace(
                function_ddl, '[[:space:]]+', ' ', 'g');
            proposal_projection_found :=
                pg_catalog.strpos(function_ddl_normalized,
                    proposal_projection || ' AS p') > 0
                OR pg_catalog.strpos(function_ddl_normalized,
                    proposal_projection || ' p') > 0;
            assessment_projection_found :=
                pg_catalog.strpos(function_ddl_normalized,
                    assessment_projection || ' AS a') > 0
                OR pg_catalog.strpos(function_ddl_normalized,
                    assessment_projection || ' a') > 0;
            registry_projection_found :=
                pg_catalog.strpos(function_ddl_normalized,
                    registry_projection || ' AS r') > 0
                OR pg_catalog.strpos(function_ddl_normalized,
                    registry_projection || ' r') > 0;
            already_projected :=
                pg_catalog.strpos(function_ddl_normalized, 'proposal record;') > 0
                AND pg_catalog.strpos(function_ddl_normalized, 'assessment record;') > 0
                AND pg_catalog.strpos(function_ddl_normalized, 'registry record;') > 0
                AND proposal_projection_found
                AND assessment_projection_found
                AND registry_projection_found
                AND pg_catalog.strpos(function_ddl_normalized, 'SELECT * INTO proposal') = 0
                AND pg_catalog.strpos(function_ddl_normalized, 'SELECT * INTO assessment') = 0
                AND pg_catalog.strpos(function_ddl_normalized, 'SELECT * INTO registry') = 0
                AND (pg_catalog.length(function_ddl_normalized) - pg_catalog.length(
                    pg_catalog.regexp_replace(
                        function_ddl_normalized, 'INTO proposal[[:space:];]', '', 'g')))
                    = pg_catalog.length('INTO proposal ')
                AND (pg_catalog.length(function_ddl_normalized) - pg_catalog.length(
                    pg_catalog.regexp_replace(
                        function_ddl_normalized, 'INTO assessment[[:space:];]', '', 'g')))
                    = pg_catalog.length('INTO assessment ')
                AND (pg_catalog.length(function_ddl_normalized) - pg_catalog.length(
                    pg_catalog.regexp_replace(
                        function_ddl_normalized, 'INTO registry[[:space:];]', '', 'g')))
                    = pg_catalog.length('INTO registry ');

            IF NOT already_projected THEN
                IF (pg_catalog.strpos(function_ddl,
                        'proposal public.learning_retrain_proposal' || pg_catalog.chr(37)
                        || 'ROWTYPE;') = 0
                    AND pg_catalog.strpos(function_ddl, 'proposal record;') = 0)
                   OR (pg_catalog.strpos(function_ddl,
                        'assessment public.learning_drift_assessment' || pg_catalog.chr(37)
                        || 'ROWTYPE;') = 0
                    AND pg_catalog.strpos(function_ddl, 'assessment record;') = 0)
                   OR (pg_catalog.strpos(function_ddl,
                        'registry public.learning_model_registry' || pg_catalog.chr(37)
                        || 'ROWTYPE;') = 0
                    AND pg_catalog.strpos(function_ddl, 'registry record;') = 0)
                   OR ((pg_catalog.strpos(function_ddl_normalized,
                        'SELECT * INTO proposal FROM public.learning_retrain_proposal AS p'
                        ) = 0
                        AND pg_catalog.strpos(function_ddl_normalized,
                        'SELECT * INTO proposal FROM public.learning_retrain_proposal p') = 0)
                    AND NOT proposal_projection_found)
                   OR ((pg_catalog.strpos(function_ddl_normalized,
                        'SELECT * INTO assessment FROM public.learning_drift_assessment AS a') = 0
                        AND pg_catalog.strpos(function_ddl_normalized,
                        'SELECT * INTO assessment FROM public.learning_drift_assessment a') = 0)
                    AND NOT assessment_projection_found)
                   OR ((pg_catalog.strpos(function_ddl_normalized,
                        'SELECT * INTO registry FROM public.learning_model_registry AS r') = 0
                        AND pg_catalog.strpos(function_ddl_normalized,
                        'SELECT * INTO registry FROM public.learning_model_registry r') = 0)
                    AND NOT registry_projection_found) THEN
                    RAISE EXCEPTION 'WP06_INCIDENT_ELIGIBILITY_PROJECTION_NOT_FOUND';
                END IF;

                function_ddl := pg_catalog.replace(function_ddl,
                    'proposal public.learning_retrain_proposal' || pg_catalog.chr(37)
                    || 'ROWTYPE;', 'proposal record;');
                function_ddl := pg_catalog.replace(function_ddl,
                    'assessment public.learning_drift_assessment' || pg_catalog.chr(37)
                    || 'ROWTYPE;', 'assessment record;');
                function_ddl := pg_catalog.replace(function_ddl,
                    'registry public.learning_model_registry' || pg_catalog.chr(37)
                    || 'ROWTYPE;', 'registry record;');
                function_ddl := pg_catalog.replace(function_ddl,
                    'SELECT * INTO proposal FROM public.learning_retrain_proposal AS p'
                    || pg_catalog.chr(10),
                    proposal_projection || ' AS p' || pg_catalog.chr(10));
                function_ddl := pg_catalog.replace(function_ddl,
                    'SELECT * INTO proposal FROM public.learning_retrain_proposal p'
                    || pg_catalog.chr(10),
                    proposal_projection || ' p' || pg_catalog.chr(10));
                function_ddl := pg_catalog.replace(function_ddl,
                    'SELECT * INTO assessment FROM public.learning_drift_assessment AS a'
                    || pg_catalog.chr(10),
                    assessment_projection || ' AS a' || pg_catalog.chr(10));
                function_ddl := pg_catalog.replace(function_ddl,
                    'SELECT * INTO assessment FROM public.learning_drift_assessment a'
                    || pg_catalog.chr(10),
                    assessment_projection || ' a' || pg_catalog.chr(10));
                function_ddl := pg_catalog.replace(function_ddl,
                    'SELECT * INTO registry FROM public.learning_model_registry AS r'
                    || pg_catalog.chr(10),
                    registry_projection || ' AS r' || pg_catalog.chr(10));
                function_ddl := pg_catalog.replace(function_ddl,
                    'SELECT * INTO registry FROM public.learning_model_registry r'
                    || pg_catalog.chr(10),
                    registry_projection || ' r' || pg_catalog.chr(10));
            END IF;

            function_ddl_normalized := pg_catalog.regexp_replace(
                function_ddl, '[[:space:]]+', ' ', 'g');
            proposal_projection_found :=
                pg_catalog.strpos(function_ddl_normalized,
                    proposal_projection || ' AS p') > 0
                OR pg_catalog.strpos(function_ddl_normalized,
                    proposal_projection || ' p') > 0;
            assessment_projection_found :=
                pg_catalog.strpos(function_ddl_normalized,
                    assessment_projection || ' AS a') > 0
                OR pg_catalog.strpos(function_ddl_normalized,
                    assessment_projection || ' a') > 0;
            registry_projection_found :=
                pg_catalog.strpos(function_ddl_normalized,
                    registry_projection || ' AS r') > 0
                OR pg_catalog.strpos(function_ddl_normalized,
                    registry_projection || ' r') > 0;
            IF pg_catalog.strpos(function_ddl_normalized, 'proposal record;') = 0
               OR pg_catalog.strpos(function_ddl_normalized, 'assessment record;') = 0
               OR pg_catalog.strpos(function_ddl_normalized, 'registry record;') = 0
               OR NOT proposal_projection_found
               OR NOT assessment_projection_found
               OR NOT registry_projection_found
               OR pg_catalog.strpos(function_ddl_normalized, 'SELECT * INTO proposal') > 0
               OR pg_catalog.strpos(function_ddl_normalized, 'SELECT * INTO assessment') > 0
               OR pg_catalog.strpos(function_ddl_normalized, 'SELECT * INTO registry') > 0
               OR (pg_catalog.length(function_ddl_normalized) - pg_catalog.length(
                    pg_catalog.regexp_replace(
                        function_ddl_normalized, 'INTO proposal[[:space:];]', '', 'g')))
                    <> pg_catalog.length('INTO proposal ')
               OR (pg_catalog.length(function_ddl_normalized) - pg_catalog.length(
                    pg_catalog.regexp_replace(
                        function_ddl_normalized, 'INTO assessment[[:space:];]', '', 'g')))
                    <> pg_catalog.length('INTO assessment ')
               OR (pg_catalog.length(function_ddl_normalized) - pg_catalog.length(
                    pg_catalog.regexp_replace(
                        function_ddl_normalized, 'INTO registry[[:space:];]', '', 'g')))
                    <> pg_catalog.length('INTO registry ') THEN
                RAISE EXCEPTION 'WP06_INCIDENT_ELIGIBILITY_PROJECTION_NOT_RESTRICTED';
            END IF;
            EXECUTE function_ddl;
        END $$
    """)


def _grant_incident_guard_access() -> None:
    """Cho trigger đọc ledger theo workspace và gọi đúng các hàm kiểm chứng."""
    tables = (
        "user_membership",
        "role_binding",
        "learning_incident_entry_hold_event",
        "learning_retrain_proposal",
    )
    for table in tables:
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {INCIDENT_GUARD_ROLE}")
        op.execute(f"DROP POLICY IF EXISTS wp06_incident_guard_read ON public.{table}")
        op.execute(f"""
            CREATE POLICY wp06_incident_guard_read ON public.{table} FOR SELECT
            TO {INCIDENT_GUARD_ROLE} USING (workspace_id = NULLIF(
                pg_catalog.current_setting('app.workspace_id', true), '')::uuid)
        """)
    op.execute(f"GRANT SELECT ON TABLE public.learning_drift_assessment TO {INCIDENT_GUARD_ROLE}")
    op.execute("DROP POLICY IF EXISTS wp06_incident_guard_read ON public.learning_drift_assessment")
    op.execute("""
        CREATE POLICY wp06_incident_guard_read ON public.learning_drift_assessment FOR SELECT
        TO crypidian_learning_incident_guard USING (workspace_id = NULLIF(
            pg_catalog.current_setting('app.workspace_id', true), '')::uuid)
    """)
    op.execute(f"GRANT EXECUTE ON FUNCTION {LOCK_FUNCTION} TO {INCIDENT_GUARD_ROLE}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {INCIDENT_ELIGIBILITY_FUNCTION} TO {INCIDENT_GUARD_ROLE}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_learning_incident_canonical(jsonb) "
        f"TO {INCIDENT_GUARD_ROLE}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_compact_canonical_json(json) "
        f"TO {INCIDENT_GUARD_ROLE}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_canonical_utc_timestamp_json(timestamptz) "
        f"TO {INCIDENT_GUARD_ROLE}"
    )


def _revoke_incident_guard_access() -> None:
    """Thu hồi policy, quyền gọi và quyền đọc riêng của role guard."""
    for table in (
        "user_membership",
        "role_binding",
        "learning_incident_entry_hold_event",
        "learning_retrain_proposal",
        "learning_drift_assessment",
    ):
        op.execute(f"DROP POLICY IF EXISTS wp06_incident_guard_read ON public.{table}")
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {INCIDENT_GUARD_ROLE}")
    for signature in (
        LOCK_FUNCTION,
        INCIDENT_ELIGIBILITY_FUNCTION,
        "public.wp06_learning_incident_canonical(jsonb)",
        "public.wp06_compact_canonical_json(json)",
        "public.wp06_canonical_utc_timestamp_json(timestamptz)",
    ):
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM {INCIDENT_GUARD_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {INCIDENT_GUARD_ROLE}")
    op.execute(
        f"DO $$ BEGIN DROP ROLE IF EXISTS {INCIDENT_GUARD_ROLE}; "
        "EXCEPTION WHEN dependent_objects_still_exist THEN NULL; END $$;"
    )
